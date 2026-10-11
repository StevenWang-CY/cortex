# Product and evidence limitations

Last reviewed: 2026-10-11. Cortex 0.5.1 is an alpha research prototype for
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

The current registry version is `2.6.0`, with feature schema
`support-features-v2.2.0`. Missing input sources abstain; silence counts only
while the corresponding listeners are observed and available. Duplicate or
regressed timestamps cannot advance smoothing/dwell, and observation gaps
longer than three seconds restart confirmation. These are evidence-integrity
checks, not empirical validation of the inferred hypotheses. The model digest
covers eleven behavioral acquisition/aggregation/schema/fusion/scoring/
smoothing components; it does not identify the full runtime, configuration,
personal baselines, or intervention policy.

The rules have no training data and no population accuracy claim. They may
behave differently with assistive technology, voice/touch input, keyboard-only
or mouse-only work, remote desktops, unusual key mappings, different motor
patterns, and tasks whose normal mechanics resemble “thrashing.” Calibration
supplies local acquisition and behavioral baselines; it does not validate or
diagnose a state.
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

### Historical synthetic checks of the pulse publication gate

The following results were recorded during earlier v0.5.0 POS/publication-gate
repairs and are preserved as regression evidence. The fixtures and relevant
measurement notes are in
[`test_pulse_spectrum_and_gates.py`](../cortex/tests/physio/test_pulse_spectrum_and_gates.py)
and [`rppg.py`](../cortex/services/physio_engine/rppg.py). They describe synthetic
windows, not participants, physical cameras, clinical accuracy, or a new
external validation performed during the October refinement. Residual failure
modes remain.

**Signal-free publication fell on the recorded synthetic fixtures.** Before v0.5.0
the publication gate tested only a composite quality score whose acquisition
terms (motion and face coverage) alone contributed 0.25 of a possible 1.0, so a
still, fully visible face cleared the 0.30 threshold with no cardiac evidence
at all: every signal-free window published a rate. The gate now tests signal
presence on the raw spectrum — in-band SNR in decibels, a normalised spectral
quality index, and the share of in-band power concentrated at the selected
peak. Over 2,400 signal-free windows spanning white, 1/f and drift nuisance,
none published a rate, down from all of them. For independent windows sampled
from those synthetic generators, 0/2,400 gives an approximate one-sided 95%
binomial upper bound of 0.125%. This does not establish zero or a real-world
false-publication rate.

The thresholds are not set as tight as they could be, deliberately. Past
2.0 dB SNR, or above a 0.40 concentration floor, genuine low-amplitude pulses
start being discarded — a 0.50 floor more than halves sensitivity at a
synthetic 0.3% modulation depth, for a specificity gain indistinguishable from
noise at this sample size. Driving the residual lower is a job for temporal
hysteresis, so that one surviving window cannot move a displayed rate. The
legacy estimator has such a stabilizer; porting it to the published v2 path is
deferred because v2 publishes one estimate per window with its own provenance,
and a held or smoothed value is not measured in the window it is attributed
to. That is a design decision, not an oversight.

**The overlap-add repair removes artificial zeros without a demonstrated
accuracy gain in the paired fixtures.** Until v0.5.0 the POS overlap-add
reconstruction left the first sample and the trailing ~0.4 s of every window
at the allocator's exact 0.0 — 14 of 300 samples at the default 30 fps — and
the backend contract checks only shape and finiteness, so those reached the
quality metrics and the spectral estimator as measurement. They are gone. It
is worth being clear that this was a correctness fix and not an accuracy
improvement: over 1,500 paired windows through the shipped publication gate,
sensitivity at a synthetic 0.3% modulation depth moved 0.2360 → 0.2340
(discordant 9/6, McNemar p = 0.61) and false publication on drift and 1/f
noise was unchanged. The first repair attempted — a strictly positive taper,
which is the obvious one — removed the same zeros but perturbed the interior
and cost real sensitivity for it: 0.2360 → 0.2260, discordant 20/5,
p = 0.004. It was measured and discarded.

**The tested passband-edge harmonic errors are withheld.** The
analysis band starts at 0.7 Hz (42 BPM), so a slower fundamental is removed by
the bandpass while its harmonics survive; before v0.5.0 such rates published at
exactly double or triple the truth (35 BPM as 69.9, 40 as 80.1, 42 as 124.8).
The tested cases are now detected by re-examining the pre-bandpass waveform
and withheld with an explicit reason. An unavailable result is the intended
outcome for this evidence failure; it cannot diagnose a slow pulse or guarantee
correct rejection for every real signal. The review retained the narrower
band because widening it also admits additional respiration/drift energy and
requires separate validation.

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

- **Suggestions arrive without taking keyboard focus.** The dashboard now
  provides a keyboard-operable **Review suggestion** action that explicitly
  activates the exact current card and focuses **Dismiss**. This route does
  not require a global hotkey or additional input-monitoring permission.
  Reviewing is navigation and does not authorize execution. Pending suggestions
  last at most five minutes from first receipt, shortened by an earlier
  manifest expiry; updates and reopening cannot renew that deadline. Dismissal,
  action, quiet mode, a matching terminal transaction, stop or disconnect
  retires the card. Automated Qt coverage checks this route; complete VoiceOver,
  native focus, and physical multi-Space review remain part of device validation.

- **Display freshness is not measurement accuracy.** The desktop clears
  biometrics on disconnect and waits for a fresh state update after reconnect.
  Stale/not-flowing capture and invalid pulse values remain unavailable.
  Browser popup and Pulse Room additionally expire the displayed signal after
  ten seconds without an update. That UI continuity deadline cannot validate
  the physiological measurement. Pulse Room's motion is decorative, with
  explicit pause and hidden/reduced-motion cleanup; it is neither an ECG nor
  heartbeat-paced breathing guidance.

- **Close, End session, and Quit have different outcomes.** Closing the
  dashboard hides it while the menu-bar app and session remain available.
  Ending a session stops sensing and leaves the recap/desktop open; the
  in-process host can start another session. Explicit Quit finishes cleanup
  and exits Qt. Real event-loop regression tests cover final Quit in both
  desktop hosts, and the packaged headless restart check covers process exit
  and listener release. Neither proves physical camera release, native
  keyboard/VoiceOver behavior, or recovery after every OS-level failure.

- **Source tests enforce a measured 65% coverage floor.** Earlier
  configuration declared an 85% floor without the canonical gate enabling
  coverage or selecting its configuration. The current gate explicitly passes
  `--cov --cov-config=cortex/pyproject.toml`, enables branch coverage, and
  excludes test modules from the denominator. The 2026-10-02 integrated run
  measured 69.32% over 44,496 statements (3,305 passed, 3 skipped). A floor
  above that measurement would fail the current baseline; one immediately
  below it could flap across CI environments. The 65% floor catches
  sufficiently large losses in exercised code, but does not prove that every
  subsystem or failure path is covered. The floor may ratchet upward; lowering
  it to accept a regression is not permitted.

- **The preceding Chrome production build was 269,269 bytes gzipped**, measured
  2026-10-02 from `plasmo build` (838,326 bytes uncompressed, 17 files). The
  equivalent Edge build is 269,271 bytes gzipped. Both remain within the
  existing 280,000-byte gzip and 900,000-byte uncompressed gates. Prior
  documentation quoted a "< 250 KB" target, calibrated in 2026-08 against a
  bundle that has since grown; the guard now measures the complete build and
  discovers all shipping source directories, including extracted utilities.

- **Release targets and local evidence differ.** The configured release matrix
  targets macOS 13 or later, arm64 and x86_64 artifacts. The October review
  built and verified arm64 only; it establishes no Intel artifact or hardware
  result.
  MediaPipe no longer publishes current Intel wheels, so the locked Intel
  artifact uses the last compatible 0.10.21/NumPy 1.x branch. Architecture
  parity is therefore test-gated, not assumed. Public promotion requires the
  [release assurance protocol](release/README.md#required-real-device-release-candidate-pass):
  both architectures need automated evidence; self-attested promotion requires
  at least one physical architecture record and labels any other architecture
  CI-verified only, while independent review requires both. MediaPipe's
  capped OpenCV contrib dependency is the sole `cv2` provider so wheel install
  order cannot silently change the runtime OpenCV major. That final Intel
  MediaPipe wheel also constrains Protobuf to a release affected by
  `PYSEC-2026-1805`; Cortex does not expose the affected Protobuf JSON parser,
  and a real-model smoke plus an Intel-only audit exception constrain the risk.
  The exception is re-reviewed on a dated schedule (currently through
  2026-10-15) and is never extended silently; Intel release support must
  change or adopt a compatible patched backend before it lapses.
- **Dependency audit results are scoped snapshots.** The October local arm64
  Python and VS Code audits reported no known findings. The browser build
  chain retains eleven reviewed exceptions (nine low/moderate, two high
  build-tool advisories with no patched release) in
  [`node-audit-exceptions.json`](../cortex/security/node-audit-exceptions.json),
  with advisory, path, mitigation, and expiry checks. The Intel-only Protobuf
  exception above is separate and high severity. A passing policy gate is not
  a claim that every architecture or dependency is vulnerability-free.
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

The 2026-10-02 ad-hoc arm64 DMG passed mounted bundle/signature/layout scans,
native-host and frozen-resource checks, and full hardware-disabled startup and
SIGTERM shutdown. A separate two-launch check reused the same isolated
HOME/storage and ephemeral ports; both runs became healthy/ready, exited with
code 0, and released their listeners. The second retained the first run's
storage and received a new boot ID. See the
[implementation record](implementation-2026-10.md#packaged-artifact-and-completion)
for exact artifacts and evidence. The sandboxed probes did not access physical
cameras or the user's real profile, grant TCC permissions, validate VoiceOver,
establish reference-sensor accuracy, or notarize/publish a release.

The [0.5.1 validation record](release/0.5.1-validation.md) reports the latest
source and packaging results; the measurements above retain their original
dates. Desktop live signals now share the browser's ten-second expiry, and
time-scaled smoothing prevents cadence changes from changing the effective
time constant. Neither correction validates the underlying state labels.

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
