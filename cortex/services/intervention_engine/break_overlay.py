"""User-requested or elapsed-focus guided break controller.

The production path is not physiology-driven. It uses an explicit breathing
pattern (neutral box breathing by default), suppresses competing proposals
while the overlay is visible, and records completion. Legacy ``BreakRecord``
physiology fields remain ``None`` for decode compatibility.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from cortex.libs.schemas.session_report import BreakRecord

logger = logging.getLogger(__name__)

BreathingPattern = Literal["box", "4-7-8", "coherent"]
BreakUIResult = tuple[float, bool]
BreakUIHandler = Callable[[float, BreathingPattern, bool], Awaitable[BreakUIResult]]


class GuidedBreakController:
    """Orchestrate one guided break without inferring a physiological need."""

    def __init__(
        self,
        *,
        session_report: Any,
        suppress_interventions: Callable[[bool], None] | None = None,
    ) -> None:
        self._session_report = session_report
        self._suppress_interventions = suppress_interventions
        self._ui_handler: BreakUIHandler | None = None
        self._active = False

    def set_ui_handler(self, handler: BreakUIHandler | None) -> None:
        """Bind the Qt-side overlay handler."""

        self._ui_handler = handler

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(
        self,
        *,
        duration_seconds: int = 240,
        breathing_pattern: BreathingPattern | None = None,
        audio_cue: bool = True,
        reason: str = "",
    ) -> BreakRecord | None:
        """Run one guided break; concurrent duplicate starts are ignored."""

        if self._active:
            logger.info("GuidedBreakController: re-entrant start ignored")
            return None
        self._active = True
        if self._suppress_interventions is not None:
            try:
                self._suppress_interventions(True)
            except Exception:
                logger.debug("suppress_interventions(True) failed", exc_info=True)

        try:
            pattern: BreathingPattern = (
                breathing_pattern
                if breathing_pattern in ("box", "4-7-8", "coherent")
                else "box"
            )
            duration = max(30, int(duration_seconds))
            elapsed, completed = await self._run_overlay(
                duration=float(duration),
                pattern=pattern,
                audio_cue=audio_cue,
            )
            record = BreakRecord(
                started_at=datetime.now(UTC),
                duration_seconds=float(elapsed),
                pattern=pattern,
                pre_hrv=None,
                post_hrv=None,
                recovery_delta=None,
                completed=bool(completed),
                audio_cue=bool(audio_cue),
                reason=reason[:120] if reason else "",
            )
            try:
                self._session_report.record_break(
                    recommended=True,
                    taken=True,
                    record=record,
                )
            except Exception:
                logger.exception("session_report.record_break failed")
            logger.info(
                "Guided break finished: pattern=%s elapsed=%.1fs completed=%s",
                pattern,
                elapsed,
                completed,
            )
            return record
        except Exception:
            logger.exception("Guided break failed")
            return None
        finally:
            self._active = False
            if self._suppress_interventions is not None:
                try:
                    self._suppress_interventions(False)
                except Exception:
                    logger.debug(
                        "suppress_interventions(False) failed",
                        exc_info=True,
                    )

    async def _run_overlay(
        self,
        *,
        duration: float,
        pattern: BreathingPattern,
        audio_cue: bool,
    ) -> BreakUIResult:
        """Run a bound UI; a missing/failed surface is visibly incomplete."""

        handler = self._ui_handler
        if handler is None:
            logger.warning(
                "GuidedBreakController: no UI handler bound; returning incomplete",
            )
            return (0.0, False)
        try:
            elapsed, completed = await handler(duration, pattern, audio_cue)
            return (max(0.0, float(elapsed)), bool(completed))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Break overlay UI handler raised; treating as early exit")
            return (0.0, False)


__all__ = [
    "BreathingPattern",
    "BreakUIHandler",
    "BreakUIResult",
    "GuidedBreakController",
]
