# Calibration

## Current behavior

The desktop wizard, Settings recalibration and `cortex-calibrate` use the same
`CalibrationRunner`. Live capture uses the production `CapturePipeline`, camera
feature factory and input aggregator. Calibration records personal reference
measurements; it does not validate support labels or make scores probabilistic,
diagnostic or accurate against an independent sensor.

The current protocol is `calibration/2.0.0`, with profile schema `2.0` and
calibration feature schema `features/2.0`. These are separate from behavioral
inference's `support-features-v2.2.0` and model `2.5.0`. The
[current implementation record](../../docs/implementation-2026-10.md) and
[behavioral model card](../../docs/model-cards/deterministic-support-v2.md)
describe that boundary.

## Capture, review and application

The default capture lasts 120 seconds and separates camera-quality checking,
physiological rest and representative work. Neutral head-pose observations are
collected in the quality/rest phases; mouse and blink references come from the
work phase. Missing channels remain unavailable rather than receiving synthetic
defaults. Failure to obtain live camera observations fails the live run; it does
not switch to simulation.

After capture, the runner presents a profile for review. Explicit approval stages
an immutable measured profile; staging alone does not change the running daemon.
The daemon validates provenance, approval, protocol/feature versions and the
algorithm identities of applicable metrics, constructs replacement components,
commits active calibration and synchronously swaps the prepared graph. Failed
validation or preparation leaves the old profile active. The live swap clears
held features/estimates and emits `CalibrationUpdated`; the desktop reports
completion after application is confirmed.

Camera-relative head/neck calibration additionally requires the active camera's
stable identity and delivered geometry to match the profile. A different camera
or geometry does not inherit a previous neutral pose. Changed algorithm or
relevant configuration identities can make a profile incompatible; the runtime
starts uncalibrated rather than silently applying incompatible evidence.

## Command line

```bash
cortex-calibrate --duration 120
```

This captures and prints a review, then asks for approval in an interactive
terminal. Without approval it does not save an active profile. `--approve`
provides explicit non-interactive approval. CLI activation takes effect on the
next daemon start; it does not claim to reconfigure an already running daemon.
`--output PATH` writes an optional copy of the approved immutable profile while
retaining the canonical stored profile.

For a device-free demonstration:

```bash
cortex-calibrate --simulate --duration 3
```

Simulation previews synthetic values. `--save-demo` optionally stores them under
the separate `calibration/demo_profiles/` namespace. Demo profiles cannot be
approved or activated, and never become measured calibration.

## Stored evidence and limits

Profiles contain a UUID, measured/demo provenance, creation and approval times,
protocol/feature versions, stable camera identity and geometry, metric summaries,
and named baseline values. Each metric carries its unit, reference task, maturity,
distribution, sample count, effective sample count, valid duration, missing
fraction, quality percentiles, algorithm/configuration digests, and an unavailable
reason when needed. Raw frames, landmarks, key contents and workspace text are
not stored in the profile.

The runner bounds effective sample count by both sample count and valid phase
duration divided by the metric's effective window. This limits the apparent
sample size of overlapping windows; it is an engineering exposure bound, not an
empirically estimated number of statistically independent samples. Phase quality
and missingness summarize the quality-gated camera observations for that phase;
they are not independent ground truth for mouse or physiological accuracy.

Mouse baseline samples additionally require a currently available mouse source.
Failed or stopped listeners cannot contribute fabricated zeros. A healthy source
with no movement can contribute an observed zero. A low mouse-variance baseline
can still be unsuitable for normalization: the support scorer abstains on that
feature below its documented 1,000 px²/s² readiness floor.

Heart-rate and respiration summaries remain experimental and are not applied to
production scoring. Blink and head/neck references can calibrate their diagnostic
proxies, but cannot establish attention, comprehension or support need. A profile
cannot enable camera HRV, respiration or physiology-triggered production actions.

## Persistence and validation

Immutable profiles live under the configured storage root's
`calibration/profiles/`. The running daemon's active calibration is authoritative
in SQLite, with a checksummed `calibration/active.json` compatibility projection.
The legacy `baselines/default.json` is a projection, not independent authority.
The CLI uses the offline profile/pointer path for the next startup, where the
normal migration and compatibility checks reconcile it with the daemon.

Automated tests cover live failure, demo isolation, explicit approval, immutable
profiles, checksum/identity rejection, staged-versus-active state, failed
application and estimator replacement. Those checks do not establish sensor or
product validity. Physiological measurements still need a consented synchronized
reference-sensor study; support inference needs participant-held-out evaluation
and intervention benefit needs randomized evidence. See the
[study protocol](../../docs/research/support-inference-study-protocol.md) and
[limitations](../../docs/limitations.md).
