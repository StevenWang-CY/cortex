# Calibration

Calibration establishes local acquisition and behavior baselines. It does not
train a cognitive-state model, diagnose a condition, or validate camera-derived
HRV/respiration.

## What is measured

- camera timing/availability, face/ROI quality, motion and lighting quality;
- experimental pulse baseline when a quality-valid window exists;
- local blink and camera-relative head/neck proxy baselines;
- user interaction baselines used by deterministic telemetry rules.

Unsupported HRV, LF/HF, respiration, breath-pause, stress-integral, and
physiology-triggered-break fields remain unavailable regardless of calibration.
Camera features remain excluded from the production support score.

## Validity contract

A profile records schema/algorithm version, source device identity, measured
duration, missing fraction, quality summaries, clock provenance, and creation
time. Synthetic/simulated fallback cannot be saved as a measured profile. A
new profile is committed atomically only after review; cancel/failure preserves
the previous profile. Reload verifies the exact committed profile ID.

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

Implementation details: [calibration documentation](cortex/docs/calibration.md)
and [limitations](docs/limitations.md).
