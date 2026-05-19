# Calibration

## Purpose

Calibration establishes personal baseline distributions used by state scoring and trigger personalization. Cortex 0.2.0 keeps the original seed capture and adds richer baseline statistics for robust z-score/percentile logic.

## What Changed in 0.2.0

- Baseline artifacts now include per-metric distribution stats:
  - `mu`, `sigma`, `p10`, `p90`
- Baseline loader includes migration logic so legacy baseline JSON files still load.
- Blink threshold personalization support is available from EAR sample percentiles.
- Config includes rolling re-baseline and decay knobs for longitudinal adaptation.

## Running Baseline Capture

```bash
cortex-calibrate --duration 120
```

Optional simulation mode:

```bash
cortex-calibrate --simulate
```

## Output Schema (Key Fields)

`UserBaselines` (in `cortex/libs/schemas/state.py`) persists both scalar baselines and additive distribution metadata. The schema's actual field names:

```json
{
  "hr_baseline": 72.0,
  "hr_std": 5.0,
  "hrv_baseline": 50.0,
  "blink_rate_baseline": 17.0,
  "mouse_velocity_baseline": 500.0,
  "mouse_variance_baseline": 10000.0,
  "shoulder_neutral_y": 0.5,
  "resp_baseline": 15.0,
  "calibrated_at": "2026-05-19T08:00:00",
  "metric_distributions": {
    "hr": {"mu": 72.0, "sigma": 4.2, "p10": 66.0, "p90": 78.0},
    "hrv": {"mu": 50.0, "sigma": 9.5, "p10": 38.0, "p90": 63.0}
  },
  "circadian_hr_cosinor": {},
  "rolling_rebaseline_seconds": 60.0,
  "ew_decay_half_life_days": 7.0
}
```

## How Baselines Are Used

- Pulse/HRV sub-scores use personalized z-score style logic when distribution stats exist.
- FLOW/HYPER rules reference personalized baseline bands before fallback heuristics.
- Stress integral normalization can use baseline variance (`sigma`) rather than absolute ms deficit.
- Trigger threshold adaptation and dismissal modeling use feedback tied to personal history.

## Compatibility

Legacy files without `metric_distributions` are auto-migrated in runtime load paths. No manual data migration is required.
