# API reference

The canonical, field-level HTTP/WebSocket/native contract is generated and
maintained in [cortex/docs/apis.md](cortex/docs/apis.md). Pydantic models under
`cortex/libs/schemas/` are the source of truth; browser and VS Code TypeScript
declarations are generated and rejected by CI when stale.

## Endpoints and authentication

| Service | Address | Boundary |
| --- | --- | --- |
| FastAPI | `http://127.0.0.1:9472` | Bearer capability required for sensitive/operational routes |
| WebSocket | `ws://127.0.0.1:9473` | First frame must authenticate; then identify client type |
| Launcher | `http://127.0.0.1:9471` | Separate authenticated, allowlisted launch surface |
| Native messaging | browser-managed stdio | 4-byte little-endian length + bounded generated JSON union |

The capability token is a local 256-bit secret stored mode `0600` under the
user's Cortex application-support directory. Loopback location is not treated
as authentication. Mutating HTTP requests carry `Authorization: Bearer …` and
may receive an `X-Cortex-Request-ID` for correlation.

## Core HTTP groups

- health/readiness and current state;
- configuration and consent;
- session history/trends/recap;
- calibration commit/reload;
- intervention proposal, exact authorization, receipt/restore state;
- policy diagnostics and separately governed research exports;
- privacy status, exact context preview, one-time confirm/cancel;
- scoped storage export/delete and authenticated shutdown.

Compatibility routes for HRV, respiration, stress integral, or biological
break status return explicit unavailable/legacy responses. They are not
evidence and cannot trigger a product action.

## WebSocket contract

Each `WSMessage` has a canonical `type`, payload, sequence, and explicit time
contract. The catalogue contains only types with a production producer or
consumer; a repository gate rejects dead reserved literals. Unknown types are
rejected at schema construction, and clients log defensive unhandled cases.

High-level flows include:

```text
AUTH → IDENTIFY → state/proposal/settings traffic

INTERVENTION_PROPOSED/PRESENTED
  → INTERVENTION_AUTHORIZE
  → ACTION_DISPATCH
  → INTERVENTION_RECEIPT
  → INTERVENTION_TRANSACTION_STATE / RESTORE

CONTEXT_PREVIEW
  → exact local display
  → authenticated HTTP one-time confirmation
  → proposal only
```

Legacy proposal/trigger frames are presentation-only. `ACTION_DISPATCH` is not
accepted without the exact active authorization transaction.

## Privacy routes

| Method/path | Behavior |
| --- | --- |
| `GET /privacy/context/status` | Reports configured posture and live-preview count; no provider probe |
| `POST /privacy/context/preview/current` | Builds exact redacted preview from current snapshot; no network |
| `POST /privacy/context/preview` | Developer/API preview from supplied local snapshot; no network |
| `POST /privacy/context/confirm` | Requires exact handle and confirmation phrase; burns handle before send |
| `DELETE /privacy/context/preview/{id}` | Burns a preview without sending |

## Change protocol

When adding a boundary field or message:

1. edit the Pydantic source and the canonical `MessageType` catalogue;
2. implement both producer and consumer with auth/size/replay bounds;
3. run `python -m cortex.scripts.generate_ts_schemas`;
4. add Python↔TypeScript golden and dispatch tests;
5. run `make contracts`; update this page only when high-level behavior changes.

Do not hand-edit generated `.d.ts` files or `.plasmo/` output.
