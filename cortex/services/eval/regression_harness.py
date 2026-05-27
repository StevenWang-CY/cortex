"""Audit F41 — regression harness for the trigger-policy + LLM-eval cohort.

Replays a small library of synthetic state traces against the
``TriggerPolicy`` and the ``ContextualBandit`` policies, then computes
the metrics CI tracks against a versioned ``baseline.json``:

* ``oscillation_intervention_rate_per_hr`` — interventions/hour produced
  when fed the canonical jittery state trace (Ledger F25 adversarial
  pattern). Pre-F25 default settings produced ~160/hr; the F25 cap
  should clamp this to ≤ ``max_interventions_per_hour`` (default 6).
* ``sustained_overwhelm_pass_rate`` — fraction of "genuinely overwhelmed"
  traces that produce at least one intervention. Must stay near 1.0
  even with F25 hysteresis active.
* ``flow_negative_trigger_rate`` — fraction of pure-FLOW traces that
  produced a trigger (should be 0).
* ``bandit_regret_p95`` — 95th-percentile regret of the contextual
  bandit replay over a 200-arm-pull synthetic stream. Bounded by the
  Hoeffding inequality at the bandit's exploration rate.

Each metric has a "direction" (lower-is-better / higher-is-better) and
a tolerance band; the CLI fails CI when any metric crosses its
tolerance against the committed baseline. ``--update-baseline``
re-records the metrics after a deliberate change.

Determinism: all RNG paths are seeded via ``random.Random(seed)``;
the harness fixes ``seed=20260519`` by default so reruns produce
byte-identical metrics. Override with ``--seed`` for ad-hoc work.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from cortex.libs.config.settings import InterventionConfig, StateConfig
from cortex.libs.schemas.state import SignalQuality, StateEstimate, StateScores
from cortex.services.eval.bandit import ContextualBandit
from cortex.services.state_engine.trigger_policy import TriggerPolicy

logger = logging.getLogger(__name__)

DEFAULT_SEED = 20260519
"""Fixed RNG seed so eval runs reproduce byte-identical metrics. The
date-stamp is the audit close-out day; bump on intentional baseline
refreshes via ``--seed``."""

# Tolerance bands per metric. ``direction`` is how a regression looks:
#   "lower"  — measured value going DOWN is a regression
#              (e.g. pass-rate dropping)
#   "higher" — measured value going UP is a regression
#              (e.g. intervention spam)
# ``rel_tol`` is the fractional band tolerated (3 % is the audit-plan
# Phase-F default); ``abs_tol`` is the absolute band for metrics whose
# baseline can be near zero (where a relative band degenerates).
_METRIC_DIRECTIONS: dict[str, tuple[str, float, float]] = {
    "oscillation_intervention_rate_per_hr": ("higher", 0.03, 0.5),
    "sustained_overwhelm_pass_rate":        ("lower",  0.03, 0.05),
    "flow_negative_trigger_rate":           ("higher", 0.03, 0.05),
    "bandit_regret_p95":                    ("higher", 0.03, 0.05),
}


def default_baseline_path() -> Path:
    """Where the committed regression baseline lives."""
    return Path(__file__).resolve().parent / "baseline.json"


# ---------------------------------------------------------------------------
# Synthetic state-estimate generators
# ---------------------------------------------------------------------------


def _hyper(confidence: float = 0.9, dwell: float = 60.0) -> StateEstimate:
    return StateEstimate(
        state="HYPER",
        confidence=confidence,
        scores=StateScores(flow=0.05, hypo=0.05, hyper=0.85, recovery=0.05),
        reasons=["synthetic"],
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=dwell,
    )


def _flow(dwell: float = 10.0) -> StateEstimate:
    return StateEstimate(
        state="FLOW",
        confidence=0.85,
        scores=StateScores(flow=0.85, hypo=0.05, hyper=0.05, recovery=0.05),
        reasons=["synthetic"],
        signal_quality=SignalQuality(physio=0.9, kinematics=0.9, telemetry=0.9),
        timestamp=0.0,
        dwell_seconds=dwell,
    )


def _oscillation_trace(hours: float = 4.0) -> list[tuple[StateEstimate, float]]:
    """Adversarial F25 trace: 90 s cycle of 30 s HYPER, 60 s FLOW.

    Sampled at the cycle endpoints. Returns (estimate, monotonic_time)
    pairs so the harness can call ``evaluate(... current_time=t)``
    without touching the real clock.
    """
    out: list[tuple[StateEstimate, float]] = []
    t = 1.0
    cycle_seconds = 90.0
    n_cycles = int((hours * 3600.0) / cycle_seconds)
    for _ in range(n_cycles):
        # 30 s of HYPER sampled at t + 30
        t += 30.0
        out.append((_hyper(dwell=35.0), t))
        # 60 s of FLOW sampled at the end
        t += 60.0
        out.append((_flow(dwell=55.0), t))
    return out


def _sustained_overwhelm_trace(seconds: float = 600.0) -> list[tuple[StateEstimate, float]]:
    """Genuine sustained HYPER: 10 minutes of stable HYPER with growing
    dwell. The harness must trigger at least once on this trace even
    with F25 hysteresis active."""
    out: list[tuple[StateEstimate, float]] = []
    t = 1.0
    step = 5.0
    dwell = 0.0
    while t < seconds:
        dwell += step
        out.append((_hyper(dwell=dwell), t))
        t += step
    return out


def _pure_flow_trace(seconds: float = 1800.0) -> list[tuple[StateEstimate, float]]:
    """30 minutes of pure FLOW. Triggers from this trace count as
    false positives; we expect zero."""
    out: list[tuple[StateEstimate, float]] = []
    t = 1.0
    step = 10.0
    while t < seconds:
        out.append((_flow(dwell=t), t))
        t += step
    return out


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------


def _count_triggers(policy: TriggerPolicy, trace: list[tuple[StateEstimate, float]]) -> int:
    fires = 0
    for est, t in trace:
        dec = policy.evaluate(est, current_time=t)
        if dec.should_trigger:
            policy.record_intervention(timestamp=t)
            fires += 1
    return fires


def _make_policy() -> TriggerPolicy:
    """Build a TriggerPolicy with default config (the production
    settings). Tests that want to vary thresholds construct their own."""
    # Disable adaptive bumps so the harness measures the rate-limit /
    # hysteresis gates in isolation. Tests of dismissal-driven adaptive
    # behaviour live in dedicated test files.
    cfg = InterventionConfig(adaptive_threshold_enabled=False)
    return TriggerPolicy(config=cfg, state_config=StateConfig())


def compute_oscillation_rate_per_hr(hours: float = 4.0) -> float:
    policy = _make_policy()
    trace = _oscillation_trace(hours=hours)
    fires = _count_triggers(policy, trace)
    return fires / hours


def compute_sustained_overwhelm_pass_rate(n_traces: int = 10) -> float:
    """Run ``n_traces`` independent sustained-overwhelm traces, each
    starting from a fresh policy (per-user simulation). Pass-rate is
    the fraction that produce ≥1 trigger."""
    passes = 0
    for i in range(n_traces):
        policy = _make_policy()
        # Slight jitter on duration so policies don't converge on the
        # same cooldown / dwell phase by accident.
        trace = _sustained_overwhelm_trace(seconds=600.0 + i * 30.0)
        fires = _count_triggers(policy, trace)
        if fires >= 1:
            passes += 1
    return passes / n_traces


def compute_flow_negative_trigger_rate(n_traces: int = 10) -> float:
    """False-positive rate on pure-FLOW traces."""
    fp = 0
    for i in range(n_traces):
        policy = _make_policy()
        trace = _pure_flow_trace(seconds=1800.0 + i * 60.0)
        fires = _count_triggers(policy, trace)
        if fires >= 1:
            fp += 1
    return fp / n_traces


def compute_bandit_regret_p95(n_steps: int = 200, *, seed: int = DEFAULT_SEED) -> float:
    """95th-percentile regret of the contextual bandit over a
    synthetic stream. The "true" optimal arm is fixed (arm 0); the
    bandit's regret is 1 if it picks anything else."""
    from cortex.services.eval.bandit import N_FEATURES

    rng = random.Random(seed)
    bandit = ContextualBandit(store=None)
    regrets: list[float] = []
    for _ in range(n_steps):
        features = np.array(
            [rng.random() for _ in range(N_FEATURES)], dtype=np.float64,
        )
        arm = bandit.select_arm(features)
        # Reward: 1.0 for arm 0, 0.0 otherwise (so optimal-arm fraction
        # is the inverse of regret).
        reward = 1.0 if arm == 0 else 0.0
        bandit.update(context=features, arm_idx=arm, reward=reward)
        regrets.append(1.0 - reward)
    arr = np.array(regrets)
    return float(np.percentile(arr, 95))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalMetrics:
    """One run of the harness."""

    oscillation_intervention_rate_per_hr: float
    sustained_overwhelm_pass_rate: float
    flow_negative_trigger_rate: float
    bandit_regret_p95: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class BaselineFile:
    """The committed baseline.json shape."""

    version: int
    seed: int
    metrics: dict[str, float]


def run_harness(*, seed: int = DEFAULT_SEED) -> EvalMetrics:
    """Run every metric once with deterministic seeding."""
    return EvalMetrics(
        oscillation_intervention_rate_per_hr=compute_oscillation_rate_per_hr(),
        sustained_overwhelm_pass_rate=compute_sustained_overwhelm_pass_rate(),
        flow_negative_trigger_rate=compute_flow_negative_trigger_rate(),
        bandit_regret_p95=compute_bandit_regret_p95(seed=seed),
    )


def load_baseline(path: Path | None = None) -> BaselineFile:
    target = path or default_baseline_path()
    raw = json.loads(target.read_text(encoding="utf-8"))
    return BaselineFile(
        version=int(raw["version"]),
        seed=int(raw["seed"]),
        metrics={k: float(v) for k, v in raw["metrics"].items()},
    )


def save_baseline(metrics: EvalMetrics, *, path: Path | None = None, seed: int = DEFAULT_SEED) -> Path:
    """Write a fresh baseline file. Used by ``--update-baseline`` when
    a deliberate threshold change has been signed off."""
    from cortex.libs.utils.atomic_write import atomic_write_json

    target = path or default_baseline_path()
    payload = {
        "version": 1,
        "seed": seed,
        "metrics": metrics.to_dict(),
    }
    atomic_write_json(target, payload)
    return target


@dataclass(frozen=True)
class MetricDelta:
    name: str
    measured: float
    baseline: float
    direction: str
    regressed: bool
    rel_tol: float
    abs_tol: float


def compare_to_baseline(metrics: EvalMetrics, baseline: BaselineFile) -> list[MetricDelta]:
    """Return a per-metric report. ``regressed=True`` iff the measured
    value crosses its tolerance band in the direction marked as a
    regression."""
    out: list[MetricDelta] = []
    measured = metrics.to_dict()
    for name, (direction, rel_tol, abs_tol) in _METRIC_DIRECTIONS.items():
        base = baseline.metrics.get(name)
        if base is None:
            # New metric — record as non-regressing; CI prompts the
            # operator to update the baseline.
            out.append(MetricDelta(
                name=name, measured=measured[name], baseline=float("nan"),
                direction=direction, regressed=False,
                rel_tol=rel_tol, abs_tol=abs_tol,
            ))
            continue
        m = measured[name]
        delta = m - base
        # Choose the bigger of rel and abs tolerance to handle the
        # "baseline near zero" degenerate case.
        tol_band = max(abs(base) * rel_tol, abs_tol)
        if direction == "higher":
            regressed = delta > tol_band
        else:  # lower
            regressed = -delta > tol_band
        out.append(MetricDelta(
            name=name, measured=m, baseline=base,
            direction=direction, regressed=regressed,
            rel_tol=rel_tol, abs_tol=abs_tol,
        ))
    return out


def format_report(deltas: list[MetricDelta]) -> str:
    """Human-readable, also legible in CI logs."""
    lines = [
        "Cortex eval regression report",
        "-" * 60,
        f"{'metric':40s} {'measured':>10s} {'baseline':>10s} {'verdict':>8s}",
    ]
    for d in deltas:
        verdict = "REGRESS" if d.regressed else "ok"
        base_s = f"{d.baseline:.4f}" if d.baseline == d.baseline else "  N/A"
        lines.append(
            f"{d.name:40s} {d.measured:10.4f} {base_s:>10s} {verdict:>8s}"
        )
    return "\n".join(lines)


def any_regression(deltas: list[MetricDelta]) -> bool:
    return any(d.regressed for d in deltas)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Cortex regression harness (audit F41)."
    )
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="Re-record baseline.json after a deliberate, reviewed change."
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED,
        help="RNG seed (default: %(default)s — change only for ad-hoc work)."
    )
    parser.add_argument(
        "--baseline", type=Path, default=None,
        help="Alternative baseline path (default: services/eval/baseline.json)."
    )
    args = parser.parse_args(argv)

    metrics = run_harness(seed=args.seed)
    if args.update_baseline:
        path = save_baseline(metrics, path=args.baseline, seed=args.seed)
        print(f"Wrote new baseline to {path}")
        print(format_report(compare_to_baseline(metrics, load_baseline(args.baseline))))
        return 0

    baseline = load_baseline(args.baseline)
    if baseline.seed != args.seed:
        logger.warning(
            "Baseline seed %d differs from run seed %d; deltas may be noise "
            "rather than signal.",
            baseline.seed,
            args.seed,
        )
    deltas = compare_to_baseline(metrics, baseline)
    print(format_report(deltas))
    return 1 if any_regression(deltas) else 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(_cli(sys.argv[1:]))
