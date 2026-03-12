"""
Intervention Engine — Restore Manager

Manages intervention lifecycle: auto-timeout (5 min), recovery detection
(FLOW > 0.70 for 15s), workspace restoration, and outcome logging.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from cortex.libs.schemas.intervention import InterventionOutcome, WorkspaceSnapshot
from cortex.libs.schemas.state import StateEstimate
from cortex.services.intervention_engine.executor import InterventionExecutor

logger = logging.getLogger(__name__)


@dataclass
class ActiveIntervention:
    """Tracks an active intervention's lifecycle."""

    intervention_id: str
    snapshot: WorkspaceSnapshot
    started_at: float
    timeout_seconds: float = 300.0  # 5 min
    recovery_threshold: float = 0.70
    recovery_dwell_seconds: float = 15.0

    # Recovery tracking
    flow_start_time: float | None = None

    @property
    def is_timed_out(self) -> bool:
        """Check if intervention has exceeded timeout (using real time)."""
        return (time.monotonic() - self.started_at) > self.timeout_seconds

    def timed_out_at(self, t: float) -> bool:
        """Check if intervention has exceeded timeout at a given time."""
        return (t - self.started_at) > self.timeout_seconds

    def check_recovery(self, estimate: StateEstimate, now: float) -> bool:
        """
        Check if recovery conditions are met.

        Recovery = FLOW state with confidence > threshold sustained for
        recovery_dwell_seconds.
        """
        if (
            estimate.state == "FLOW"
            and estimate.confidence >= self.recovery_threshold
        ):
            if self.flow_start_time is None:
                self.flow_start_time = now
            elif (now - self.flow_start_time) >= self.recovery_dwell_seconds:
                return True
        else:
            # Reset flow tracking if state changes
            self.flow_start_time = None

        return False

    @property
    def duration_seconds(self) -> float:
        """Duration since intervention started."""
        return time.monotonic() - self.started_at

    def duration_at(self, t: float) -> float:
        """Duration at a given timestamp."""
        return t - self.started_at


class RestoreManager:
    """
    Manages active interventions, auto-timeout, recovery detection,
    and workspace restoration.
    """

    def __init__(
        self,
        executor: InterventionExecutor | None = None,
        *,
        timeout_seconds: float = 300.0,
        recovery_threshold: float = 0.70,
        recovery_dwell_seconds: float = 15.0,
    ) -> None:
        self._executor = executor
        self._timeout = timeout_seconds
        self._recovery_threshold = recovery_threshold
        self._recovery_dwell = recovery_dwell_seconds
        self._active: dict[str, ActiveIntervention] = {}
        self._outcomes: list[InterventionOutcome] = []

    def start_intervention(
        self,
        intervention_id: str,
        snapshot: WorkspaceSnapshot,
        *,
        started_at: float | None = None,
    ) -> ActiveIntervention:
        """Register a new active intervention."""
        now = started_at if started_at is not None else time.monotonic()
        active = ActiveIntervention(
            intervention_id=intervention_id,
            snapshot=snapshot,
            started_at=now,
            timeout_seconds=self._timeout,
            recovery_threshold=self._recovery_threshold,
            recovery_dwell_seconds=self._recovery_dwell,
        )
        self._active[intervention_id] = active
        return active

    async def update(
        self,
        estimate: StateEstimate,
        *,
        current_time: float | None = None,
    ) -> list[InterventionOutcome]:
        """
        Check all active interventions for timeout or recovery.

        Should be called periodically (e.g., every state update cycle).

        Returns list of outcomes for any interventions that ended.
        """
        now = current_time if current_time is not None else time.monotonic()
        ended: list[InterventionOutcome] = []

        for iid in list(self._active.keys()):
            active = self._active[iid]

            # Check timeout
            if active.timed_out_at(now):
                outcome = await self._end_intervention(
                    active, "timed_out", now
                )
                ended.append(outcome)
                continue

            # Check recovery
            if active.check_recovery(estimate, now):
                outcome = await self._end_intervention(
                    active, "natural_recovery", now
                )
                ended.append(outcome)
                continue

        return ended

    async def dismiss(
        self,
        intervention_id: str,
        *,
        current_time: float | None = None,
    ) -> InterventionOutcome | None:
        """
        Handle user dismissal of an intervention.

        Returns the outcome, or None if intervention wasn't found.
        """
        active = self._active.get(intervention_id)
        if active is None:
            return None

        now = current_time if current_time is not None else time.monotonic()
        return await self._end_intervention(active, "dismissed", now)

    async def engage(
        self,
        intervention_id: str,
        *,
        current_time: float | None = None,
    ) -> InterventionOutcome | None:
        """Handle user engagement with an intervention (e.g., clicking steps)."""
        active = self._active.get(intervention_id)
        if active is None:
            return None

        now = current_time if current_time is not None else time.monotonic()
        return await self._end_intervention(active, "engaged", now)

    async def _end_intervention(
        self,
        active: ActiveIntervention,
        user_action: str,
        now: float,
    ) -> InterventionOutcome:
        """End an intervention, restore workspace, and log outcome."""
        iid = active.intervention_id

        # Restore workspace via executor
        workspace_restored = False
        restore_errors: list[str] = []

        if self._executor is not None:
            try:
                reversals = await self._executor.reverse(iid)
                failed = [r for r in reversals if not r.success]
                workspace_restored = len(failed) == 0
                restore_errors = [
                    f"{r.adapter}:{r.action} failed" for r in failed
                ]
            except Exception as exc:
                logger.exception("Error restoring workspace for %s", iid)
                restore_errors.append(str(exc))

        duration = active.duration_at(now)
        is_recovery = user_action in ("natural_recovery", "engaged")

        outcome = InterventionOutcome(
            intervention_id=iid,
            started_at=datetime.now(),  # approximate
            ended_at=datetime.now(),
            duration_seconds=duration,
            user_action=user_action,
            recovery_detected=is_recovery,
            recovery_confidence=(
                active.recovery_threshold if is_recovery else None
            ),
            workspace_restored=workspace_restored,
            restore_errors=restore_errors,
        )

        self._outcomes.append(outcome)
        self._active.pop(iid, None)

        logger.info(
            "Intervention %s ended: action=%s, duration=%.1fs, restored=%s",
            iid,
            user_action,
            duration,
            workspace_restored,
        )

        return outcome

    def get_active(self, intervention_id: str) -> ActiveIntervention | None:
        """Get an active intervention by ID."""
        return self._active.get(intervention_id)

    @property
    def active_count(self) -> int:
        """Number of currently active interventions."""
        return len(self._active)

    @property
    def active_ids(self) -> list[str]:
        """List of active intervention IDs."""
        return list(self._active.keys())

    @property
    def outcomes(self) -> list[InterventionOutcome]:
        """All recorded outcomes."""
        return list(self._outcomes)

    def clear(self) -> None:
        """Clear all active interventions and outcomes."""
        self._active.clear()
        self._outcomes.clear()
