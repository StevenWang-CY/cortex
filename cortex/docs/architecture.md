# Architecture

## Product boundary

Cortex is currently a modular monolith supervised by
`cortex/services/runtime_daemon.py`. It exposes an authenticated FastAPI
service on port 9472 and WebSocket service on port 9473, and can run inside the
PySide6 macOS shell. Browser and VS Code extensions are untrusted clients at
the process boundary.

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
- Workspace excerpts sent to a cloud LLM can be sensitive. Until the WP-9
  privacy broker makes every category previewable and controllable, cloud
  planning remains an explicit alpha-risk boundary.

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

The following modules remain in the tree for compatibility or gated research:

- expanded camera-derived HRV and respiration algorithms;
- stress integral and physiology-triggered break detectors;
- optional per-user ML classifier;
- retired AMIP/contextual-bandit implementations and legacy diagnostic replay;
- action adapters and undo helpers.

Their existence is not evidence that their output is valid or release-ready.
The classifier and stress-integral modules are not exported, instantiated, or
registered on the production daemon path.
Production policy selection is deterministic and non-learning. Every v2 action
or no-action decision has one durable outcome window and at most one versioned
reward. Separately consented research mode is restricted to a fixed two-arm
micro-randomized epoch whose specification checksum is bound to every
assignment. See
[`research/policy-evaluation-protocol.md`](research/policy-evaluation-protocol.md).
Legacy “causal report” files are renamed diagnostic summaries and cannot enter
the research export.

## Target module ownership

The incremental target is:

- **domain:** pure value objects, signal/status semantics, consent outcomes,
  manifests, receipts, and policies;
- **application:** use cases coordinated with injected clock, event store,
  context broker, and command/event ports;
- **infrastructure:** camera, Redis/SQLite, Anthropic, OS, browser, and editor
  adapters;
- **interfaces:** FastAPI, WebSocket, native messaging, PySide6, browser, and
  VS Code transport adapters;
- **composition:** one root that constructs dependencies and owns structured
  background tasks.

This is an incremental extraction, not a rewrite. Characterization tests must
precede moving each flow out of the current runtime daemon.

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
  Continuity Camera index changes;
- warm-up reads are retried;
- browser native messaging launches the foreground daemon through Terminal.app
  to obtain the correct TCC lineage;
- shutdown releases camera resources and uses multiple bounded termination
  mechanisms;
- ad-hoc application signing does not enable hardened runtime.

These constraints require installed-artifact verification in addition to unit
tests.

## Repository map

- `cortex/application`: application ports, beginning with the clock
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
