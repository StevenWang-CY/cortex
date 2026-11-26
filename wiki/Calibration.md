# Calibration

Calibration establishes local acquisition, behavior and personal physiology
baselines. It does not train a cognitive-state model, diagnose a condition, or
validate camera-derived heart rate, variability or breathing.

## What is measured

- camera timing/availability, face/ROI quality, motion and lighting quality;
- experimental resting pulse and breathing levels when quality-valid windows
  exist;
- local blink and camera-relative head/neck proxy baselines;
- user interaction baselines used by deterministic telemetry rules.

The measured blink level seeds the personal reference the support rules read
camera physiology against, for the camera the profile was taken with. The
pulse and breathing levels are kept in the profile for review and are not
used, and calibration does not measure heart-rate variability: Cortex learns
the usual level for those three during each session, over about two minutes of
valid readings. A different physical camera discards the reference. Camera
physiology only
corroborates a behavioral reading that is already eligible; calibration cannot
make it establish one. RMSSD, LF/HF and breath-pause detection are not product
inputs regardless of calibration.

Mouse samples enter the measured baseline only while the mouse source is
available. A stationary, observed mouse can legitimately contribute zero;
unstarted hooks, denied permissions and failed listeners cannot. This rule
matches production feature fusion, which distinguishes missing evidence from
observed inactivity.

## Validity contract

A profile records schema/algorithm version, source device identity, measured
duration, missing fraction, quality summaries, clock provenance, and creation
time. Synthetic/simulated fallback cannot be saved as a measured profile. A
new profile is committed atomically only after review; cancel/failure preserves
the previous profile. Reload verifies the exact committed profile ID.

Acquisition/profile versions and the production behavioral model version are
separate. Model `3.0.0` uses feature schema `support-features-v3.0.0`; calibrating
does not train that model or validate its labels. The production scorer uses
the calibrated mouse-variance baseline where applicable; the measured blink
level serves only as part of the personal physiology reference, and blink
baselines cannot change reading-check-in timing.

Camera opening follows macOS-specific safeguards: automatic selection unless a
device override is explicit, live re-enumeration after open to reject a
Continuity Camera whose index changed, and warm-up retries for the Mac camera.
Do not use `system_profiler` order as an AVFoundation index and do not globally
reset camera permissions.

## When to recalibrate

Recalibrate after changing camera, normal camera position/distance, lighting,
input device, or primary work setup, or when the UI reports a stale/incompatible
profile. Do not recalibrate merely to force an expected state label.

If calibration falls back to simulation, reports insufficient valid exposure,
or loses the face/camera, resolve the visible cause and retry. Cortex should
degrade to telemetry-only/unknown rather than save fabricated evidence.

Implementation details: [calibration documentation](https://github.com/StevenWang-CY/cortex/blob/main/cortex/docs/calibration.md)
and [limitations](https://github.com/StevenWang-CY/cortex/blob/main/docs/limitations.md).
