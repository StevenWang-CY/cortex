"""
State Engine — Trigger Policy

Determines when interventions should be triggered based on state
estimates, signal quality, cooldown periods, and dismissal tracking.

Trigger conditions (all must be met):
1. State is HYPER
2. Confidence > 0.85
3. Workspace complexity > 0.7
4. Signal quality acceptable
5. Cooldown period elapsed (60s since last intervention)
6. Not in quiet mode (3 dismissals in 5 min → progressive quiet: 15/30/60 min)
7. Dwell time met (15s in HYPER state — sustained overwhelm, not transient spikes)

Adaptive behavior:
- Each dismissal raises trigger threshold by +0.05 for 1 hour
- 3 dismissals within 5 minutes → progressive quiet mode (15→30→60 min)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cortex.libs.config.settings import InterventionConfig, StateConfig
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.libs.utils.platform import get_config_dir
from cortex.services.state_engine.hypo_detector import (
    DEFAULT_HYPO_DWELL_SECONDS,
    HypoGateConfig,
    is_disengaged,
)
from cortex.services.state_engine.recovery_detector import (
    DEFAULT_RECOVERY_WINDOW_SECONDS,
    RecoveryGateConfig,
    RecoveryReinforcer,
    in_recovery_window,
)

logger = logging.getLogger(__name__)


# F21: persisted dismissal-model record version. Bump if the weights
# tuple shape changes so installed clients cold-start instead of loading
# an incompatible shape.
DISMISSAL_MODEL_VERSION: int = 1

# F21: debounce window for disk flushes. Whichever comes first
# (N updates OR T seconds) triggers a write. Tuned to avoid a flush
# storm during burst-dismissal sessions while still bounding loss
# at restart.
_DISMISSAL_FLUSH_EVERY_N_UPDATES: int = 10
_DISMISSAL_FLUSH_EVERY_SECONDS: float = 30.0

# F26: persisted quiet-mode-history record version.
QUIET_MODE_HISTORY_VERSION: int = 1


def _default_dismissal_model_path() -> Path:
    """Where the persisted dismissal-model record lives (F21).

    Lazy so tests can override via the ``dismissal_model_path``
    constructor kwarg instead of monkeypatching globals.
    """
    return get_config_dir() / "dismissal_model.json"


def _default_quiet_mode_history_path() -> Path:
    """Where the persisted quiet-mode-escalation record lives (F26)."""
    return get_config_dir() / "quiet_mode_history.json"


@dataclass(frozen=True)
class DismissalEvent:
    """Record of a user dismissing an intervention."""

    timestamp: float


@dataclass(frozen=True)
class Outcome:
    """Structured outcome record passed to :meth:`TriggerPolicy.record_outcome`.

    Carries the F27 ``is_fallback_origin`` flag so the dismissal model
    can exclude rule-based-fallback outcomes from training. Without the
    flag, a user dismissing a generic fallback plan during a Bedrock
    outage would push the dismissal model toward "no plans wanted",
    making real LLM plans harder to fire once Bedrock recovered.
    """

    dismissed: bool
    confidence: float = 0.0
    context_complexity: float = 0.0
    typing_burst_seconds: float = 0.0
    is_fallback_origin: bool = False


@dataclass(frozen=True)
class TriggerDecision:
    """Result of trigger policy evaluation."""

    should_trigger: bool
    reason: str
    confidence: float
    cooldown_remaining: float  # Seconds until cooldown expires
    quiet_mode_active: bool
    effective_threshold: float  # Adjusted threshold after dismissals
    context_complexity: float | None = None
    receptivity_blocked: bool = False
    dismissal_probability: float | None = None


class TriggerPolicy:
    """
    Evaluates whether an intervention should be triggered.

    Tracks cooldowns, dismissals, quiet mode, and adaptive thresholds
    to determine if an intervention is appropriate.

    Usage:
        policy = TriggerPolicy()
        decision = policy.evaluate(state_estimate)
        if decision.should_trigger:
            # ... trigger intervention ...
            pass
        # After user dismisses:
        policy.record_dismissal()
    """

    def __init__(
        self,
        config: InterventionConfig | None = None,
        state_config: StateConfig | None = None,
        *,
        hyper_dwell_seconds: float | None = None,
        hypo_dwell_seconds: float | None = None,
        recovery_window_seconds: float | None = None,
        dismissal_model_path: Path | None = None,
        quiet_mode_history_path: Path | None = None,
    ) -> None:
        self._config = config or InterventionConfig()
        # Single source of truth for HYPER dwell is StateConfig (v0.2.0 C.5).
        # Tests may inject ``hyper_dwell_seconds`` directly for short cycles.
        if hyper_dwell_seconds is not None:
            self._hyper_dwell_seconds = float(hyper_dwell_seconds)
        else:
            self._hyper_dwell_seconds = float(
                (state_config or StateConfig()).hyper_dwell_seconds
            )

        # P0 §3.5: HYPO/RECOVERY gates. Defaults come from the
        # hypo_detector / recovery_detector modules so a single tuning
        # knob in settings.py can override them later. Tests inject
        # short values to fast-forward the gates.
        self._hypo_dwell_seconds = float(
            hypo_dwell_seconds
            if hypo_dwell_seconds is not None
            else DEFAULT_HYPO_DWELL_SECONDS
        )
        self._recovery_window_seconds = float(
            recovery_window_seconds
            if recovery_window_seconds is not None
            else DEFAULT_RECOVERY_WINDOW_SECONDS
        )
        # Build per-policy gate configs so we don't share global state
        # with other policy instances (tests may run several in parallel).
        self._hypo_gate_config = HypoGateConfig(dwell_seconds=self._hypo_dwell_seconds)
        self._recovery_gate_config = RecoveryGateConfig(
            window_seconds=self._recovery_window_seconds,
            reinforce_cooldown_seconds=self._recovery_window_seconds,
        )
        self._recovery_reinforcer = RecoveryReinforcer(self._recovery_gate_config)

        # Tracks the last time we observed a HYPER -> non-HYPER transition
        # so the RECOVERY detector can age out the window. ``None`` means
        # the user has never been in HYPER during this session.
        self._last_hyper_exit_time: float | None = None
        # Last observed state literal — used to detect HYPER->X exits and
        # X->HYPER re-entries (the latter resets the reinforcer).
        self._prev_state: str | None = None

        # Cooldown tracking
        self._last_intervention_time: float = 0.0

        # F25 (audit): hysteresis trackers.
        #   ``_intervention_timestamps`` — sliding window of trigger
        #     times. ``evaluate`` prunes entries older than 3600 s and
        #     rejects when ``len(...) >= max_interventions_per_hour``.
        #   ``_hyper_enter_timestamps`` — sliding window of HYPER-entry
        #     transitions. When the count within
        #     ``oscillation_window_seconds`` exceeds
        #     ``oscillation_max_flips``, the required dwell is
        #     multiplied so genuine sustained overwhelm still passes
        #     but jittery flicker does not.
        #   ``_prev_overwhelmed`` — last observed state-bucket so we
        #     can detect a False→True transition (and thus a "flip").
        self._intervention_timestamps: deque[float] = deque(maxlen=128)
        self._hyper_enter_timestamps: deque[float] = deque(maxlen=128)
        self._prev_overwhelmed: bool = False

        # Dismissal tracking
        self._dismissals: deque[DismissalEvent] = deque(maxlen=100)
        self._threshold_bumps: deque[tuple[float, float]] = deque(maxlen=50)

        # Quiet mode
        self._quiet_mode_until: float = 0.0
        self._quiet_mode_count: int = 0
        self._quiet_mode_count_reset_at: float = 0.0

        # Intervention counter
        self._intervention_count: int = 0
        self._dismissals_total: int = 0
        self._approvals_total: int = 0
        self._dismissal_model_weights: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._dismissal_outcomes: int = 0

        # F21: dismissal-model persistence.
        # Lock guards the (weights, write-debounce counters) tuple so a
        # concurrent ``record_outcome`` cannot tear the persisted record.
        # The on-disk write itself is atomic via ``atomic_write_json``;
        # the lock only protects the in-memory state we are about to
        # serialise.
        self._dismissal_model_path: Path = (
            dismissal_model_path
            if dismissal_model_path is not None
            else _default_dismissal_model_path()
        )
        self._dismissal_persist_lock: threading.Lock = threading.Lock()
        self._dismissal_updates_since_flush: int = 0
        self._dismissal_last_flush_at: float = time.monotonic()
        self._load_dismissal_model()

        # F26: quiet-mode escalation persistence.
        # The pre-fix code reset the counter back to 0 whenever 2 hours
        # passed between dismissal bursts, so a user dismissing every
        # ~2h forever stayed at level-1 quiet (15 min) and never escalated.
        # We now persist the counter + the last-escalation timestamp and
        # only zero them on an explicit reset_quiet_mode() call.
        self._quiet_mode_history_path: Path = (
            quiet_mode_history_path
            if quiet_mode_history_path is not None
            else _default_quiet_mode_history_path()
        )
        self._quiet_mode_history_lock: threading.Lock = threading.Lock()
        self._load_quiet_mode_history()

        # P0 §3.20: weekly schedule cache. Keys are lowercase
        # day-of-week → list of 4 slot strings (``on``/``quiet``/
        # ``off``) for morning / midday / afternoon / evening. Empty
        # dict means "no schedule armed" and every slot is implicitly
        # ``on``.
        self._weekly_schedule: dict[str, list[str]] = {}

    # ──────────────────────────────────────────────────────────────────
    # P0 §3.20: weekly schedule
    # ──────────────────────────────────────────────────────────────────

    _SCHEDULE_DAYS: tuple[str, ...] = (
        "monday", "tuesday", "wednesday",
        "thursday", "friday", "saturday", "sunday",
    )
    _SCHEDULE_SLOT_HOURS: tuple[tuple[int, int], ...] = (
        (6, 12),   # morning  06:00–11:59
        (12, 14),  # midday   12:00–13:59
        (14, 18),  # afternoon 14:00–17:59
        (18, 24),  # evening  18:00–23:59
    )

    def set_weekly_schedule(self, schedule: dict[str, list[str]] | None) -> None:
        """P0 §3.20: install a user-edited weekly schedule.

        Invalid input (non-dict, wrong day keys, wrong slot count) clears
        the schedule so a misconfigured client cannot silently suppress
        every intervention.
        """
        if not isinstance(schedule, dict):
            self._weekly_schedule = {}
            return
        cleaned: dict[str, list[str]] = {}
        for day, slots in schedule.items():
            if not isinstance(day, str) or not isinstance(slots, list):
                continue
            key = day.lower().strip()
            if key not in set(self._SCHEDULE_DAYS):
                continue
            normed = [str(s).lower().strip() for s in slots[:4]]
            while len(normed) < 4:
                normed.append("on")
            cleaned[key] = normed
        self._weekly_schedule = cleaned

    def lookup_schedule_slot(
        self, *, when: float | None = None,
    ) -> str | None:
        """Return the schedule slot value for the current wall-clock time.

        ``None`` means "no schedule armed" (legacy behaviour, every
        intervention permitted). Returns one of ``"on" | "quiet" | "off"``.
        The wall-clock time is used (not ``time.monotonic``) so the
        schedule honours actual day-of-week.
        """
        if not self._weekly_schedule:
            return None
        import datetime as _dt
        ts = _dt.datetime.now() if when is None else _dt.datetime.fromtimestamp(when)
        day = self._SCHEDULE_DAYS[ts.weekday()]
        slots = self._weekly_schedule.get(day)
        if not slots:
            return None
        hour = ts.hour
        for idx, (lo, hi) in enumerate(self._SCHEDULE_SLOT_HOURS):
            if lo <= hour < hi and idx < len(slots):
                return slots[idx]
        return None

    def update_thresholds(
        self,
        config: InterventionConfig | None = None,
        *,
        state_config: StateConfig | None = None,
        hyper_dwell_seconds: float | None = None,
    ) -> None:
        """Live-update thresholds without losing cooldown / dwell state.

        ``apply_settings`` previously re-created ``TriggerPolicy`` on a
        slider change, resetting every counter (cooldown, dismissal
        ladder, quiet mode, oscillation window). A user nudging the
        sensitivity slider during an active session could then re-enable
        interventions immediately even though they had just dismissed
        three in a row. This mutator preserves all sliding-window state
        and only swaps the threshold values.
        """
        if config is not None:
            self._config = config
        if hyper_dwell_seconds is not None:
            self._hyper_dwell_seconds = float(hyper_dwell_seconds)
        elif state_config is not None:
            self._hyper_dwell_seconds = float(state_config.hyper_dwell_seconds)

    @property
    def is_quiet_mode(self) -> bool:
        """Check if quiet mode is currently active."""
        return time.monotonic() < self._quiet_mode_until

    @property
    def intervention_count(self) -> int:
        return self._intervention_count

    def evaluate(
        self,
        estimate: StateEstimate,
        *,
        context_complexity: float | None = None,
        mic_active: bool = False,
        fullscreen_active: bool = False,
        typing_burst_seconds: float = 0.0,
        within_work_hours: bool = True,
        current_time: float | None = None,
        keystroke_rate: float = 0.0,
        scroll_rate: float = 0.0,
        hr_delta_pct: float | None = None,
        enable_hypo_recovery_interventions: bool | None = None,
    ) -> TriggerDecision:
        """
        Evaluate whether an intervention should be triggered.

        Args:
            estimate: Current state estimate from the smoother.
            current_time: Reference time. Defaults to now.
            keystroke_rate: Keystrokes/minute over the last ~60s. Used by
                the P0 §3.5 HYPO behavioural-conjunction gate. Default
                0.0 keeps legacy callers' behaviour identical for HYPER.
            scroll_rate: Scroll events/minute over the last ~60s. Same
                discipline as ``keystroke_rate``.
            hr_delta_pct: HR delta from baseline as a fraction
                (``-0.10`` = 10 % below baseline). ``None`` (default)
                means HR not currently measurable; the HYPO HR gate is
                skipped rather than failed-closed.
            enable_hypo_recovery_interventions: Optional gate override.
                When ``None`` (default) the value is read from
                :class:`InterventionConfig.enable_hypo_recovery_interventions`.
                When ``False``, HYPO and RECOVERY arms are short-circuited
                (legacy HYPER-only behaviour preserved exactly).

        Returns:
            TriggerDecision with trigger verdict and explanation.
        """
        now = time.monotonic() if current_time is None else current_time

        # F25 (audit): track HYPER-enter transitions BEFORE any gate so
        # the oscillation tracker stays accurate even when other gates
        # block the trigger. A "flip" is False→True on
        # ``is_overwhelmed``; we ignore True→True (still HYPER) and any
        # transition into a non-HYPER state.
        self._record_hyper_transition(estimate.is_overwhelmed, now)

        # P0 §3.5: detect HYPER -> non-HYPER transitions for the RECOVERY
        # reinforcement window, and reset the reinforcer on the inverse
        # transition so the next post-HYPER window can fire again.
        if self._prev_state == "HYPER" and estimate.state != "HYPER":
            self._last_hyper_exit_time = now
            self._recovery_reinforcer.reset_window()
        elif self._prev_state != "HYPER" and estimate.state == "HYPER":
            # User went back into overwhelm — invalidate any stale window.
            self._recovery_reinforcer.reset_window()
        self._prev_state = estimate.state

        # Compute effective threshold (base + dismissal bumps + adaptive feedback).
        effective_threshold = self._compute_effective_threshold(now)
        confidence = estimate.confidence
        cooldown_remaining = max(
            0.0, self._last_intervention_time + self._config.cooldown_seconds - now
        )
        quiet_active = now < self._quiet_mode_until

        # F25: drop intervention timestamps that fell out of the
        # trailing-hour window so the count below reflects only the
        # last 3600 s. ``deque`` is bounded by ``maxlen``, but we still
        # prune by time to keep the window sharp.
        self._prune_intervention_window(now)
        recent_intervention_count = len(self._intervention_timestamps)
        rate_limit_cap = int(getattr(
            self._config, "max_interventions_per_hour", 6,
        ))

        # Receptivity gate (don't interrupt high-friction moments).
        if self._config.receptivity_enforced:
            if self._config.receptivity_block_if_mic_active and mic_active:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: microphone/call active",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )
            if self._config.receptivity_block_fullscreen and fullscreen_active:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: fullscreen active",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )
            if typing_burst_seconds >= self._config.receptivity_typing_burst_seconds:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: active typing burst",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )
            if not within_work_hours:
                return TriggerDecision(
                    should_trigger=False,
                    reason="Receptivity gate: outside configured work hours",
                    confidence=confidence,
                    cooldown_remaining=cooldown_remaining,
                    quiet_mode_active=quiet_active,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                    receptivity_blocked=True,
                )

        # Check quiet mode
        if quiet_active:
            return TriggerDecision(
                should_trigger=False,
                reason="Quiet mode active",
                confidence=confidence,
                cooldown_remaining=cooldown_remaining,
                quiet_mode_active=True,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # P0 §3.20: weekly schedule. ``off`` slots block every
        # intervention outright; ``quiet`` slots are honoured by upstream
        # PREVIEW-only callers (the receptivity gate already passed). A
        # ``None`` slot (no schedule armed) preserves legacy behaviour.
        slot_value = self.lookup_schedule_slot(when=current_time)
        if slot_value == "off":
            return TriggerDecision(
                should_trigger=False,
                reason="weekly_schedule_off",
                confidence=confidence,
                cooldown_remaining=cooldown_remaining,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Check cooldown
        if cooldown_remaining > 0:
            return TriggerDecision(
                should_trigger=False,
                reason=f"Cooldown active ({cooldown_remaining:.0f}s remaining)",
                confidence=confidence,
                cooldown_remaining=cooldown_remaining,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # F25 (audit): hourly cap. A session sustaining more than
        # ``max_interventions_per_hour`` triggers in the trailing hour
        # is almost certainly oscillating, not in genuine sustained
        # overwhelm. This gate is independent of the dismissal-driven
        # quiet-mode (F26): it protects users who never dismiss but
        # whose biometrics flutter at the threshold.
        if rate_limit_cap > 0 and recent_intervention_count >= rate_limit_cap:
            return TriggerDecision(
                should_trigger=False,
                reason=(
                    f"Hourly intervention cap reached "
                    f"({recent_intervention_count}/{rate_limit_cap} in last hour)"
                ),
                confidence=confidence,
                cooldown_remaining=cooldown_remaining,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # ------------------------------------------------------------------
        # P0 §3.5: state-dispatch.
        #
        # The shared gates above (receptivity, quiet mode, cooldown, hourly
        # cap) run BEFORE the dispatch so they uniformly apply to every
        # state arm. The state-specific gate decides whether *this state*
        # warrants a trigger, returning a fully-formed TriggerDecision.
        # ------------------------------------------------------------------
        if enable_hypo_recovery_interventions is None:
            enable_hypo_recovery_interventions = bool(
                getattr(
                    self._config,
                    "enable_hypo_recovery_interventions",
                    False,
                )
            )

        # ``dismiss_prob`` is computed here so every arm can stamp it on
        # its TriggerDecision uniformly (preserves the F-series telemetry).
        dismiss_prob = self._predict_dismiss_probability(
            confidence=confidence,
            context_complexity=context_complexity or 0.0,
            typing_burst_seconds=typing_burst_seconds,
        )

        STATE_GATES: dict[str, Callable[..., TriggerDecision]] = {
            "HYPER": self._check_hyper_gates,
            "HYPO": self._check_hypo_gates,
            "RECOVERY": self._check_recovery_gates,
            "FLOW": self._reject_flow_state,
        }
        gate = STATE_GATES.get(estimate.state, self._reject_flow_state)
        return gate(
            estimate,
            now=now,
            effective_threshold=effective_threshold,
            context_complexity=context_complexity,
            dismiss_prob=dismiss_prob,
            keystroke_rate=keystroke_rate,
            scroll_rate=scroll_rate,
            hr_delta_pct=hr_delta_pct,
            enable_hypo_recovery_interventions=enable_hypo_recovery_interventions,
        )

    # ------------------------------------------------------------------
    # State-arm gates (P0 §3.5).
    #
    # Each gate is invoked from ``evaluate`` AFTER the shared
    # receptivity / quiet-mode / cooldown / hourly-cap gates have already
    # passed. The gate decides whether *this state* warrants triggering
    # and returns a fully-formed TriggerDecision.
    # ------------------------------------------------------------------

    def _check_hyper_gates(
        self,
        estimate: StateEstimate,
        *,
        now: float,
        effective_threshold: float,
        context_complexity: float | None,
        dismiss_prob: float,
        keystroke_rate: float,  # unused for HYPER but accepted uniformly
        scroll_rate: float,
        hr_delta_pct: float | None,
        enable_hypo_recovery_interventions: bool,
    ) -> TriggerDecision:
        """HYPER (overwhelm) gate. Identical to the pre-refactor logic."""
        confidence = estimate.confidence

        # Check confidence exceeds effective threshold
        if confidence < effective_threshold:
            return TriggerDecision(
                should_trigger=False,
                reason=f"Confidence {confidence:.2f} below threshold {effective_threshold:.2f}",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        if (
            context_complexity is not None
            and context_complexity < self._config.complexity_threshold
        ):
            return TriggerDecision(
                should_trigger=False,
                reason=(
                    f"Workspace complexity {context_complexity:.2f} below "
                    f"{self._config.complexity_threshold:.2f}"
                ),
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # P1 Pipeline A: HYPER-specific signal-quality floor. Overall
        # ``acceptable`` is a 0.3 weighted blend that can pass even when
        # physio and kinematics are both near zero (e.g. webcam blocked
        # at night). HYPER requires either a usable physio channel
        # (physio >= 0.3) or at least mid-quality kinematics (>= 0.5)
        # before we let the trigger fire — otherwise the overwhelm
        # estimate is driven entirely by telemetry and is too noisy to
        # justify interrupting the user.
        sq = estimate.signal_quality
        if not (sq.physio >= 0.3 or sq.kinematics >= 0.5):
            logger.info(
                "HYPER trigger deferred — insufficient physio signal quality "
                "(physio=%.2f, kinematics=%.2f, telemetry=%.2f)",
                sq.physio,
                sq.kinematics,
                sq.telemetry,
            )
            return TriggerDecision(
                should_trigger=False,
                reason=(
                    f"Insufficient physio signal quality "
                    f"(physio={sq.physio:.2f}, kinematics={sq.kinematics:.2f})"
                ),
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Check signal quality — with telemetry-only fallback
        # In poor lighting (dorm rooms at night), webcam signals degrade but
        # behavioral telemetry (mouse, keyboard, tab switching) remains reliable.
        # Allow interventions when telemetry is strong, with stricter confidence.
        if not estimate.signal_quality.acceptable:
            telemetry_fallback = (
                estimate.signal_quality.telemetry >= 0.7
                and confidence >= min(0.95, effective_threshold + 0.10)
            )
            if not telemetry_fallback:
                return TriggerDecision(
                    should_trigger=False,
                    reason=f"Signal quality too low ({estimate.signal_quality.overall:.2f})",
                    confidence=confidence,
                    cooldown_remaining=0.0,
                    quiet_mode_active=False,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                )

        # Check dwell time (must be in HYPER for >= hyper_dwell_seconds).
        # F25 (audit): when the user's state has been oscillating, the
        # base dwell is too short to distinguish jitter from genuine
        # sustained overwhelm. Multiply the required dwell by
        # ``oscillation_dwell_multiplier`` when recent flip count
        # exceeds ``oscillation_max_flips``. The multiplier is bounded
        # by config; values <= 1.0 disable the gate.
        dwell_required = self._hyper_dwell_seconds
        if self._is_oscillating(now):
            multiplier = float(getattr(
                self._config, "oscillation_dwell_multiplier", 2.0,
            ))
            if multiplier > 1.0:
                dwell_required *= multiplier
        if estimate.dwell_seconds < dwell_required:
            return TriggerDecision(
                should_trigger=False,
                reason=f"Dwell time {estimate.dwell_seconds:.1f}s < {dwell_required:.0f}s required",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        if (
            self._config.dismissal_model_enabled
            and self._dismissal_outcomes >= 10
            and dismiss_prob > self._config.dismissal_model_threshold
        ):
            return TriggerDecision(
                should_trigger=False,
                reason=f"Predicted dismissal probability too high ({dismiss_prob:.2f})",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
                dismissal_probability=dismiss_prob,
            )

        # All conditions met — trigger intervention
        return TriggerDecision(
            should_trigger=True,
            reason="All trigger conditions met",
            confidence=confidence,
            cooldown_remaining=0.0,
            quiet_mode_active=False,
            effective_threshold=effective_threshold,
            context_complexity=context_complexity,
            dismissal_probability=dismiss_prob,
        )

    def _check_hypo_gates(
        self,
        estimate: StateEstimate,
        *,
        now: float,
        effective_threshold: float,
        context_complexity: float | None,
        dismiss_prob: float,
        keystroke_rate: float,
        scroll_rate: float,
        hr_delta_pct: float | None,
        enable_hypo_recovery_interventions: bool,
    ) -> TriggerDecision:
        """HYPO (disengaged) re-engagement gate (P0 §3.5).

        Conservative by design: same confidence floor as HYPER, and a
        behavioural conjunction (keystroke + scroll, optional HR-below-
        baseline) on top.
        """
        confidence = estimate.confidence

        if not enable_hypo_recovery_interventions:
            return TriggerDecision(
                should_trigger=False,
                reason="HYPO interventions disabled (opt-in)",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Same confidence floor as HYPER — see P0 §3.5 design contract.
        if confidence < effective_threshold:
            return TriggerDecision(
                should_trigger=False,
                reason=f"HYPO confidence {confidence:.2f} below threshold {effective_threshold:.2f}",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        # Signal quality gate (telemetry fallback as for HYPER).
        if not estimate.signal_quality.acceptable:
            telemetry_fallback = (
                estimate.signal_quality.telemetry >= 0.7
                and confidence >= min(0.95, effective_threshold + 0.10)
            )
            if not telemetry_fallback:
                return TriggerDecision(
                    should_trigger=False,
                    reason=f"HYPO signal quality too low ({estimate.signal_quality.overall:.2f})",
                    confidence=confidence,
                    cooldown_remaining=0.0,
                    quiet_mode_active=False,
                    effective_threshold=effective_threshold,
                    context_complexity=context_complexity,
                )

        should_fire, reason = is_disengaged(
            None,
            estimate.dwell_seconds,
            None,
            keystroke_rate=keystroke_rate,
            scroll_rate=scroll_rate,
            hr_delta_pct=hr_delta_pct,
            config=self._hypo_gate_config,
        )
        if not should_fire:
            return TriggerDecision(
                should_trigger=False,
                reason=f"HYPO behavioural gate: {reason}",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        return TriggerDecision(
            should_trigger=True,
            reason=f"HYPO re-engagement: {reason}",
            confidence=confidence,
            cooldown_remaining=0.0,
            quiet_mode_active=False,
            effective_threshold=effective_threshold,
            context_complexity=context_complexity,
            dismissal_probability=dismiss_prob,
        )

    def _check_recovery_gates(
        self,
        estimate: StateEstimate,
        *,
        now: float,
        effective_threshold: float,
        context_complexity: float | None,
        dismiss_prob: float,
        keystroke_rate: float,
        scroll_rate: float,
        hr_delta_pct: float | None,
        enable_hypo_recovery_interventions: bool,
    ) -> TriggerDecision:
        """RECOVERY reinforcement gate (P0 §3.5).

        Fires at most once per post-HYPER window. The plan emitted by
        the downstream prompt is ``intervention_type="overlay_only"``
        with ``tone="minimal"`` — must not block input.
        """
        confidence = estimate.confidence

        if not enable_hypo_recovery_interventions:
            return TriggerDecision(
                should_trigger=False,
                reason="RECOVERY interventions disabled (opt-in)",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        just_exited = self._last_hyper_exit_time is not None
        seconds_since_exit = (
            now - self._last_hyper_exit_time
            if self._last_hyper_exit_time is not None
            else float("inf")
        )
        in_window, reason = in_recovery_window(
            None,
            just_exited_hyper=just_exited,
            seconds_since_exit=seconds_since_exit,
            config=self._recovery_gate_config,
        )
        if not in_window:
            return TriggerDecision(
                should_trigger=False,
                reason=f"RECOVERY gate: {reason}",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        if not self._recovery_reinforcer.should_reinforce(
            dwell_seconds=estimate.dwell_seconds,
        ):
            return TriggerDecision(
                should_trigger=False,
                reason="RECOVERY reinforcement already delivered this window",
                confidence=confidence,
                cooldown_remaining=0.0,
                quiet_mode_active=False,
                effective_threshold=effective_threshold,
                context_complexity=context_complexity,
            )

        return TriggerDecision(
            should_trigger=True,
            reason="RECOVERY reinforcement",
            confidence=confidence,
            cooldown_remaining=0.0,
            quiet_mode_active=False,
            effective_threshold=effective_threshold,
            context_complexity=context_complexity,
            dismissal_probability=dismiss_prob,
        )

    def _reject_flow_state(
        self,
        estimate: StateEstimate,
        *,
        now: float,
        effective_threshold: float,
        context_complexity: float | None,
        dismiss_prob: float,
        keystroke_rate: float,
        scroll_rate: float,
        hr_delta_pct: float | None,
        enable_hypo_recovery_interventions: bool,
    ) -> TriggerDecision:
        """FLOW (or any unrecognised state): never intervene."""
        return TriggerDecision(
            should_trigger=False,
            reason="State is FLOW, no intervention needed",
            confidence=estimate.confidence,
            cooldown_remaining=0.0,
            quiet_mode_active=False,
            effective_threshold=effective_threshold,
            context_complexity=context_complexity,
        )

    def record_intervention(self, timestamp: float | None = None) -> None:
        """Record that an intervention was triggered."""
        now = time.monotonic() if timestamp is None else timestamp
        self._last_intervention_time = now
        self._intervention_count += 1
        # F25 (audit): append to the sliding-hour window so the next
        # evaluate() correctly enforces ``max_interventions_per_hour``.
        # The deque is bounded by ``maxlen``; pruning by time happens
        # in evaluate().
        self._intervention_timestamps.append(now)
        logger.info(f"Intervention #{self._intervention_count} triggered")

    # ------------------------------------------------------------------
    # F25 helpers — hysteresis against cooldown/dwell oscillation.
    # ------------------------------------------------------------------

    def _prune_intervention_window(self, now: float) -> None:
        """Remove intervention timestamps older than 3600 s.

        Called from ``evaluate`` before the rate-limit gate so the
        count reflects only the trailing-hour window.
        """
        cutoff = now - 3600.0
        while self._intervention_timestamps and self._intervention_timestamps[0] < cutoff:
            self._intervention_timestamps.popleft()

    def _record_hyper_transition(self, is_overwhelmed: bool, now: float) -> None:
        """Append a timestamp on every False→True transition into HYPER.

        The sliding window is later read by ``_is_oscillating`` to
        decide whether to escalate the required dwell time.
        """
        if is_overwhelmed and not self._prev_overwhelmed:
            self._hyper_enter_timestamps.append(now)
        self._prev_overwhelmed = is_overwhelmed

    def _is_oscillating(self, now: float) -> bool:
        """Return True iff the user's state has flipped into HYPER more
        than ``oscillation_max_flips`` times in the last
        ``oscillation_window_seconds``.

        Prunes stale entries as a side effect so the check is O(K) on
        each call where K is the number of recent flips.
        """
        window = float(getattr(
            self._config, "oscillation_window_seconds", 600.0,
        ))
        max_flips = int(getattr(
            self._config, "oscillation_max_flips", 6,
        ))
        if max_flips <= 0 or window <= 0:
            return False
        cutoff = now - window
        while (
            self._hyper_enter_timestamps
            and self._hyper_enter_timestamps[0] < cutoff
        ):
            self._hyper_enter_timestamps.popleft()
        return len(self._hyper_enter_timestamps) > max_flips

    def record_dismissal(self, timestamp: float | None = None) -> None:
        """
        Record that the user dismissed an intervention.

        Tracks dismissals for quiet mode and adaptive thresholds.
        """
        now = time.monotonic() if timestamp is None else timestamp
        self._dismissals.append(DismissalEvent(timestamp=now))
        self._dismissals_total += 1

        # Add threshold bump (+0.05 for 1 hour)
        expiry = now + self._config.dismissal_decay_hours * 3600.0
        self._threshold_bumps.append((self._config.dismissal_threshold_bump, expiry))

        # Check for quiet mode trigger (3 dismissals in 5 min)
        recent_window = self._config.dismissal_window_minutes * 60.0
        recent_dismissals = sum(
            1 for d in self._dismissals
            if d.timestamp >= now - recent_window
        )

        if recent_dismissals >= self._config.max_dismissals:
            # F26: do NOT reset the escalation counter purely because
            # ``now > self._quiet_mode_count_reset_at``. A user dismissing
            # every >2h forever stayed at level-1 quiet (15 min) and never
            # graduated to longer cool-downs. Counter only zeroes on an
            # explicit ``reset_quiet_mode()`` invocation (e.g. dashboard
            # "Reset suggestions"). We still track the timestamp of the
            # last escalation for observability.
            self._quiet_mode_count += 1
            # Progressive escalation: 15min -> 30min -> 60min
            durations = [
                self._config.quiet_mode_minutes,       # 15 min (base)
                self._config.quiet_mode_minutes * 2,    # 30 min
                self._config.quiet_mode_minutes * 4,    # 60 min
            ]
            minutes = durations[min(self._quiet_mode_count - 1, len(durations) - 1)]
            self._quiet_mode_until = now + minutes * 60.0
            # Track the last-escalation timestamp for diagnostics; we no
            # longer use it as a reset trigger.
            self._quiet_mode_count_reset_at = now

            logger.info(
                f"Quiet mode activated for {minutes} minutes (level {self._quiet_mode_count}, "
                f"{recent_dismissals} dismissals in {self._config.dismissal_window_minutes} min)"
            )
            self._persist_quiet_mode_history()

    def record_outcome(
        self,
        *,
        dismissed: bool,
        confidence: float = 0.0,
        context_complexity: float = 0.0,
        typing_burst_seconds: float = 0.0,
        is_fallback_origin: bool = False,
    ) -> None:
        """Update adaptive thresholding and dismissal model with user feedback.

        F27: when ``is_fallback_origin`` is true the outcome came from a
        rule-based fallback plan (circuit-breaker open, retries
        exhausted, or budget kill). We skip the logistic-regression
        update so a Bedrock outage cannot poison the personalisation
        layer with "user dismissed a generic plan" labels. The aggregate
        approval/dismissal counters still tick so quiet-mode escalation
        and adaptive threshold feedback still see the user behaviour.
        """
        if dismissed:
            self._dismissals_total += 1
        else:
            self._approvals_total += 1

        # F27: a fallback-origin outcome is real user behaviour that the
        # quiet-mode counter and adaptive threshold should reflect, but
        # it must NOT teach the dismissal model — the generic plan is
        # not representative of what the LLM would have proposed.
        if is_fallback_origin:
            logger.debug(
                "record_outcome: skipping dismissal-model update for "
                "fallback-origin outcome (dismissed=%s)",
                dismissed,
            )
            return

        self._dismissal_outcomes += 1

        # Online logistic update (very small-step SGD).
        y = 1.0 if dismissed else 0.0
        x = np.array(
            [float(confidence), float(context_complexity), min(1.0, float(typing_burst_seconds) / 10.0)],
            dtype=np.float64,
        )
        snapshot: tuple[float, float, float] | None = None
        outcomes_snapshot = 0
        with self._dismissal_persist_lock:
            w = np.array(self._dismissal_model_weights, dtype=np.float64)
            z = float(w @ x)
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0)))
            grad = (p - y) * x
            w = w - 0.05 * grad
            self._dismissal_model_weights = (float(w[0]), float(w[1]), float(w[2]))
            self._dismissal_updates_since_flush += 1
            now = time.monotonic()
            should_flush = (
                self._dismissal_updates_since_flush
                >= _DISMISSAL_FLUSH_EVERY_N_UPDATES
                or (now - self._dismissal_last_flush_at)
                >= _DISMISSAL_FLUSH_EVERY_SECONDS
            )
            if should_flush:
                self._dismissal_updates_since_flush = 0
                self._dismissal_last_flush_at = now
                snapshot = self._dismissal_model_weights
                outcomes_snapshot = self._dismissal_outcomes

        if snapshot is not None:
            self._persist_dismissal_model(snapshot, outcomes_snapshot)

    def flush_dismissal_model(self) -> None:
        """Force a persistence write of the current dismissal-model record.

        Useful at shutdown, or in tests that want to observe the latest
        weights without waiting for the debounce window to close (F21).
        """
        with self._dismissal_persist_lock:
            self._dismissal_updates_since_flush = 0
            self._dismissal_last_flush_at = time.monotonic()
            snapshot = self._dismissal_model_weights
            outcomes_snapshot = self._dismissal_outcomes
        self._persist_dismissal_model(snapshot, outcomes_snapshot)

    def _persist_dismissal_model(
        self,
        weights: tuple[float, float, float],
        outcomes: int,
    ) -> None:
        """Write the dismissal-model record to disk atomically (F21)."""
        record = {
            "model_version": DISMISSAL_MODEL_VERSION,
            "weights": [float(w) for w in weights],
            "outcomes": int(outcomes),
            "saved_at": time.time(),
        }
        try:
            atomic_write_json(self._dismissal_model_path, record)
        except OSError as exc:
            logger.warning(
                "Failed to persist dismissal model to %s: %s",
                self._dismissal_model_path,
                exc,
            )

    def _load_dismissal_model(self) -> None:
        """Rehydrate persisted weights on construction (F21).

        Missing file or version mismatch -> cold-start with zeros.
        Malformed JSON is tolerated identically: log and cold-start.
        """
        path = self._dismissal_model_path
        if not path.exists():
            logger.debug("No dismissal model at %s; cold-starting.", path)
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not read dismissal model at %s (%s); cold-starting.",
                path,
                exc,
            )
            return
        if not isinstance(data, dict):
            logger.warning(
                "Dismissal model at %s is not a JSON object; cold-starting.",
                path,
            )
            return
        version = data.get("model_version")
        if version != DISMISSAL_MODEL_VERSION:
            logger.info(
                "Dismissal model version mismatch (have=%r, want=%r); cold-starting.",
                version,
                DISMISSAL_MODEL_VERSION,
            )
            return
        weights = data.get("weights")
        if (
            not isinstance(weights, list)
            or len(weights) != 3
            or not all(isinstance(w, (int, float)) for w in weights)
        ):
            logger.warning(
                "Dismissal model weights shape invalid at %s; cold-starting.",
                path,
            )
            return
        self._dismissal_model_weights = (
            float(weights[0]),
            float(weights[1]),
            float(weights[2]),
        )
        outcomes = data.get("outcomes", 0)
        if isinstance(outcomes, int) and outcomes >= 0:
            self._dismissal_outcomes = outcomes
        logger.info(
            "Rehydrated dismissal model from %s (outcomes=%d)",
            path,
            self._dismissal_outcomes,
        )

    def activate_quiet_mode(
        self,
        *,
        duration_minutes: int | None = None,
        current_time: float | None = None,
    ) -> None:
        """Force quiet mode on for an explicit duration."""
        now = time.monotonic() if current_time is None else current_time
        minutes = duration_minutes or self._config.quiet_mode_minutes
        self._quiet_mode_until = now + max(1, minutes) * 60.0

    def clear_quiet_mode(self) -> None:
        """Disable quiet mode immediately."""
        self._quiet_mode_until = 0.0

    def reset_quiet_mode(self) -> None:
        """User-driven quiet-mode reset (F26).

        Clears the active quiet window AND the persisted escalation
        counter. Wired (by dashboard / API in a follow-up commit) to a
        "Reset suggestions" affordance. The 2-hour idle reset that used
        to fire silently inside ``record_dismissal`` is gone: only this
        explicit call zeroes the escalation memory.
        """
        self._quiet_mode_until = 0.0
        self._quiet_mode_count = 0
        self._quiet_mode_count_reset_at = 0.0
        try:
            if self._quiet_mode_history_path.exists():
                self._quiet_mode_history_path.unlink()
        except OSError:
            logger.debug(
                "Could not remove quiet mode history file at %s",
                self._quiet_mode_history_path,
            )

    def _persist_quiet_mode_history(self) -> None:
        """Write the current quiet-mode escalation record (F26).

        Crash-safe via ``atomic_write_json``. Lock-guarded so a snapshot
        read inside ``reset_quiet_mode`` cannot interleave with the
        record_dismissal writer.
        """
        with self._quiet_mode_history_lock:
            now = time.monotonic()
            # ``quiet_mode_until`` is in the future, so the remainder is
            # ``until - now`` (positive while the window is still open).
            # ``quiet_mode_count_reset_at`` is the monotonic timestamp of
            # the LAST escalation — strictly in the past — so the "how
            # long ago did the last escalation fire" delta is
            # ``now - reset_at``. audit-w2: the original implementation
            # flipped these operands and ``max(0.0, ...)`` clamped the
            # negative result to 0, so the persisted age was always 0 and
            # rehydration stamped ``quiet_mode_count_reset_at`` back at
            # load-time (i.e. "the last escalation was just now"). The
            # field is currently diagnostic only — no other branch reads
            # it — but the latent bug would land the moment any
            # follow-up consumer trusts the value.
            record = {
                "version": QUIET_MODE_HISTORY_VERSION,
                "quiet_mode_count": int(self._quiet_mode_count),
                "quiet_mode_until_monotonic_delta": max(
                    0.0,
                    self._quiet_mode_until - now,
                ),
                "last_escalation_age_seconds": max(
                    0.0,
                    now - self._quiet_mode_count_reset_at,
                )
                if self._quiet_mode_count_reset_at > 0.0
                else 0.0,
                "saved_at": time.time(),
            }
        try:
            atomic_write_json(self._quiet_mode_history_path, record)
        except OSError as exc:
            logger.warning(
                "Failed to persist quiet mode history to %s: %s",
                self._quiet_mode_history_path,
                exc,
            )

    def _load_quiet_mode_history(self) -> None:
        """Rehydrate the quiet-mode escalation counter on construction (F26).

        Missing file / wrong version / malformed JSON => cold start.
        Wall-clock-relative remainders are converted back to monotonic
        time so a paused-and-resumed restart still honours any quiet
        window that should still be active. If clock skew or a long
        downtime makes the remainder non-sensible, the loader falls back
        to a fresh ``_quiet_mode_until = 0`` while preserving the
        escalation counter — the user's escalation memory is the bit
        we care about most.
        """
        path = self._quiet_mode_history_path
        if not path.exists():
            logger.debug("No quiet mode history at %s; cold-starting.", path)
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Could not read quiet mode history at %s (%s); cold-starting.",
                path,
                exc,
            )
            return
        if not isinstance(data, dict):
            logger.warning(
                "Quiet mode history at %s is not a JSON object; cold-starting.",
                path,
            )
            return
        if data.get("version") != QUIET_MODE_HISTORY_VERSION:
            logger.info(
                "Quiet mode history version mismatch (have=%r, want=%r); cold-starting.",
                data.get("version"),
                QUIET_MODE_HISTORY_VERSION,
            )
            return
        count = data.get("quiet_mode_count", 0)
        if isinstance(count, int) and count >= 0:
            self._quiet_mode_count = count
        # Restore the active quiet window if a remainder is still set.
        remaining = data.get("quiet_mode_until_monotonic_delta", 0.0)
        if isinstance(remaining, (int, float)) and remaining > 0.0:
            self._quiet_mode_until = time.monotonic() + float(remaining)
        # audit-w2: ``last_escalation_age_seconds`` replaces the legacy
        # ``last_escalation_at_monotonic_delta`` field whose sign was
        # inverted at write time. Read both names so an upgrade-in-place
        # does not lose the field; subtract the age from ``now`` to get
        # the monotonic timestamp of the last escalation. Legacy records
        # carrying the bugged 0-clamped value rehydrate to "just now",
        # which matches the pre-fix observable behaviour.
        last_age = data.get("last_escalation_age_seconds")
        if last_age is None:
            last_age = data.get("last_escalation_at_monotonic_delta", 0.0)
        if isinstance(last_age, (int, float)) and last_age >= 0.0:
            self._quiet_mode_count_reset_at = time.monotonic() - float(last_age)
        logger.info(
            "Rehydrated quiet-mode history from %s (level=%d)",
            path,
            self._quiet_mode_count,
        )

    def _compute_effective_threshold(self, now: float) -> float:
        """
        Compute the effective trigger threshold.

        Base threshold (0.85) + active dismissal bumps.
        """
        base = self._config.overlay_threshold

        # Add active threshold bumps
        total_bump = sum(
            bump for bump, expiry in self._threshold_bumps
            if expiry > now
        )

        threshold = base + total_bump
        if self._config.adaptive_threshold_enabled:
            feedback_offset = (self._dismissals_total - self._approvals_total) * 0.01
            threshold += float(np.clip(feedback_offset, -0.10, 0.10))
            threshold = float(np.clip(
                threshold,
                self._config.adaptive_threshold_min,
                self._config.adaptive_threshold_max,
            ))
        # Cap at reasonable maximum
        return min(0.99, threshold)

    def _predict_dismiss_probability(
        self,
        *,
        confidence: float,
        context_complexity: float,
        typing_burst_seconds: float,
    ) -> float:
        """Predict dismissal probability from lightweight online model."""
        w = np.array(self._dismissal_model_weights, dtype=np.float64)
        if not np.any(np.abs(w) > 1e-6):
            # Cold start: neutral prior until we have enough labeled outcomes.
            return 0.5
        x = np.array(
            [float(confidence), float(context_complexity), min(1.0, float(typing_burst_seconds) / 10.0)],
            dtype=np.float64,
        )
        z = float(w @ x)
        return float(1.0 / (1.0 + np.exp(-np.clip(z, -20.0, 20.0))))

    def reset(self) -> None:
        """Reset all trigger policy state."""
        self._last_intervention_time = 0.0
        self._dismissals.clear()
        self._threshold_bumps.clear()
        self._quiet_mode_until = 0.0
        self._quiet_mode_count = 0
        self._quiet_mode_count_reset_at = 0.0
        self._intervention_count = 0
        self._dismissal_outcomes = 0
        self._dismissals_total = 0
        self._approvals_total = 0
        self._dismissal_model_weights = (0.0, 0.0, 0.0)
        # F21: also clear the persisted record so a subsequent restart
        # does not re-hydrate the model we just wiped.
        try:
            if self._dismissal_model_path.exists():
                self._dismissal_model_path.unlink()
        except OSError:
            logger.debug(
                "Could not remove dismissal model file at %s",
                self._dismissal_model_path,
            )
        # F26: also clear the persisted quiet-mode escalation memory.
        try:
            if self._quiet_mode_history_path.exists():
                self._quiet_mode_history_path.unlink()
        except OSError:
            logger.debug(
                "Could not remove quiet mode history file at %s",
                self._quiet_mode_history_path,
            )
