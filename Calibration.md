# Calibration

## Why Calibrate?

Cortex detects cognitive overwhelm by comparing real-time signals against **your personal baselines**. Heart rate, HRV, blink rate, and posture vary widely between individuals. Without calibration, Cortex uses population-level defaults that may trigger false positives for some people and miss real overwhelm for others.

Calibration is optional but recommended. Without it, the system still works — it just uses generic thresholds.

---

## Running Calibration

### With Webcam (recommended)

Sit relaxed in your normal work position for 2 minutes:

```bash
cortex-calibrate --duration 120
```

1. Look at the screen naturally
2. Avoid talking or large movements
3. Don't think about work — let your physiology settle to baseline

### Simulated Mode (no webcam / testing)

```bash
cortex-calibrate --simulate
```

Generates synthetic calibration data using population averages with realistic variance. Useful for development without a webcam attached.

### Options

```
cortex-calibrate [OPTIONS]

  --simulate           Use simulated data instead of webcam
  --duration SECONDS   Calibration duration (default: 120)
  --output PATH        Output path (default: storage/baselines/<timestamp>.json)
```

---

## What Gets Measured

| Metric | Description | Typical Range |
|--------|-------------|---------------|
| `resting_hr` | Resting heart rate (BPM) | 55–85 BPM |
| `resting_hrv` | RMSSD at rest (ms) | 20–80 ms |
| `baseline_blink_rate` | Normal blink rate (blinks/min) | 12–20/min |
| `baseline_mouse_velocity` | Typical mouse speed (px/s) | 200–600 px/s |
| `shoulder_neutral_y` | Neutral shoulder Y-coordinate (normalized) | Varies |

---

## Output

Calibration saves a `UserBaselines` JSON file to `storage/baselines/`:

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

---

## How Baselines Are Used

| Signal | Trigger |
|--------|---------|
| Pulse elevation | Current HR > `resting_hr + 15 BPM` |
| HRV drop | Current RMSSD < `resting_hrv × 0.60` |
| Blink suppression | Blink rate < `baseline_blink_rate × 0.50` |
| Mouse thrashing | Velocity variance > `baseline_mouse_velocity × 3` |
| Posture collapse | Shoulder drop > 15% + forward lean > 20° |

---

## When to Recalibrate

- First use or new device
- New desk, chair, or monitor setup
- Consistently too many false alarms or too few detections
- After a long break (>2 weeks)

---

## Computation Details

The script:
1. Collects samples at ~2 Hz for the full duration
2. Computes `statistics.mean()` for each metric
3. Computes `statistics.stdev()` for variance tracking
4. Discards outliers (>3 stdev from mean)
5. Saves a validated `UserBaselines` Pydantic model

The seed config script creates a default baseline from population averages if you skip calibration:

```bash
python -m cortex.scripts.seed_config --root .
# Creates storage/baselines/default.json
```
