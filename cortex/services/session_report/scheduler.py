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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

from cortex.services.session_report.longitudinal import _local_tz

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._callback = callback
        self._hour = hour
        self._minute = minute
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        # Track the last calendar date we fired on so a clock jump
        # near the target instant cannot fire two ticks for the same
        # date (P0 §3.2 fix #20).
        self._last_fired_date: date | None = None

    def start(self) -> None:
        """Create the asyncio task. Idempotent — second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="cortex-midnight-tick")

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
                self._last_fired_date = today_local

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
