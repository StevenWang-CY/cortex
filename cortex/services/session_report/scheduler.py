"""
Midnight Scheduler for the Longitudinal Aggregator (P0 §3.2).

Fires an async callback at 00:05 local time every day. The daemon's
``_midnight_tick`` reads "aggregate yesterday + refresh chronotype +
sweep chronotype retention."

Design decisions:

* 00:05 (not 00:00) so the day boundary is unambiguous — sessions
  that started before midnight have finished writing by then.
* Idempotent: calling ``start()`` twice is a no-op (the existing task
  keeps running). Calling ``stop()`` cancels the task and is safe to
  call before any ``start()``.
* The loop is in pure asyncio: ``asyncio.sleep`` until the next
  scheduled time, then ``await callback()``. Any exception inside the
  callback is logged and swallowed so the loop survives a single bad
  tick.
* Local time is resolved via :func:`cortex.services.session_report.longitudinal._local_tz`
  (DST-aware ``ZoneInfo`` when available). DST transitions are handled
  by ``timedelta`` arithmetic on a tz-aware ``datetime``, which follows
  real elapsed time rather than wall clock. After firing a tick the
  loop short-circuits subsequent firings for the same calendar date so
  a clock skew near 00:05 cannot cause a double-tick (P0 §3.2 fix #20).
* The same-day dedupe is persisted to ``scheduler_state.json`` in the
  chronotype storage dir so a daemon restart between 00:05 and the
  next start does not re-fire the tick for the same date and double-
  aggregate yesterday's DailyBaseline (P0 audit fix #4.B-1).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.services.session_report.longitudinal import _local_tz

logger = logging.getLogger(__name__)

_STATE_FILENAME = "scheduler_state.json"


def _seconds_until_next(hour: int, minute: int) -> float:
    """Return the number of seconds from now until the next occurrence
    of ``hour:minute`` in local time.

    DST-safe (P0 §3.2 fix #20): the candidate is constructed by
    ``replace(hour=..., minute=...)`` on the *current* local-tz
    ``datetime``; if that target has already passed, we compute
    tomorrow's by adding a day THEN replacing. ``timedelta(days=1)`` on
    a tz-aware datetime follows real elapsed seconds — exactly the
    semantics we want around a spring-forward / fall-back transition.
    """
    now = datetime.now(_local_tz())
    today_target = now.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    if today_target <= now:
        candidate = (now + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )
    else:
        candidate = today_target
    delta = (candidate - now).total_seconds()
    return max(1.0, delta)


# Minimum seconds the loop will sleep after firing a tick before
# considering the next one — guards against clock skew near 00:05
# firing the callback twice in one calendar minute.
_MIN_POST_TICK_SLEEP_S: float = 60.0


class MidnightScheduler:
    """Schedules an async callback to fire at 00:05 local time daily.

    The constructor only stores the callback; ``start()`` creates the
    asyncio task. The task is cancellable via ``stop()`` and can be
    restarted on a fresh ``MidnightScheduler`` instance.
    """

    def __init__(
        self,
        callback: Callable[[], Awaitable[None]],
        *,
        hour: int = 0,
        minute: int = 5,
        state_dir: Path | None = None,
    ) -> None:
        self._callback = callback
        self._hour = hour
        self._minute = minute
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # Persisted state path: chronotype dir (or any caller-supplied
        # dir) hosts ``scheduler_state.json``. ``None`` disables the
        # disk hop (used by unit tests that don't care about restart
        # semantics).
        self._state_dir: Path | None = state_dir
        self._state_path: Path | None = (
            state_dir / _STATE_FILENAME if state_dir is not None else None
        )
        # Track the last calendar date we fired on so a clock jump
        # near the target instant cannot fire two ticks for the same
        # date (P0 §3.2 fix #20). Loaded from disk at construction so
        # a daemon restart between firing and the next 00:05 does NOT
        # re-aggregate the same day (P0 audit fix #4.B-1).
        self._last_fired_date: date | None = self._load_state()

    def start(self) -> None:
        """Create the asyncio task. Idempotent — second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="cortex-midnight-tick")

    def _load_state(self) -> date | None:
        """Load ``last_fired_date`` from disk if the state file exists.

        Returns ``None`` on missing-file, parse error, malformed shape,
        or any I/O issue. Logs at WARNING for corruption so an operator
        sees a stale dedupe state was discarded; INFO when the file is
        simply absent (first run / brand new install).
        """
        if self._state_path is None:
            return None
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info(
                "midnight scheduler: no state file at %s; starting fresh",
                self._state_path,
            )
            return None
        except OSError as exc:
            logger.warning(
                "midnight scheduler: cannot read state file %s: %s",
                self._state_path,
                exc,
            )
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "midnight scheduler: state file %s is malformed JSON (%s); starting fresh",
                self._state_path,
                exc,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "midnight scheduler: state file %s has unexpected shape; starting fresh",
                self._state_path,
            )
            return None
        raw_date = data.get("last_fired_date")
        if not isinstance(raw_date, str):
            return None
        try:
            d = date.fromisoformat(raw_date)
        except ValueError:
            logger.warning(
                "midnight scheduler: state file %s has invalid date %r; starting fresh",
                self._state_path,
                raw_date,
            )
            return None
        logger.info(
            "midnight scheduler: loaded last_fired_date=%s from %s",
            d,
            self._state_path,
        )
        return d

    def _persist_state(self, fired_date: date) -> None:
        """Atomically write ``last_fired_date`` to the state file.

        Failures are logged but do not propagate — losing the dedupe
        guarantee for one tick is preferable to crashing the daemon
        loop. ``atomic_write_json`` already handles tmp+rename so a
        SIGKILL mid-write cannot leave a half-written file.
        """
        if self._state_path is None:
            return
        try:
            atomic_write_json(
                self._state_path,
                {"last_fired_date": fired_date.isoformat()},
            )
        except OSError:
            logger.exception(
                "midnight scheduler: failed to persist state to %s",
                self._state_path,
            )

    async def stop(self) -> None:
        """Cancel the task and wait for it to drain.

        Safe to call before ``start()`` (no-op) and to call twice
        (second call short-circuits because the task is already done).
        """
        self._stopped.set()
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("midnight scheduler task ended with exception", exc_info=True)

    async def _run(self) -> None:
        """Loop body: sleep until next tick, fire callback, repeat."""
        try:
            while not self._stopped.is_set():
                wait_s = _seconds_until_next(self._hour, self._minute)
                logger.info(
                    "midnight scheduler: next tick in %.0f s (target %02d:%02d local)",
                    wait_s,
                    self._hour,
                    self._minute,
                )
                # Use wait_for(stopped.wait, timeout=wait_s) so stop()
                # can interrupt the sleep promptly.
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=wait_s)
                    # If we got here, stop() was called.
                    return
                except TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise

                # Dedupe firings for the same local calendar date so a
                # clock jump near the target instant cannot trigger
                # two ticks for the same day.
                today_local = datetime.now(_local_tz()).date()
                if self._last_fired_date == today_local:
                    logger.debug(
                        "midnight scheduler: already fired for %s; sleeping %0.0fs to avoid double-tick",
                        today_local,
                        _MIN_POST_TICK_SLEEP_S,
                    )
                    try:
                        await asyncio.wait_for(
                            self._stopped.wait(),
                            timeout=_MIN_POST_TICK_SLEEP_S,
                        )
                        return
                    except TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        raise

                try:
                    await self._callback()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("midnight scheduler callback raised; loop continues")
                else:
                    logger.info("midnight scheduler: callback finished")
                # Always mark this day fired (and persist) — even on a
                # callback failure — so a crash-loop after midnight does
                # not re-aggregate the same day on every restart. The
                # callback itself is responsible for logging its own
                # failure mode; the scheduler's only contract is "fire
                # once per local day."
                self._last_fired_date = today_local
                self._persist_state(today_local)

                # Sleep at least _MIN_POST_TICK_SLEEP_S before computing
                # the next target so the dedupe window above always
                # applies when the clock hasn't moved on. ``stop()``
                # can still interrupt.
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=_MIN_POST_TICK_SLEEP_S,
                    )
                    return
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            logger.debug("midnight scheduler cancelled")
            raise


__all__ = ["MidnightScheduler"]
