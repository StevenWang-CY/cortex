"""
State Engine — HYPO Behavioural-Conjunction Detector (P0 §3.5).

HYPO ("disengaged") cannot be inferred from biometrics alone. Slow reading
looks identical to drifting: HR drops, blink rate dips, posture relaxes.
The discriminator is *what the user is doing with their hands*. A user
who is reading attentively still scrolls; a user who is drifting does
neither.

This module wraps that conjunction so the trigger policy can fire HYPO
interventions only when:

1. HYPO has been sustained for >= ``hypo_dwell_seconds`` (default 60s)
2. ``keystroke_rate`` is below ``max_keystroke_per_min`` (default 5/min)
3. ``scroll_rate`` is below ``max_scroll_per_min`` (default 1/min)
4. *(optional)* if ``hr_delta_pct`` is provided (HR relative to baseline)
   it must be at or below ``min_hr_delta_pct`` (default -0.05, i.e.
   ≥ 5 % below baseline). When not measured (``None``), the HR gate is
   skipped — never fail closed on a missing sensor.

Defaults are intentionally conservative; a false "you're drifting"
intervention is as disruptive as a false "you're overwhelmed" one.

Returned by :func:`is_disengaged`: ``(should_fire, reason)`` where
``reason`` is a short human-readable label suitable for inclusion in a
:class:`TriggerDecision.reason` string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Module-level defaults the trigger_policy passes in.
# ---------------------------------------------------------------------------

# Minimum seconds of sustained HYPO before a re-engagement intervention is
# allowed to fire. Matches the P0 §3.5 design contract (60 s).
DEFAULT_HYPO_DWELL_SECONDS: float = 60.0

# Behavioural-inactivity thresholds. A user typing > 5 keystrokes/min or
# scrolling > 1 page/min is engaged — do NOT interrupt them.
DEFAULT_MAX_KEYSTROKE_PER_MIN: float = 5.0
DEFAULT_MAX_SCROLL_PER_MIN: float = 1.0

# If HR is measured relative to baseline, require at least a 5 % drop
# below baseline. Slow reading without HR drop is left alone.
DEFAULT_MIN_HR_DELTA_PCT: float = -0.05


@dataclass(frozen=True)
class HypoGateConfig:
    """Tunables for the HYPO behavioural conjunction.

    Surfaced as a dataclass so :mod:`cortex.libs.config.settings` can
    plumb future user-facing sliders straight through without the trigger
    policy needing further surgery.
    """

    dwell_seconds: float = DEFAULT_HYPO_DWELL_SECONDS
    max_keystroke_per_min: float = DEFAULT_MAX_KEYSTROKE_PER_MIN
    max_scroll_per_min: float = DEFAULT_MAX_SCROLL_PER_MIN
    min_hr_delta_pct: float = DEFAULT_MIN_HR_DELTA_PCT


def is_disengaged(
    features: Any,
    dwell_seconds: float,
    baselines: Any,
    *,
    keystroke_rate: float,
    scroll_rate: float,
    hr_delta_pct: float | None = None,
    config: HypoGateConfig | None = None,
) -> tuple[bool, str]:
    """Decide whether a sustained HYPO state warrants a re-engagement.

    Args:
        features: Live feature vector (unused here, accepted for symmetry
            with other detectors so the trigger policy can pass it
            uniformly; future feature additions can be wired in without
            changing the call site).
        dwell_seconds: Seconds the user has been in HYPO continuously.
            Sourced from :class:`StateEstimate.dwell_seconds`.
        baselines: User baselines (unused here today; reserved for
            future per-user threshold scaling).
        keystroke_rate: Keystrokes per minute over the last ~60 s.
        scroll_rate: Scroll events per minute over the last ~60 s.
        hr_delta_pct: HR delta from baseline as a fraction (``-0.10``
            means HR is 10 % below baseline). ``None`` if HR is not
            currently measurable (poor lighting, camera off, etc.) — in
            that case the HR gate is skipped, never failed closed.
        config: Optional override for the module-level defaults.

    Returns:
        ``(should_fire, reason)``. ``reason`` is a short human-readable
        label suitable for surfacing through
        :class:`TriggerDecision.reason` and the AMIP outcome telemetry.
    """
    cfg = config or HypoGateConfig()

    if dwell_seconds < cfg.dwell_seconds:
        return (
            False,
            f"HYPO dwell {dwell_seconds:.0f}s < {cfg.dwell_seconds:.0f}s required",
        )

    if keystroke_rate >= cfg.max_keystroke_per_min:
        return (
            False,
            f"Keystroke rate {keystroke_rate:.1f}/min >= "
            f"{cfg.max_keystroke_per_min:.1f}/min (still engaged)",
        )

    if scroll_rate >= cfg.max_scroll_per_min:
        return (
            False,
            f"Scroll rate {scroll_rate:.1f}/min >= "
            f"{cfg.max_scroll_per_min:.1f}/min (still reading)",
        )

    # HR gate is *only* enforced when we have a measurement. A missing
    # sensor (None) skips the check rather than failing closed.
    hr_clause = "HR not measured"
    if hr_delta_pct is not None:
        if hr_delta_pct > cfg.min_hr_delta_pct:
            return (
                False,
                f"HR delta {hr_delta_pct:+.1%} above floor "
                f"{cfg.min_hr_delta_pct:+.1%} (HR not sub-baseline)",
            )
        hr_clause = f"sub-baseline HR ({hr_delta_pct:+.1%})"

    return (
        True,
        f"drift detected: low keystrokes ({keystroke_rate:.1f}/min), "
        f"low scroll ({scroll_rate:.1f}/min), {hr_clause}",
    )
