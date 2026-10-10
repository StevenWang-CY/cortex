# API Reference

Cortex exposes a REST API on `http://127.0.0.1:9472` and a WebSocket server on `ws://127.0.0.1:9473`.

Pydantic schemas in `cortex/libs/schemas/` are authoritative. Examples below are
selected payloads, not substitutes for generated schemas. See the
[current implementation record](../../docs/implementation-2026-10.md) for the
model, source-availability and lifecycle changes.

## REST API

Base URL: `http://127.0.0.1:9472`

All request and response bodies are JSON. Legacy v1 payloads contain a bare
`timestamp` whose clock varies by endpoint and must not be compared across
processes. New v2 payloads use explicit `observed_at_unix_ms`,
`observed_at_mono_ns`, and `boot_id`; see the compatibility notes below.

### Authentication

Operational HTTP routes, including sensitive reads and mutations, require a capability token, sent as `Authorization: Bearer <token>` (canonical) or via the legacy `X-Cortex-Auth-Token: <token>` header. The token is generated at first daemon start and persisted with mode `0600` at `~/Library/Application Support/Cortex/auth.token`. Missing or wrong tokens return `401 Unauthorized` with `WWW-Authenticate: Bearer`. `GET /health` accepts an optional token (the supervisor liveness probe must reach the daemon without owning the token).

Every mutating response also carries an `X-Cortex-Request-ID` header. Clients may set their own on the request to correlate across logs (F19); otherwise the server generates one.

---

### Health & Status

#### `GET /health`

Health check for all registered services. Capability token is optional.

**Response:**
```json
{
  "status": "healthy",
  "services": {
    "state_engine": "up",
    "context_engine": "up",
    "llm_engine": "up",
    "intervention_engine": "up"
  },
  "uptime_seconds": 3421.5
}
```

#### `GET /status/current`

Current legacy support label, heuristic score, and signal quality. The
`confidence` name is retained for wire compatibility; it is not a calibrated
probability.

**Response:**
```json
{
  "state": "FLOW",
  "support_state": "flow_like",
  "estimate_status": "estimated",
  "confidence": 0.87,
  "evidence_strength": 0.87,
  "evidence_coverage": 0.82,
  "signal_quality": {
    "physio": 0.92,
    "kinematics": 0.88,
    "telemetry": 0.95,
    "overall": 0.91
  },
  "features": null,
  "timestamp": 12345.6
}
```

Returns `null` fields if no state has been computed yet.

---

### Capture & Features

#### `POST /capture/frame_meta`

Submit frame metadata from the capture service.

**Request body:** `FrameMeta`
```json
{
  "frame_id": 42,
  "timestamp": 1000.5,
  "width": 640,
  "height": 480,
  "brightness": 127.3,
  "blur_score": 85.2,
  "face_detected": true,
  "face_confidence": 0.97,
  "quality_pass": true
}
```

**Response:**
```json
{ "status": "ok", "timestamp": 1000.51 }
```

#### `POST /features/physio`

Submit physiology features from the physio engine.

**Request body:** `PhysioFeatures`
```json
{
  "pulse_bpm": 72.0,
  "pulse_quality": 0.9,
  "pulse_variability_proxy": null,
  "hr_delta_5s": 0.5,
  "valid": true
}
```

Pulse is experimental; HRV/respiration compatibility fields remain unavailable
in product publication. Camera measurements do not contribute to production
support scores.

#### `POST /features/kinematics`

Submit kinematic features from the kinematics engine.

**Request body:** `KinematicFeatures`
```json
{
  "blink_rate": 16.0,
  "blink_rate_delta": 0.0,
  "blink_suppression_score": 0.0,
  "head_pitch": 0.0,
  "head_yaw": 0.0,
  "head_roll": 0.0,
  "blink_valid_exposure_seconds": 60.0,
  "head_neck_flexion_angle": null,
  "head_neck_flexion_score": null,
  "head_neck_proxy_available": false,
  "slump_score": null,
  "shoulder_drop_ratio": null,
  "confidence": 0.9
}
```

#### `POST /features/telemetry`

Submit telemetry features from the telemetry engine.

**Request body:** `TelemetryFeatures`
```json
{
  "mouse_velocity_mean": 400.0,
  "mouse_velocity_variance": 5000.0,
  "mouse_jerk_score": 0.1,
  "click_burst_score": 0.1,
  "click_frequency": 0.5333333333333333,
  "keyboard_burst_score": 0.2,
  "keystroke_interval_variance": 500.0,
  "backspace_density": 0.05,
  "inactivity_seconds": 2.0,
  "window_switch_rate": 4.0,
  "observation_window_seconds": 15.0,
  "mouse_source_available": true,
  "keyboard_source_available": true,
  "window_focus_source_available": true,
  "mouse_move_count": 120,
  "click_press_count": 8,
  "key_press_count": 20,
  "keystroke_interval_count": 18,
  "scroll_event_count": 0,
  "window_focus_event_count": 1,
  "keypress_rate_per_min": 80.0,
  "correction_rate_per_100_keys": 5.0,
  "scroll_back_rate_per_min": 0.0
}
```

All feature endpoints return `{ "status": "ok", "timestamp": <float> }`.
An acknowledgement records receipt, not measurement validity. Mouse/keyboard
availability defaults to false for older payloads; omitting those flags cannot
turn legacy zeros into observed inactivity. The producer must report actual
listener availability. Inactivity requires both channels and continuous observed
exposure; losing either source or a sampling gap above 3 seconds restarts it.
Typing variance needs at least two positive regular-key intervals, independently
of backspace count. The actual aggregation window and observation counts qualify
the named features created by fusion.

---

### State Inference

#### `POST /state/infer`

Compute an evidence-aware deterministic support estimate. Named
`FeatureValue` entries are canonical; flat fields are decode-only. Every named
entry carries validity, quality, age, source-window exposure, algorithm
version, and a missing reason when invalid. The current registered model is
`deterministic-support@2.5.0`, using `support-features-v2.2.0`.
The smoother ignores duplicate/regressed observation times and resets scores,
pending confirmation and the committed label after gaps above 3 seconds.
Supply the complete v2 time tuple for explicit observation timing; without it,
this endpoint uses its own injected clock rather than treating a caller's bare
`timestamp` as a comparable monotonic clock.

**Request body:**
```json
{
  "feature_vector": {
    "timestamp": 1000.5,
    "telemetry_seen_count": 5,
    "features": {
      "keypress_rate_per_min": {
        "value": 60.0,
        "valid": true,
        "quality": 1.0,
        "age_ms": 0,
        "source_window_ms": 15000,
        "algorithm_version": "input-aggregation-v2.1"
      }
    }
  },
  "signal_quality": {
    "physio": 0.9,
    "kinematics": 0.85,
    "telemetry": 0.95,
    "overall": 0.9
  }
}
```

**Response:**
```json
{
  "estimate": {
    "state": "UNKNOWN",
    "support_state": "unknown",
    "status": "insufficient_evidence",
    "confidence": 0.0,
    "scores": {
      "flow": 0.0,
      "hypo": 0.0,
      "hyper": 0.0,
      "recovery": 0.0
    },
    "support_scores": {
      "support_likely": 0.0,
      "under_engaged": 0.0,
      "flow_like": 0.0,
      "recovering": 0.0
    },
    "evidence_coverage": 0.0,
    "probabilities": null,
    "reasons": ["Not enough current evidence for a support estimate"],
    "signal_quality": { "physio": 0.9, "kinematics": 0.85, "telemetry": 0.95, "overall": 0.9 },
    "timestamp": 1000.5,
    "dwell_seconds": 0.0
  },
  "source": "rules",
  "degraded": true,
  "timestamp": 1000.52
}
```

---

### Context Building

#### `POST /context/build`

Build task context from workspace adapters.

**Request body:**
```json
{
  "include_editor": true,
  "include_terminal": true,
  "include_browser": true
}
```

**Response:**
```json
{
  "context": {
    "mode": "coding_debugging",
    "active_app": "vscode",
    "current_goal_hint": "Debugging TypeScript type error",
    "complexity_score": 0.72,
    "editor_context": {
      "file_path": "src/components/App.tsx",
      "visible_range": [45, 95],
      "symbol_at_cursor": "handleSubmit",
      "diagnostics": [
        {
          "severity": "error",
          "message": "Type 'string' is not assignable to type 'number'",
          "line": 67,
          "column": 12,
          "source": "typescript",
          "code": "TS2322"
        }
      ],
      "recent_edits": [],
      "visible_code": "function handleSubmit() { ... }"
    },
    "terminal_context": null,
    "browser_context": null
  },
  "available": true,
  "timestamp": 1000.52
}
```

---

### LLM Planning

External planning is network-off by default. The privacy routes below require
the same local bearer capability as other sensitive routes. Preview creation
does not call a provider; confirmation burns the handle before the external
await. See [`privacy.md`](privacy.md) for the field catalog and retention
contract.

#### `GET /privacy/context/status`

Returns `planner_mode`, whether configuration permits an external transport,
the number of live prepared previews, provider, and conservative retention
disclosure. It never probes the provider.

#### `POST /privacy/context/preview/current`

Creates an exact redacted preview from the daemon's current in-memory
`TaskContext` and `StateEstimate`. The caller supplies only `selection`, an
optional template/constraints object, and an optional user-authored note.

```json
{
  "selection": {
    "workspace_aggregates": true,
    "support_estimate": false,
    "user_goal": true,
    "editor_metadata": true,
    "editor_content": false,
    "terminal_content": false,
    "browser_metadata": true,
    "browser_content": false,
    "learned_preferences": false,
    "extra_context": false
  },
  "extra_context": ""
}
```

The response includes the exact outbound context/prompt, field disclosures,
redaction/omission counts, encoded size, destination/model, provider caveat,
expiry, and one-time `preview_id`. `raw_context_retained` is false; the exact
prepared redacted payload is retained in memory until confirmation,
cancellation, eviction, or expiry (maximum 60 seconds).

#### `POST /privacy/context/preview`

Developer equivalent of `/current` that accepts a caller-supplied local
`state_estimate` and `task_context`. It has the same no-network semantics and
is useful for contract tests or an explicitly constructed local client.

#### `POST /privacy/context/confirm`

```json
{
  "preview_id": "ctx_<opaque-random-handle>",
  "confirmation_phrase": "SEND PREVIEWED CONTEXT ONCE"
}
```

Both fields are required. The handle is consumed on every attempt. Missing,
expired, cancelled, replayed, or wrongly confirmed handles return `409` and do
not send. A successful response contains a schema-validated proposal and
`authority_granted: false`; workspace actions require a separate transaction.

#### `DELETE /privacy/context/preview/{preview_id}`

Idempotently burns a prepared handle without contacting the provider. The
response reports whether the handle was still live, always reports
`sent: false`, and grants no authority.

#### `POST /llm/plan`

Request an intervention plan from the configured engine. In `no_llm` this is a
local deterministic plan. In external mode, omitting a valid preview handle
and confirmation yields a deterministic no-content fallback rather than an
implicit model request.

**Request body:**
```json
{
  "state_estimate": { "state": "HYPER", "confidence": 0.91, "...": "..." },
  "task_context": { "mode": "coding_debugging", "...": "..." }
}
```

**Response:**
```json
{
  "plan": {
    "intervention_id": "int_a1b2c3d4e5f6",
    "level": "simplified_workspace",
    "situation_summary": "You've been switching between 5 files with type errors for 12 minutes.",
    "headline": "Focus on one error at a time",
    "primary_focus": "Fix the TS2322 type error in App.tsx line 67",
    "micro_steps": [
      "Look at the error on line 67",
      "Check the expected type in the interface",
      "Update the value to match"
    ],
    "hide_targets": ["sidebar", "terminal"],
    "ui_plan": {
      "dim_background": true,
      "show_overlay": true,
      "fold_unrelated_code": true,
      "intervention_type": "simplified_workspace"
    },
    "tone": "direct"
  },
  "fallback_used": false,
  "timestamp": 1002.1
}
```

---

### Intervention Control

#### `POST /intervention/apply`

This legacy endpoint validates and sanitizes a plan, broadcasts a
presentation-only proposal, performs no adapter call or snapshot, and returns
`applied: false` in every execution mode. It has no exact manifest authorization
fields and cannot grant workspace authority. The implemented mutation path is
`INTERVENTION_AUTHORIZE` → `INTERVENTION_APPLY` → durable receipt and verification;
see [adapters](adapters.md). Compatibility confirmation query parameters do not
turn this route into that transaction path.

**Request body:**
```json
{
  "plan": { "intervention_id": "int_a1b2c3d4e5f6", "...": "..." }
}
```

**Safe-default response:**
```json
{
  "applied": false,
  "snapshot": null,
  "confirmation": null,
  "correlation_id": "..."
}
```

#### `POST /intervention/restore`

Request a receipt-verified, scoped restore of Cortex-owned effects. This is not
a blanket workspace rollback; a missing coordinator, failed receipt or changed
ownership cannot be reported as a successful restoration.

**Request body:**
```json
{
  "intervention_id": "int_a1b2c3d4e5f6",
  "user_action": "dismissed"
}
```

**Response:**
```json
{
  "restored": true,
  "outcome": {
    "intervention_id": "int_a1b2c3d4e5f6",
    "started_at": "2025-01-15T10:30:00",
    "ended_at": "2025-01-15T10:32:15",
    "duration_seconds": 135.0,
    "user_action": "dismissed",
    "recovery_detected": false,
    "recovery_confidence": null,
    "workspace_restored": true,
    "restore_errors": []
  },
  "timestamp": 1137.5
}
```

Valid `user_action` values: `dismissed`, `engaged`, `snoozed`, `timed_out`, `natural_recovery`, `system_cancelled`.

---

### Stress & Learning

#### `GET /api/stress-integral`

Compatibility endpoint for the removed physiology-triggered break feature.
It is side-effect free and always reports unavailable.

**Response:**
```json
{
  "status": "unavailable",
  "unavailable_reason": "validation_required",
  "current_value": 0.0,
  "threshold": 0.0,
  "should_break": false,
  "sensitivity_multiplier": 1.0,
  "timestamp": 1787592000.5
}
```

#### `GET /api/helpfulness/summary`

Legacy descriptive engagement/dismissal summary. It does not update the
production policy and is not causal evidence.

**Response:**
```json
{
  "total_interventions": 15,
  "mean_reward": 0.62,
  "engagement_rate": 0.74,
  "recent_rewards": [0.6, 0.8, 0.55],
  "timestamp": 1000.5
}
```

---

### Consent

#### `GET /consent/level`

Return the consent ladder state for every tracked action type.

**Response:**
```json
{
  "levels": {
    "close_tab": { "level": 2, "approvals": 4, "rejections": 0 },
    "group_tabs": { "level": 1, "approvals": 1, "rejections": 0 }
  },
  "timestamp": 1000.5
}
```

#### `POST /consent/reset`

Reset the consent ladder to its defaults and return the new state.

**Response:**
```json
{
  "reset": true,
  "levels": { "close_tab": { "level": 0 } },
  "timestamp": 1000.5
}
```

---

### Project Launcher

#### `GET /api/projects`

List available project profiles.

**Response:**
```json
{
  "projects": ["default", "research", "leetcode"],
  "timestamp": 1000.5
}
```

#### `POST /api/launch/{project_name}`

Launch a project profile (opens VS Code workspace, Chrome URLs, terminal commands).

**Response:**
```json
{
  "launched": true,
  "project": "research",
  "timestamp": 1000.5
}
```

---

## WebSocket Protocol

Endpoint: `ws://127.0.0.1:9473`

All messages are JSON objects with the following envelope:

```json
{
  "type": "<MESSAGE_TYPE>",
  "payload": { ... },
  "timestamp": 12345.6,
  "sequence": 42
}
```

### Auth Handshake

Every connection MUST send an `AUTH` frame before any other message. The server holds the connection in `pending_auth` until the token validates; any non-AUTH frame sent first triggers `close(code=1011, reason="auth required")` and emits an `AUTH_REJECTED` event.

Client → server (first frame):

```json
{
  "type": "AUTH",
  "payload": { "auth_token": "<capability_token>" },
  "timestamp": 12300.0,
  "sequence": 0
}
```

Server → client on success:

```json
{ "type": "AUTH_OK", "payload": { "selected_protocol_version": "2" }, "timestamp": 12300.05, "sequence": 0 }
```

`AUTH_OK` carries the protocol version the daemon selected from the client's `AUTH` offer; clients pin their dispatch to it. Only after `AUTH_OK` does the client send `IDENTIFY` (declaring `client_type`) and then exchange normal traffic.

### Generated TypeScript Types

The full list below is the canonical message-type catalog (Python source of truth: `cortex/libs/schemas/ws_message_types.py::MessageType`). The browser extension imports matching types from `cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts`, which is regenerated from the Pydantic models by `python -m cortex.scripts.generate_ts_schemas` and gated against drift by pre-commit and CI.

### Message Types

The catalogue below is derived from `MessageType` and must stay identical to
it; `python -m cortex.scripts.verify_repository_contracts` rejects any member
without a production producer or consumer, and the generated `.d.ts` union
rejects any literal outside it. Counts: 29 inbound, 33 outbound,
6 LeetCode cues.

**Inbound (client → daemon), dispatched by the WebSocket server:**
`AUTH`, `IDENTIFY`, `USER_ACTION`, `ACTION_EXECUTE`, `USER_RATING`, `CONTEXT_RESPONSE`, `SETTINGS_SYNC`, `CALIBRATION_RELOAD`, `ACTIVITY_SYNC`, `TAB_RELEVANCE_FEEDBACK`, `LEETCODE_CONTEXT_UPDATE`, `INTERVENTION_APPLIED`, `INTERVENTION_AUTHORIZE`, `INTERVENTION_RECEIPT`, `SHUTDOWN`, `REQUEST_SESSION_LIST`, `REQUEST_SESSION_DETAIL`, `REQUEST_TRENDS`, `REQUEST_SESSION_RECAP`, `SESSION_RECAP_ACKNOWLEDGED`, `MICRO_STEP_TOGGLED`, `WHY_DETAIL_REQUEST`, `QUIET_MODE_TOGGLE`, `SNOOZE_REQUEST`, `COST_REQUEST`, `TEST_PROVIDER`, `GOAL_SET`, `FORCE_RECAP`, `DISMISS_OVERLAY`.

**Outbound (daemon → client):**
`AUTH_OK`, `PROTOCOL_ERROR`, `STATE_UPDATE`, `CALIBRATION_UPDATED`, `CALIBRATION_UPDATE_FAILED`, `INTERVENTION_TRIGGER`, `INTERVENTION_APPLY`, `INTERVENTION_AUTHORIZATION_DENIED`, `INTERVENTION_TRANSACTION_STATE`, `INTERVENTION_RESTORE`, `CONTEXT_REQUEST`, `ACTIVE_RECALL`, `BREATHING_OVERLAY`, `PRE_BREAK_WARNING`, `MORNING_BRIEFING`, `COPILOT_THROTTLE`, `AMBIENT_STATE_UPDATE`, `ACTION_DISPATCH`, `SESSION_LIST`, `SESSION_DETAIL`, `TRENDS_PAYLOAD`, `SESSION_RECAP`, `WHY_DETAIL`, `BREAK_RECOMMENDATION`, `QUIET_MODE_STATE`, `START_FOCUS_AUTO`, `STOP_FOCUS_AUTO`, `INTERVENTION_FAILED`, `INTERVENTION_PROMPT`, `COST_RESPONSE`, `TEST_PROVIDER_RESULT`, `RAISE_DASHBOARD`, `ERROR`.

`BREAK_RECOMMENDATION`, `BREATHING_OVERLAY`, `PRE_BREAK_WARNING` are decode-only compatibility entries: the daemon can
still emit the legacy break/respiration frames for one migration window, but
product clients ignore them and they cannot trigger an action.

**LeetCode cues (daemon → chrome, `target_client_types=["chrome"]`):**
`LEETCODE_SHOW_SCRATCHPAD`, `LEETCODE_SHOW_PATTERN_LADDER`, `LEETCODE_SHOW_LOCKOUT`, `LEETCODE_SHOW_CONSOLIDATION`, `LEETCODE_SHOW_SUBMISSION_GATE`, `LEETCODE_SHOW_SOLUTION_FRICTION`.

Selected payload shapes follow.

### Browser extension runtime messages are a separate protocol

The worker, popup and new-tab page also communicate through `chrome.runtime`.
Their private types live in `lib/extension-protocol.ts`, not daemon `MessageType`.
For example, the worker reports a stop outcome as:

```json
{ "type": "STOP_STATUS", "status": "error", "error": "Could not confirm shutdown." }
```

`status` is `idle`, `stopping`, `stopped` or `error`; `error` may be null.
`STOP_INTENT` separately carries the user's `stopRequested` intent. `GET_STATE`
hydration includes `stopStatus`, `stopError` and `stateAgeMs`. None of these
private status frames is sent as a daemon WebSocket command. The popup waits
for the shared stop operation's verified helper result; a disconnected socket
alone is insufficient proof of process exit.

#### `STATE_UPDATE` (server → client)

Nominally broadcast every 500 ms to authenticated clients. The payload also
carries explicit estimate status, support scores, evidence coverage, model
identity, exclusions and optional diagnostic/capture/store fields. A minimal
illustrative subset follows.

```json
{
  "type": "STATE_UPDATE",
  "payload": {
    "state": "FLOW",
    "support_state": "flow_like",
    "status": "estimated",
    "confidence": 0.87,
    "evidence_coverage": 0.82,
    "scores": {
      "flow": 0.87,
      "hypo": 0.05,
      "hyper": 0.08,
      "recovery": 0.0
    },
    "signal_quality": {
      "physio": 0.9,
      "kinematics": 0.85,
      "telemetry": 0.95,
      "overall": 0.9
    },
    "dwell_seconds": 45.2,
    "reasons": ["Steady observed input"]
  },
  "timestamp": 12345.6,
  "sequence": 42
}
```

Browser surfaces clear unavailable or disconnected diagnostics immediately and
expire a held state after 10 seconds without a fresh update. Worker hydration
passes `stateAgeMs` as an elapsed duration; pages add their own elapsed time
instead of comparing monotonic clocks from different contexts. This UI bound
is separate from the 3-second inference-continuity bound. Neither timing rule
establishes physiological validity. Clients must inspect capture status and
pulse-unavailability reasons as well as numeric values.

#### `INTERVENTION_TRIGGER` (server → client)

Legacy name for a presentation-only proposal in `suggest_only` mode.
Receiving this message is never authorization to mutate a workspace.

```json
{
  "type": "INTERVENTION_TRIGGER",
  "payload": {
    "intervention_id": "int_a1b2c3d4e5f6",
    "level": "simplified_workspace",
    "headline": "Focus on one error at a time",
    "situation_summary": "You've been stuck on type errors for 12 minutes.",
    "primary_focus": "Fix TS2322 in App.tsx:67",
    "micro_steps": [
      "Look at the error on line 67",
      "Check the expected type",
      "Update the value"
    ],
    "hide_targets": [],
    "ui_plan": {
      "dim_background": false,
      "show_overlay": true,
      "fold_unrelated_code": false,
      "intervention_type": "overlay_only"
    },
    "tone": "direct"
  },
  "timestamp": 12346.1,
  "sequence": 43
}
```

#### `USER_ACTION` (client → server)

Sent by extensions when the user interacts with an intervention.

```json
{
  "type": "USER_ACTION",
  "payload": {
    "action": "dismissed",
    "intervention_id": "int_a1b2c3d4e5f6"
  },
  "timestamp": 12400.0,
  "sequence": 1
}
```

Valid actions: `dismissed`, `engaged`, `snoozed`.

#### `IDENTIFY` (client → server)

Sent by extensions on connection to identify their type.

```json
{
  "type": "IDENTIFY",
  "payload": {
    "client_type": "vscode"
  },
  "timestamp": 12300.0,
  "sequence": 0
}
```

Valid client types: `vscode`, `chrome`, `desktop`.

#### `SETTINGS_SYNC` (bidirectional)

Sent by clients to update settings, or by the server to broadcast settings changes.

```json
{
  "type": "SETTINGS_SYNC",
  "payload": {
    "consent_levels": { "close_tabs": 3, "fold_code": 4 },
    "quiet_mode": false,
    "max_autonomy": "SUGGEST",
    "execution_mode": "suggest_only"
  },
  "timestamp": 12350.0,
  "sequence": 2
}
```

#### `ACTIVITY_SYNC` (client → server)

Sent by the browser extension to report learning activity progress.

```json
{
  "type": "ACTIVITY_SYNC",
  "payload": {
    "platform": "youtube",
    "url": "https://youtube.com/watch?v=...",
    "title": "Data Structures Lecture 5",
    "position": { "type": "video", "timestamp_seconds": 1234 },
    "duration_seconds": 300
  },
  "timestamp": 12360.0,
  "sequence": 3
}
```

#### `CONTEXT_REQUEST` (server → client)

Sent by the daemon to request context from a specific extension.

```json
{
  "type": "CONTEXT_REQUEST",
  "payload": {},
  "timestamp": 12370.0,
  "sequence": 44
}
```

### Connection Behavior

- New clients receive the latest `STATE_UPDATE` immediately on connection
- Dead connections are automatically cleaned up on next broadcast
- The daemon never reconnects to clients; a dropped connection is the client's to re-establish
- Extensions should implement reconnection with exponential backoff
