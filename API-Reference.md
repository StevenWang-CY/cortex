# API Reference

Cortex exposes a REST API on `http://127.0.0.1:9472` and a WebSocket server on `ws://127.0.0.1:9473`.

All request/response bodies are JSON. Timestamps are `time.monotonic()` seconds.

---

## REST API

### Health & Status

#### `GET /health`

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
{ "stress_integral": 12.5, "threshold": 30.0, "percentage": 41.7 }
```

#### `GET /api/helpfulness/summary`

```json
{
  "total_interventions": 15,
  "mean_reward": 0.62,
  "best_arm": "simplified_workspace",
  "arm_counts": {
    "overlay_only": 3,
    "simplified_workspace": 5,
    "guided_mode": 2,
    "breathing": 2,
    "active_recall": 1,
    "circuit_breaker": 1,
    "none": 1
  }
}
```

#### `POST /shutdown`

Gracefully shuts down the daemon.

---

## WebSocket Protocol

Endpoint: `ws://127.0.0.1:9473`

Message envelope:
```json
{ "type": "<TYPE>", "payload": { ... }, "timestamp": 12345.6, "sequence": 42 }
```

### Message Types

| Type | Direction | Description |
|------|-----------|-------------|
| `STATE_UPDATE` | server → client | Cognitive state broadcast every 500ms |
| `INTERVENTION_TRIGGER` | server → client | Intervention plan ready to show |
| `USER_ACTION` | client → server | User interacted with overlay |
| `IDENTIFY` | client → server | Client identifies its type on connect |
| `SETTINGS_SYNC` | bidirectional | Sync consent levels and quiet mode |
| `ACTIVITY_SYNC` | client → server | Learning activity progress report |
| `CONTEXT_REQUEST` | server → client | Request context from extension |
| `SHUTDOWN` | client → server | Request graceful daemon shutdown |

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

```json
{
  "type": "IDENTIFY",
  "payload": { "client_type": "chrome" }
}
```

Valid client types: `vscode` · `chrome` · `desktop`

### Connection Behavior

- New clients receive the latest `STATE_UPDATE` immediately on connect
- Dead connections are cleaned up on next broadcast
- Extensions should reconnect with exponential backoff
