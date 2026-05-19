# API Reference

Cortex exposes a REST API on `http://127.0.0.1:9472` and a WebSocket server on `ws://127.0.0.1:9473`.

All request/response bodies are JSON. Timestamps are `time.monotonic()` seconds.

The Pydantic models under `cortex/libs/schemas/` are the single source of truth for every wire-level shape on this page; the corresponding TypeScript types live at `cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts` and are regenerated from those models — see [Browser Extension](Browser-Extension) and `CLAUDE.md` for the codegen flow.

---

## Authentication

Every mutating route is gated by a capability token. The token is written at first launch to:

```
~/Library/Application Support/Cortex/auth.token   (mode 0600)
```

Clients present it via the canonical header:

```
Authorization: Bearer <token>
```

The legacy `X-Cortex-Auth-Token: <token>` header is still accepted as a fallback for older browser-extension builds.

On failure the server returns `401 Unauthorized` with `WWW-Authenticate: Bearer`. `GET /health` accepts the token but does not require it (optional auth), so liveness probes work without a token.

The daemon also assigns each mutating request a correlation id and echoes it back on the response as `X-Cortex-Request-ID`; if a client sets the header on the way in, the server reuses that value end-to-end.

---

## REST API

### Health & Status

#### `GET /health`

Optional-auth liveness probe. Returns service status without requiring a capability token.

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

Current cognitive state and signal quality.

```json
{
  "state": "FLOW",
  "confidence": 0.87,
  "signal_quality": {
    "physio": 0.92,
    "kinematics": 0.88,
    "telemetry": 0.95,
    "overall": 0.91
  },
  "timestamp": 12345.6
}
```

---

### Feature Submission

#### `POST /features/physio`

```json
{
  "pulse_bpm": 72.0,
  "pulse_quality": 0.9,
  "pulse_variability_proxy": 55.0,
  "hr_delta_5s": 0.5,
  "valid": true
}
```

#### `POST /features/kinematics`

```json
{
  "blink_rate": 16.0,
  "blink_suppression_score": 0.0,
  "head_pitch": 0.0,
  "head_yaw": 0.0,
  "slump_score": 0.1,
  "forward_lean_score": 0.1,
  "shoulder_drop_ratio": 0.05,
  "confidence": 0.9
}
```

#### `POST /features/telemetry`

```json
{
  "mouse_velocity_mean": 400.0,
  "mouse_velocity_variance": 5000.0,
  "mouse_jerk_score": 0.1,
  "click_burst_score": 0.1,
  "keyboard_burst_score": 0.2,
  "backspace_density": 0.05,
  "inactivity_seconds": 2.0,
  "window_switch_rate": 5.0
}
```

All feature endpoints return `{ "status": "ok", "timestamp": <float> }`.

---

### State Inference

#### `POST /state/infer`

```json
// Request
{
  "feature_vector": {
    "pulse_norm": 0.3,
    "hrv_norm": 0.6,
    "blink_norm": 0.5,
    "posture_norm": 0.2,
    "mouse_velocity_norm": 0.4,
    "mouse_jerk_norm": 0.1,
    "window_switch_norm": 0.3,
    "complexity_norm": 0.4,
    "timestamp": 1000.5
  },
  "signal_quality": { "physio": 0.9, "kinematics": 0.85, "telemetry": 0.95, "overall": 0.9 }
}

// Response
{
  "estimate": {
    "state": "FLOW",
    "confidence": 0.87,
    "scores": { "flow": 0.87, "hypo": 0.05, "hyper": 0.08, "recovery": 0.0 },
    "reasons": ["Good HRV", "Normal blink rate", "Steady input"],
    "dwell_seconds": 45.2,
    "timestamp": 1000.5
  }
}
```

---

### Context Building

#### `POST /context/build`

```json
// Request
{ "include_editor": true, "include_terminal": true, "include_browser": true }

// Response
{
  "context": {
    "mode": "coding_debugging",
    "complexity_score": 0.72,
    "editor_context": {
      "file_path": "src/components/App.tsx",
      "symbol_at_cursor": "handleSubmit",
      "diagnostics": [
        {
          "severity": "error",
          "message": "Type 'string' is not assignable to type 'number'",
          "line": 67,
          "source": "typescript",
          "code": "TS2322"
        }
      ]
    }
  },
  "available": true
}
```

---

### LLM Planning

#### `POST /llm/plan`

```json
// Response
{
  "plan": {
    "intervention_id": "int_a1b2c3d4e5f6",
    "level": "simplified_workspace",
    "headline": "Focus on one error at a time",
    "situation_summary": "You've been switching between 5 files with type errors for 12 minutes.",
    "micro_steps": [
      "Look at the error on line 67",
      "Check the expected type in the interface",
      "Update the value to match"
    ],
    "ui_plan": {
      "dim_background": true,
      "show_overlay": true,
      "fold_unrelated_code": true
    }
  },
  "fallback_used": false
}
```

---

### Intervention Control

#### `POST /intervention/apply`

Apply an intervention plan. Returns a snapshot ID for later restore.

#### `POST /intervention/restore`

```json
// Request
{ "intervention_id": "int_a1b2c3d4e5f6", "user_action": "dismissed" }
```

Valid `user_action` values: `dismissed` · `engaged` · `snoozed` · `timed_out` · `natural_recovery` · `system_cancelled`

---

### Stress & Learning

#### `GET /api/stress-integral`

```json
{
  "current_value": 12.5,
  "threshold": 500.0,
  "should_break": false,
  "sensitivity_multiplier": 1.0,
  "timestamp": 12345.6
}
```

#### `GET /api/helpfulness/summary`

```json
{
  "total_interventions": 15,
  "mean_reward": 0.62,
  "engagement_rate": 0.47,
  "recent_rewards": [0.5, 0.8, 0.3, 0.9, 0.6],
  "timestamp": 12345.6
}
```

#### `POST /shutdown`

Gracefully shuts down the daemon. Requires `Authorization: Bearer <token>`.

---

### Consent

#### `GET /consent/level`

Returns the current consent ladder state for every action type.

```json
{
  "levels": {
    "close_tab": { "level": "ask", "decay_until": 12345.6 },
    "rearrange_workspace": { "level": "auto", "decay_until": null }
  },
  "timestamp": 12345.6
}
```

#### `POST /consent/reset`

Resets the consent ladder to defaults and returns the post-reset state.

```json
{ "reset": true, "levels": { /* same shape as GET /consent/level */ }, "timestamp": 12345.6 }
```

---

### Projects

#### `GET /api/projects`

Lists the configured project launch profiles.

```json
{ "projects": [ { "name": "thesis", "apps": ["Zotero", "Obsidian"], "browser_tabs": ["..."] } ] }
```

#### `POST /api/launch/{project_name}`

Applies a saved project workspace (opens apps, restores tabs, focuses editor). Returns a flat result.

```json
{ "launched": true, "project_name": "thesis", "errors": [] }
```

On failure `launched` is `false` and `errors` carries a human-readable diagnosis.

---

## WebSocket Protocol

Endpoint: `ws://127.0.0.1:9473`

Message envelope:
```json
{ "type": "<TYPE>", "payload": { ... }, "timestamp": 12345.6, "sequence": 42 }
```

The canonical enumeration of every `type` literal lives in `cortex/libs/schemas/ws_message_types.py::MessageType`; the codegen step emits a matching TypeScript union, so extension dispatch sites fail type-check if a literal is ever typo'd.

### Auth handshake

The very first frame the client sends on every new connection MUST be `AUTH`:

```json
{ "type": "AUTH", "payload": { "auth_token": "<contents of auth.token>" } }
```

The server replies with `AUTH_OK` (and, if a cached `STATE_UPDATE` exists, replays it immediately so the client sees current state on attach). Only after `AUTH_OK` may the client send `IDENTIFY` or any other frame.

Any non-`AUTH` frame sent before the handshake completes is logged as `AUTH_REJECTED` and the server closes the socket with WebSocket close code `1011` (`"auth required"`). `SHUTDOWN` carries a defense-in-depth inline token check on top of this gate.

### Message Types

#### Client → server (inbound, dispatched by `_process_message`)

| Type | Description |
|------|-------------|
| `AUTH` | Capability-token handshake; MUST be the first frame on every connection |
| `IDENTIFY` | Declares `client_type` (`vscode` · `chrome` · `desktop`) — sent after `AUTH_OK` |
| `USER_ACTION` | User dismissed / engaged / snoozed an intervention |
| `ACTION_EXECUTE` | User invoked a `SuggestedAction` from the overlay |
| `USER_RATING` | Thumbs up/down on an intervention outcome |
| `CONTEXT_RESPONSE` | Reply to a `CONTEXT_REQUEST`; resolves a pending future |
| `SETTINGS_SYNC` | Bidirectional — push new settings or receive current ones |
| `ACTIVITY_SYNC` | Per-tab activity records for aggregation |
| `TAB_RELEVANCE_FEEDBACK` | User-reported relevance signal for the tab triage classifier |
| `LEETCODE_CONTEXT_UPDATE` | Live LeetCode DOM/code telemetry from the content script |
| `INTERVENTION_APPLIED` | Extension confirms it applied (or failed to apply) a plan |
| `SHUTDOWN` | Request graceful daemon shutdown; payload MUST include `auth_token` |

#### Server → client (outbound)

| Type | Description |
|------|-------------|
| `AUTH_OK` | Acknowledgment of a successful `AUTH` handshake |
| `STATE_UPDATE` | Periodic state estimate broadcast (every ~500ms) |
| `INTERVENTION_TRIGGER` | Intervention plan + UI hints ready to show |
| `INTERVENTION_RESTORE` | Explicit cue for clients to undo their workspace mutations |
| `CONTEXT_REQUEST` | Daemon asks a specific `client_type` for live workspace context |
| `ACTIVE_RECALL` | Active-recall prompt (e.g. recap before context switch) |
| `BREATHING_OVERLAY` | Breathing pacer overlay cue (4-7-8 by default) |
| `PRE_BREAK_WARNING` | Heads-up that a break will be recommended shortly |
| `MORNING_BRIEFING` | Daily kickoff summary delivered to the popup |
| `COPILOT_THROTTLE` | Throttle/unthrottle signal for the editor copilot adapter |
| `AMBIENT_STATE_UPDATE` | Lightweight state heartbeat for the ambient overlay |

#### LeetCode adapter cues (server → Chrome only)

Emitted by `LeetCodeAdapter.execute` and targeted at `client_type=chrome`.

| Type | Description |
|------|-------------|
| `LEETCODE_SHOW_SCRATCHPAD` | Inject the scratchpad overlay (problem reframing prompts) |
| `LEETCODE_SHOW_PATTERN_LADDER` | Surface the pattern ladder scaffold |
| `LEETCODE_SHOW_LOCKOUT` | Destructive-struggle gate — block the editor |
| `LEETCODE_SHOW_CONSOLIDATION` | Post-solve consolidation prompt |
| `LEETCODE_SHOW_SUBMISSION_GATE` | Pre-submit sanity check |
| `LEETCODE_SHOW_SOLUTION_FRICTION` | Friction overlay before revealing the editorial |
| `LEETCODE_SHOW_SESSION_BRIEFING` | Daily LeetCode briefing for popup/newtab |
| `LEETCODE_LOCK_EDITOR` | Force-focus the LeetCode editor |
| `LEETCODE_INTERCEPT_SUBMIT` | Intercept submit until acknowledgement |
| `LEETCODE_GATE_SOLUTIONS` | Gate the editorial / community-solution tab |
| `LEETCODE_AI_RESTATEMENT_CHECK` | AI-powered restatement check |
| `LEETCODE_AI_COMPREHENSION_CHECK` | AI-powered comprehension check (examples / edges) |
| `LEETCODE_AI_HYPOTHESIS_CHECK` | AI-powered hypothesis / approach check |
| `LEETCODE_AI_STUCK_ANALYSIS` | AI-powered stuck-analysis explanation |
| `LEETCODE_AI_SESSION_BRIEFING` | AI-powered session-briefing generation |

### `STATE_UPDATE`

```json
{
  "type": "STATE_UPDATE",
  "payload": {
    "state": "FLOW",
    "confidence": 0.87,
    "scores": { "flow": 0.87, "hypo": 0.05, "hyper": 0.08, "recovery": 0.0 },
    "signal_quality": { "physio": 0.9, "kinematics": 0.85, "telemetry": 0.95, "overall": 0.9 },
    "dwell_seconds": 45.2,
    "reasons": ["Good HRV", "Normal blink rate"]
  }
}
```

### `USER_ACTION`

```json
{
  "type": "USER_ACTION",
  "payload": { "action": "dismissed", "intervention_id": "int_a1b2c3d4e5f6" }
}
```

Valid actions: `dismissed` · `engaged` · `snoozed`

### `IDENTIFY`

Sent after `AUTH_OK`. Carries the client's role.

```json
{
  "type": "IDENTIFY",
  "payload": { "client_type": "chrome" }
}
```

Valid client types: `vscode` · `chrome` · `desktop`

### Connection Behavior

- New clients receive the latest cached `STATE_UPDATE` immediately after `AUTH_OK`
- Dead connections are cleaned up on next broadcast
- Extensions should reconnect with exponential backoff, replaying the `AUTH` → `IDENTIFY` sequence on each reconnect
