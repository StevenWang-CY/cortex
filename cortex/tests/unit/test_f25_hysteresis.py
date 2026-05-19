"""Audit F25 — trigger-policy hysteresis against cooldown/dwell oscillation.

Two gates are added on top of the existing cooldown / dwell:

1. **Hourly intervention cap.** ``max_interventions_per_hour`` bounds the
   number of triggers in the trailing 60 minutes. A session whose
   biometrics oscillate at the HYPER/FLOW boundary would otherwise fire
   on every cooldown expiry; the cap stops that pattern cold.

2. **Oscillation-aware dwell.** When the state has entered HYPER more
   than ``oscillation_max_flips`` times in
   ``oscillation_window_seconds``, the required dwell is multiplied by
   ``oscillation_dwell_multiplier``. Jittery flicker fails the
   stretched dwell; genuine sustained overwhelm still passes.

Each test fails on pre-F25 ``main``: the cap and oscillation logic
don't exist, so a synthetic oscillating trace produces as many
interventions as the daemon's cooldown allows.
"""

from __future__ import annotations

from cortex.libs.config.settings import InterventionConfig, StateConfig
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.state_engine.trigger_policy import TriggerPolicy


def _hyper_estimate(*, confidence: float = 0.85, dwell: float = 60.0) -> StateEstimate:
    """Build a passing HYPER estimate. Confidence and dwell default to
    values that comfortably exceed thresholds so the test isolates the
    F25 gate from confidence / dwell / signal-quality checks."""
    return StateEstimate(
        state="HYPER",
        confidence=confidence,
        scores=StateScores(flow=0.05, hypo=0.05, hyper=0.85, recovery=0.05),
        reasons=["test"],
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=dwell,
    )


def _flow_estimate(*, dwell: float = 10.0) -> StateEstimate:
    return StateEstimate(
        state="FLOW",
        confidence=0.85,
        scores=StateScores(flow=0.85, hypo=0.05, hyper=0.05, recovery=0.05),
        reasons=["test"],
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=dwell,
    )


# ---------------------------------------------------------------------------
# Hourly intervention cap
# ---------------------------------------------------------------------------


def test_hourly_cap_blocks_after_max_triggers() -> None:
    """Once ``max_interventions_per_hour`` triggers have fired in the
    trailing 60 minutes, the next evaluate() returns ``should_trigger=False``
    with a rate-limit reason."""
    cfg = InterventionConfig(max_interventions_per_hour=3, cooldown_seconds=0)
    p = TriggerPolicy(config=cfg, state_config=StateConfig(hyper_dwell_seconds=10))
    est = _hyper_estimate(dwell=20.0)
    now = 1000.0
    fired = 0
    for i in range(5):
        dec = p.evaluate(est, current_time=now + i)
        if dec.should_trigger:
            p.record_intervention(timestamp=now + i)
            fired += 1
    assert fired == 3, f"expected 3 triggers; got {fired}"

    # The 4th and 5th attempts must explicitly cite the rate-limit gate.
    dec = p.evaluate(est, current_time=now + 10)
    assert dec.should_trigger is False
    assert "cap" in dec.reason.lower() or "hour" in dec.reason.lower()


def test_hourly_cap_releases_after_window_slides() -> None:
    """Triggers older than 3600 s drop out of the window so the cap
    releases on the trailing edge."""
    cfg = InterventionConfig(max_interventions_per_hour=2, cooldown_seconds=0)
    p = TriggerPolicy(config=cfg, state_config=StateConfig(hyper_dwell_seconds=10))
    est = _hyper_estimate(dwell=20.0)

    # Fire two at t=0 to fill the bucket.
    for t in (0.0, 1.0):
        dec = p.evaluate(est, current_time=t)
        assert dec.should_trigger
        p.record_intervention(timestamp=t)

    # At t=3500 (still within hour) the bucket is full.
    dec = p.evaluate(est, current_time=3500.0)
    assert dec.should_trigger is False

    # At t=3602 the t=0 entry slides out; the bucket has room again.
    dec = p.evaluate(est, current_time=3602.0)
    assert dec.should_trigger is True


def test_hourly_cap_disabled_when_zero() -> None:
    """Setting ``max_interventions_per_hour=0`` disables the gate.
    Useful for tests and for deployments that prefer cooldown-only
    rate limiting."""
    cfg = InterventionConfig(max_interventions_per_hour=0, cooldown_seconds=0)
    p = TriggerPolicy(config=cfg, state_config=StateConfig(hyper_dwell_seconds=10))
    est = _hyper_estimate(dwell=20.0)
    fired = 0
    for i in range(50):
        dec = p.evaluate(est, current_time=float(i))
        if dec.should_trigger:
            p.record_intervention(timestamp=float(i))
            fired += 1
    assert fired == 50  # only cooldown=0 + cap-disabled lets all through


# ---------------------------------------------------------------------------
# Oscillation-aware dwell
# ---------------------------------------------------------------------------


def test_oscillation_lengthens_required_dwell() -> None:
    """After enough False→True HYPER flips, the dwell requirement
    multiplies so an estimate that would have passed the base dwell
    is rejected until the longer dwell accrues."""
    cfg = InterventionConfig(
        max_interventions_per_hour=0,  # cap off — isolate dwell gate
        cooldown_seconds=0,
        oscillation_max_flips=3,
        oscillation_window_seconds=600.0,
        oscillation_dwell_multiplier=3.0,
    )
    p = TriggerPolicy(config=cfg, state_config=StateConfig(hyper_dwell_seconds=20))

    # Flicker: HYPER → FLOW → HYPER → FLOW → ... five flip events.
    # Each call to evaluate() either with FLOW or with HYPER records
    # the transition via ``_record_hyper_transition``.
    hyper = _hyper_estimate(dwell=25.0)  # base dwell satisfied
    flow = _flow_estimate()
    t = 0.0
    for _ in range(5):
        p.evaluate(flow, current_time=t)
        t += 5.0
        p.evaluate(hyper, current_time=t)
        t += 5.0
    # 5 transitions FLOW→HYPER recorded; > oscillation_max_flips=3.

    # The next HYPER estimate with dwell=25 (< 20 * 3 = 60) MUST be
    # rejected by the lengthened-dwell gate.
    dec = p.evaluate(hyper, current_time=t)
    assert dec.should_trigger is False
    assert "dwell" in dec.reason.lower()


def test_oscillation_does_not_block_genuine_sustained_overwhelm() -> None:
    """A user with prolonged HYPER dwell still passes through even
    with the oscillation multiplier active. The multiplier is 2× the
    base dwell; this test gives 3× to clear the gate cleanly."""
    cfg = InterventionConfig(
        max_interventions_per_hour=0,
        cooldown_seconds=0,
        oscillation_max_flips=2,
        oscillation_window_seconds=600.0,
        oscillation_dwell_multiplier=2.0,
    )
    p = TriggerPolicy(config=cfg, state_config=StateConfig(hyper_dwell_seconds=20))

    hyper_short = _hyper_estimate(dwell=20.0)
    flow = _flow_estimate()
    t = 0.0
    # Five flips → multiplier active.
    for _ in range(5):
        p.evaluate(flow, current_time=t); t += 1.0
        p.evaluate(hyper_short, current_time=t); t += 1.0

    # Now the user actually sustains HYPER for 90 s (> 20 * 2 = 40).
    sustained = _hyper_estimate(dwell=90.0)
    dec = p.evaluate(sustained, current_time=t + 90.0)
    assert dec.should_trigger is True, (
        f"sustained overwhelm should still trigger; got {dec.reason}"
    )


def test_oscillation_flips_outside_window_are_pruned() -> None:
    """Flips older than ``oscillation_window_seconds`` drop out of
    the count. A user who oscillated yesterday but is steady today
    still receives interventions on today's normal dwell."""
    cfg = InterventionConfig(
        max_interventions_per_hour=0,
        cooldown_seconds=0,
        oscillation_max_flips=2,
        oscillation_window_seconds=60.0,  # short window for the test
        oscillation_dwell_multiplier=3.0,
    )
    p = TriggerPolicy(config=cfg, state_config=StateConfig(hyper_dwell_seconds=10))

    hyper = _hyper_estimate(dwell=15.0)
    flow = _flow_estimate()

    # Flip five times around t=0 — way over the cap of 2.
    t = 0.0
    for _ in range(5):
        p.evaluate(flow, current_time=t); t += 0.5
        p.evaluate(hyper, current_time=t); t += 0.5

    # Wait for the window to pass; all stale flips prune on the
    # next ``_is_oscillating`` call.
    dec = p.evaluate(hyper, current_time=1000.0)
    assert dec.should_trigger is True, (
        f"after window prune, base dwell should suffice; got {dec.reason}"
    )


# ---------------------------------------------------------------------------
# Integration: oscillating sequence with all defaults
# ---------------------------------------------------------------------------


def test_oscillation_pattern_bounded_at_or_below_hourly_cap() -> None:
    """The Ledger's canonical adversarial trace: state oscillates at the
    HYPER/FLOW boundary with a 90 s cycle (30 s HYPER, 60 s FLOW). With
    pre-F25 defaults this fired on every cycle (~40 triggers/hour); the
    F25 hourly cap clamps that at the configured value (default 6/hr).
    """
    cfg = InterventionConfig()  # all defaults — cap=6, cooldown=60
    p = TriggerPolicy(config=cfg, state_config=StateConfig(hyper_dwell_seconds=30))
    hyper = _hyper_estimate(dwell=35.0)
    flow = _flow_estimate()

    triggered = 0
    t = 0.0
    # Simulate 4 hours of 90 s oscillation = ~160 cycles.
    for cycle in range(160):
        # 30 s HYPER followed by 60 s FLOW; sample evaluation at the
        # cycle midpoint and at the FLOW return.
        dec = p.evaluate(hyper, current_time=t)
        if dec.should_trigger:
            p.record_intervention(timestamp=t)
            triggered += 1
        t += 30.0
        p.evaluate(flow, current_time=t)
        t += 60.0

    # Cap is 6/hr over the rolling window. Four hours of capped firing
    # = 24 ceiling; the count must be <= 24, comfortably below the
    # pre-F25 ~160. Lower-bound > 0 confirms the cap doesn't deadlock.
    assert 0 < triggered <= 24, f"expected 1..24 triggers, got {triggered}"
