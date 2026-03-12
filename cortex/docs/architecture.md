# Architecture

## Overview

Cortex is a five-layer real-time pipeline that detects cognitive overwhelm via webcam-based biofeedback and autonomously restructures the user's digital workspace.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Desktop Shell (PySide6)                  │
│            Tray icon · Dashboard · Overlay · Settings           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  L1: Bio-Extraction    L2: State Engine    L3: Context Engine   │
│  ┌─────────────────┐   ┌───────────────┐   ┌────────────────┐  │
│  │ Webcam Capture   │   │ Feature Fusion│   │ VS Code Adapter│  │
│  │ Face Tracker     │──▶│ Rule Scorer   │   │ Chrome Adapter │  │
│  │ rPPG (POS/CHROM)│   │ EMA Smoother  │   │ Terminal Adapt.│  │
│  │ Blink Detector   │   │ Trigger Policy│   │ App Classifier │  │
│  │ Head Pose        │   └───────┬───────┘   └───────┬────────┘  │
│  │ Posture Tracker  │           │                   │           │
│  │ Input Hooks      │           ▼                   ▼           │
│  │ Window Tracker   │   ┌───────────────────────────────────┐   │
│  └─────────────────┘   │  L4: LLM Scaffolding Engine       │   │
│                         │  Remote Qwen-3-8B (gwhiz1)        │   │
│                         │  Prompt Templates · JSON Parser    │   │
│                         │  LRU Cache · Ollama Fallback       │   │
│                         └───────────────┬───────────────────┘   │
│                                         │                       │
│                                         ▼                       │
│                         ┌───────────────────────────────────┐   │
│                         │  L5: Intervention Engine           │   │
│                         │  Trigger · Snapshot · Planner      │   │
│                         │  Executor · Restore Manager        │   │
│                         └───────────────────────────────────┘   │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                  API Gateway (FastAPI + WebSocket)               │
│            REST :9472 · WebSocket :9473 · Service Registry      │
├──────────────────┬──────────────────────┬───────────────────────┤
│  VS Code Ext.    │    Chrome Ext.       │    Desktop Shell      │
│  (TypeScript)    │    (Plasmo/React)    │    (PySide6)          │
└──────────────────┴──────────────────────┴───────────────────────┘
```

## Layer Details

### L1: Bio-Extraction Pipeline

Captures raw biometric and behavioral signals from the user.

| Component | Input | Output | Rate |
|-----------|-------|--------|------|
| `capture_service/webcam.py` | Camera device | Timestamped frames | 30 FPS |
| `capture_service/face_tracker.py` | Frame | Landmarks + bounding box | 30 FPS |
| `capture_service/quality.py` | Frame + landmarks | Quality score (brightness, blur, jitter) | 30 FPS |
| `physio_engine/roi_extractor.py` | Frame + landmarks | RGB traces (forehead, cheeks) | 30 FPS |
| `physio_engine/rppg.py` | RGB traces | BVP signal (POS/CHROM/green) | 30 FPS |
| `physio_engine/pulse_estimator.py` | 10s BVP window | HR (BPM), RMSSD, HR delta | 1 Hz |
| `kinematics_engine/blink_detector.py` | Eye landmarks | Blink rate, suppression score | 30 FPS |
| `kinematics_engine/head_pose.py` | Face landmarks | Pitch/yaw/roll, jitter, freeze | 30 FPS |
| `kinematics_engine/posture.py` | Pose landmarks | Slump score, forward lean, shoulder drop | 30 FPS |
| `telemetry_engine/input_hooks.py` | OS events | Mouse/keyboard events | 60→10 Hz |
| `telemetry_engine/window_tracker.py` | OS APIs | Window switch events | On change |
| `telemetry_engine/feature_aggregator.py` | Raw events | TelemetryFeatures | 1 Hz |

### L2: State Classification Engine

Fuses multi-modal features into a unified cognitive state estimate.

**Feature Fusion** (`feature_fusion.py`): Merges PhysioFeatures, KinematicFeatures, and TelemetryFeatures into a 12-dimensional FeatureVector every 500ms. Handles missing channels via confidence weighting.

**Rule Scorer** (`rule_scorer.py`): Computes state scores using weighted sub-scorers:

| Sub-scorer | Weight | Signal |
|------------|--------|--------|
| Pulse elevation | 0.20 | HR above baseline |
| HRV drop | 0.15 | RMSSD below baseline |
| Blink suppression | 0.12 | Low blink rate + blink suppression score |
| Posture collapse | 0.08 | Slump + forward lean + shoulder drop |
| Mouse thrashing | 0.15 | High velocity variance + jerk |
| Window switching | 0.15 | Rapid window/tab switching |
| Workspace complexity | 0.15 | Error count + tab count + context complexity |

**Smoother** (`smoother.py`): Applies EMA (alpha=0.3), hysteresis (entry=0.85, exit=0.70), and dwell time enforcement (HYPER: 8s, HYPO: 15s) before emitting state transitions.

**States**: `FLOW` (productive) · `HYPER` (overwhelmed) · `HYPO` (disengaged) · `RECOVERY` (returning to flow)

### L3: Context Engine

Gathers workspace context from VS Code, Chrome, and terminal to inform LLM interventions.

- **App Classifier** — detects workspace mode: `coding_debugging`, `reading_docs`, `browsing`, `terminal_errors`, `mixed`
- **Editor Adapter** — VS Code extension WebSocket → file path, diagnostics, cursor symbol, visible code
- **Browser Adapter** — Chrome extension WebSocket → active tab, all tabs, content excerpt (≤2000 tokens), tab classification
- **Terminal Adapter** — captures recent output, detects error blocks, identifies root-cause

Output: `TaskContext` with complexity score (0.0–1.0).

### L4: LLM Scaffolding Engine

Sends state + context to the LLM and parses structured intervention plans.

- **Remote client** — SSH tunnel to gwhiz1, OpenAI-compatible API via httpx
- **Local fallback** — Ollama REST API
- **Prompt templates** — 5 mode-specific templates: `debug_error_summary`, `code_focus_reduction`, `browser_tab_reduction`, `micro_step_planner`, `calm_overlay_writer`
- **Parser** — fault-tolerant JSON parsing with 2-retry, handles missing braces, trailing commas, unescaped quotes
- **Cache** — LRU by context hash, 5-minute TTL

### L5: Intervention Engine

Validates LLM plans and applies workspace modifications.

1. **Trigger** — evaluates whether to intervene based on state, confidence, complexity, cooldown, and dismissal history
2. **Snapshot** — captures pre-intervention workspace state (fold states, tab visibility, scroll positions)
3. **Planner** — validates plan constraints (no destructive actions, headline <15 words, 1-3 steps)
4. **Executor** — applies fold commands to VS Code, tab hide/dim to Chrome, desktop overlay
5. **Restore** — restores from snapshot on dismiss/timeout/recovery, auto-timeout at 5 minutes

### API Gateway

FastAPI REST API on port 9472, WebSocket on port 9473.

- **Service Registry** — dependency injection for all services
- **REST endpoints** — feature submission, state inference, context building, LLM planning, intervention control
- **WebSocket** — real-time bidirectional communication with extensions: `STATE_UPDATE` (500ms), `INTERVENTION_TRIGGER`, `USER_ACTION`

## Data Flow

```
Webcam Frame (30 FPS)
  │
  ├──▶ Face Tracker ──▶ ROI Extractor ──▶ rPPG ──▶ Pulse Estimator ──▶ PhysioFeatures
  │                  ├──▶ Blink Detector ─────────────────────────────▶ KinematicFeatures
  │                  ├──▶ Head Pose ──────────────────────────────────▶
  │                  └──▶ Posture ────────────────────────────────────▶
  │
  └──▶ Quality Gate (brightness, blur, jitter)
         │
         ▼
Mouse/Keyboard/Window Events ──▶ Feature Aggregator ──▶ TelemetryFeatures
                                                             │
                                                             ▼
PhysioFeatures + KinematicFeatures + TelemetryFeatures ──▶ Feature Fusion (500ms)
                                                             │
                                                             ▼
                                                    12-dim FeatureVector
                                                             │
                                                             ▼
                                                    Rule Scorer ──▶ StateScores
                                                             │
                                                             ▼
                                                    EMA Smoother ──▶ StateEstimate
                                                             │
                                    ┌────────────────────────┤
                                    ▼                        ▼
                            WebSocket Broadcast      Trigger Policy
                            (STATE_UPDATE, 500ms)         │
                                                          ▼
                                                  Context Engine ──▶ TaskContext
                                                          │
                                                          ▼
                                                  LLM Engine ──▶ InterventionPlan
                                                          │
                                                          ▼
                                                  Intervention Engine
                                                  (snapshot → execute → restore)
```

## Repository Structure

```
cortex/
├── libs/
│   ├── config/          # Settings, defaults.yaml
│   ├── schemas/         # Pydantic models (features, state, context, intervention)
│   ├── signal/          # DSP: bandpass filters, peak detection, windowing
│   ├── logging/         # Structured JSON logging
│   └── utils/           # Platform detection, async helpers
├── services/
│   ├── capture_service/   # Webcam, face tracker, quality gate
│   ├── physio_engine/     # ROI extraction, rPPG, pulse estimation
│   ├── kinematics_engine/ # Blink, head pose, posture
│   ├── telemetry_engine/  # Input hooks, window tracker, feature aggregation
│   ├── state_engine/      # Feature fusion, rule scorer, smoother, trigger
│   ├── context_engine/    # Editor/browser/terminal adapters, app classifier
│   ├── llm_engine/        # LLM client, prompts, parser, cache
│   ├── intervention_engine/ # Trigger, snapshot, planner, executor, restore
│   └── api_gateway/       # FastAPI app, REST routes, WebSocket server
├── scripts/               # Dev tools: run_dev, calibrate, replay, seed_config
├── apps/                  # VS Code ext, Chrome ext, desktop shell (future)
├── tests/
│   ├── unit/              # Per-module unit tests
│   ├── integration/       # Cross-service integration tests
│   └── fixtures/          # Sample data (features, context, LLM responses)
└── docs/                  # This documentation
```

## Performance Targets

| Metric | Budget |
|--------|--------|
| Frame processing (capture → landmarks) | < 50 ms |
| Feature fusion (3 channels → vector) | < 10 ms |
| State classification (vector → state) | < 5 ms |
| Full signal-to-state pipeline | < 200 ms |
| LLM response (remote Qwen) | < 10 s |
| Intervention apply/restore | < 500 ms |

## Privacy Architecture

- No video frames are saved to disk
- No face images leave the device
- No biometric data (HR, HRV, blink rate) is sent to the LLM
- LLM receives only workspace context (file paths, error messages, tab titles)
- Chrome extension uses only `activeTab` and `scripting` permissions
- All sensing runs locally; only LLM inference uses the remote GPU
