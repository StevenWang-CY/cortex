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
capture / telemetry / client context
                 |
                 v
      versioned observations + quality
                 |
                 v
 evidence-aware deterministic support estimates
                 |
                 v
 deterministic eligibility / safety gates
                 |
                 v
 schema-validated LLM or rule-based plan
                 |
                 v
         non-mutating proposal
```

The complete target design, migration order, and definition of done are in
[`../../IMPLEMENTATION.md`](../../IMPLEMENTATION.md).

## Current layers

1. **Observation.** `capture_service`, `physio_engine`,
   `kinematics_engine`, and `telemetry_engine` create local signals.
   Quality-gated camera pulse is experimental. Unsupported HRV, respiration,
   breath-pause, and stress-integral fields are forced unavailable before
   state scoring and publication.
2. **Support inference.** `state_engine` currently applies deterministic
   fixed-denominator behavior rules, explicit abstention, smoothing, and
   hysteresis. Outputs are heuristic evidence strengths, not probabilities or
   diagnostic states. Camera signals are diagnostic-only for this decision.
3. **Eligibility and context.** trigger gates decide whether to assemble
   browser/editor/terminal context. Missing evidence should suppress or
   abstain; named validity and coverage gates enforce that invariant.
4. **Planning.** `llm_engine` calls a configured Anthropic transport or a
   deterministic fallback, then parses and validates the returned plan.
5. **Presentation/authorized execution.** `intervention_engine` and the clients
   present proposals. The shipping default is non-mutating. Any enabled
   capability requires a manifest-bound authorization, idempotent command,
   durable effect receipt, verification result, and restore lifecycle.

## Trust boundaries

- HTTP mutation routes require the canonical bearer capability token.
- WebSocket connections must authenticate before identifying or exchanging
  application messages.
- Native messaging uses a generated request/response union and the canonical
  `auth_token` response field.
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

## Evidence boundary

The following compatibility fields or bounded research surfaces remain for
decoded historical data or explicitly gated analysis:

- expanded camera-derived HRV and respiration algorithms;
- legacy stress-integral/report fields, emitted as unavailable or neutral;
- legacy AMIP diagnostic replay (the retired policy implementations and
  unregistered per-user classifier have been deleted);
- action adapters and undo helpers.

Their existence is not evidence that their output is valid or release-ready.
The former stress-integral implementation, physiology-triggered break
controller, adaptive bandit/AMIP implementations, and unregistered classifier
have been deleted. Compatibility routes and fields cannot restore them.
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

Compatibility facades remain intentionally thin and are removed only after
transport parity coverage proves no external caller relies on them.

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
- browser native messaging launches the foreground daemon through Terminal.app
  to obtain the correct TCC lineage;
- shutdown releases camera resources and uses multiple bounded termination
  mechanisms;
- ad-hoc application signing does not enable hardened runtime.

These constraints require installed-artifact verification in addition to unit
tests.

## Repository map

- `cortex/application`: kernel, typed commands/events, coordinators, task owner
- `cortex/libs/schemas`: canonical process-boundary models
- `cortex/libs/store`: current Redis/in-memory abstraction
- `cortex/services/capture_service`: camera, tracking, ROI, and quality
- `cortex/services/physio_engine`: experimental rPPG pipeline
- `cortex/services/kinematics_engine`: blink and head/neck proxies
- `cortex/services/telemetry_engine`: input/window/focus telemetry
- `cortex/services/state_engine`: legacy inference and detector modules
- `cortex/services/context_engine`: workspace context adapters
- `cortex/services/llm_engine`: planning transports and validation
- `cortex/services/intervention_engine`: proposal and contained action code
- `cortex/services/api_gateway`: HTTP/WebSocket boundary
- `cortex/apps`: desktop, browser, and VS Code clients
