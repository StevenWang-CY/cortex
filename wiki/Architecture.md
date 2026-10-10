# Architecture

Cortex is a local modular monolith. Splitting camera ownership, authorization,
and recovery into network microservices would add failure and privacy surfaces
without helping a single-user macOS app.

## Application ownership

`cortex/application/` contains the typed kernel, command/event ports, bounded
coordinators, and named task owner. Sensing, inference, intervention, policy,
context, and lifecycle coordinators organize use cases. The runtime daemon
remains the composition and orchestration boundary, with substantial production
wiring; inference algorithms live in the service/domain modules. Browser and
desktop adapters present state and submit commands without independently
granting intervention authority.

The task supervisor strongly owns active tasks. Finished and failed task
histories are bounded to 128 value-only records each, so completed coroutine
results and exceptions do not accumulate for an entire session. Cancellation
uses a bounded wait and retains ownership of tasks that have not finished.

```text
camera/activity/client observations
              ↓
quality + missingness + dual-clock envelopes
              ↓
deterministic evidence-aware support estimate / unknown
              ↓
eligibility, receptivity, cooldown, deterministic policy
              ↓
local plan or preview-confirmed external plan
              ↓
side-effect-free proposal
              ↓ optional explicit authority
manifest → authorization → apply → receipt → verify → restore
```

## Boundaries

- **Domain:** immutable value objects, signal validity, support estimates,
  consent outcomes, manifests, receipts, and policies.
- **Application:** kernel, coordinators, injected dual clock, event store,
  privacy broker, command/event ports, and structured background tasks.
- **Infrastructure:** camera, SQLite, provider, OS, browser, editor, Keychain,
  and compatibility cache adapters.
- **Interfaces:** authenticated FastAPI/WebSocket/native messaging, PySide6,
  Chrome/Edge MV3, and VS Code.
- **Composition:** constructs dependencies and compatibility facades without
  owning domain decisions.

## Service topology

The daemon is a modular process; these service packages describe responsibilities,
not separately deployed network services:

| Package | Responsibility |
| --- | --- |
| `capture_service` | Webcam ownership and raw-frame acquisition |
| `physio_engine` | Experimental camera-derived physiology and signal quality |
| `kinematics_engine` | Face and motion observations |
| `telemetry_engine` | Aggregate input/window telemetry features |
| `state_engine` | Evidence-aware state estimation and trigger policy |
| `context_engine` | Privacy-filtered application context |
| `eval` | Offline replay, regression, and research evaluation |
| `llm_engine` | Optional untrusted planning providers |
| `intervention_engine` | Proposal construction and bounded orchestration |
| `consent` | Consent and authority decisions |
| `session_report` | Local session summaries and persistence adapters |
| `api_gateway` | Authenticated HTTP and WebSocket interfaces |
| `launcher` | Process lifecycle coordination |
| `janitor` | Retention and cleanup work |
| `activity_tracker` | Coarse foreground activity observations |

Default loopback interfaces are intentionally distinct: launcher HTTP on
`9471`, authenticated FastAPI HTTP on `9472`, and authenticated WebSocket on
`9473`. These ports are local transports, not independent microservices.

| Host | Lifecycle and communication |
| --- | --- |
| Installed `Cortex.app` | PySide6 controller owns the daemon in-process and exposes HTTP/WS for integrations. |
| Source `main --in-process` | Uses the same controller/daemon ownership model as the app. |
| Source `main` without the flag | Desktop WebSocket client of a separately running daemon; it does not start a second daemon. |
| Chrome/Edge and VS Code | Separate authenticated clients. The browser worker owns presentation/cache state, not inference policy. |
| Optional launcher/native host | Process launch/stop helpers; packaged `CortexNativeHost` is a separate executable, not another sensing daemon. |

Closing the dashboard leaves the session running. Ending a focus session clears
the session timer without quitting. Explicit Quit coordinates shutdown in either
desktop host; the browser's Stop operation also verifies the daemon's exit.

## Trust and authority

Loopback clients are untrusted. HTTP mutation routes require a bearer
capability; WebSocket clients authenticate before identification; native
messages have a generated discriminated union and size limit. LLM output is
untrusted proposal data. No transport message, model response, policy arm, or
consent downgrade confers workspace authority.

An optional effect requires an exact manifest digest, capability, target,
consent revision, expiry, and one-time nonce. SQLite commits intent before
apply, records typed receipts/inverses, verifies postconditions, and recovers
unresolved work after restart. Duplicate/replayed commands are idempotent.

## Time and storage

Public time is explicit: UNIX milliseconds for display/persistence and
monotonic nanoseconds scoped by `boot_id` for same-process duration/order.
Every sensor interval is a typed observation, including missing/rejected/stale
intervals.

Behavioral acquisition also reports whether keyboard, mouse and foreground-window
sources are actually observable. Missing listeners or permissions cannot create
zero-valued evidence. Smoothing ignores nonincreasing timestamps and resets its
scores/candidate/committed dwell after an evidence gap above three seconds.
Browser state uses a separate ten-second presentation deadline. Cached age
travels as a duration, avoiding comparisons between worker and page monotonic
clocks; polling the cache cannot make an old sample new.

SQLite is authoritative with single-owner writes, rollback journaling,
`synchronous=FULL`, checksummed migrations, verified backups, integrity checks,
bounded analytics backpressure, retention, export/delete, and recovery.
Legacy JSON/JSONL is migrated or diagnostic. Redis/in-memory is not authority.

## Evidence boundary

Production support inference is a deterministic telemetry model with
abstention and no learned classifier. Retired AMIP/contextual-bandit
implementations were deleted; only read-only legacy diagnostics remain.
Research MRT/OPE is separately consented and fixed by a checksummed study
specification. Camera physiology cannot influence production support scores.
Optional planning transports are AWS Bedrock, GCP Vertex, and the direct
Anthropic API; all are BYOK, network-off by default, and remain outside the
authority boundary.

The current model is `2.5.0` and its feature schema is
`support-features-v2.2.0`. Its generated eleven-component identity covers
telemetry schema/acquisition/aggregation, focus graph, feature fusion, smoothing,
feature catalog, registry, scorer and inference wrapper. It is not a whole-app
hash: reproducing proposals also needs the application revision, configuration,
personal calibration and policy context. `safety_null` disables inference as a
fail-closed rollback.

## Release topology

PyInstaller bundles the in-process daemon, desktop shell, and a separately
signed self-contained `CortexNativeHost` stdin/stdout executable. Architecture-
specific DMGs include Chrome/Edge builds, VSIX, native-host
installer logic, migrations, model/resource files, and a secret-free
configuration.
Release tags require locked inputs, Developer ID hardened-runtime signing,
Apple notarization/stapling, mounted-artifact smoke, SBOMs, checksums, and
GitHub attestations. The tag commit must be reachable from `main`; promotion
verifies provenance for both DMGs, both evidence bundles, and both standalone
architecture checksum manifests.

The October review verified a local ad-hoc arm64 app/DMG and isolated restart
probe. That evidence does not establish an Intel build, Developer ID
notarization, public publication, or physical camera/TCC validation. The current
checkout's `docs/implementation-2026-10.md` records the exact completion scope.

Canonical detail: [cortex/docs/architecture.md](https://github.com/StevenWang-CY/cortex/blob/main/cortex/docs/architecture.md),
[ADRs](https://github.com/StevenWang-CY/cortex/blob/main/docs/adr/README.md), [data flow](https://github.com/StevenWang-CY/cortex/blob/main/docs/data-flow.md), and
[implementation history](https://github.com/StevenWang-CY/cortex/blob/main/IMPLEMENTATION.md).
