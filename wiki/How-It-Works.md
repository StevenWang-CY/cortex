# How Cortex works

## 1. Observe locally

Camera and activity collectors emit typed observation envelopes. Every
scheduled interval is `valid`, `missing`, `rejected`, or `stale`, with a
reason, quality, source identity, algorithm version, wall time, same-boot
monotonic time, and sequence. Missing intervals do not preserve a stale value.

- Camera frames, landmarks, and color traces stay in process memory.
- Input telemetry records timing/rate aggregates, never key text.
- Browser/editor content is a separate, explicit context permission.
- Camera denial or poor quality degrades to unavailable; it is not fabricated.

## 2. Derive bounded signals

The v2 pulse pipeline uses measured sample time, quality/motion/face-loss
gates, and a unique chronological beat timeline. Pulse is experimental. HRV,
LF/HF, nonlinear HRV, respiration, “screen apnea,” and the old stress integral
are unavailable in product mode. Blink and head/neck measurements are comfort
proxies only.

## 3. Estimate a support hypothesis

The production model is deterministic and has no training data. It combines
mouse, keyboard, tab, reread, and focus-transition features with fixed
denominators and explicit evidence coverage. Insufficient, weak, warming, or
ambiguous evidence returns `unknown`. Camera values cannot change a production
support score. Scores are not probabilities and state names are not diagnoses.

## 4. Apply eligibility and policy gates

Dwell/hysteresis, evidence coverage, receptivity, cooldown, quiet mode,
dismissal burden, and user settings determine whether Cortex may present a
suggestion. Production policy is deterministic and does not learn online. A
separately consented, fixed two-arm micro-randomized path exists for research;
its presence is not evidence of efficacy.

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

Detailed evidence: [model card](https://github.com/StevenWang-CY/cortex/blob/main/docs/model-cards/deterministic-support-v2.md),
[architecture](https://github.com/StevenWang-CY/cortex/blob/main/cortex/docs/architecture.md), [data flow](https://github.com/StevenWang-CY/cortex/blob/main/docs/data-flow.md), and
[limitations](https://github.com/StevenWang-CY/cortex/blob/main/docs/limitations.md).
