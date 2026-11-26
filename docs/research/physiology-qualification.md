# Camera physiology qualification

Recorded 2026-11-25 for Cortex 0.6.0 (`deterministic-support` 3.0.0,
`pulse-v2/3.0.0`, `hrv-v2/3.1.0`). This report is the evidence behind
[ADR 0008](../adr/0008-qualified-camera-physiology.md), which lets camera
physiology corroborate the behavioural support rules. It states what was
measured, against what, how well it agreed, and where it did not.

## Summary

| Measurement | Checked against | Result | Use in the product |
| --- | --- | --- | --- |
| Heart rate | ECG, 21 public recordings | MAE 1.8 BPM, 94 % within 5 BPM, r = 0.99, on the 33 % of windows it published | Shown; corroborates the rules |
| Heart-rate variability, SDNN | ECG, same recordings | MAE 8.3 ms, 88 % within 15 ms, r = 0.96, on 22 % of windows | Shown; corroborates the rules |
| Heart-rate variability, RMSSD | ECG, same recordings | MAE 22.8 ms, 9 % within 15 ms | **Not qualified.** Computed for research; no rule, suggestion or screen reads it |
| Breathing rate | Simulated truth only | MAE 0.2 /min on simulated recordings; no reference sensor | Shown; low weight in the rules |
| Blink rate, eye closure | Not measured here | Landmark geometry, unchanged from 0.5.1 | Low weight in the rules |

Three limits matter more than any number above and are repeated in
[Limits](#limits):

1. All real-data agreement comes from **one participant**. On the two
   participants with intermediate and dark skin tone the estimator published
   nothing. Abstaining is the designed behaviour, but it means these numbers
   say nothing about accuracy for those users, and that the camera adds nothing
   for them on comparable footage.
2. The publication gate was designed while looking at this same dataset. There
   is no held-out set.
3. Agreement with a reference sensor shows that the numbers track the body. It
   does not show that they indicate a need for support.

## What is measured

The capture service detects a face, takes the mean colour of the facial skin
(face outline minus eyes, brows and lips), and hands a time-stamped RGB trace to
the physiology engine. One code path,
`cortex/services/physio_engine/camera_physiology.py`, serves the running app and
this qualification.

- **Heart rate.** The POS projection of a 20 s window (10 s while the long
  window fills), band-passed to 0.7–3.5 Hz; the rate is the selected peak of a
  whole-window periodogram.
- **Publication gate.** A rate is published only when the window carries a
  cardiac line: the harmonic signal-to-noise ratio (de Haan & Jeanne, 2013) —
  power within 0.12 Hz of the candidate rate and of its second harmonic, over
  the rest of the band — must reach 3 dB. A weaker window (0 dB or more) may
  only continue a rate that a 3 dB window established within the last 60 s,
  within 8 BPM, without a gap longer than 10 s. The second harmonic counts only
  up to half the fundamental's power; without that cap the real pulse confirmed
  a candidate at half its own rate (see [Findings](#findings-that-changed-the-estimator)).
- **Beat timing and variability.** Beats are timed on a narrow band (±0.5 Hz)
  around the selected rate by Hilbert phase; overlapping windows vote on each
  beat. SDNN is the standard deviation of the accepted intervals over the
  trailing three minutes (at least 90 s and 40 intervals, at most 25 %
  rejected). RMSSD uses the trailing two minutes.
- **Breathing rate.** Three channels — pulse-amplitude envelope, vertical head
  movement, and beat-interval modulation — each estimate a rate; a value is
  published only when at least two agree, and the envelope channel abstains
  when the window has no cardiac line.

## Method

### Test benches

**Simulated recordings with known truth**
(`cortex/services/eval/physio_synthetic.py`). Eighteen five-minute recordings:
heart rates 58, 72 and 95 BPM; respiratory sinus arrhythmia 12, 35 and 70 ms;
two sensor-noise levels; breathing at 10, 14 and 19 /min. The simulator emits
what the capture service emits: a jittered-frame RGB skin trace and head
position. Eighteen further four-minute recordings contain no pulse at all
(white, drifting and 1/f noise) and measure false publication. This is a
signal-processing bench, not a model of a person: passing it is necessary and
never sufficient.

**ECG reference videos.** Hoffman & Lakens (2020), *Public Benchmark Dataset for
Testing rPPG Algorithm Performance*, 4TU.ResearchData,
<https://doi.org/10.4121/uuid:2ac74fbd-2276-44ad-aff1-2f68972b7b51>: 21 videos
of three participants (bright, intermediate and dark skin tone) with
synchronised ECG RR intervals. Seven lighting conditions from 5 to 2,720 lux
(P1; five for P2 and P3), three head-motion conditions and one recording after
exercise (P1 only).

The dataset licence allows research use and forbids redistribution of the data
or adaptations of it. Neither the videos nor the colour traces extracted from
them are in this repository; only the aggregate agreement below is.

### Replay

Each video frame is cropped once per recording to a fixed 4:3 webcam framing
(640×480, face about 29 % of the frame width) and passed through the
production capture pipeline — the same face tracker, quality gates and skin
region as the app. The resulting trace is replayed through the production
processor at the production stride. Nothing is tuned per recording.

### Scoring

Every published value is paired with the reference over the interval the value
describes: mean ECG rate over the pulse window, ECG SDNN and RMSSD over the same
trailing window. A stride where the reference is available counts as *possible*;
the share that published is the *coverage*. Agreement is computed over published
values only, so coverage and accuracy must be read together.

### Acceptance criteria

Set before the final run, in `cortex/services/eval/physio_qualification.py`:

| Metric | MAE at most | Share within tolerance at least | r at least |
| --- | ---: | ---: | ---: |
| Heart rate | 3 BPM | 85 % within 5 BPM | 0.90 |
| SDNN | 12 ms | 70 % within 15 ms | 0.80 |
| RMSSD | 12 ms | 70 % within 15 ms | 0.80 |
| Breathing rate | 2 /min | 80 % within 3 /min | 0.80 |

Each also needs at least 100 published values. These are thresholds for a
signal read as a *change against a personal reference*, not clinical accuracy
limits. They apply to the pooled result; the per-condition tables are
descriptive, and a correlation inside one condition, where the true value
barely varies, is not meaningful.

## Results

### Simulated recordings

| Metric | Published / possible | Coverage | MAE | Bias | 95th pct error | r | Within tolerance | Meets criterion |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Heart rate (BPM) | 4843 / 5093 | 0.95 | 0.32 | −0.02 | 0.94 | 1.00 | 1.00 (±5) | yes |
| SDNN (ms) | 3448 / 5093 | 0.68 | 7.69 | 3.44 | 20.38 | 0.89 | 0.82 (±15) | yes |
| RMSSD (ms) | 2402 / 5093 | 0.47 | 12.62 | −12.02 | 62.57 | 0.73 | 0.73 (±15) | no |
| Breathing rate (/min) | 3894 / 5093 | 0.76 | 0.19 | −0.09 | 0.34 | 0.96 | 0.99 (±3) | yes |

On the eighteen recordings without a pulse, no heart rate, SDNN, RMSSD or
breathing rate was published (0 of about 4,300 strides).

### ECG reference videos, all 21 recordings

| Metric | Published / possible | Coverage | MAE | Bias | 95th pct error | r | Within tolerance | Meets criterion |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Heart rate (BPM) | 1252 / 3760 | 0.33 | 1.79 | −0.97 | 5.41 | 0.99 | 0.94 (±5) | yes |
| SDNN (ms) | 697 / 3176 | 0.22 | 8.32 | −2.98 | 25.29 | 0.96 | 0.88 (±15) | yes |
| RMSSD (ms) | 920 / 3346 | 0.27 | 22.79 | −13.82 | 37.02 | 0.84 | 0.09 (±15) | no |

### By participant

| Participant | Heart rate published | Heart rate MAE | SDNN published | SDNN MAE |
| --- | ---: | ---: | ---: | ---: |
| P1, bright skin tone (11 recordings) | 1252 / 2057 (61 %) | 1.79 | 697 / 1716 (41 %) | 8.32 |
| P2, intermediate skin tone (5) | 0 / 849 | — | 0 / 743 | — |
| P3, dark skin tone (5) | 0 / 854 | — | 0 / 717 | — |

### By recording

| Recording | Condition | Heart rate published | MAE | Bias | SDNN published | MAE | Bias |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| P1LC1 | 5.2 lux | 54 / 174 | 1.06 | 0.10 | 0 / 141 | — | — |
| P1LC2 | 36 lux | 162 / 174 | 1.99 | −1.01 | 92 / 140 | 1.95 | 1.95 |
| P1LC3 | 187 lux | 171 / 178 | 1.83 | −1.35 | 94 / 144 | 4.35 | −4.23 |
| P1LC4 | 720 lux | 172 / 173 | 1.82 | −0.96 | 91 / 141 | 4.90 | −4.90 |
| P1LC5 | 2,720 lux | 170 / 174 | 1.83 | −1.20 | 92 / 139 | 10.17 | −10.17 |
| P1LC6 | 35 lux, 2,700 K | 156 / 174 | 1.84 | −0.97 | 82 / 142 | 25.26 | −25.26 |
| P1LC7 | 18 lux, lit from one side | 164 / 174 | 1.89 | −0.97 | 92 / 139 | 1.27 | −0.51 |
| P1M1 | natural head movement | 8 / 184 | 2.59 | −1.61 | 0 / 152 | — | — |
| P1M2 | nodding at about the heart rate | 8 / 184 | 3.64 | −3.64 | 0 / 153 | — | — |
| P1M3 | nodding above the heart rate | 0 / 179 | — | — | 0 / 147 | — | — |
| P1H1 | after exercise, rate falling | 187 / 289 | 1.49 | −0.59 | 154 / 278 | 10.67 | 10.67 |
| P2LC1–LC5 | 5.2–2,720 lux | 0 / 849 | — | — | 0 / 743 | — | — |
| P3LC1–LC5 | 5.2–2,720 lux | 0 / 854 | — | — | 0 / 717 | — | — |

## Findings that changed the estimator

Replaying the 0.5.1 estimator over the same recordings during development
published about 8 % of windows at 2.7 BPM mean error and could not publish a
breathing rate at all. The causes, each fixed and covered by a regression test:

- **The signal-presence gate did not test for a signal.** The in-band to
  out-of-band power ratio had the same median for windows whose rate was right
  and wrong. The harmonic ratio separates them.
- **Window quality multiplied in per-frame scores that rarely exceed 0.6**
  (exposure, sharpness, landmark confidence), rejecting about half of the
  windows that were otherwise ready. Quality is now cardiac-line strength times
  acquisition quality, with ordinary good frames carrying no penalty.
- **Breathing could never publish.** The window had to span the full 45.0 s and
  a 30 fps buffer holds 44.97 s. It now needs the configured coverage share.
- **A candidate at half the true rate was confirmed by the true pulse.** On
  P2LC3 (ECG 101 BPM) a weak 0.85 Hz line was selected, the real 1.7 Hz pulse
  was counted as its harmonic, and three windows published 51 BPM. Capping the
  harmonic's contribution removed them at a cost of two correct windows.
- **Skin region.** Forehead-and-cheek patches gave a weaker pulse than the whole
  facial skin with eyes, brows and lips removed.

RMSSD did not qualify under any variant tried (wider or narrower beat band,
noise correction on or off): beat-to-beat differences at 30 frames per second
are dominated by timing noise. SDNN, which averages that noise over a window,
did.

## Limits

- **One participant carries the real-data evidence.** Every published value
  above is from P1. The agreement figures are a check that the estimator tracks
  an ECG when it publishes, on one person and one camera. They are not a
  population accuracy claim.
- **Skin tone and image brightness.** For P2 and P3 the cardiac line was below
  the noise floor even at the ECG rate: median harmonic SNR at the true rate
  was between −1.5 and −6.4 dB for the POS, CHROM and green-channel extractions
  alike (P1 at 720 lux: +7.0 dB). The gate therefore abstained on all 1,703
  windows. P3's footage is also much darker (mean red 70 of 255 against 135 for
  P1 under the same lamp). Whether this reflects the method, this camera and
  its compression, or both cannot be told from three people. For such users the
  product behaves as if no camera were present: behaviour-only inference, no
  wrong number.
- **No held-out data.** The gate constants (3 dB, 0 dB, 0.12 Hz, the harmonic
  cap, the track rule) were chosen with these recordings in view. Agreement on
  new people, cameras and rooms may be lower.
- **Movement.** With head movement 3 % of windows published, and a nodding
  rhythm near the heart rate is the hardest case (8 windows, 3.6 BPM error).
  Typing and talking at a desk are closer to the movement recordings than to
  the still ones; expect gaps.
- **Low light.** At 5 lux a third of windows published a rate and none an SDNN.
- **SDNN bias varies by recording.** From −25 ms (P1LC6, a participant with
  unusually high variability: ECG SDNN near 100 ms) to +11 ms (after exercise).
  The camera path rejects intervals more than 30 % from the local median, which
  trims genuine large swings. The rules standardise against the same person's
  recent level, which removes a constant bias but not a changing one.
- **Breathing rate has no reference sensor behind it.** The dataset has no
  respiration channel. It is published only on two-channel agreement and has
  the lowest weight of the cardiac inputs.
- **Meaning.** The rules use the directions of well-replicated group-level
  findings: under mental load heart rate and breathing rise and variability
  falls. In one person at one moment the same changes follow from posture,
  caffeine, a conversation or a flight of stairs. Nothing here validates the
  fused score as a measure of stress, effort or need for support.

## How the measurements are used

Described fully in the
[model card](../model-cards/deterministic-support-v3.md). In short:

- Each value is standardised against the person's own reference, learned in the
  session (two minutes and sixty valid samples) or seeded by calibration, and
  discarded when the physical camera changes.
- The physiology block is mixed into a state's score only where that state's
  behaviour rule is itself eligible, with a weight of at most 0.35. With no
  camera, no face or no ready reference the score is exactly the behaviour
  score.
- Time spent with the block pointing to strain accumulates into a load that
  drains during calm; crossing its threshold produces one suggestion for a
  short breathing break, through the same interruption gate as every other
  suggestion.

## Switches

| Setting | Effect |
| --- | --- |
| `state.physiology_weight: 0` | The rules ignore camera physiology |
| `intervention.enable_strain_break_suggestions: false` | No strain-load break suggestions |
| `signal.rppg.experimental_hrv_enabled: false` | No variability values are computed |
| `signal.rppg.experimental_respiration_enabled: false` | No breathing rate is computed |

## Reproduce

```bash
# Simulated truth (about two minutes)
uv run --project cortex --locked --extra dev python -m cortex.scripts.qualify_physiology \
    synthetic --markdown

# ECG reference videos: download the dataset from the DOI above, unpack it, then
uv run --project cortex --locked --extra dev python -m cortex.scripts.qualify_physiology \
    dataset-4tu --root "<unpacked dataset folder>" --cache "<trace cache folder>" --markdown
```

The first dataset run extracts a trace per video (several minutes each); later
runs reuse the cache. `--output` writes the full result, including
per-recording agreement, as JSON.

## Reference

de Haan, G. & Jeanne, V. (2013). Robust pulse rate from chrominance-based rPPG.
*IEEE Transactions on Biomedical Engineering*, 60(10), 2878–2886.

Wang, W., den Brinker, A. C., Stuijk, S. & de Haan, G. (2017). Algorithmic
principles of remote PPG. *IEEE Transactions on Biomedical Engineering*, 64(7),
1479–1491.
