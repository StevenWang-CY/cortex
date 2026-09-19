# Product and evidence limitations

Last reviewed: 2026-09-05. Cortex 0.4.0 is an alpha research prototype for
local, user-controlled workspace support. It is not a medical device, clinical
tool, productivity judge, accessibility accommodation, or validated estimator
of cognition.

## What the current output means

The production `deterministic-support` rules summarize recent mouse, keyboard,
browser, and focus-transition aggregates into heuristic support hypotheses.
They can abstain as `unknown`. Scores are bounded evidence strengths, not
probabilities. Names such as `support_likely` and `flow_like` describe rule
patterns; they do not measure overwhelm, flow, stress, fatigue, attention,
arousal, intent, ability, or health.

The rules have no training data and no population accuracy claim. They may
behave differently with assistive technology, voice/touch input, keyboard-only
or mouse-only work, remote desktops, unusual key mappings, different motor
patterns, and tasks whose normal mechanics resemble “thrashing.” Calibration
personalizes acquisition baselines; it does not validate or diagnose a state.
See the [model card](model-cards/deterministic-support-v2.md) and
[study protocol](research/support-inference-study-protocol.md).

## Camera and physiological signals

Webcam pulse is experimental and sensitive to lighting, motion, occlusion,
camera processing, skin/camera interaction, frame timing, distance, glasses,
and device differences. It has not passed the required synchronized reference-
sensor and participant-held-out product gate. It is displayed only as an
experimental wellness signal and cannot affect production support scores.

Camera-derived HRV, RMSSD/SDNN/pNN50/LF-HF/nonlinear measures, respiration,
breath-pause or “screen apnea,” and the former stress integral are unavailable
in product mode. Blink and camera-relative head/neck pose remain local comfort
proxies, not cognitive evidence. No output is suitable for diagnosis,
treatment, emergency detection, medication decisions, or clinical monitoring.

The repository contains a checksum/license/reference-bound replay harness and
protocol. No participant dataset or passing external validation report is
committed. A future report must publish abstention coverage, error and agreement
by preregistered condition/subgroup, reference hardware and alignment, sealed
participant splits, exclusions, and uncertainty.

### Measured limits of the pulse publication gate

Two failure modes were measured directly against the packaged POS backend and
are bounded, not eliminated. Both are stated here because the honest number
matters more than a clean claim.

**A heart rate read out of noise is now rare, not impossible.** Before v0.5.0
the publication gate tested only a composite quality score whose acquisition
terms (motion and face coverage) alone contributed 0.25 of a possible 1.0, so a
still, fully visible face cleared the 0.30 threshold with no cardiac evidence
at all: every signal-free window published a rate. The gate now tests signal
presence on the raw spectrum — in-band SNR in decibels, a normalised spectral
quality index, and the share of in-band power concentrated at the selected
peak. Over 2,400 signal-free windows spanning white, 1/f and drift nuisance,
none published a rate, down from all of them. That measurement bounds the rate
near 0.1%; it does not establish zero.

The thresholds are not set as tight as they could be, deliberately. Past
2.0 dB SNR, or above a 0.40 concentration floor, genuine low-amplitude pulses
start being discarded — a 0.50 floor more than halves sensitivity at a
realistic 0.3% modulation depth, for a specificity gain indistinguishable from
noise at this sample size. Driving the residual lower is a job for temporal
hysteresis, so that one surviving window cannot move a displayed rate. The
legacy estimator has such a stabilizer; porting it to the published v2 path is
deferred because v2 publishes one estimate per window with its own provenance,
and a held or smoothed value is not measured in the window it is attributed
to. That is a design decision, not an oversight.

**Rates at or below the passband edge are withheld, not measured.** The
analysis band starts at 0.7 Hz (42 BPM), so a slower fundamental is removed by
the bandpass while its harmonics survive; before v0.5.0 such rates published at
exactly double or triple the truth (35 BPM as 69.9, 40 as 80.1, 42 as 124.8).
These are now detected by re-examining the pre-bandpass waveform and withheld
with an explicit reason. The consequence is that genuine bradycardia — trained
endurance athletes at rest, or bradyarrhythmia — yields no reading rather than
a wrong one. Widening the band instead would admit respiration and drift into
the cardiac band for every user, which is the worse trade.

## Suggestions, effects, and efficacy

The default mode is `suggest_only`; a proposal is not permission to close,
hide, fold, block, edit, or rearrange anything. Optional effects require an
exact displayed manifest, one-time authorization, durable receipt,
postcondition verification, and scoped restore. Even a correctly executed
suggestion is not proven helpful.

Production policy is deterministic and non-learning. The separately consented
micro-randomized research path is software infrastructure, not evidence of
benefit. There has been no completed human-factor study or independent
statistical review demonstrating that Cortex improves performance, wellbeing,
learning, or interruption burden.

## External model providers

The default planner makes no model network request. `external_redacted` is an
explicit mode with source selection, minimization/redaction, an exact preview,
and one-time confirmation. Redaction is defense in depth and cannot guarantee
that arbitrary selected content contains no secret or third-party confidential
information. Provider retention and regional behavior depend on the user's
account, model, features, terms, and current configuration. Cortex cannot
attest those external settings. See the [privacy disclosure](../cortex/docs/privacy.md).

## Platform, reliability, and release scope

- **The suggestion card's action buttons are mouse-only.** The overlay is a
  notification-style window: it is created with `WA_ShowWithoutActivating` and
  is shown with `raise_()` but deliberately never `activateWindow()`, so that a
  suggestion cannot steal focus from whatever the user is actually doing. The
  consequence is that the window can never receive keyboard focus, and it is
  the only surface that renders the action buttons — so a keyboard-only user
  cannot reach them. Giving it an explicit, announced way in requires a
  system-wide hotkey (the card is not focusable from the app that owns the
  keyboard), which needs a native event monitor and the macOS Accessibility
  permission. That is a product decision with a privacy cost, not a defect fix,
  so it is recorded here rather than implemented unilaterally. The dashboard
  itself is keyboard-navigable and every control in it carries an accessible
  name.

- Supported release target: macOS 13 or later, arm64 and x86_64 artifacts.
  MediaPipe no longer publishes current Intel wheels, so the locked Intel
  artifact uses the last compatible 0.10.21/NumPy 1.x branch. Architecture
  parity is therefore test-gated, not assumed; both candidate artifacts still
  require the physical validation protocol before public promotion. MediaPipe's
  capped OpenCV contrib dependency is the sole `cv2` provider so wheel install
  order cannot silently change the runtime OpenCV major. That final Intel
  MediaPipe wheel also constrains Protobuf to a release affected by
  `PYSEC-2026-1805`; Cortex does not expose the affected Protobuf JSON parser,
  and a real-model smoke plus an Intel-only audit exception constrain the risk.
  The exception is re-reviewed on a dated schedule (currently through
  2026-10-15) and is never extended silently; Intel release support must
  change or adopt a compatible patched backend before it lapses.
- Browser integration targets current Chrome/Edge MV3 behavior; browser
  updates, enterprise policy, service-worker suspension, and native-host
  installation can affect operation.
- Camera and input permissions are user-controlled and may be revoked.
  Missing permissions should degrade to explicit unavailable/unknown states.
- Shutdown and restore are fault-tested, but OS crashes, third-party extension
  conflicts, manual workspace edits, disk failures, and corrupted profiles can
  still produce visible restore failures.
- External page/editor content is untrusted. It can alter proposal wording but
  cannot legitimately grant execution authority.

Source code can build ad-hoc DMGs for development. Only a release candidate
with attached Developer ID, Apple notarization/stapling, mounted-artifact smoke,
two-architecture evidence, checksums, SBOMs, and GitHub attestations should be
described as a distributable release. This repository change does not itself
claim that a credentialed release candidate passed real-device installation.

## Prohibited uses

Do not use Cortex for employment, education, insurance, access, discipline,
ranking, surveillance, diagnosis, treatment, crisis response, or decisions
about another person. Do not collect participant data without an approved
protocol, consent, data-management plan, and applicable ethics/legal review.

Report security issues through [SECURITY.md](../SECURITY.md). Report false or
harmful product claims as bugs even when the underlying code is functioning as
implemented.
