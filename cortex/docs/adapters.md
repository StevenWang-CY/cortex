# Writing Workspace Adapters

Workspace adapters gather local application context for Cortex's local planner
and explicitly previewed external requests. VS Code and Chrome/Edge have live
producers; the terminal adapter is a developer-fed parser only. This guide
explains how to add a context adapter while preserving the
[current implementation boundaries](../../docs/implementation-2026-10.md).

> **Authority status:** the shipping default is `suggest_only`. The
> manifest-bound authorization, durable receipt, verification and scoped
> restore path is implemented for the action catalog below. A proposal or
> legacy trigger is never permission to call `execute`. New effects must meet
> the same transaction and fault-recovery requirements before promotion.

## Formal Adapter Protocol

Cortex defines a formal `CortexAdapter` protocol in `cortex/libs/adapters/base.py` with properties `name` and `capabilities`, and async methods `execute`, `get_context`, and `health_check`. Action results flow back as `AdapterResult` (`success`, `data`, `reversible`, `reverse_action`, `error`). The `AdapterRegistry` in `cortex/libs/adapters/registry.py` handles discovery, capability querying, action routing, health checks, and plugin discovery via Python entry points. Legacy adapters that pre-date the protocol are auto-wrapped for backward compatibility.

## Adapter Interface

All adapters follow the same pattern:

```python
from cortex.libs.adapters.base import AdapterResult, CortexAdapter

class MyAdapter:
    @property
    def name(self) -> str:
        return "myapp"

    @property
    def capabilities(self) -> list[str]:
        return ["get_context"]

    async def execute(self, action: str, params: dict) -> AdapterResult:
        """Execute an action. Returns AdapterResult with success / reversible / error."""
        ...

    async def get_context(self) -> dict:
        """Gather context from the application. Returns {} if unavailable."""
        ...

    async def health_check(self) -> bool:
        """Return True if the adapter is healthy and connected."""
        ...
```

Key principles:

1. **Graceful fallback** — generic `CortexAdapter.get_context()` returns `{}`
   when unavailable; legacy typed readers may return `None` before wrapping
2. **Timeout** — all operations should have a configurable timeout (default 2s)
3. **No blocking** — all methods are async
4. **Fail closed** — reject unknown actions, modes, schema majors, targets,
   and missing authorization
5. **Receipt-driven restore** — an authorized mutation must return exact before
   state, postcondition fingerprint, inverse command, and idempotency key;
   an in-memory snapshot is not sufficient
6. **Proposal purity** — context/proposal handlers have no path to
   `execute`

## Existing Adapters

Production adapters are context readers. None of them executes anything: the
only executable effects in the product are the four catalogued actions of the
intervention transaction coordinator (`open_url`, `search_error`,
`highlight_tab`, `resume_last_active_file`), and those run through the
manifest-bound authorization flow in `intervention_engine/transaction.py`,
not through an adapter `execute` call.

### EditorAdapter (`context_engine/editor_adapter.py`)

Receives `CONTEXT_RESPONSE` payloads from the VS Code extension and keeps the
latest `EditorContext`:

- current local file path and language (the external broker reduces paths to basenames)
- visible line range and symbol at cursor
- diagnostics (errors, warnings), bounded
- optional visible code excerpt, only when the user has not disabled
  `cortex.shareEditorContent`; `file` documents are supported, and `untitled`
  documents require the separate `cortex.shareUntitledDocuments` opt-in

The extension never reads terminal output or shell history; the
`TerminalContext` slot in its payload is empty and the daemon treats it as
absent.

### BrowserAdapter (`context_engine/browser_adapter.py`)

Receives `CONTEXT_RESPONSE` payloads from the Chrome/Edge extension:

- active tab title, origin, and sanitized path
- active-page excerpt (bounded, and only for origins the user has explicitly
  allowed; never in incognito)
- open tabs with type classification (documentation, stackoverflow, search,
  code_host, social, other)

### TerminalAdapter (`context_engine/terminal_adapter.py`)

A local error-block detector (Python tracebacks, JS stack traces, shell
errors, Rust/Go panics). It has no live producer in the shipping product: the
editor extension does not forward terminal lines, so its context is empty
unless a developer feeds it lines in a test or a local experiment.

## Adding a New Adapter

### Step 1: Define the Context Schema

Add a Pydantic model in `libs/schemas/context.py`:

```python
class MyAppContext(BaseModel):
    """Context from MyApp."""

    active_document: str = Field(..., description="Current document name")
    word_count: int = Field(0, description="Word count of active document")
    # ... more fields
```

### Step 2: Implement the Adapter

Create `cortex/services/context_engine/my_adapter.py`:

```python
import asyncio
import json

from cortex.libs.adapters.base import AdapterResult
from cortex.libs.schemas.context import MyAppContext


class MyAppAdapter:
    def __init__(self, ws_send_fn=None, ws_receive_fn=None):
        self._ws_send = ws_send_fn
        self._ws_receive = ws_receive_fn
        self._available = False

    @property
    def name(self) -> str:
        return "myapp"

    @property
    def capabilities(self) -> list[str]:
        return ["get_context"]

    async def get_context(self) -> dict:
        if self._ws_send is None or self._ws_receive is None:
            return {}

        try:
            await self._ws_send(json.dumps({"type": "CONTEXT_REQUEST", "payload": {}}))
            raw = await asyncio.wait_for(self._ws_receive(), timeout=2.0)
            data = json.loads(raw)
            if data.get("type") != "CONTEXT_RESPONSE":
                return {}
            self._available = True
            return MyAppContext(**data["payload"]).model_dump()
        except Exception:
            self._available = False
            return {}

    async def execute(self, action: str, params: dict) -> AdapterResult:
        # Context adapters do not execute. Effects belong to the intervention
        # transaction coordinator (manifest → authorization → receipt →
        # restore); an adapter that needs an effect adds a catalogued action
        # there instead of sending its own apply frame.
        return AdapterResult(success=False, error="context adapters do not execute")

    async def health_check(self) -> bool:
        return self._available
```

### Step 3: Register with Context Assembly

Update the context assembly to include your adapter. The `TaskContext.mode` field may need new values if your app represents a distinct workspace mode.

### Step 4: Add Intervention Actions

Do not map a model field directly to an effect. Define a typed action command,
add it to an immutable action manifest, bind an exact unexpired authorization
to that manifest hash, and return a durable action receipt. Extend the existing
transaction coordinator and the matching client protocol together. Keep a new
capability unavailable until its target validation, idempotency, partial-failure,
receipt verification, ownership-safe inverse and restart tests pass. Destructive
tab closure, regrouping existing tabs and folding ranges remain outside the
executable catalog because a safe inverse has not been established.

### Step 5: Write Tests

Create unit tests in `tests/unit/test_my_adapter.py`:

```python
import json

import pytest
from cortex.services.context_engine.my_adapter import MyAppAdapter

class TestMyAppAdapter:
    @pytest.mark.asyncio
    async def test_returns_empty_context_when_unavailable(self):
        adapter = MyAppAdapter()
        result = await adapter.get_context()
        assert result == {}
        assert not await adapter.health_check()

    @pytest.mark.asyncio
    async def test_parses_valid_response(self):
        responses = [json.dumps({
            "type": "CONTEXT_RESPONSE",
            "payload": {"active_document": "test.md", "word_count": 500}
        })]

        async def mock_send(msg): pass
        async def mock_receive(): return responses.pop(0)

        adapter = MyAppAdapter(ws_send_fn=mock_send, ws_receive_fn=mock_receive)
        result = await adapter.get_context()
        assert result["active_document"] == "test.md"
        assert await adapter.health_check()
```

## Communication Protocol

Adapters exchange typed frames with their extensions over the WebSocket server
on port 9473. Every literal below is a member of the canonical `MessageType`
catalogue (`cortex/libs/schemas/ws_message_types.py`); anything else is
rejected at schema construction.

### Context request (daemon → extension)
```json
{ "type": "CONTEXT_REQUEST", "payload": {} }
```

### Context response (extension → daemon)
```json
{ "type": "CONTEXT_RESPONSE", "payload": { "...": "typed EditorContext / BrowserContext fields" } }
```

### Effects (daemon → extension, only inside an authorized transaction)

Use the generated schemas from `libs/schemas/intervention_transaction.py`:

| Frame | Payload schema | Required binding |
| --- | --- | --- |
| `INTERVENTION_AUTHORIZE` | `InterventionAuthorizationRequest` | Exact manifest digest, requested action IDs, requester and one-time consent |
| `INTERVENTION_APPLY` | `InterventionApplyCommand` | Complete `manifest`, `authorization` and the exact authorized `actions` subset |
| `INTERVENTION_RECEIPT` | `InterventionReceiptBatch` | Typed outcomes tied to the original authorization and action |
| `INTERVENTION_RESTORE` | `InterventionRestoreCommand` | Exact Cortex-owned effects and verified inverse data |

Do not replace the typed apply payload with a bare transaction ID or an
unverified action list. Its validator checks manifest/authorization identity,
the digest and equality of the exact authorized subset.

The extension answers each action with an `INTERVENTION_RECEIPT`; the daemon
verifies the receipt's postcondition and publishes
`INTERVENTION_TRANSACTION_STATE`. Restores travel as `INTERVENTION_RESTORE`
and are verified the same way.

Extensions first send `AUTH` with the capability token and wait for
`AUTH_OK`; only then may they send `IDENTIFY`. Clients in `suggest_only`
mode reject any apply traffic, and the daemon never emits `INTERVENTION_APPLY`
without a persisted, unexpired authorization grant bound to the exact
manifest digest.

## Privacy Requirements

All adapters must follow these rules:

1. **No raw biometric stream** in the external context contract; an explicitly
   selected heuristic support status remains a separate classified field.
2. **Broker ownership** — adapters return local context; only the privacy
   broker may classify, select, minimize, preview, and externally send it.
3. **Content limits** — browser excerpts are capped at 2,000 characters before
   the daemon repeats authoritative bounds/redaction.
4. **Minimal permissions** — page-body access is exact-origin, explicitly
   recorded, revocable, and excluded in incognito. Static learning telemetry
   remains on a declared narrow allowlist.
5. **Declared persistence** — raw adapter snapshots are ephemeral and absent
   from logs. Separate sanitized browser resume metadata is locally persisted,
   capped, scrubbed on content revocation, and documented in
   [`privacy.md`](privacy.md).
