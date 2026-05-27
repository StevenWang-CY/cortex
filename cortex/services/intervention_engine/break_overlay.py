"""
Intervention Engine — Biology-Driven Break Controller (P0 §3.7).

When :class:`StressIntegralTracker.should_break` transitions True the
daemon promotes ``take_biology_break`` as the primary action of the
next intervention plan. Clicking the CTA fires this controller, which:

1. Captures ``pre_hrv`` from the most recent physio reading,
2. Picks a breathing pattern from current HRV (low HRV → 4-7-8 longer
   exhale; high HRV → coherent 5.5-5.5; otherwise box 4-4-4-4),
3. Suppresses peer interventions for the duration window,
4. Drives the UI overlay via the registered ``ui_handler`` (run on the
   Qt thread by the desktop controller),
5. Captures ``post_hrv`` and computes ``recovery_delta``,
6. Persists a :class:`BreakRecord` into the live session report and
   either resets the stress tracker (natural completion) or applies a
   proportional recovery credit (early termination).

The controller is owned by the daemon; the desktop shell binds its UI
handler via :meth:`CortexDaemon.set_break_overlay_ui_handler`. When no
UI handler is registered (headless tests, CI) the controller falls back
to ``asyncio.sleep(duration_seconds)`` so the post-HRV / recovery_delta
math is still exercised — the schemas it produces are wire-stable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from cortex.libs.schemas.session_report import BreakRecord

logger = logging.getLogger(__name__)


BreathingPattern = Literal["box", "4-7-8", "coherent"]


def select_pattern(hrv_rmssd: float | None) -> BreathingPattern:
    """Pick a breathing pattern by current HRV.

    ``low HRV → 4-7-8`` (longer exhale promotes parasympathetic
    activation); ``high HRV → coherent`` 5.5/5.5 (resonant breathing
    sustains the already-relaxed state); otherwise ``box 4-4-4-4``.
    The thresholds (30/55 ms RMSSD) are conservative for adult
    desk-workers; the daemon's longitudinal tracker is free to override
    by passing an explicit pattern into :meth:`BiologyBreakController.start`.
    """
    if hrv_rmssd is None:
        return "box"
    if hrv_rmssd < 30.0:
        return "4-7-8"
    if hrv_rmssd > 55.0:
        return "coherent"
    return "box"


# Result type returned by the UI handler: (elapsed_seconds, completed_full_duration).
BreakUIResult = tuple[float, bool]
BreakUIHandler = Callable[[float, BreathingPattern, bool], Awaitable[BreakUIResult]]


class BiologyBreakController:
    """Orchestrates one guided breathing session end-to-end.

    Parameters
    ----------
    hrv_sampler:
        Callable returning the most recent RMSSD reading in ms (or
        ``None`` when physio is unavailable). Called twice per break:
        immediately before the overlay shows and immediately after it
        closes.
    session_report:
        :class:`SessionReportGenerator` instance whose ``record_break``
        receives the produced :class:`BreakRecord`.
    suppress_interventions:
        Callable invoked with ``True`` before the overlay starts and
        ``False`` after it ends. The daemon flips the receptivity gate
        so peer adapters do not surface a competing intervention.
    stress_tracker:
        :class:`StressIntegralTracker` whose ``reset`` (natural
        completion) or ``apply_recovery_credit`` (early termination) is
        called once the break finishes.

    The optional :meth:`set_ui_handler` registers the Qt-side break
    overlay; ``start`` falls back to an ``asyncio.sleep`` when no UI is
    bound so headless paths still produce valid :class:`BreakRecord`.
    """

    def __init__(
        self,
        *,
        hrv_sampler: Callable[[], float | None],
        session_report: Any,
        suppress_interventions: Callable[[bool], None] | None = None,
        stress_tracker: Any | None = None,
    ) -> None:
        self._hrv_sampler = hrv_sampler
        self._session_report = session_report
        self._suppress_interventions = suppress_interventions
        self._stress_tracker = stress_tracker
        self._ui_handler: BreakUIHandler | None = None
        self._active: bool = False
        # Reset count exposed for tests — they don't need to mock the
        # full StressIntegralTracker just to verify lifecycle.
        self.reset_count: int = 0
        self.credit_seconds: float = 0.0

    def set_ui_handler(self, handler: BreakUIHandler | None) -> None:
        """Bind the Qt-side break overlay handler.

        Signature: ``async (duration_seconds, breathing_pattern,
        audio_cue) -> (elapsed_seconds, completed)``. ``elapsed`` is
        the wall-clock duration the overlay was shown for and
        ``completed`` is True only when the pattern ran the full
        ``duration_seconds`` (a click on "End early" yields False).
        """
        self._ui_handler = handler

    @property
    def is_active(self) -> bool:
        """Return True while a break is in progress."""
        return self._active

    async def start(
        self,
        *,
        duration_seconds: int = 240,
        breathing_pattern: BreathingPattern | None = None,
        audio_cue: bool = True,
        reason: str = "",
    ) -> BreakRecord | None:
        """Run one guided break end-to-end.

        Returns the produced :class:`BreakRecord` (also appended to the
        session report), or ``None`` if a break is already in progress
        (re-entrant calls are no-ops to prevent two overlays stacking).
        """
        if self._active:
            logger.info("BiologyBreakController: re-entrant start ignored")
            return None
        self._active = True
        if self._suppress_interventions is not None:
            try:
                self._suppress_interventions(True)
            except Exception:
                logger.debug("suppress_interventions(True) failed", exc_info=True)

        started_at = datetime.now(UTC)
        wall_start = time.monotonic()
        try:
            pre_hrv = self._sample_hrv()
            pattern: BreathingPattern = (
                breathing_pattern
                if breathing_pattern in ("box", "4-7-8", "coherent")
                else select_pattern(pre_hrv)
            )
            duration = max(30, int(duration_seconds))

            elapsed, completed = await self._run_overlay(
                duration=float(duration),
                pattern=pattern,
                audio_cue=audio_cue,
            )

            post_hrv = self._sample_hrv()
            recovery_delta: float | None = None
            if pre_hrv is not None and post_hrv is not None:
                recovery_delta = float(post_hrv - pre_hrv)

            record = BreakRecord(
                started_at=started_at,
                duration_seconds=float(elapsed),
                pattern=pattern,
                pre_hrv=float(pre_hrv) if pre_hrv is not None else None,
                post_hrv=float(post_hrv) if post_hrv is not None else None,
                recovery_delta=recovery_delta,
                completed=bool(completed),
                audio_cue=bool(audio_cue),
                reason=reason[:120] if reason else "",
            )

            # Persist into the live session report — same call shape as
            # the legacy ``record_break(recommended=True)`` so the
            # ``breaks_taken`` / ``breaks_recommended`` counters keep
            # working alongside the new ``break_records`` list.
            try:
                self._session_report.record_break(
                    recommended=True,
                    taken=True,
                    record=record,
                )
            except Exception:
                logger.exception("session_report.record_break failed")

            # Reward shaping for the stress tracker. Natural completion
            # is a full reset (the user genuinely relaxed); early
            # termination credits the integral proportionally to the
            # fraction of the requested duration that elapsed.
            if self._stress_tracker is not None:
                try:
                    if completed:
                        self._stress_tracker.reset()
                        self.reset_count += 1
                    else:
                        credit = elapsed
                        self._stress_tracker.apply_recovery_credit(credit)
                        self.credit_seconds += credit
                except Exception:
                    logger.exception("stress_tracker post-break update failed")

            logger.info(
                "Biology break finished: pattern=%s elapsed=%.1fs completed=%s "
                "recovery_delta=%s",
                pattern, elapsed, completed,
                f"{recovery_delta:.1f}ms" if recovery_delta is not None else "n/a",
            )
            return record
        except Exception:
            logger.exception("Biology break failed")
            return None
        finally:
            self._active = False
            if self._suppress_interventions is not None:
                try:
                    self._suppress_interventions(False)
                except Exception:
                    logger.debug(
                        "suppress_interventions(False) failed", exc_info=True
                    )
            _ = wall_start  # explicit no-op to keep mypy quiet on unused start time

    async def _run_overlay(
        self,
        *,
        duration: float,
        pattern: BreathingPattern,
        audio_cue: bool,
    ) -> BreakUIResult:
        """Invoke the UI handler when registered.

        Phase-4b TASK G: when no UI handler is registered, return
        ``(0.0, False)`` immediately. The legacy
        ``asyncio.sleep(duration)`` faked a successful break — fine for
        the schema round-trip in headless tests, but a misleading
        "completed=True" credit when the daemon actually had no surface
        bound (e.g. a forgotten ``set_ui_handler`` in a desktop build).
        Callers detect the no-handler case by the ``completed=False``
        result and skip the reset-on-completion path.
        """
        handler = self._ui_handler
        if handler is None:
            logger.warning(
                "BiologyBreakController: no UI handler bound; returning "
                "(0.0, False) instead of faking a sleep-based completion",
            )
            return (0.0, False)
        try:
            elapsed, completed = await handler(duration, pattern, audio_cue)
            elapsed = max(0.0, float(elapsed))
            completed = bool(completed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Break overlay UI handler raised — treating as early exit",
            )
            return (0.0, False)
        return (elapsed, completed)

    def _sample_hrv(self) -> float | None:
        """Safely sample HRV; logs but never raises."""
        try:
            v = self._hrv_sampler()
        except Exception:
            logger.debug("hrv_sampler raised", exc_info=True)
            return None
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


__all__ = [
    "BreathingPattern",
    "BreakUIHandler",
    "BreakUIResult",
    "BiologyBreakController",
    "select_pattern",
]
