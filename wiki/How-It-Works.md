# How Cortex works

## 1. Observe locally

Camera and activity collectors emit typed observation envelopes. Every
scheduled interval is `valid`, `missing`, `rejected`, or `stale`, with a
reason, quality, source identity, algorithm version, wall time, same-boot
monotonic time, and sequence. Missing intervals do not preserve a stale value.

- Camera frames, landmarks, and color traces stay in process memory.
- Input telemetry records timing/rate aggregates, never key text.
- Browser page content requires an explicit origin grant. The editor's local
  visible-code sharing defaults on and can be disabled with
  `cortex.shareEditorContent`; external disclosure is separately opt-in.
- Camera denial or poor quality degrades to unavailable; it is not fabricated.

Input listeners and foreground-window tracking report actual availability.
An empty input buffer counts as inactivity only while that source is observable.
Denied permissions, a stopped listener or unsupported acquisition cannot become
fresh zero-valued behavior. A source's exposure window restarts when availability
changes. Typing variance needs at least two observed regular-key intervals;
backspaces do not satisfy that requirement, while measured zero variance is valid.

## 2. Derive bounded signals

The pulse pipeline takes the mean color of the whole facial skin (face
outline minus eyes, brows and lips), applies the POS projection over a 20 s
window with measured sample time and quality/motion/face-loss gates, and
publishes a rate only when the harmonic signal-to-noise ratio shows a cardiac
line. Beats are timed on a narrow band around that rate. SDNN comes from the
accepted beat intervals over the trailing three minutes. Breathing rate is
published only when two of three camera channels agree. All of these are
experimental; heart rate and SDNN were checked against ECG on one participant,
and breathing rate has only simulated truth behind it. RMSSD, LF/HF and the
other beat-to-beat measures did not qualify and stay research-only; “screen
apnea” detection and the old stress integral are not provided. Head/neck
measurements are comfort proxies only.

## 3. Estimate a support hypothesis

The production model (`3.0.0`, feature schema `support-features-v3.0.0`) is
deterministic and has no fitted training data. It combines
mouse, keyboard, tab, reread, and focus-transition features with fixed
denominators and explicit evidence coverage. Insufficient, weak, warming, or
ambiguous evidence returns `unknown`. Camera heart rate, SDNN, breathing rate,
blink rate and eye-closure share are read against the person's own reference
(from calibration, or learned over the first two minutes of a session) and
mixed only into a state whose behavioral rule is already eligible, with a
weight of at most 0.35. They can strengthen or temper a reading; they cannot
create one. With no camera, no face or no reference the score is exactly the
behavior-only score. Scores are not probabilities and state names are not
diagnoses.

Scores are smoothed with a per-sample exponential moving average, then entry/exit
hysteresis and elapsed candidate dwell. Duplicate or regressed observation times
are ignored. A gap greater than three seconds resets accumulated scores, the
candidate and committed-label dwell; time with no observations cannot count as
sustained evidence. This continuity bound comes from the nominal 500 ms pipeline
cadence, not a validated cognitive timescale.

The generated model identity hashes twenty-one implementation components,
including acquisition, fusion, smoothing, the physiology evidence and reference,
and the camera estimators. It does not cover every helper or
the entire app. Personal baselines, settings and the application revision remain
part of the context required to reproduce a result.

## 4. Apply eligibility and policy gates

Dwell/hysteresis, evidence coverage, receptivity, cooldown, quiet mode,
dismissal burden, and user settings determine whether Cortex may present a
suggestion. Production policy is deterministic and does not learn online. A
separately consented, fixed two-arm micro-randomized path exists for research;
its presence is not evidence of efficacy.

The reading check-in uses sustained browser/input behavior with a conservative
120-second dwell. Blink rate does not set its timing, although the
`under_engaged` estimate it requires may be corroborated by blink rate and
eye-closure share. Goal check-ins
use available current editor-file or browser-title context and abstain when it
is missing; changing the goal clears the prior file history. These are heuristic
prompts, not measurements of mind-wandering or a biological need for a break.

A strain load accumulates while the physiology points to strain against the
person's own reference and drains during calm. Crossing its threshold suggests
one three-minute breathing break through the same gates as every other
suggestion; turn it off with `intervention.enable_strain_break_suggestions`.
The load is a pacing aid, not a measure of stress, fatigue or health.

## 5. Build a proposal

The default local rule planner makes no network request. External planning is
off unless the user enables `external_redacted`, selects each source, reviews
the exact minimized/redacted payload and prompt, and confirms a short-lived
handle once. The handle is burned before provider I/O. Model output is parsed
as untrusted proposal data.

## 6. Preserve user authority

Presentation is side-effect free. If optional mutation is enabled, the flow is:

```text
PROPOSED → DELIVERED → AUTHORIZED → APPLYING → APPLIED | PARTIAL | FAILED
                                              → RESTORING → RESTORED | RESTORE_FAILED
                                                                     → ABANDONED
```

Verification is per receipt rather than a lifecycle state: an action counts
as applied only when the bound client returns a receipt whose postcondition
fingerprint the daemon can check.

Authority binds the exact manifest digest, effect capability, target, consent
revision, expiry, and one-time nonce. Each effect returns a minimal typed
receipt. Duplicate/reordered/replayed commands are idempotent; partial failure
is compensated or reported truthfully. A downgrade creates a new lower-
authority proposal and never executes the original.

## 7. Persist and recover

A single-owner SQLite database with checksummed migrations and full
synchronization stores intervention and policy lifecycles atomically. On
restart Cortex reconciles unresolved intent/receipts before claiming success.
Redis/in-memory and legacy JSON are compatibility or diagnostic surfaces only.

## 8. Present current evidence honestly

The browser popup and Pulse Room share a ten-second state-continuity deadline.
Disconnect clears the current sample immediately; a connected stream that stops
updating becomes “Waiting for fresh signals.” Cache hydration preserves sample
age, camera availability and the reason pulse is unavailable. Polling does not
renew an old estimate. Invalid, unavailable or stale pulse, HRV, breathing and
blink values are hidden.

This UI deadline is separate from the three-second inference gap limit and from
the physiology pipeline's measurement-quality gates. A fresh browser message
does not establish pulse accuracy. Pulse Room artwork follows estimated pulse
cadence for decoration; it is not an ECG, beat-by-beat recording or breathing
pacer. Users can pause that motion without stopping acquisition.

Detailed evidence: [model cards](https://github.com/StevenWang-CY/cortex/tree/main/docs/model-cards)
(`deterministic-support-v3.md` is current),
[research records](https://github.com/StevenWang-CY/cortex/tree/main/docs/research)
(`physiology-qualification.md`),
[architecture](https://github.com/StevenWang-CY/cortex/blob/main/cortex/docs/architecture.md), [data flow](https://github.com/StevenWang-CY/cortex/blob/main/docs/data-flow.md), and
[limitations](https://github.com/StevenWang-CY/cortex/blob/main/docs/limitations.md).
