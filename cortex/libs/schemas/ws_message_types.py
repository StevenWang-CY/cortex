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

Convention: leading-underscore payload keys
-------------------------------------------

A small number of payload keys are reserved for daemon-internal
bookkeeping and MUST NOT be set or trusted by clients. Their leading
underscore signals "wire-implementation, not user data":

* ``_seq`` — monotonic in-process bridge sequence number stamped on
  callbacks (see ``runtime_daemon.CortexDaemon._state_callback_seq``).
* ``_source_client_type`` — the daemon stamps this onto USER_ACTION
  payloads from the WS receive path so handlers can gate on
  origin-of-truth (e.g. ``_handle_user_action`` rejects
  ``request_dispatch=True`` from anything other than ``"desktop"``).
  Closes a confused-deputy where a compromised browser could trigger
  ACTION_DISPATCH against peer browser clients via the daemon
  broadcast bus.

Underscore-prefixed keys must never appear in outbound broadcast
payloads — they live only on in-process callback dicts or on inbound
payloads consumed by daemon handlers. New keys following this
convention should be documented here.
"""

from __future__ import annotations

from enum import Enum


class MessageType(str, Enum):  # noqa: UP042 — pydantic-to-typescript requires (str, Enum); StrEnum changes JSON output
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

    REQUEST_SESSION_LIST = "REQUEST_SESSION_LIST"
    """P0 §3.1: client asks for a paginated session-history listing.

    Payload: ``{since: float | None, limit: int}`` where ``since`` is an
    epoch-seconds cursor returned by the previous ``SESSION_LIST`` reply
    (``next_cursor``) and ``limit`` is the page size (server clamps to
    1..100; default 30). Reply: ``SESSION_LIST``."""

    REQUEST_SESSION_DETAIL = "REQUEST_SESSION_DETAIL"
    """P0 §3.1: client asks for the full ``SessionReport`` for one id.

    Payload: ``{session_id: str}``. Reply: ``SESSION_DETAIL`` (or an
    empty payload + ``error`` field if the file is missing or corrupt)."""

    REQUEST_TRENDS = "REQUEST_TRENDS"
    """P0 §3.2: client asks for the longitudinal trend / chronotype rollup.

    Payload: ``{window: "week" | "month" | "quarter", refresh: bool}``.
    ``refresh`` forces a recompute from disk; default is False (serve the
    cached ``model.json``). Reply: ``TRENDS_PAYLOAD``."""

    REQUEST_SESSION_RECAP = "REQUEST_SESSION_RECAP"
    """P0 §3.3: client re-requests the most-recent SESSION_RECAP envelope.

    Sent by surfaces (browser extension popup) that joined after the live
    broadcast was emitted, so the recap card can be shown on next open.
    Payload: ``{}``. Reply: ``SESSION_RECAP`` (or empty payload if no
    recap is cached yet)."""

    SESSION_RECAP_ACKNOWLEDGED = "SESSION_RECAP_ACKNOWLEDGED"
    """P0 §3.3 (Wave-2 P1): UI confirms the user dismissed the recap card.

    Sent by the desktop shell's recap sheet (and any peer surface that
    rendered the recap) after the user clicks Close or the autohide
    fires. The daemon's ``stop()`` awaits this acknowledgement (or a
    5 s timeout) before tearing down the WS server so a fast UI hide
    doesn't race the shutdown. Payload: ``{session_id: str | None}``
    (the id is informational; the daemon's wait is unconditional)."""

    MICRO_STEP_TOGGLED = "MICRO_STEP_TOGGLED"
    """P0 §3.6: client toggles a micro-step's completion state.

    Payload: ``{intervention_id: str, step_index: int, new_status: "done"|"skipped"|"pending"}``.
    Daemon updates the active intervention's micro_step status with timestamps and
    broadcasts the updated INTERVENTION_TRIGGER envelope to all clients."""

    WHY_DETAIL_REQUEST = "WHY_DETAIL_REQUEST"
    """P0 §3.9: client requests the structured causal rationale for an
    intervention. Used when the popup / VS Code panel surface only
    received the headline (e.g. joined late, or wants to refresh the
    sparkline buffers).

    Payload: ``{intervention_id: str}``. Reply: :attr:`WHY_DETAIL` with
    the most recent ``CausalSignal`` list for that intervention."""

    QUIET_MODE_TOGGLE = "QUIET_MODE_TOGGLE"
    """P0 §3.11: client requests entering or leaving a quiet/pause mode.

    Payload: ``{kind: "snooze_15" | "quiet_session" | "pause" | "off",
    duration_minutes: int | None}``. ``"off"`` is a sentinel that clears
    any active mode; the daemon broadcasts the resulting
    :attr:`QUIET_MODE_STATE` so every surface (dashboard, overlay, tray,
    browser popup, VS Code status bar) reflects the same truth."""

    SNOOZE_REQUEST = "SNOOZE_REQUEST"
    """P0 §3.11: alias for a brief, overlay-only suppression.

    Sent by the overlay's "Snooze 15" footer button as a shorthand for
    ``QUIET_MODE_TOGGLE`` with ``kind="snooze_15"``. Carried as a
    separate type so the daemon can record the source affordance (an
    overlay click vs. a dashboard menu pick) without bloating the
    QUIET_MODE_TOGGLE payload. Payload:
    ``{duration_minutes: int | None}`` — defaults to 15."""

    COST_REQUEST = "COST_REQUEST"
    """P0 §3.15: client asks for the running daily LLM spend snapshot.

    Sent by the desktop shell on a ~10 s poll AND any time the dashboard
    re-opens. Payload: ``{}``. Reply: :attr:`COST_RESPONSE`."""

    TEST_PROVIDER = "TEST_PROVIDER"
    """P0 §3.19: client asks the daemon to send a minimal probe to the
    selected LLM provider and report the round-trip latency.

    Payload: ``{provider: "bedrock"|"vertex"|"anthropic_direct"|"rule_based"}``.
    Reply: :attr:`TEST_PROVIDER_RESULT`. The probe uses a 5 s timeout;
    the ``rule_based`` provider short-circuits to ``ok=True,
    latency_ms=0`` because it never hits the network."""

    GOAL_SET = "GOAL_SET"
    """P0 §3.13: client (desktop dashboard, browser popup) announces the
    user-provided session goal so the daemon can stamp it on the next
    SessionReport and feed it to the planner's ``current_goal_hint``.

    Payload: ``{title: str}`` (trimmed; empty clears the override)."""

    FORCE_RECAP = "FORCE_RECAP"
    """P0 §3.21: developer/keyboard-shortcut request to emit a
    SESSION_RECAP for the in-progress session right now.

    Payload: ``{}``. When no session is active the daemon broadcasts a
    minimal synthesised recap with ``persisted=False``."""

    DISMISS_OVERLAY = "DISMISS_OVERLAY"
    """P0 §3.21: developer/keyboard-shortcut request to dismiss any
    active overlay across every surface. The daemon also clears any
    pending intervention state so a new one can be triggered.

    Inbound payload: ``{}``. Outbound broadcast carries
    ``{intervention_id: str | None, reason: "user_shortcut"}``."""

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

    ACTION_DISPATCH = "ACTION_DISPATCH"
    """Audit-prod G4: daemon → browser-extension directive to actually
    execute a ``SuggestedAction`` (e.g. close a tab, group tabs). Emitted
    when the desktop-shell overlay's action button is clicked for a
    browser-bound action — the desktop shell can't directly run
    ``chrome.tabs.*`` so the daemon forwards the action object to a
    chrome/edge client. Payload:
    ``{intervention_id: str, action: SuggestedAction.model_dump()}``.
    The receiver runs ``executeAction(action)`` then sends back the
    standard ``ACTION_EXECUTE`` log message."""

    SESSION_LIST = "SESSION_LIST"
    """P0 §3.1: paginated session-history listing reply.

    Payload mirrors :class:`SessionListResponse` (items + next_cursor)."""

    SESSION_DETAIL = "SESSION_DETAIL"
    """P0 §3.1: full ``SessionReport`` reply for the requested id.

    Payload: ``{report: SessionReport | None, error: str | None}``."""

    TRENDS_PAYLOAD = "TRENDS_PAYLOAD"
    """P0 §3.2: longitudinal rollup reply.

    Payload mirrors :class:`TrendsResponse` (window + daily + chronotype)."""

    SESSION_RECAP = "SESSION_RECAP"
    """P0 §3.3: end-of-session recap broadcast.

    Emitted after ``SessionReportGenerator.finish()`` completes and the
    atomic write succeeds but BEFORE the daemon tears the WS server down,
    so the desktop shell can show its slide-up recap sheet and the
    browser-extension popup can cache the summary for next open. Payload
    mirrors :class:`SessionReport` (model_dump(mode='json'))."""

    WHY_DETAIL = "WHY_DETAIL"
    """P0 §3.9: reply to :attr:`WHY_DETAIL_REQUEST`.

    Carries the structured causal rationale (top 2-3 signals, primary
    first) plus the originating ``intervention_id`` so the requesting
    client can match the response to its prompt.
    Payload: ``{intervention_id: str, causal_signals: list[CausalSignal]}``."""

    BREAK_RECOMMENDATION = "BREAK_RECOMMENDATION"
    """P0 §3.7: daemon nudges the user to take a biology-driven break.

    Emitted exactly once per ``StressIntegralTracker.should_break()``
    transition (False → True). The popup / desktop overlay surfaces a
    soft pill with a single CTA that fires ``take_biology_break`` via
    ``EXECUTE_ACTION``. Payload mirrors the contract documented next to
    the ``_break_recommendation_sent`` flag in
    :class:`cortex.services.runtime_daemon.CortexDaemon`:
    ``{reason: str, urgency: "low"|"medium"|"high", stress_load: float,
    threshold: float, duration_seconds: int, breathing_pattern: str}``."""

    QUIET_MODE_STATE = "QUIET_MODE_STATE"
    """P0 §3.11: broadcast of the active quiet / pause mode (or its clear).

    Emitted whenever ``RuntimeDaemon.set_quiet_mode`` runs (whether
    armed by the dashboard menu, the overlay footer, the global
    keyboard shortcut, or the existing F26 frustration-spiral path).
    Payload mirrors :class:`cortex.libs.schemas.realtime.QuietModeState`:
    ``{kind: "snooze_15" | "quiet_session" | "pause" | "off",
    duration_minutes: float | None, ends_at: float | None,
    source: "dashboard" | "overlay" | "tray" | "shortcut" | "popup" |
    "vscode" | "os_notification" | "settings_sync" | "daemon" |
    "daemon_decay"}`` where ``ends_at`` is a unix timestamp (seconds)."""

    START_FOCUS_AUTO = "START_FOCUS_AUTO"
    """P0 §3.10: daemon-armed focus session start directive.

    Emitted to the browser extension when the user has opted in to
    ``CORTEX_INTERVENTION__ENABLE_AUTO_DISTRACTION_BLOCK`` AND the live
    state is HYPER with confidence above the gate. The browser
    extension reuses the existing focus-session start path; the daemon
    broadcasts a paired :attr:`STOP_FOCUS_AUTO` when the user exits
    HYPER. Payload: ``{duration_minutes: int, reason: str,
    preset: "developer"|"student"|"writer"|"custom",
    custom_domains: list[str]}``."""

    STOP_FOCUS_AUTO = "STOP_FOCUS_AUTO"
    """P0 §3.10: daemon-armed focus session stop directive.

    Sent after sustained non-HYPER state (FLOW ≥ 5 min) OR an explicit
    user disarm; the browser extension calls ``stopFocusSession`` only
    if the daemon was the one that armed the current session. Payload:
    ``{reason: str}``."""

    INTERVENTION_FAILED = "INTERVENTION_FAILED"
    """Phase-4b: emitted when ``InterventionExecutor.apply`` returned
    only failed mutations and the workspace was NOT actually mutated.

    Distinct from :attr:`INTERVENTION_TRIGGER` (which still fires for
    suggested-action-only plans). Payload:
    ``{intervention_id: str, error_reason: str,
    failed_action_types: list[str]}``.
    """

    INTERVENTION_PROMPT = "INTERVENTION_PROMPT"
    """Phase-4b: WS broadcast for the prompt-only adapter hooks
    (``prompt_micro_commit`` / ``suggest_movement_break``).

    Emitted by the :class:`InterventionExecutor` via the daemon's
    ``_broadcast_prompt`` hook when the active plan carries one of these
    action types. Payload:
    ``{action_type: str, prompt: str, timeout_seconds: int | None,
    metadata: dict}``.
    """

    COST_RESPONSE = "COST_RESPONSE"
    """P0 §3.15: reply to :attr:`COST_REQUEST` plus a push broadcast on
    every plan-finalised event so the UI's cost meter updates without
    polling lag. Payload mirrors :class:`CostResponse` —
    ``{cost_today: float, budget_today: float, provider: str | None,
    budget_exhausted: bool}``."""

    TEST_PROVIDER_RESULT = "TEST_PROVIDER_RESULT"
    """P0 §3.19: reply to :attr:`TEST_PROVIDER`. Payload mirrors
    :class:`TestProviderResult` — ``{provider: str, ok: bool,
    latency_ms: float | None, error: str | None}``."""

    RAISE_DASHBOARD = "RAISE_DASHBOARD"
    """Phase-4b: instruct the desktop shell to raise its window.

    Emitted in response to ``POST /dashboard/raise`` so any surface
    (browser extension, VS Code, external tool) can bring the dashboard
    to the foreground. Payload: ``{target: str | None}`` — the ``target``
    string is a free-form hint (e.g. ``"history"``, ``"trends"``) the
    shell may use to pick a starting tab; ``None`` opens to the default
    tab.
    """

    ERROR = "ERROR"
    """P2-22: unicast error frame from the daemon to the requesting client.

    Sent when a handler cannot serve a request, e.g. because the daemon
    has not finished starting up yet (``code="daemon_not_ready"``).
    Payload: ``{code: str, correlation_id: str | None}`` — ``code`` is a
    machine-readable error code; ``correlation_id`` echoes the cid from
    the triggering message so the client can match the reply to its
    pending request."""

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
    """Daily LeetCode briefing for the popup/newtab.

    Reserved capability: the adapter advertises this in
    ``LeetCodeAdapter.capabilities`` but ``InterventionMatrix.select``
    does not yet emit it. Browser-side handler at
    ``audit_w2_unhandled_ws_frame.spec.ts`` asserts the silent-drop
    contract."""

    LEETCODE_LOCK_EDITOR = "LEETCODE_LOCK_EDITOR"
    """Force-focus the LeetCode editor (no other tabs).

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""

    LEETCODE_INTERCEPT_SUBMIT = "LEETCODE_INTERCEPT_SUBMIT"
    """Intercept the submit button until acknowledgement.

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""

    LEETCODE_GATE_SOLUTIONS = "LEETCODE_GATE_SOLUTIONS"
    """Gate the editorial / community-solution tab.

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""

    LEETCODE_AI_RESTATEMENT_CHECK = "LEETCODE_AI_RESTATEMENT_CHECK"
    """Trigger AI-powered restatement check (paraphrase the problem).

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""

    LEETCODE_AI_COMPREHENSION_CHECK = "LEETCODE_AI_COMPREHENSION_CHECK"
    """Trigger AI-powered comprehension check (examples / edges).

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""

    LEETCODE_AI_HYPOTHESIS_CHECK = "LEETCODE_AI_HYPOTHESIS_CHECK"
    """Trigger AI-powered hypothesis check (approach articulation).

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""

    LEETCODE_AI_STUCK_ANALYSIS = "LEETCODE_AI_STUCK_ANALYSIS"
    """Trigger AI-powered stuck-analysis explanation.

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""

    LEETCODE_AI_SESSION_BRIEFING = "LEETCODE_AI_SESSION_BRIEFING"
    """Trigger AI-powered session-briefing generation.

    Reserved capability — see ``LEETCODE_SHOW_SESSION_BRIEFING``."""
