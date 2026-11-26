# Model card: deterministic support rules v3.0.0

## Status and ownership

| Field | Value |
| --- | --- |
| Registry name | `deterministic-support` |
| Version | `3.0.0` |
| Feature schema | `support-features-v3.0.0` |
| Implementation | `cortex/services/state_engine/rule_scorer.py`, `physiology_evidence.py`, `physiology_baseline.py` |
| Operational wrapper | `cortex/services/state_engine/support_inference.py` |
| Validation status | deterministic rules; camera heart rate and SDNN checked against ECG on one participant ([qualification](../research/physiology-qualification.md)); no validation of the support hypotheses |
| Production eligible | yes, subject to abstention and intervention gates below |
| Safe rollback | `no-inference@safety-null-v1`; `state.physiology_weight: 0` restores behaviour-only scoring |

Each estimate carries a build-generated digest of the feature schema, input
acquisition and aggregation, focus graph, fusion, scorer, physiology evidence
and reference, the camera physiology estimators, temporal smoother and
registry. The digest identifies the reviewed implementation, not its validity;
it does not claim that these heuristic rules have been empirically validated.
The twenty-one-component manifest excludes intervention helpers, runtime
orchestration, configuration values and personal calibration; the digest alone
cannot reproduce a complete proposal decision.

This card supersedes [v2.6.0](deterministic-support-v2.md), whose rules used
behaviour only. [ADR 0008](../adr/0008-qualified-camera-physiology.md) records
the decision to admit camera physiology as corroborating evidence.

## Intended use

This rule set summarizes recent, locally observed input patterns, optionally
corroborated by camera physiology, into one of four decision-support
hypotheses:

- `support_likely`: the input pattern contains several disruption indicators;
- `flow_like`: affirmative activity is recent and interaction is comparatively
  steady;
- `under_engaged`: sustained inactivity is corroborated by another quiet input
  channel;
- `recovering`: a temporal transition after a confirmed `support_likely`
  episode, owned by the smoother rather than a single feature frame.

`unknown` is the required output while evidence is missing, warming, weak, or
ambiguous. The uppercase `HYPER`, `FLOW`, `HYPO`, and `RECOVERY` values are
legacy wire compatibility aliases for these hypotheses.

The output may support a reversible, suggest-only workspace proposal. It must
not be used to diagnose stress, overwhelm, fatigue, attention, arousal,
disengagement, productivity, health, or a neurological or psychological state.
It must not be used for employment, education, insurance, access, discipline,
ranking, or surveillance decisions. The camera values shown beside it are
wellness estimates, not medical measurements.

## Training data

None. This is a hand-specified deterministic rule set. It has no fitted
coefficients, training loss, empirical probability calibration, accuracy
estimate, or population-generalization claim. The values are engineering
priors that require the study in
[`support-inference-study-protocol.md`](../research/support-inference-study-protocol.md)
before they can be described as a validated predictor. The constants of the
pulse publication gate were chosen while examining the qualification dataset;
there is no held-out set.

## Inputs

Every input is a frozen `FeatureValue` containing value, validity, quality,
age, source-window duration, algorithm version, and an explicit missing reason.
Model order is owned only by `feature_schema.py`; dimension mismatch is an
error, never implicit truncation or padding.

### Behaviour inputs

| Feature | Unit | Default source window | Support use |
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

The aggregator publishes per-device availability and observation counts. Mouse
features require a live mouse listener; keyboard features require a live,
trusted keyboard listener and a current, non-prompting permission check.
Neither absent listeners nor unavailable legacy provenance become observed zeros.
Partial startup failure releases the listener pair, and a stopped listener
invalidates its own features while leaving another observed channel available.

Mouse statistics require at least two mouse observations. Typing-interval
variance requires at least two positive intervals between regular keypresses
(at least three regular presses); backspaces do not supply interval evidence.
Correction rate requires at least one typing key. A connected stream containing
zero events remains a valid zero for rates. Legitimately equal measured
intervals may have zero variance.

Inactivity is the elapsed time since the most recent input event of any kind,
taken from the input hooks' last-input marker rather than from the 15 s
sliding event window (which by construction can never witness more than 15 s
of silence). It is bounded only by actual observation exposure: on collector
startup, no-event inactivity begins at zero and grows with exposure rather
than machine uptime. Both input channels must be observed to assert inactivity.
Losing either source or a sampling gap longer than 3 s restarts exposure. The
30 s under-engaged floor and flow-like recent-activity gate (inactivity ≤ 30 s)
therefore operate only on an observed interval.

Per-minute rates (tab switches, re-read scroll bursts) use the configured input
aggregation window, 15 s by default, and are stamped with its actual duration;
the focus-transition graph keeps its own 60 s window. Every focus
event inside the window is a real transition (the tracker de-duplicates
same-window refreshes), so a window holding *n* events reports *n* switches.

### Camera physiology inputs

| Feature | Unit | Source window | Reference check | Support use |
| --- | --- | ---: | --- | --- |
| heart rate | BPM | 20 s (10 s while filling) | ECG: MAE 1.8 BPM on published windows | all three |
| heart-rate variability, SDNN | ms | trailing 3 min | ECG: MAE 8.2 ms on published windows | support-likely, flow-like |
| breathing rate | breaths/min | 45 s | simulated truth only | all three |
| blink rate | blinks/min | 60 s | none | all three |
| eye-closure share (PERCLOS) | fraction | 60 s | none | under-engaged |

Each is published only when its own gate passes: a confirmed cardiac line for
heart rate; at least 80 accepted beat intervals, the newest no more than 10 s
old, for SDNN; head movement agreeing with a pulse-derived channel for
breathing. Blink rate and eye-closure share have no reference check; they are
read only while heart rate or SDNN is usable in the same evaluation. The
figures, the conditions under which nothing is published and the limits of the
evidence — in particular that every reference comparison comes from one
participant and that the estimator abstained on the two participants with
intermediate and dark skin tone — are in the
[qualification report](../research/physiology-qualification.md).

Camera inputs are optional. Removing the camera, losing the face, or lacking a
personal reference leaves every score exactly equal to the behaviour-only
score.

Diagnostic-only camera values, excluded from every rule:

- RMSSD and the other beat-to-beat variability measures (pNN50, SD1, SD2,
  LF/HF, sample entropy), which did not meet the reference criterion;
- camera-relative head/neck flexion, a comfort and pose proxy rather than
  evidence of cognitive state.

## Rule definition

### Behaviour block

For state hypothesis `s`, feature `i`, fixed weight `w[s,i]`, observed quality
`q[i]`, and bounded transform `T[s,i](x)`:

```text
B[s]  = Σ_i w[s,i] · q[i] · T[s,i](x[i])
Cb[s] = Σ_i w[s,i] · q[i] · I(feature i is valid)
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

### Personal reference

Camera physiology is interpreted only against the same person measured through
the same camera. `PhysiologyBaselineTracker` keeps a centre and spread per
metric. A reference becomes ready when the session has supplied sixty valid
values over at least two minutes (median and scaled median absolute
deviation), or when a calibration profile taken through the camera in use
holds a measured level for the metric. In practice calibration seeds blink
rate only: it records heart rate and breathing at a maturity the profile does
not yet promote, and never records SDNN, so those three are learned in each
session. The reference then adapts with a one-hour half-life, with each update
clipped to three spreads, so a long demanding stretch reads as a deviation
instead of becoming the new normal. Spreads have absolute and relative floors
(heart rate 3 BPM or 5 %, SDNN 6 ms or 20 %, breathing 1.5 /min or 12 %, blink
3 /min or 25 %). A different physical camera discards the reference.

Heart rate, SDNN and breathing enter as standard scores `z` against the
reference, and blink rate as the ratio to its reference. Eye-closure share is
the one input read on a fixed scale, as an absolute fraction.

Blink rate and eye-closure share were not checked against any reference
sensor. They enter the block only while heart rate or SDNN is usable in the
same evaluation. When the camera yields no pulse the block is empty, whatever
the eyes are doing.

### Physiology block

```text
P[s]  = Σ_j v[s,j] · q[j] · U[s,j](z[j])
Cp[s] = Σ_j v[s,j] · q[j] · I(feature j is valid and its reference is ready)
```

Weights:

| Feature | support-likely | flow-like | under-engaged |
| --- | ---: | ---: | ---: |
| heart rate | 0.35 | 0.30 | 0.25 |
| SDNN | 0.35 | 0.30 | — |
| breathing rate | 0.15 | 0.15 | 0.15 |
| blink rate | 0.15 | 0.25 | 0.20 |
| eye-closure share | — | — | 0.40 |
| Sum | 1.00 | 1.00 | 1.00 |

Transforms (each a linear ramp clipped to 0–1):

| Feature | support-likely | flow-like | under-engaged |
| --- | --- | --- | --- |
| heart rate | 0 at z = 1, 1 at z = 3 | 1 at \|z\| ≤ 0.75, 0 at \|z\| = 2 | 0 at z = −1, 1 at z = −2.5 |
| SDNN | 0 at z = −1, 1 at z = −2.5 | 1 at z ≥ −0.5, 0 at z = −2 | — |
| breathing rate | 0 at z = 1, 1 at z = 3 | 1 at \|z\| ≤ 0.75, 0 at \|z\| = 2 | 0 at z = −1, 1 at z = −2.5 |
| blink rate (ratio r) | 1 at r ≤ 0.35, 0 at r = 0.75 | 0 at r = 0.20, 1 from r = 0.45 to 1.10, 0 at r = 1.60 | 0 at r = 1.40, 1 at r = 2.20 |
| eye-closure share | — | — | 0 at 0.15, 1 at 0.40 |

The directions follow well-replicated group-level findings: mental load raises
heart rate and breathing rate and lowers heart-rate variability, sustained
visual attention suppresses blinking, drowsiness lengthens eye closure. That
makes them usable as corroboration. It does not make any of them a measurement
of stress in a person.

### Fusion

```text
score[s]    = (1 − L · Cp[s]) · B[s]  + L · P[s]     where rule s is eligible
coverage[s] = (1 − L · Cp[s]) · Cb[s] + L · Cp[s]
```

`L` is `state.physiology_weight`, 0.35 by default and capped at 0.9 of the
smoother's entry threshold. Two bounds apply to every state:

1. Physiology is mixed into a state only where that state's own behaviour rule
   produced a positive eligible score. Where the rule abstained or was gated to
   zero, the state stays at zero and keeps its behaviour coverage.
2. Physiology may lower any eligible score. It may raise one only if the
   behaviour score alone is already at or past the smoother's entry threshold
   (0.40 by default); below that, `score[s] = min(score[s], B[s])`.

Consequences, each covered by a test:

- with no camera, no face, no pulse or no ready reference, `Cp[s] = 0` and
  every score equals the behaviour score;
- physiology moves an eligible score only within
  `[(1 − L) · B[s], (1 − L) · B[s] + L]`;
- a weak or ambiguous behavioural reading cannot be turned into an estimate:
  no state whose behaviour score is below the entry threshold ends above its
  behaviour score, whatever the camera reads;
- between two hypotheses that behaviour has each carried past the entry
  threshold, physiology can decide which is dominant, and it can temper a
  dominant reading until it no longer clears the threshold.

## Temporal model and action boundary

`ScoreSmoother` starts at `UNKNOWN`, uses an EMA, Schmitt-style entry/exit
thresholds, monotonic elapsed time, and state-specific dwell. Duplicate or
regressed observations do not update scores or dwell. An evaluation gap longer
than 3 s (six nominal 500 ms ticks, matching the source freshness boundary)
invalidates the committed label, scores and pending candidate. The next fresh
observation begins a new confirmation interval; unobserved time cannot complete
entry dwell, prolong a label, or establish recovery. A changed `boot_id` starts
a new temporal history because monotonic times from different boots cannot be
compared. Source fusion also ignores nonincreasing timestamps and restarts its
five-snapshot telemetry warm-up after a gap above three seconds.

The EMA preserves the nominal 500 ms response with elapsed-time weight
`alpha(dt) = 1 - (1 - ema_alpha) ** (dt / 0.5)`. This follows by composing the
retention factor from [single exponential smoothing](https://www.itl.nist.gov/div898/handbook/pmc/section4/pmc431.htm)
over the elapsed number of nominal intervals. It is an engineering time-scale
correction, not an empirically fitted cognitive model. The first sample and the
first sample after a gap receive one nominal update; unobserved gaps cannot
earn extra weight. Constant-input traces sampled every 10, 100, 250, 500,
1,000 or 2,000 ms produce equal scores at the same elapsed time. Varying inputs
still depend on the actual observations. Non-finite timestamps are rejected
without altering the prior state; finite numeric observations are required at
the schema boundary. Invalid alpha or negative dwell settings are rejected.

`recovering` can only
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

### Strain load and break suggestions

Two kinds of break suggestion exist. Both pass the same interruption gate
(quiet mode, pause, receptivity, cooldown, hourly cap) as every other
suggestion.

- **Elapsed focus** (opt-in, `intervention.enable_focus_break_reminders`).
  Based only on the user's preferred active-work interval.
- **Strain load** (on by default,
  `intervention.enable_strain_break_suggestions`). `StrainLoadTracker`
  integrates the support-likely
  physiology block, read as `P / Cp` so that a missing metric does not read as
  calm, and only when `Cp ≥ 0.30`:

  ```text
  load(t + dt) = load(t) · 0.5 ^ ((1 − a) · dt / half_life) + a · dt
  ```

  where `a` is the reading above a 0.15 floor, rescaled to 0–1. The default
  threshold is 600 strain-seconds and the recovery half-life 600 s. Gaps longer
  than 30 s earn neither load nor recovery. Crossing the threshold suggests a
  three-minute paced-breathing break once, and only while a camera reading
  from the last 30 s exists: a load built earlier is not acted on after the
  person has left the camera. The offer goes to the desktop app and to the
  client in front (browser or editor), not to every connected client. A
  crossing the interruption gate refuses stays owed and is retried no more
  often than every 30 s; a completed break pays back twice its duration. The
  wording names camera readings and the person's usual level and makes no
  claim about stress. The load is a pacing aid, not a measure of stress,
  fatigue or health.

Reading check-ins read no camera value directly: blink values and blink
baselines cannot change their timing. They do require an `under_engaged`
estimate, and since 3.0.0 that estimate may be corroborated by blink rate and
eye-closure share where its behaviour rule is eligible. The default
is 120 s of observed mouse velocity at or below 30 px/s in a browser while the
support estimate is `under_engaged`. Quiet interaction does not establish
comprehension or attention. Goal check-ins use only the foreground
editor file or active browser title; missing foreground metadata resets the pending check-in
rather than counting as a mismatch. Both remain subject to the shared interruption
and authorization gates.

### Other consumers of camera physiology

- **Session report and multi-day trend.** The session's mean heart rate and
  mean SDNN (`avg_hrv_sdnn`) are recorded. `avg_hrv_rmssd` is kept in the
  schema so older session files load; it is no longer written, and an older
  file's value is not read into the trend.
- **Coding-practice coach (struggle detection).** Both pathways are
  behaviour-led — re-reading with a long dwell on one stage, or repeated wrong
  answers with heavy deletion — and each needs a physiological condition to
  corroborate it: a rising strain load, or SDNN below 80 % of the person's own
  reference level. With no camera reading or no ready reference neither
  condition can hold and the detector is silent. Its actions change the page
  and therefore run only in an authorized execution mode.
- **Contained.** Two coach modes would be established by physiology rather
  than corroborated by it — an immediate heart-rate reaction to a submission,
  which a 20 s pulse window cannot resolve, and a post-success recovery
  window. Their physiological inputs remain zero, as in 2.6.0. The coach's
  mode resolver is also given no strain load, so its two load-driven modes
  cannot be selected by camera physiology.
- **Not sent.** RMSSD is computed for research output only. The realtime
  state message carries `hrv_rmssd: null`, and no client reads the field.

## Change log

**3.0.0 / support-features-v3.0.0** — Camera physiology becomes corroborating
evidence. Added the personal reference tracker, the physiology block and its
fusion with the behaviour block, heart rate / SDNN / breathing rate / blink
rate / eye-closure share as production features, and the strain-load break
suggestion. The pulse estimator was rebuilt around a harmonic signal-to-noise
publication gate, a whole-face skin region, a 20 s window and narrow-band beat
timing (`pulse-v2/3.0.0`, `hrv-v2/3.2.0`); a window bug that prevented any
breathing rate from being published was fixed. Behaviour weights, transforms,
gates and temporal behaviour are unchanged, and with `state.physiology_weight:
0` or no camera the output is identical to 2.6.0. See the
[qualification report](../research/physiology-qualification.md).

**2.6.0 / support-features-v2.2.0** — Time-scale the EMA, isolate clock domains,
reject non-finite numeric evidence and invalid temporal configuration, and
make source warm-up count only fresh ordered snapshots. The rule weights,
normal 500 ms response and diagnostic-only physiology boundary are unchanged.
The [0.5.1 validation record](../release/0.5.1-validation.md) records software
checks; physiological accuracy and participant-held-out effectiveness remain
unestablished.

**2.5.0 / support-features-v2.2.0** — Corrected input-source missingness,
regular-key interval readiness and temporal continuity. Expanded build-time
implementation provenance through acquisition, aggregation, fusion and smoothing.
Camera-derived blink no longer influences reading proposals. Goal checks consume
actual foreground metadata; a stationary live foreground app no longer expires
with the 60 s focus-change window. Regression tests cover denied/dead/partial
listeners, genuine inactivity, source recovery, long gaps, timestamp replay,
camera invariance and the daemon's actual sampling/proposal call paths.

Compatibility: additional telemetry fields have defaults, so older payloads still
decode. Without explicit source availability their input features abstain until
a current producer supplies it. Configured rule weights and normal 500 ms EMA
behavior are unchanged. The retained `safety_null` entry disables inference.
Reference-sensor and participant-held-out validation remain outstanding.

**2.4.0** — `score_window_switch` is continuous and matches its own
documentation. Both branches used the wrong divisor: the 10-20 switches/min
band divided by 20 where continuity requires 10, so it reached only 0.25 at its
top and the function stepped 0.25 -> 0.50 across an infinitesimal change at
exactly 20 switches/min; the upper branch also divided by 20, saturating at 30
rather than the 40 the docstring states. Feeding a feature weighted 0.18, that
step meant two windows a hair either side of 20 switches/min produced
materially different support scores for no real difference in behaviour. The
map is now 10 -> 0.0, 20 -> 0.5, 40 -> 1.0.

Also in this release, tab categories reach the scorer. `set_tab_categories`
had no production caller, so `_same_category_ratio` returned 0.0 on every tick
and the same-category discount in `_support_transform` could never be applied —
switching between ten tabs of one topic scored exactly like switching between
ten unrelated ones. The data was already assembled upstream; it simply never
reached the scorer. Because this activates a previously dead discount, support
scores fall for topically coherent switching.

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

- Camera physiology was checked against a reference sensor on one participant.
  On two participants with intermediate and dark skin tone the estimator
  published nothing; for such users the rules are behaviour-only. Accuracy on
  other people, cameras and rooms is unestablished.
- Heart rate rises and variability falls for many reasons unrelated to work:
  posture, caffeine, conversation, temperature, illness, medication. The
  physiology block cannot tell them apart, which is why it may only corroborate
  a behavioural reading.
- Movement, low light and a face partly out of frame cause gaps. The estimate
  then rests on behaviour alone; no stale value is carried forward.
- A session-learned reference describes the first minutes of the session. If
  those minutes were unusual, deviations are measured from an unusual level
  until the reference adapts.
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
missing-value coherence, all channel-presence combinations, camera absence
equivalence (no camera, no face, no pulse or no reference reproduces the
behaviour-only score exactly), bounded physiology influence (including that no
state below the entry threshold is raised), ineligible-rule exclusion,
zero-stream rejection, evidence monotonicity, deterministic replay, dwell and
recovery semantics, probability-artifact rejection, and safe-null rollback.
The physiology estimators additionally run the synthetic qualification
(agreement and zero false publication on signal-free input) as a step of
`make verify`; the ECG comparison needs the separately downloaded dataset and
is run by hand when an estimator changes.

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
- The original D2 divisor guard was superseded by the 1,000 px²/s²
  readiness floor in version 2.3.0, as recorded above.
- Tab-switch and scroll-back rates are stamped with their true 15 s window
  and switches are no longer under-counted by one per window (audit D13).
- The legacy physiology/posture composites were deleted from the scorer
  (audit D17). Version 3.0.0 did not restore them; its physiology block is the
  separately specified one above.

## Change and rollback policy

Any change to feature names/order, units, readiness, windows, transforms,
weights, coverage, thresholds, or dwell requires:

1. a feature-schema or model version increment;
2. regenerated TypeScript schemas and updated clients;
3. this model card and the implementation ledger updated in the same change;
4. replay, metamorphic, missingness, and UI unknown-state tests;
5. a migration/rollback note and the retained `safety_null` entry.

Set `state.physiology_weight: 0` to return to behaviour-only scoring, or
`state.inference_mode: safety_null` to disable inference on restart. The
safe-null model emits only insufficient-evidence `UNKNOWN`; there is no hidden
learned-classifier fallback.
