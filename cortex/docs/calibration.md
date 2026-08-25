# Calibration

## Current status

The existing calibration command is a legacy research utility. It does not yet
consume exactly the same versioned observation/feature pipeline as the runtime,
does not establish independent reference-sensor truth, and may overstate the
effective sample size of overlapping windows. It must not be described as
making state scores accurate, probabilistic, or diagnostic.

```bash
cortex-calibrate --duration 120
```

The `--simulate` option is for UI/developer demonstrations only. Synthetic
values are not user measurements and must not be promoted or persisted as a
measured calibration profile.

## Product use

Until Calibration v2 is complete:

- resting heart-rate and basic blink/head-neutral values are advisory legacy
  baselines;
- HRV and respiration baselines cannot enable product metrics or
  physiology-triggered actions;
- a profile cannot turn a heuristic support score into a calibrated
  probability;
- camera or configuration changes can invalidate prior values;
- stale or simulated profiles must not silently drive decisions.

## Calibration v2 contract

WP-4 in [`../../IMPLEMENTATION.md`](../../IMPLEMENTATION.md) replaces the
parallel runner with a profile produced by the same observation and feature
pipeline as normal operation. A valid profile records:

- schema and algorithm versions;
- camera identity class and relevant configuration;
- start/end wall time and valid monotonic exposure;
- per-channel quality and missingness;
- accepted observation/beat counts and effective sample size;
- reference task and collection conditions;
- explicit `measured` provenance;
- metric-specific readiness and rejection reasons.

Publishing a new profile emits a `CalibrationUpdated` event and atomically
rebuilds every dependent estimator. Values derived under another feature
version are compatibility data, not silently reusable evidence.

## Validation

Calibration UX is not validation. Pulse and any future derived physiology need
a consented, subject-disjoint reference-sensor protocol with condition and
subgroup reporting. Support estimates need a declared target such as
self-reported near-term support need, with participant-held-out evaluation.

The release gates and reference protocol are defined in Sections 13 and 18 of
the implementation plan.
