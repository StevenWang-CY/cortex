# Engineering finding ledger

This is the tracked historical ledger referenced by the changelog and commit
history. It records what each `Fxx` remediation established; it is not a claim
that Cortex is clinically validated, independently security-audited, or
production-certified. Current product limitations and external gates are in
[limitations.md](../docs/limitations.md) and the
[release evidence guide](../docs/release/README.md).

Status meanings:

- **Closed:** the named code defect has a committed regression contract.
- **Superseded:** later architecture replaced the affected mechanism while
  preserving or strengthening the invariant.
- **External gate:** software support exists, but credentials, hardware,
  participant data, or independent review are still required.

| ID | Historical finding / invariant | Resolution evidence | Status |
| --- | --- | --- | --- |
| F01 | Capture stop could leak a camera on timeout | `5828fa7`; unconditional release and bounded stop tests | Closed |
| F02 | Session writes could be torn | `7169750`; atomic replace contract | Superseded by transactional SQLite |
| F03 | Background tasks lacked a drain boundary | `f0b95b0`; tracked task drain | Superseded by application task ownership |
| F04 | Settings payload serialization drifted | `b9b0f38`; typed serialization tests | Closed |
| F05 | Suggested effects lacked confirmation | `29346ed`; explicit confirmation | Superseded by manifest authorization |
| F06 | Overlay cleanup could retain UI effects | `fbe2494`; cleanup tests | Superseded by receipt/restore lifecycle |
| F07 | WebSocket shutdown lacked exact auth | `3268a04`; authenticated shutdown | Closed |
| F08 | Launcher/native auth contracts diverged | `65d110a`; canonical token flow | Closed |
| F09 | Workspace text could inject planner instructions | `e636588`; untrusted-context delimiting | Superseded by privacy broker |
| F10 | Planner actions were under-validated | `36cc15f`; deterministic action validation | Superseded by manifest validator |
| F11 | Capability tokens were over-broad | `2a02194`; scoped token handling | Closed |
| F12 | Launcher accepted unbounded commands | `ef65d88`; executable allowlist | Closed |
| F13 | Loopback routes lacked rate limits | `695a8f9`; bounded middleware | Closed |
| F14 | Native messages lacked size/schema bounds | `6fca20d`; generated schema and 64 KiB cap | Closed |
| F15 | Stream JSON failures were opaque | `64ad1f1`; typed decode errors | Closed |
| F16 | Active intervention correlation was inconsistent | `d5854b2`, `bd7f203`; durable IDs | Superseded by transaction ID |
| F17 | Reordered client frames could regress state | `71b94c1`; per-type sequence rejection | Closed |
| F18 | State frames lacked a stable envelope | `7ec42ca`; typed envelope | Superseded by dual-clock events |
| F19 | HTTP/WS/log correlation was incomplete | `6eca4c1`, `afa80a6`; end-to-end correlation IDs | Closed |
| F20 | Provider spend had no hard stop | `eb93fd6`; budget kill switch | Closed |
| F21 | Dismissal history was not durable | `7b71c1a`; persistence contract | Superseded by event store |
| F22 | Slow WebSocket clients could exhaust queues | `9fdc1ad`; bounded close policy | Closed |
| F23 | Pending futures survived disconnect | `9c889bf`; cancellation and drain | Closed |
| F24 | Consent updates were not serialized | `b0c03fa`; ordered consent state | Superseded by exact authorization |
| F25 | Trigger thresholds could chatter | `16c8bd5`; hysteresis tests | Closed |
| F26 | Quiet mode escalation was inconsistent | `0e6d2b4`; deterministic suppression | Closed |
| F27 | Circuit fallback could bypass policy | `43ca079`; bounded fallback | Superseded by deterministic product policy |
| F28 | Template cache ignored template version | `4d8663f`; versioned cache key | Closed |
| F29 | Truncation was invisible | `bbb75b8`; truncation telemetry | Superseded by preview disclosure |
| F30 | Cancellation could skip cost accounting | `83c3762`; shielded accounting | Closed |
| F31 | Rendering continued after cancellation | `698e431`; short-circuit contract | Closed |
| F32 | Reconnect retained stale sequence state | `2cceafd`; reset-on-connect | Closed |
| F33 | Goal changes could flood updates | `14d38e3`; debounce | Closed |
| F34 | Shutdown UI paths diverged | `70035ef`; shared shutdown behavior | Closed |
| F35 | Retention cleanup blocked async work | `7d8a955`; bounded asynchronous maintenance | Superseded by storage worker |
| F36 | Storage lacked an enforced budget | `3b64203`; capacity/retention limits | Superseded by SQLite maintenance |
| F37 | Native payloads were handwritten | `6fca20d`; generated contracts | Closed |
| F38 | Retired providers remained reachable | `462614c`; provider removal | Closed |
| F39 | Provider documentation advertised dead paths | `462614c`; docs aligned | Closed |
| F40 | Browser tests were not a hard TypeScript gate | `94de59e`; blocking test infrastructure | Closed |
| F41 | Evaluation regressions lacked a baseline | `4fc42fd`; deterministic harness | Closed |
| F42 | Python/TypeScript state schemas could drift | `9d5e11f`; generated schema | Closed |
| F43 | Intervention schemas could drift | `b053343`; generated schema | Closed |
| F44 | Native response schemas could drift | `f146c8f`; cross-language golden test | Closed |
| F45 | WebSocket type literals could drift | `1a6592d`; canonical catalogue and dead-message gate | Closed |
| F46 | Debug environment behavior was ambiguous | `79ca532`; explicit environment contract | Closed |
| F47 | UI palettes diverged between surfaces | `54db152`; generated semantic tokens | Closed |
| F48 | Breathing animation cadence diverged | `dcb53be`; shared cadence contract | Closed |
| F49 | Onboarding marker transitions raced | `86059ab`; atomic marker flow | Closed |
| F50 | Listener identity made disconnect unreliable | `9298f59`; stable listener ownership | Closed |
| F51 | Truncation was not visible in UI | `50cdf94`; user-facing status | Superseded by exact context preview |
| F52 | Tab close acknowledgements could duplicate | `eb92a1b`; idempotent acknowledgement | Superseded by action receipts |
| F53 | Settings sync failures were silent | `8b264fb`; failure surface | Closed |
| F54 | Connection status was binary and misleading | `df18591`; explicit connection states | Closed |
| F55 | Contrast/focus/accessibility lacked gates | `5c0d3e3`; contrast and keyboard tests | Closed |
| F56 | Signal handlers could outlive owners | `c95541e`; teardown ownership | Superseded by application kernel |
| F57 | Camera type selection depended on localized names and an optional LLM | v0.3.12; explicit AVFoundation Continuity/connection/type descriptors and synthetic contracts | Closed |
| F58 | Numeric camera reorder could alias calibration identity | v0.3.12; privacy-preserving stable device key plus mandatory post-open resolution | Closed |
| F59 | Blocking injected UI lacked complete focus ownership | v0.3.12; labelled timer, initial focus, Tab containment, Escape, and restoration tests | Closed |
| F60 | Browser materials ignored reduced transparency and activity semantics were incomplete | v0.3.12; opaque preference fallback and list/list-item/link contracts | Closed |
| F61 | Tag CI did not independently rerun every shipped client and audit policy | v0.3.12; browser/editor tag gates and main-ancestry proof | Closed |
| F62 | Standalone checksum manifests lacked independent provenance verification | v0.3.12; checksum attestations plus six-subject promotion verification | Closed |
| F63 | Desktop comments could invite reintroduction of the Qt-content-view launch crash | v0.3.12; tint-only native contract documented at implementation and UI boundaries | Closed |
| F64 | Continuous pacers ignored Reduce Motion/hidden-view lifecycle and the full-screen break withheld exit | v0.3.12; static preference paths, single cancelable editor loop, immediate Escape/control, and focused contracts | Closed |
| F65 | Lockout countdown interval targeted a stale DOM ID | v0.3.12; labelled timer target and executable fake-clock transition contract | Closed |
| F66 | Desktop overlay footer labels clipped at the shipped window width | v0.3.12; concise equal-width controls, accessible shortcut descriptions, offscreen text-fit contract, and visual reinspection | Closed |
| F67 | Camera-disabled packaged startup still enumerated AVFoundation metadata while constructing onboarding | v0.3.12; pre-import headless guard plus release-verifier rejection of discovery/open markers | Closed |

## Current cross-cutting work packages

The original ledger is preserved for traceability. The rigorous redesign in
[IMPLEMENTATION.md](../IMPLEMENTATION.md) groups later changes into WP0–WP11;
its exact commits and verification results are recorded in
[execution-log.md](execution-log.md). A historical “closed” status never
overrides a current limitation or an external validation gate.
