# Calibration Guide

## Why Calibrate?

Cortex detects cognitive overwhelm by comparing current signals against your personal baselines. Heart rate, HRV, blink rate, and posture vary widely between individuals, so calibration captures your "normal" to make detection accurate.

Without calibration, Cortex uses population-level defaults that may trigger false positives or miss real overwhelm.

## What Gets Measured

| Metric | Description | Typical Range |
|--------|-------------|---------------|
| Resting heart rate | HR at relaxed baseline | 55-85 BPM |
| Heart rate variability | RMSSD from IBI series | 20-80 ms |
| Baseline blink rate | Blinks per minute at rest | 12-20/min |
| Mouse velocity | Normal mouse speed | 200-600 px/s |
| Shoulder position | Neutral shoulder Y-coordinate | Varies |

## Running Calibration

### With Webcam (Recommended)

```bash
cortex-calibrate
```

This runs a 2-minute capture session:
1. Sit comfortably in your normal work position
2. Look at the screen naturally
3. Avoid talking or large movements
4. The script captures 120 seconds of biometric data

### Simulated Mode (Testing)

```bash
cortex-calibrate --simulate
```

Generates synthetic calibration data using population averages with realistic variance. Useful for development and testing without a webcam.

### Options

```
cortex-calibrate [OPTIONS]

Options:
  --simulate           Use simulated data instead of webcam
  --duration SECONDS   Calibration duration (default: 120)
  --output PATH        Output file path (default: storage/baselines/<timestamp>.json)
```

## Baseline Output

Calibration produces a `UserBaselines` JSON file:

```json
{
  "resting_hr": 72.0,
  "resting_hrv": 50.0,
  "baseline_blink_rate": 17.0,
  "baseline_mouse_velocity": 500.0,
  "shoulder_neutral_y": 0.5,
  "calibrated_at": "2025-01-15T10:00:00"
}
```

### Fields

| Field | Unit | Description |
|-------|------|-------------|
| `resting_hr` | BPM | Mean resting heart rate |
| `resting_hrv` | ms | Mean RMSSD at rest |
| `baseline_blink_rate` | blinks/min | Normal blink rate |
| `baseline_mouse_velocity` | px/s | Typical mouse speed |
| `shoulder_neutral_y` | normalized | Shoulder position reference |
| `calibrated_at` | ISO 8601 | When calibration was performed |

## How Baselines Are Used

The state engine's rule scorer compares real-time features against baselines:

- **Pulse elevation**: Current HR vs `resting_hr`. Elevation > 15 BPM scores high.
- **HRV drop**: Current RMSSD vs `resting_hrv`. Drop > 40% scores high.
- **Blink suppression**: Current blink rate vs `baseline_blink_rate`. Rate < 50% of baseline indicates hyperfocus/stress.
- **Mouse thrashing**: Current velocity vs `baseline_mouse_velocity`. Variance > 3x baseline scores high.
- **Posture collapse**: Current shoulder position vs `shoulder_neutral_y`. Drop > 15% + forward lean > 20 degrees triggers detection.

## When to Recalibrate

Recalibrate when:
- First use or new device setup
- Significant change in work environment (new desk, chair, monitor)
- Consistently inaccurate state detection (too many false positives/negatives)
- After a long break (>2 weeks)

Cortex stores baselines per profile. You can maintain multiple profiles for different environments.

## Storage Location

Default: `storage/baselines/`

The seed config script creates a default baseline:

```bash
python -m cortex.scripts.seed_config
# Creates storage/baselines/default_baselines.json
```

## Computation Details

The calibration script (`scripts/calibrate.py`) computes baselines as follows:

1. **Collect samples** — 120 seconds of continuous data at ~2 Hz sampling
2. **Compute mean** — `statistics.mean()` for each metric
3. **Compute stdev** — `statistics.stdev()` for variance tracking
4. **Validate** — discard outliers (>3 stdev from mean)
5. **Produce `UserBaselines`** — Pydantic model with all baseline values

In simulated mode, data is generated using seeded random distributions:
- HR: Normal(70, 3)
- HRV: Normal(50, 8)
- Blink rate: Normal(17, 2)
- Mouse velocity: Normal(500, 100)
- Shoulder Y: Normal(0.5, 0.02)
