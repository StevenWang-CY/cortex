"""
WebSocket Message Type Catalog

The canonical enumeration of every ``type`` literal that flows over the
Cortex WebSocket protocol (daemon ↔ desktop_shell ↔ browser extension).

This module is the Python source of truth for F45 closure (audit ledger).
The codegen pipeline (``cortex/scripts/generate_ts_schemas.py``) emits a
corresponding TypeScript union so dispatch sites on the extension side
no longer typo-bypass handlers — the TS compiler enforces every literal
matches a member of this catalog.

Why an Enum, not a Literal alias
--------------------------------

Python ``Literal[...]`` is invisible to ``pydantic2ts``'s JSON Schema
walk unless it is a field annotation. Promoting to ``str``-enum makes
the catalog generate as a TypeScript ``enum``-equivalent union and lets
the daemon import members by name (``MessageType.STATE_UPDATE.value``)
when constructing ``WSMessage``s, eliminating typos at the wire boundary.

Membership policy
-----------------

A type literal earns membership when:

1. The daemon either emits it (``_make_*`` in ``websocket_server.py`` or
   ``send_message`` from ``runtime_daemon.py`` / ``copilot_throttle.py``)
   OR dispatches it on receipt (``_process_message``).
2. A consumer on the extension or desktop_shell side reads it.

Types that are popup-internal (e.g. ``CONNECTION_CHANGED``, broadcast
only between background.ts and popup.tsx via ``chrome.runtime``) are
NOT in this catalog — they never cross the WebSocket boundary and are
properly typed in the extension's own message-channel types.
"""

from __future__ import annotations

from enum import Enum


class MessageType(str, Enum):
    """All WS-protocol ``WSMessage.type`` values, daemon ↔ client.

    Membership is policy-bounded: this enum names every wire-level
    message type the daemon ever emits and every type it dispatches on
    receipt. Adding a new dispatch arm in ``WebSocketServer`` without
    extending this catalog is a regression caught by the ws-message
    round-trip test.
    """

    # ─── Client → Daemon (inbound, dispatched by _process_message) ───

    AUTH = "AUTH"
    """First message every client MUST send after connecting; carries the
    capability token in ``payload.auth_token``. Audit Debt-2: the server
    holds the connection in ``pending_auth`` until this frame validates;
    any other type before ``AUTH`` triggers a close(code=1011) with
    ``EventType.AUTH_REJECTED`` logged. Defense-in-depth: the F07 SHUTDOWN
    handler keeps its inline token check too."""

    IDENTIFY = "IDENTIFY"
    """First message after connect; carries ``client_type``."""

    USER_ACTION = "USER_ACTION"
    """User dismissed/engaged/snoozed an intervention."""

    ACTION_EXECUTE = "ACTION_EXECUTE"
    """User invoked a ``SuggestedAction`` (routed via _handle_user_action)."""

    USER_RATING = "USER_RATING"
    """Thumbs-up/-down rating on an intervention outcome."""

    CONTEXT_RESPONSE = "CONTEXT_RESPONSE"
    """Reply to a ``CONTEXT_REQUEST``; resolves a pending future."""

    SETTINGS_SYNC = "SETTINGS_SYNC"
    """Bidirectional — client sends new settings, daemon broadcasts current."""

    ACTIVITY_SYNC = "ACTIVITY_SYNC"
    """Extension forwards per-tab activity records for aggregation."""

    TAB_RELEVANCE_FEEDBACK = "TAB_RELEVANCE_FEEDBACK"
    """User-reported relevance signal for the tab triage classifier."""

    LEETCODE_CONTEXT_UPDATE = "LEETCODE_CONTEXT_UPDATE"
    """Live LeetCode DOM/code telemetry from the content script."""

    INTERVENTION_APPLIED = "INTERVENTION_APPLIED"
    """Extension confirms it applied (or failed to apply) a plan."""

    SHUTDOWN = "SHUTDOWN"
    """Request the daemon shut itself down (gated by capability token)."""

    # ─── Daemon → Client (outbound, made by _make_* helpers) ─────────

    AUTH_OK = "AUTH_OK"
    """Daemon acknowledgment of a successful ``AUTH`` handshake. Audit
    Debt-2: clients block on this frame before sending further requests
    (e.g. ``IDENTIFY``) so they know the server accepted the token. A
    missing ``AUTH_OK`` (close-immediately) means the token was wrong
    and the client must refresh its cache."""

    STATE_UPDATE = "STATE_UPDATE"
    """Periodic state estimate broadcast (every ~500 ms)."""

    INTERVENTION_TRIGGER = "INTERVENTION_TRIGGER"
    """Plan + UI hints for a new intervention."""

    INTERVENTION_RESTORE = "INTERVENTION_RESTORE"
    """Explicit cue for clients to undo their workspace mutations."""

    CONTEXT_REQUEST = "CONTEXT_REQUEST"
    """Daemon asks a specific client_type for live workspace context."""

    ACTIVE_RECALL = "ACTIVE_RECALL"
    """Active-recall prompt template (e.g. recap before context switch)."""

    BREATHING_OVERLAY = "BREATHING_OVERLAY"
    """Breathing pacer overlay cue (4-7-8 pattern by default)."""

    PRE_BREAK_WARNING = "PRE_BREAK_WARNING"
    """Heads-up that a break is being recommended in N seconds."""

    MORNING_BRIEFING = "MORNING_BRIEFING"
    """Daily kickoff summary delivered to the popup."""

    COPILOT_THROTTLE = "COPILOT_THROTTLE"
    """Throttle / unthrottle signal for the editor copilot adapter."""

    AMBIENT_STATE_UPDATE = "AMBIENT_STATE_UPDATE"
    """Lightweight state heartbeat for the always-on ambient overlay."""

    # ─── LeetCode adapter cues (Daemon → Chrome, target_client_types=["chrome"]) ─
    # Emitted by ``LeetCodeAdapter.execute`` via
    # ``runtime_daemon._send_leetcode_ws_message`` → ``WebSocketServer.send_message``
    # → ``WSMessage(type=...)``. These literals MUST be enumerated here or
    # the Pydantic validator rejects them at construction time.

    LEETCODE_SHOW_SCRATCHPAD = "LEETCODE_SHOW_SCRATCHPAD"
    """Inject the LeetCode scratchpad overlay (problem reframing prompts)."""

    LEETCODE_SHOW_PATTERN_LADDER = "LEETCODE_SHOW_PATTERN_LADDER"
    """Surface the pattern ladder (DP/graph/etc. scaffold)."""

    LEETCODE_SHOW_LOCKOUT = "LEETCODE_SHOW_LOCKOUT"
    """Block the editor — destructive-struggle gate."""

    LEETCODE_SHOW_CONSOLIDATION = "LEETCODE_SHOW_CONSOLIDATION"
    """Post-solve consolidation prompt (recap + retain)."""

    LEETCODE_SHOW_SUBMISSION_GATE = "LEETCODE_SHOW_SUBMISSION_GATE"
    """Pre-submit gate (run examples / sanity-check before submit)."""

    LEETCODE_SHOW_SOLUTION_FRICTION = "LEETCODE_SHOW_SOLUTION_FRICTION"
    """Friction overlay before revealing the editorial / solution."""

    LEETCODE_SHOW_SESSION_BRIEFING = "LEETCODE_SHOW_SESSION_BRIEFING"
    """Daily LeetCode briefing for the popup/newtab."""

    LEETCODE_LOCK_EDITOR = "LEETCODE_LOCK_EDITOR"
    """Force-focus the LeetCode editor (no other tabs)."""

    LEETCODE_INTERCEPT_SUBMIT = "LEETCODE_INTERCEPT_SUBMIT"
    """Intercept the submit button until acknowledgement."""

    LEETCODE_GATE_SOLUTIONS = "LEETCODE_GATE_SOLUTIONS"
    """Gate the editorial / community-solution tab."""

    LEETCODE_AI_RESTATEMENT_CHECK = "LEETCODE_AI_RESTATEMENT_CHECK"
    """Trigger AI-powered restatement check (paraphrase the problem)."""

    LEETCODE_AI_COMPREHENSION_CHECK = "LEETCODE_AI_COMPREHENSION_CHECK"
    """Trigger AI-powered comprehension check (examples / edges)."""

    LEETCODE_AI_HYPOTHESIS_CHECK = "LEETCODE_AI_HYPOTHESIS_CHECK"
    """Trigger AI-powered hypothesis check (approach articulation)."""

    LEETCODE_AI_STUCK_ANALYSIS = "LEETCODE_AI_STUCK_ANALYSIS"
    """Trigger AI-powered stuck-analysis explanation."""

    LEETCODE_AI_SESSION_BRIEFING = "LEETCODE_AI_SESSION_BRIEFING"
    """Trigger AI-powered session-briefing generation."""
