# Model card: deterministic support rules v2.3.0

## Status and ownership

| Field | Value |
| --- | --- |
| Registry name | `deterministic-support` |
| Version | `2.3.0` |
| Feature schema | `support-features-v2.1.0` |
| Implementation | `cortex/services/state_engine/rule_scorer.py` |
| Operational wrapper | `cortex/services/state_engine/support_inference.py` |
| Validation status | deterministic rules; no learned-model validation claim |
| Production eligible | yes, subject to abstention and intervention gates below |
| Safe rollback | `no-inference@safety-null-v1` |

The runtime hashes the scorer implementation and feature catalog into every
estimate. The digest is provenance, not proof of validity.

## Intended use

This rule set summarizes recent, locally observed input patterns into one of
four decision-support hypotheses:

- `support_likely`: the input pattern contains several disruption indicators;
- `flow_like`: affirmative activity is recent and interaction is comparatively
  steady;
- `under_engaged`: sustained inactivity is corroborated by another quiet input
  channel;
- `recovering`: a temporal transition after a confirmed `support_likely`
  episode, owned by the smoother rather than a single feature frame.

`unknown` is the required output while evidence is missing, warming, weak, or
ambiguous. The uppercase `HYPER`, `FLOW`, `HYPO`, and `RECOVERY` values are
one-cycle compatibility aliases only.

The output may support a reversible, suggest-only workspace proposal. It must
not be used to diagnose stress, overwhelm, fatigue, attention, arousal,
disengagement, productivity, health, or a neurological or psychological state.
It must not be used for employment, education, insurance, access, discipline,
ranking, or surveillance decisions.

## Training data

None. This is a hand-specified deterministic rule set. It has no fitted
coefficients, training loss, empirical probability calibration, accuracy
estimate, or population-generalization claim. The values are engineering
priors that require the study in
[`support-inference-study-protocol.md`](../research/support-inference-study-protocol.md)
before they can be described as a validated predictor.

## Inputs and exclusions

Every input is a frozen `FeatureValue` containing value, validity, quality,
age, source-window duration, algorithm version, and an explicit missing reason.
Model order is owned only by `feature_schema.py`; dimension mismatch is an
error, never implicit truncation or padding.

Production behavior inputs:

| Feature | Unit | Source window | Support use |
| --- | --- | ---: | --- |
| mouse velocity mean | px/s | 15 s | flow-like, under-engaged |
| mouse velocity variance | px²/s² | 15 s | support-likely, flow-like |
| click frequency | clicks/s | 15 s | all three stateless hypotheses |
| keypress rate | keypresses/min | 15 s | flow-like, under-engaged |
| keystroke interval variance | ms² | 15 s | support-likely, flow-like |
| correction rate | corrections/100 keys | 15 s | support-likely, flow-like |
| inactivity | seconds | since the last input event, bounded by observed exposure | under-engaged and flow activity gate |
| tab-switch rate | switches/min | 15 s | all three stateless hypotheses |
| re-read scroll bursts | bursts/min | 15 s | support-likely, flow-like |
| focus-transition thrashing | ratio | 60 s | support-likely, flow-like |

The aggregator publishes observation counts. Mouse statistics require at least
two mouse observations; typing-interval variance requires at least two typing
keypresses; correction rate requires at least one typing key. A connected
stream containing zero events is an observed zero for rates, not a fabricated
measurement.

Inactivity is the elapsed time since the most recent input event of any kind,
taken from the input hooks' last-input marker rather than from the 15 s
sliding event window (which by construction can never witness more than 15 s
of silence). It is bounded only by actual observation exposure: on collector
startup, no-event inactivity begins at zero and grows with exposure rather
than machine uptime. This is what makes the 30 s under-engaged floor, the
flow-like "recent activity" gate (inactivity ≤ 30 s), and the zombie-reading
detector reachable.

Per-minute rates (tab switches, re-read scroll bursts) are computed over the
same 15 s window as every other input feature and are stamped with that
window; the focus-transition graph keeps its own 60 s window. Every focus
event inside the window is a real transition (the tracker de-duplicates
same-window refreshes), so a window holding *n* events reports *n* switches.

Diagnostic-only inputs are explicitly excluded from every production rule:

- webcam pulse, because it has not passed the reference-sensor product gate;
- blink rate, because no participant-held-out evidence connects this proxy to
  the decision-support target;
- camera-relative head/neck flexion, because it is a comfort/pose proxy rather
  than evidence of cognitive state.

Removing the camera, losing a face, or changing any excluded camera value
cannot change a production support score.

## Rule definition

For state hypothesis `s`, feature `i`, fixed weight `w[s,i]`, observed quality
`q[i]`, and bounded transform `T[s,i](x)`:

```text
score[s]    = Σ_i w[s,i] · q[i] · T[s,i](x[i])
coverage[s] = Σ_i w[s,i] · q[i] · I(feature i is valid)
```

Missing inputs contribute zero score and zero coverage. Scores are never
renormalized over the surviving inputs. Therefore removing a feature or
lowering its quality cannot increase either the corresponding score or its
coverage. Scores are bounded heuristic support strengths, not probabilities;
they need not sum to one.

Weights:

| Feature | support-likely | flow-like | under-engaged |
| --- | ---: | ---: | ---: |
| mouse velocity mean | — | 0.07 | 0.15 |
| mouse velocity variance | 0.18 | 0.13 | — |
| click frequency | 0.06 | 0.05 | 0.10 |
| keypress rate | — | 0.15 | 0.15 |
| keystroke interval variance | 0.08 | 0.09 | — |
| correction rate | 0.16 | 0.13 | — |
| inactivity | — | activity gate only | 0.45 |
| tab-switch rate | 0.18 | 0.14 | 0.15 |
| re-read scroll bursts | 0.12 | 0.09 | — |
| focus-transition thrashing | 0.22 | 0.15 | — |
| Sum | 1.00 | 1.00 | 1.00 |

Each transform and breakpoint is source-controlled in `rule_scorer.py`.
Important compound gates are:

- a state-specific score is eligible only with at least 0.45 weighted quality
  coverage;
- support-likely and flow-like require at least three observed inputs;
- under-engaged requires at least two inputs, inactivity above the 30-second
  transform floor (40 s of observed silence yields a positive under-engaged
  score), and a corroborating channel;
- flow-like additionally requires inactivity no greater than 30 seconds and
  affirmative mouse, click, or typing activity; stable zero streams cannot
  imply flow-like activity;
- the dominant eligible score must be at least 0.25;
- five telemetry snapshots are required before leaving `warming_up`.

## Temporal model and action boundary

`ScoreSmoother` starts at `UNKNOWN`, uses an EMA, Schmitt-style entry/exit
thresholds, monotonic elapsed time, and state-specific dwell. Regressed replay
timestamps are clamped and cannot create negative dwell. `recovering` can only
appear after a confirmed support-likely episode begins to subside; it is never
scored from a mixed single frame. A committed label whose own smoothed score
stays at or below the exit threshold for 10 s without any other state clearing
its entry threshold falls back to `UNKNOWN` (exit dwell) instead of persisting
indefinitely.

The intervention policy measures dwell as the time the support-likely
confidence has been *above the trigger gate*, bounded by the label's own
dwell; a confidence spike that has only just crossed the gate cannot borrow
dwell from a long sub-threshold label.

An insufficient or warming evaluation forces `UNKNOWN` and clears candidates.
The UI receives explicit `status`, `evidence_coverage`, contributions,
exclusions, and model identity. Intervention policy separately requires an
estimated support-likely state, minimum evidence coverage, dwell, receptivity,
cooldown, consent, and suggest-only authorization. A score alone is never an
execution permission.

Break reminders are outside this model. They are opt-in and based only on the
user's preferred elapsed active-work interval. Pulse, HRV, camera features,
state labels, and the research stress integral are not inputs.

## Change log

**2.3.0** — the `mouse_velocity_variance` abstention threshold is a magnitude,
not `> 0.0`. Both scoring paths floor the divisor, so *any* baseline below
roughly 3 000 px^2/s^2 saturates exactly as a zero one does: the feature is
pinned to maximum support evidence and zero flow evidence permanently. A small
non-zero baseline is an ordinary calibration outcome rather than a corrupt one
— windows with fewer than two mouse moves contribute a variance of 0.0, and the
baseline is their plain mean, so a keyboard-heavy calibration averages down
into that band and passed the previous guard. The threshold is now
`_MIN_MOUSE_VARIANCE_BASELINE` (1 000 px^2/s^2, an order of magnitude below the
10 000 working default in `causal_attribution`, so genuine calibrations still
pass), and both divisor floors use the same constant so a caller that skips the
abstention check cannot obtain the saturating substitution either.

Also in this release, a `RECOVERING` label now publishes the evidence coverage
its own score was built from (the flow / under-engaged components) instead of
the scorer's placeholder `0.0`. The scorer has no `RECOVERING` hypothesis to
measure — recovery is a temporal relation only the smoother can see — and
publishing the placeholder made `TriggerPolicy.evaluate` reject every recovery
estimate at its 0.45 coverage floor, which runs before the per-state arm
dispatch. The opt-in recovery reinforcement arm was therefore unreachable.

**2.2.0** — `mouse_velocity_variance` abstains when no personal
`mouse_variance_baseline` has been measured. Calibration may legitimately
persist a zero baseline; the previous release floored it to `1.0` so the state
loop could not raise, but mouse velocity variance is measured in thousands, so
every window scored as maximum thrash evidence and minimum flow evidence. The
verdict was permanent, invisible, and derived from a baseline that was never
taken. The feature now contributes neither evidence nor score until a baseline
exists, which is what the abstention contract already promised for any
unavailable feature.

This card previously read `2.1.0` while the registry shipped `2.1.1`; the two
are reconciled here.

## Known limitations and failure modes

- Rules are not empirically optimized and their breakpoints may not transfer
  across devices, accessibility settings, work styles, motor differences, or
  input modalities.
- Keyboard-only, mouse-only, assistive-technology, voice, touch, and remote
  desktop workflows can have different coverage and abstention rates.
- Aggregates may reflect task demands, debugging, editing style, or application
  mechanics rather than a need for support.
- A true need for support can occur with quiet behavior; the system may abstain
  or miss it. Abstention is preferable to fabricating certainty.
- Local baselines may be stale or unrepresentative. Calibration does not turn a
  heuristic score into a probability.
- The current fixed weights do not establish benefit. Only a randomized study
  can estimate whether a proposal helps at a decision point.

## Verification and monitoring

Required pre-merge tests include feature-catalog completeness, exact dimension,
missing-value coherence, all channel-presence combinations, camera invariance,
zero-stream rejection, evidence monotonicity, deterministic replay, dwell and
recovery semantics, probability-artifact rejection, and safe-null rollback.

Operational telemetry must record only aggregate health and model identity:
status/abstention rate, coverage distribution, feature missingness by reason,
rule version, intervention proposal/authorization/outcome, and rollback events.
It must not record raw frames or keystrokes. Monitoring must be stratified by
supported device/input configurations where consent and sample size permit.

## Implementation notes (2026-09)

- Inactivity provenance fixed (last-input marker, exposure-bounded; audit D1).
  The feature's documented meaning ("time since last input event") is
  unchanged, so no feature-schema or model version increment was taken; the
  under-engaged rule, previously unreachable, is now live.
- `mouse_variance_baseline` is floored at 1.0 in every transform (audit D2).
- Tab-switch and scroll-back rates are stamped with their true 15 s window
  and switches are no longer under-counted by one per window (audit D13).
- The legacy physiology/posture composites were deleted from the scorer
  (audit D17); only the behaviour transforms above remain.

## Change and rollback policy

Any change to feature names/order, units, readiness, windows, transforms,
weights, coverage, thresholds, or dwell requires:

1. a feature-schema or model version increment;
2. regenerated TypeScript schemas and updated clients;
3. this model card and the implementation ledger updated in the same change;
4. replay, metamorphic, missingness, and UI unknown-state tests;
5. a migration/rollback note and the retained `safety_null` entry.

Set `state.inference_mode: safety_null` to disable inference on restart. The
safe-null model emits only insufficient-evidence `UNKNOWN`; there is no hidden
learned-classifier fallback.
