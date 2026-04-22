"""
LeetCode Interventions — Stage x Mode Matrix

Maps (LeetCodeStage, LeetCodeMode) pairs to concrete intervention actions.
Each intervention class encapsulates its trigger condition, action payload,
and cooldown logic for a specific cell (or set of cells) in the matrix.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from cortex.libs.schemas.leetcode import (
    LeetCodeContext,
    LeetCodeMode,
    LeetCodeModeEstimate,
    LeetCodeStage,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. RestatementScratchpad
# ---------------------------------------------------------------------------


class RestatementScratchpad:
    """Offer a restatement scratchpad during destructive comprehension struggle.

    Triggers at (READ|PLAN, DESTRUCTIVE_STRUGGLE) when the detection pathway
    is "comprehension".  Helps the user re-anchor on the problem statement
    before spiralling further.

    Cooldown: 300 s (5 min).
    """

    _COOLDOWN_S: float = 300.0
    _TRIGGER_STAGES = {LeetCodeStage.READ, LeetCodeStage.PLAN}

    def __init__(self) -> None:
        self._last_trigger_time: float | None = None

    def should_trigger(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> bool:
        """Check if this intervention should fire for the given stage x mode cell."""
        if mode_estimate.stage not in self._TRIGGER_STAGES:
            return False
        if mode_estimate.mode != LeetCodeMode.DESTRUCTIVE_STRUGGLE:
            return False
        if mode_estimate.destructive.pathway != "comprehension":
            return False
        if self._last_trigger_time is not None:
            elapsed = time.monotonic() - self._last_trigger_time
            if elapsed < self._COOLDOWN_S:
                return False
        return True

    def build_action(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> dict[str, Any]:
        """Build the action payload to send via the LeetCode adapter."""
        self._last_trigger_time = time.monotonic()
        logger.info(
            "RestatementScratchpad fired for problem %s (%s)",
            leetcode_ctx.problem_id,
            leetcode_ctx.title,
        )
        return {
            "action": "show_scratchpad",
            "required_consent_level": "preview",
            "payload": {
                "problem_title": leetcode_ctx.title,
                "problem_id": leetcode_ctx.problem_id,
                "code_snapshot": leetcode_ctx.code_snapshot,
            },
        }


# ---------------------------------------------------------------------------
# 2. PatternLadder
# ---------------------------------------------------------------------------


class PatternLadder:
    """Surface progressive hints during productive struggle.

    Triggers at (PLAN|IMPLEMENT, PRODUCTIVE_STRUGGLE).  Presents a tiered
    hint ladder so the user can self-regulate how much guidance they want.

    Cooldown: 120 s (2 min).
    """

    _COOLDOWN_S: float = 120.0
    _TRIGGER_STAGES = {LeetCodeStage.PLAN, LeetCodeStage.IMPLEMENT}

    def __init__(self) -> None:
        self._last_trigger_time: float | None = None

    def should_trigger(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> bool:
        """Check if this intervention should fire for the given stage x mode cell."""
        if mode_estimate.stage not in self._TRIGGER_STAGES:
            return False
        if mode_estimate.mode != LeetCodeMode.PRODUCTIVE_STRUGGLE:
            return False
        if self._last_trigger_time is not None:
            elapsed = time.monotonic() - self._last_trigger_time
            if elapsed < self._COOLDOWN_S:
                return False
        return True

    def build_action(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> dict[str, Any]:
        """Build the action payload to send via the LeetCode adapter."""
        self._last_trigger_time = time.monotonic()
        logger.info(
            "PatternLadder fired for problem %s (difficulty=%s)",
            leetcode_ctx.title,
            leetcode_ctx.difficulty,
        )
        return {
            "action": "show_pattern_ladder",
            "required_consent_level": "suggest",
            "payload": {
                "problem_title": leetcode_ctx.title,
                "difficulty": leetcode_ctx.difficulty,
                "tags": leetcode_ctx.tags,
                "time_elapsed_s": leetcode_ctx.time_elapsed_s,
            },
        }


# ---------------------------------------------------------------------------
# 3. AmygdalaLockout
# ---------------------------------------------------------------------------


class AmygdalaLockout:
    """Force a cooldown after an amygdala hijack during debugging.

    Triggers at (DEBUG, AMYGDALA_HIJACK).  Prevents rage-resubmitting by
    blocking the submit button and encouraging a breathing reset.

    Debounce: 100 s — won't re-fire within the lockout period plus buffer.
    Duration escalates with repeated Wrong Answers (90s base, +30s per
    extra WA above 3, capped at 180s).
    """

    _DEBOUNCE_S: float = 100.0
    _BASE_DURATION_S: int = 90
    _ESCALATION_PER_WA: int = 30
    _MAX_DURATION_S: int = 180
    _ESCALATION_WA_FLOOR: int = 3

    def __init__(self) -> None:
        self._last_trigger_time: float | None = None

    def should_trigger(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> bool:
        """Check if this intervention should fire for the given stage x mode cell."""
        if mode_estimate.stage != LeetCodeStage.DEBUG:
            return False
        if mode_estimate.mode != LeetCodeMode.AMYGDALA_HIJACK:
            return False
        # Debounce: don't re-fire within the lockout period
        if self._last_trigger_time is not None:
            elapsed = time.monotonic() - self._last_trigger_time
            if elapsed < self._DEBOUNCE_S:
                return False
        return True

    def _compute_duration(self, wa_count: int) -> int:
        """Escalate lockout duration based on Wrong Answer count."""
        extra = max(0, wa_count - self._ESCALATION_WA_FLOOR)
        return min(
            self._BASE_DURATION_S + extra * self._ESCALATION_PER_WA,
            self._MAX_DURATION_S,
        )

    def build_action(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> dict[str, Any]:
        """Build the action payload to send via the LeetCode adapter."""
        self._last_trigger_time = time.monotonic()
        duration = self._compute_duration(leetcode_ctx.wrong_answer_count)
        logger.info(
            "AmygdalaLockout fired — locking out for %d s (WA=%d, last result: %s)",
            duration,
            leetcode_ctx.wrong_answer_count,
            leetcode_ctx.last_submission_result,
        )
        return {
            "action": "show_lockout",
            "required_consent_level": "preview",
            "payload": {
                "duration_s": duration,
                "last_submission_result": leetcode_ctx.last_submission_result,
                "code_snapshot": leetcode_ctx.code_snapshot,
            },
        }


# ---------------------------------------------------------------------------
# 4. SubmissionDisciplineGuard
# ---------------------------------------------------------------------------


class SubmissionDisciplineGuard:
    """Gate submissions when wrong-answer count exceeds threshold.

    Triggers at (IMPLEMENT|DEBUG, *any mode*) when ``ctx.wrong_answer_count > 2``.
    Encourages the user to add a test case or trace through logic before
    submitting again.

    Cooldown: 0 — fires on every submission attempt.
    """

    _TRIGGER_STAGES = {LeetCodeStage.IMPLEMENT, LeetCodeStage.DEBUG}
    _WA_THRESHOLD: int = 2

    def __init__(self) -> None:
        self._last_trigger_time: float | None = None

    def should_trigger(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> bool:
        """Check if this intervention should fire for the given stage x mode cell."""
        if mode_estimate.stage not in self._TRIGGER_STAGES:
            return False
        if leetcode_ctx.wrong_answer_count <= self._WA_THRESHOLD:
            return False
        return True

    def build_action(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> dict[str, Any]:
        """Build the action payload to send via the LeetCode adapter."""
        self._last_trigger_time = time.monotonic()
        logger.info(
            "SubmissionDisciplineGuard fired — WA count %d, submissions %d",
            leetcode_ctx.wrong_answer_count,
            leetcode_ctx.submission_count,
        )
        return {
            "action": "show_submission_gate",
            "required_consent_level": "preview",
            "payload": {
                "wrong_answer_count": leetcode_ctx.wrong_answer_count,
                "submission_count": leetcode_ctx.submission_count,
            },
        }


# ---------------------------------------------------------------------------
# 5. SolutionEscapeFriction
# ---------------------------------------------------------------------------


class SolutionEscapeFriction:
    """Add friction when the user tries to peek at solutions under duress.

    Triggers at (*any stage*, PANIC|FATIGUE) when ``ctx.solutions_tab_attempted``
    is True.  Presents a reflection prompt before allowing access to solutions.

    Cooldown: adaptive — starts at 60s and decays exponentially toward 10s
    as the user spends more time on the problem.  Scaled by difficulty.
    """

    _TRIGGER_MODES = {LeetCodeMode.PANIC, LeetCodeMode.FATIGUE}

    def __init__(self) -> None:
        self._last_trigger_time: float | None = None

    @staticmethod
    def _compute_friction(time_elapsed_s: float, difficulty: str) -> float:
        """Compute adaptive cooldown using exponential decay.

        Early in a problem the friction is high (up to 60s * difficulty_mult).
        As the user spends more time, friction decays toward a 10s floor.

        Args:
            time_elapsed_s: Seconds elapsed since the user started the problem.
            difficulty: Problem difficulty ("Easy", "Medium", "Hard").

        Returns:
            Cooldown duration in seconds.
        """
        difficulty_mult = {"Easy": 0.5, "Medium": 1.0, "Hard": 1.5}.get(difficulty, 1.0)
        base = max(10, 60 * math.exp(-time_elapsed_s / 600))
        return base * difficulty_mult

    def should_trigger(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> bool:
        """Check if this intervention should fire for the given stage x mode cell."""
        if mode_estimate.mode not in self._TRIGGER_MODES:
            return False
        if not leetcode_ctx.solutions_tab_attempted:
            return False
        if self._last_trigger_time is not None:
            elapsed = time.monotonic() - self._last_trigger_time
            cooldown = self._compute_friction(
                leetcode_ctx.time_elapsed_s, leetcode_ctx.difficulty,
            )
            if elapsed < cooldown:
                return False
        return True

    def build_action(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> dict[str, Any]:
        """Build the action payload to send via the LeetCode adapter."""
        self._last_trigger_time = time.monotonic()
        friction = self._compute_friction(
            leetcode_ctx.time_elapsed_s, leetcode_ctx.difficulty,
        )
        logger.info(
            "SolutionEscapeFriction fired at stage %s (elapsed %.0f s, friction=%.1f s)",
            leetcode_ctx.stage.value,
            leetcode_ctx.time_elapsed_s,
            friction,
        )
        return {
            "action": "show_solution_friction",
            "required_consent_level": "preview",
            "payload": {
                "stage": leetcode_ctx.stage.value,
                "time_elapsed_s": leetcode_ctx.time_elapsed_s,
                "difficulty": leetcode_ctx.difficulty,
                "friction_s": friction,
            },
        }


# ---------------------------------------------------------------------------
# InterventionMatrix
# ---------------------------------------------------------------------------


class InterventionMatrix:
    """Routes Stage x Mode pairs to the appropriate interventions.

    Iterates all registered interventions and returns every action whose
    trigger condition is satisfied for the current state.
    """

    def __init__(self) -> None:
        self._interventions: list = [
            RestatementScratchpad(),
            PatternLadder(),
            AmygdalaLockout(),
            SubmissionDisciplineGuard(),
            SolutionEscapeFriction(),
        ]

    def select(
        self,
        mode_estimate: LeetCodeModeEstimate,
        leetcode_ctx: LeetCodeContext,
    ) -> list[dict[str, Any]]:
        """Return all intervention actions that should fire for the current state."""
        actions: list[dict[str, Any]] = []
        for intervention in self._interventions:
            if intervention.should_trigger(mode_estimate, leetcode_ctx):
                action = intervention.build_action(mode_estimate, leetcode_ctx)
                actions.append(action)
                logger.debug(
                    "Intervention %s selected for (%s, %s)",
                    type(intervention).__name__,
                    mode_estimate.stage.value,
                    mode_estimate.mode.value,
                )
        return actions
