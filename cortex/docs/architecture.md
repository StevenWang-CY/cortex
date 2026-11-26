# Architecture

## Product boundary

Cortex is a local modular monolith composed through `cortex/application/`.
The application kernel binds typed commands and isolated events; bounded
coordinators own sensing, inference, intervention, policy, context, and
lifecycle tasks. `cortex/services/runtime_daemon.py` remains the compatibility
composition facade, not the owner of domain algorithms. Cortex exposes an
authenticated FastAPI service on port 9472 and WebSocket service on port 9473,
and can run inside the PySide6 macOS shell. Browser and VS Code extensions are
untrusted clients at the process boundary.

The shipping execution mode is `suggest_only`. Sensing and planning can
produce a proposal; receipt of a proposal is never authority to mutate a tab,
editor, window, or file.

```text
input/window telemetry -> availability + aggregation
                                   |
                                   v
              abstaining deterministic support estimates <-- camera physiology:
                                   |                         per-metric gates,
                                   |                         personal reference,
                                   |                         eligible rules only
                                   v
                  eligibility / interruption gates
                                   |
local task context ----------------+
                                   v
        local plan or explicitly previewed external request
                                   |
                                   v
                        non-mutating proposal
```

The [current implementation record](../../docs/implementation-2026-10.md)
describes the reviewed changes and verification boundaries. The earlier
[implementation history](../../IMPLEMENTATION.md) records the broader migration.

## Current layers

1. **Observation.** `capture_service`, `physio_engine`,
   `kinematics_engine`, and `telemetry_engine` create local signals.
   Camera heart rate, SDNN and breathing rate are experimental and each is
   published only when its own gate passes (a confirmed cardiac line, enough
   accepted beats, two agreeing breathing channels). RMSSD and the other
   beat-to-beat measures are computed for research and never read by a rule,
   suggestion or screen. Breath-pause detection is not provided.
2. **Support inference.** `state_engine` currently applies deterministic
   fixed-denominator behavior rules, explicit abstention, smoothing, and
   hysteresis. Outputs are heuristic evidence strengths, not probabilities or
   diagnostic states. The registered model is `deterministic-support@3.0.0`
   with `support-features-v3.0.0`. `physiology_baseline.py` keeps a personal
   reference per metric, and `physiology_evidence.py` mixes the camera
   physiology block into a state only where that state's behavioral rule is
   eligible, with weight `state.physiology_weight` (default 0.35); camera
   evidence alone cannot produce an estimate
   ([ADR 0008](../../docs/adr/0008-qualified-camera-physiology.md)).
   `strain_load.py` integrates the same block into the load behind the
   breathing-break suggestion. Reading check-ins read no camera value
   directly; the `under_engaged` estimate they require may be corroborated by
   blink rate and eye-closure share.
3. **Eligibility and context.** trigger gates decide whether to assemble
   browser/editor context. The terminal parser has no shipping live producer.
   Missing evidence should suppress or abstain; named validity and coverage
   gates enforce that invariant.
4. **Planning.** the default `no_llm` mode uses a local deterministic planner.
   Explicit external mode requires an exact redacted preview and one-time
   confirmation before the configured Anthropic transport is called. Returned
   plans are parsed and validated locally; provider failure has a deterministic
   fallback.
5. **Presentation/authorized execution.** `intervention_engine` and the clients
   present proposals. The shipping default is non-mutating. Any enabled
   capability requires a manifest-bound authorization, idempotent command,
   durable effect receipt, verification result, and restore lifecycle.

## Trust boundaries

- HTTP mutation routes require the canonical bearer capability token.
- WebSocket connections must authenticate before identifying or exchanging
  application messages.
- Native messaging uses a generated request/response union and the canonical
  `auth_token` response field. Packaged manifests point only to the signed
  self-contained helper, carry browser-local exact origins, and are accepted
  only after a real framed status exchange.
- LLM output is untrusted data. Schema validation is necessary but does not
  confer execution authority.
- Client messages are untrusted and must be schema-, authorization-, replay-,
  and target-validated.
- Workspace excerpts sent to a cloud LLM can be sensitive. External planning
  is therefore network-off by default and passes through the WP-9 field
  catalog, per-source selection, exact redacted preview, one-time confirmation,
  and provider-retention disclosure. See [`privacy.md`](privacy.md).

## Time and schema contracts

`cortex/application/clock.py` defines the injected clock port. New public
events use:

- `observed_at_unix_ms` for display, persistence, and cross-process
  correlation;
- `observed_at_mono_ns` for duration and in-process ordering;
- `boot_id` to scope monotonic values to a process lifetime;
- a sequence number for stream ordering.

Legacy bare `timestamp` fields remain compatibility-only during migration.
Pydantic schemas in `cortex/libs/schemas` are canonical; generated
TypeScript is checked in CI. Unknown major protocol versions are rejected.

Input availability is tracked per device. A newly timestamped empty buffer
does not establish inactivity: mouse observations need a live listener, and
keyboard observations additionally need the current permission/trust checks.
Inactivity requires both channels and is bounded by their continuous observed
exposure. Source loss or a sampling gap above 3 seconds restarts that exposure.
Typing variance requires two positive intervals between regular keypresses;
backspaces cannot satisfy this readiness condition.

The smoother scales EMA retention by actual elapsed time, preserving the
nominal 500 ms response. Duplicate or regressed timestamps do not update it;
non-finite times are rejected and a changed clock domain starts a new history.
Fusion counts only ordered source updates and restarts telemetry warm-up after
a gap above 3 seconds. A gap above 3 seconds in inference
clears accumulated scores, pending confirmation and the committed label before
fresh evidence begins another dwell. This continuity rule is an engineering
bound, not a claim about a person's cognitive timescale.

Foreground identity and transition history are separate. A live foreground
application does not disappear merely because it has not changed for 60 seconds.
Goal check-ins read the foreground editor's nested file metadata or active
browser title; unavailable foreground metadata causes abstention, and a new goal
clears the previous goal's file history. See the
[model card](../../docs/model-cards/deterministic-support-v3.md) for exact rules.

The generated implementation digest covers twenty-one components: schema,
acquisition, aggregation, fusion, scoring and smoothing, the physiology
evidence and personal reference, and the camera physiology estimators. It does
not hash the whole daemon, intervention helpers, configuration or calibration.
Reproducing a proposal requires those inputs and the application revision too.

## Evidence boundary

The following compatibility fields or bounded research surfaces remain for
decoded historical data or explicitly gated analysis:

- camera-derived variability measures that did not qualify (RMSSD, pNN50,
  SD1/SD2, LF/HF, sample entropy), computed for research only;
- `GET /api/stress-integral`, which always reports unavailable;
- legacy AMIP diagnostic replay (the retired policy implementations and
  unregistered per-user classifier have been deleted);
- action adapters and undo helpers.

Their existence is not evidence that their output is valid or release-ready.
The former stress-integral implementation, physiology-triggered break
controller, adaptive bandit/AMIP implementations, and unregistered classifier
have been deleted. Compatibility routes and fields cannot restore them. The
0.6.0 strain load is a separate, specified implementation: it reuses the
`stress_integral` field name and the `BREAK_RECOMMENDATION` message with
`basis: physiological_strain`, and a message with the legacy basis is still
ignored.
Production policy selection is deterministic and non-learning. Every v2 action
or no-action decision has one durable outcome window and at most one versioned
reward. Separately consented research mode is restricted to a fixed two-arm
micro-randomized epoch whose specification checksum is bound to every
assignment. See
[`research/policy-evaluation-protocol.md`](research/policy-evaluation-protocol.md).
Legacy “causal report” files are renamed diagnostic summaries and cannot enter
the research export.

## Module ownership

The implemented ownership boundary is:

- **domain:** pure value objects, signal/status semantics, consent outcomes,
  manifests, receipts, and policies;
- **application:** kernel and bounded coordinators using injected clock, event
  store, context broker, command/event ports, and named task ownership;
- **infrastructure:** camera, Redis/SQLite, Anthropic, OS, browser, and editor
  adapters;
- **interfaces:** FastAPI, WebSocket, native messaging, PySide6, browser, and
  VS Code transport adapters;
- **composition:** a compatibility facade that constructs dependencies while
  the kernel owns structured background tasks.

The runtime facade still contains substantial composition and orchestration.
The coordinator/port boundary does not imply a completed rewrite of that file;
further extraction requires demonstrated value and transport-parity coverage.

`TaskSupervisor` strongly owns every registered live task. Completion removes
the Task reference and retains value-only completion/failure histories capped
at 128 records each. Cancellation waits have deadlines; a child that has not
finished remains owned, and a coordinator cannot start duplicate loops while
its old children remain alive. A cancellation request or timeout is not proof
that cleanup completed. Expected graceful completion still reports real errors.

## Persistence status

The authoritative durable path is a single-owner SQLite store using rollback
journaling, `synchronous=FULL`, checksummed migrations, verified pre-migration
backups, integrity checks, bounded analytics backpressure, retention, scoped
export/delete, and restart recovery. It transactionally stores intervention
authorizations/receipts/restores and the policy decision→delivery→outcome→reward
lifecycle. Legacy JSON/JSONL artifacts are imported only through checksummed,
reversible migration or retained as diagnostics. Keychain secrets remain in
Keychain. Redis/in-memory storage is compatibility/ephemeral state, not the
authority for v2 intervention or policy lifecycles.

## macOS lifecycle

Camera and shutdown behavior are part of the architecture:

- smart selection re-enumerates AVFoundation devices after open to avoid
  Continuity Camera index changes, using the explicit `isContinuityCamera`
  property rather than an LLM or localized name when the property is available;
- the raw AVFoundation unique ID is reduced to a one-way stable device key, so
  numeric reordering cannot alias calibration while the platform identifier is
  never logged or persisted;
- macOS discovery fails closed rather than blind-probing indices, and every
  configured or automatic handle must pass live post-warm-up identity checks;
- warm-up reads are retried;
- packaged browser native messaging uses a self-contained signed helper that
  opens `/Applications/Cortex.app`, preserving the GUI bundle's TCC identity;
  source-development hosts launch the foreground daemon through Terminal.app
  to obtain a development-safe camera lineage;
- shutdown releases camera resources and uses bounded graceful/termination
  stages; the launcher reports success only after the endpoint and identified
  daemon processes have exited, and native messaging preserves stop errors;
- ad-hoc application signing does not enable hardened runtime.

These constraints require installed-artifact verification in addition to unit
tests.

## Repository map

- `cortex/application`: kernel, typed commands/events, coordinators, task owner
- `cortex/libs/schemas`: canonical process-boundary models
- `cortex/libs/store`: current Redis/in-memory abstraction
- `cortex/services/capture_service`: camera, tracking, ROI, and quality
- `cortex/services/physio_engine`: experimental rPPG pulse, variability and breathing pipeline
- `cortex/services/kinematics_engine`: blink and head/neck proxies
- `cortex/services/telemetry_engine`: input/window/focus telemetry
- `cortex/services/state_engine`: registered support inference, personal physiology reference, strain load, smoothing and check-in eligibility
- `cortex/services/eval`: synthetic physiology truth, dataset replay and the qualification harness behind `cortex.scripts.qualify_physiology`
- `cortex/services/context_engine`: workspace context adapters
- `cortex/services/llm_engine`: planning transports and validation
- `cortex/services/intervention_engine`: proposal and contained action code
- `cortex/services/api_gateway`: HTTP/WebSocket boundary
- `cortex/apps`: desktop, browser, and VS Code clients
