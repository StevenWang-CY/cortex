# Writing Workspace Adapters

Workspace adapters connect Cortex to external applications (VS Code, Chrome, terminals) to gather context for LLM-powered interventions. This guide explains how to add a new adapter.

## Adapter Interface

All adapters follow the same pattern:

```python
class MyAdapter:
    def __init__(self, ws_send_fn=None, ws_receive_fn=None):
        self._ws_send = ws_send_fn
        self._ws_receive = ws_receive_fn
        self._available = False
        self._last_context = None

    @property
    def available(self) -> bool:
        return self._available

    async def get_context(self, timeout: float = 2.0):
        """Gather context from the application. Returns None if unavailable."""
        ...

    async def apply_action(self, action: str, params: dict) -> bool:
        """Apply a workspace modification. Returns True on success."""
        ...

    async def restore_state(self, snapshot: dict) -> bool:
        """Restore application state from a snapshot. Returns True on success."""
        ...
```

Key principles:
1. **Graceful fallback** — always return `None` if the application isn't available
2. **Timeout** — all operations should have a configurable timeout (default 2s)
3. **No blocking** — all methods are async
4. **Snapshot/restore** — support capturing state before intervention and restoring after

## Existing Adapters

### EditorAdapter (`context_engine/editor_adapter.py`)

Communicates with the VS Code extension via WebSocket to gather:
- Current file path
- Visible code range (start/end lines)
- Symbol at cursor (function/class name)
- Diagnostics (errors, warnings)
- Visible code content

Actions supported:
- `fold_except` — fold all code except specified function
- `unfold_all` — restore all code folds
- `scroll_to` — scroll to specific line

### BrowserAdapter (`context_engine/browser_adapter.py`)

Communicates with the Chrome extension via WebSocket to gather:
- Active tab title and URL
- Active tab content excerpt (max 2000 tokens)
- All open tabs with type classification
- Tab type distribution (documentation, stackoverflow, search, code_host, social, other)

Actions supported:
- `hide_tabs` — hide/group specified tabs
- `restore_tabs` — restore hidden tabs
- `focus_tab` — switch to a specific tab

### TerminalAdapter (`context_engine/terminal_adapter.py`)

Captures terminal context locally:
- Last N lines of terminal output
- Detected error messages (stack traces, compilation errors)
- Repeated commands
- Currently running command

Actions supported:
- `clear_history` — clear terminal history display
- `highlight_error` — highlight error region

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

Create `services/context_engine/my_adapter.py`:

```python
from cortex.libs.schemas.context import MyAppContext

class MyAppAdapter:
    def __init__(self, ws_send_fn=None, ws_receive_fn=None):
        self._ws_send = ws_send_fn
        self._ws_receive = ws_receive_fn
        self._available = False

    async def get_context(self, timeout: float = 2.0) -> MyAppContext | None:
        if self._ws_send is None or self._ws_receive is None:
            return None

        try:
            # Request context from extension
            await self._ws_send(json.dumps({
                "type": "GET_CONTEXT",
                "payload": {}
            }))

            # Wait for response with timeout
            raw = await asyncio.wait_for(self._ws_receive(), timeout=timeout)
            data = json.loads(raw)

            if data.get("type") != "CONTEXT_RESPONSE":
                return None

            self._available = True
            return MyAppContext(**data["payload"])

        except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
            self._available = False
            return None

    async def apply_action(self, action: str, params: dict) -> bool:
        if self._ws_send is None:
            return False

        try:
            await self._ws_send(json.dumps({
                "type": "APPLY_ACTION",
                "payload": {"action": action, **params}
            }))
            return True
        except Exception:
            return False
```

### Step 3: Register with Context Assembly

Update the context assembly to include your adapter. The `TaskContext.mode` field may need new values if your app represents a distinct workspace mode.

### Step 4: Add Intervention Actions

If your adapter supports workspace modifications, map LLM `hide_targets` to adapter actions in `intervention_engine/executor.py`.

### Step 5: Write Tests

Create unit tests in `tests/unit/test_my_adapter.py`:

```python
import pytest
from cortex.services.context_engine.my_adapter import MyAppAdapter

class TestMyAppAdapter:
    @pytest.mark.asyncio
    async def test_returns_none_when_unavailable(self):
        adapter = MyAppAdapter()
        result = await adapter.get_context()
        assert result is None
        assert not adapter.available

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
        assert result is not None
        assert result.active_document == "test.md"
        assert adapter.available
```

## Communication Protocol

Adapters communicate with their corresponding extensions via the WebSocket server on port 9473. The protocol uses JSON messages:

### Request (daemon → extension)
```json
{
  "type": "GET_CONTEXT",
  "payload": {}
}
```

### Response (extension → daemon)
```json
{
  "type": "CONTEXT_RESPONSE",
  "payload": { ... }
}
```

### Action (daemon → extension)
```json
{
  "type": "APPLY_ACTION",
  "payload": {
    "action": "fold_except",
    "function_name": "handleSubmit",
    "file_path": "src/App.tsx"
  }
}
```

Extensions connect to the WebSocket and send an `IDENTIFY` message to declare their type. The daemon routes adapter messages to the correct extension based on client type.

## Privacy Requirements

All adapters must follow these rules:

1. **No biometric data** in context sent to LLM — only workspace metadata
2. **Content limits** — browser content excerpts must not exceed 2000 tokens
3. **Minimal permissions** — request only what's needed (e.g., `activeTab` not `<all_urls>`)
4. **No persistent storage** — adapter context is ephemeral, not logged to disk
