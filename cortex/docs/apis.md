# API Reference

Cortex exposes a REST API on `http://127.0.0.1:9472` and a WebSocket server on `ws://127.0.0.1:9473`.

## REST API

Base URL: `http://127.0.0.1:9472`

All request and response bodies are JSON. Timestamps use monotonic seconds (`time.monotonic()`).

---

### Health & Status

#### `GET /health`

Health check for all registered services.

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

Current cognitive state, confidence, and signal quality.

**Response:**
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
  "pulse_variability_proxy": 55.0,
  "hr_delta_5s": 0.5,
  "valid": true
}
```

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
  "slump_score": 0.1,
  "forward_lean_score": 0.1,
  "shoulder_drop_ratio": 0.05,
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
  "click_frequency": 0.5,
  "keyboard_burst_score": 0.2,
  "keystroke_interval_variance": 500.0,
  "backspace_density": 0.05,
  "inactivity_seconds": 2.0,
  "window_switch_rate": 5.0
}
```

All feature endpoints return `{ "status": "ok", "timestamp": <float> }`.

---

### State Inference

#### `POST /state/infer`

Compute cognitive state from a fused feature vector.

**Request body:**
```json
{
  "feature_vector": {
    "pulse_norm": 0.3,
    "hrv_norm": 0.6,
    "blink_norm": 0.5,
    "posture_norm": 0.2,
    "mouse_velocity_norm": 0.4,
    "mouse_jerk_norm": 0.1,
    "click_burst_norm": 0.1,
    "keyboard_burst_norm": 0.2,
    "inactivity_norm": 0.05,
    "window_switch_norm": 0.3,
    "backspace_norm": 0.05,
    "complexity_norm": 0.4,
    "timestamp": 1000.5
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
    "state": "FLOW",
    "confidence": 0.87,
    "scores": {
      "flow": 0.87,
      "hypo": 0.05,
      "hyper": 0.08,
      "recovery": 0.0
    },
    "reasons": ["Good HRV", "Normal blink rate", "Steady input"],
    "signal_quality": { "physio": 0.9, "kinematics": 0.85, "telemetry": 0.95, "overall": 0.9 },
    "timestamp": 1000.5,
    "dwell_seconds": 45.2
  },
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

#### `POST /llm/plan`

Request an intervention plan from the LLM engine.

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

Apply an intervention plan to the workspace.

**Request body:**
```json
{
  "plan": { "intervention_id": "int_a1b2c3d4e5f6", "...": "..." }
}
```

**Response:**
```json
{
  "applied": true,
  "snapshot": {
    "intervention_id": "int_a1b2c3d4e5f6",
    "timestamp": 1002.2,
    "fold_states": [],
    "editor_visible_range": [45, 95],
    "tab_visibility": [],
    "active_tab_id": null,
    "overlay_present": false,
    "terminal_scroll_position": null
  },
  "timestamp": 1002.3
}
```

#### `POST /intervention/restore`

Restore workspace to pre-intervention state.

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

### Message Types

#### `STATE_UPDATE` (server → client)

Broadcast every 500ms to all connected clients.

```json
{
  "type": "STATE_UPDATE",
  "payload": {
    "state": "FLOW",
    "confidence": 0.87,
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
    "reasons": ["Good HRV", "Normal blink rate"]
  },
  "timestamp": 12345.6,
  "sequence": 42
}
```

#### `INTERVENTION_TRIGGER` (server → client)

Sent when the intervention engine triggers an intervention.

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
    "hide_targets": ["sidebar", "terminal"],
    "ui_plan": {
      "dim_background": true,
      "show_overlay": true,
      "fold_unrelated_code": true,
      "intervention_type": "simplified_workspace"
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

### Connection Behavior

- New clients receive the latest `STATE_UPDATE` immediately on connection
- Dead connections are automatically cleaned up on next broadcast
- The server auto-reconnects if the `websockets` package is available
- Extensions should implement reconnection with exponential backoff
