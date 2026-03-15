# Architecture

## Overview

Cortex is a five-layer real-time pipeline that detects cognitive overwhelm via webcam-based biofeedback and autonomously restructures the user's digital workspace. It includes a progressive consent system, contextual bandit learning loop, activity tracking, and specialized detectors for coding and study workflows.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Desktop Shell (PySide6)                  │
│       Tray icon · Dashboard · Overlay · Onboarding · Settings   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  L1: Bio-Extraction    L2: State Engine    L3: Context Engine   │
│  ┌─────────────────┐   ┌───────────────┐   ┌────────────────┐  │
│  │ Webcam Capture   │   │ Feature Fusion│   │ VS Code Adapter│  │
│  │ Face Tracker     │──▶│ Rule Scorer   │   │ Chrome Adapter │  │
│  │ rPPG (POS/CHROM)│   │ EMA Smoother  │   │ Terminal Adapt.│  │
│  │ Blink Detector   │   │ Trigger Policy│   │ App Classifier │  │
│  │ Head Pose        │   │ v2.0 Detectors│   └───────┬────────┘  │
│  │ Posture Tracker  │   └───────┬───────┘           │           │
│  │ Input Hooks      │           │                   │           │
│  │ Window Tracker   │           ▼                   ▼           │
│  └─────────────────┘   ┌───────────────────────────────────┐   │
│                         │  L4: LLM Scaffolding Engine       │   │
│                         │  Azure OpenAI (primary)           │   │
│                         │  Ollama (local fallback)           │   │
│                         │  Prompt Templates · JSON Parser    │   │
│                         │  LRU Cache · Contextual Bandit     │   │
│                         └───────────────┬───────────────────┘   │
│                                         │                       │
│                                         ▼                       │
│                         ┌───────────────────────────────────┐   │
│                         │  L5: Intervention Engine           │   │
│                         │  Consent Ladder · Trigger · Plan   │   │
│                         │  Executor · Restore · Learn        │   │
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

**v2.0 Detectors** (specialized signal analyzers):

| Detector | Purpose |
|----------|---------|
| `stress_integral.py` | Cumulative HRV suppression integral for biology-driven breaks |
| `zombie_detector.py` | HYPO + browser + low mouse + high blink for 90+ sec |
| `rabbit_hole.py` | Goal drift detection (<30% alignment after 10+ min) |
| `longitudinal.py` | Daily HRV baseline tracking via linear regression |
| `amygdala_hijack.py` | Acute stress spike detection (LeetCode mode) |
| `destructive_struggle.py` | Productive→destructive transition (LeetCode mode) |
| `parasympathetic_rebound.py` | Optimal learning window post-accept (LeetCode mode) |
| `leetcode_mode_resolver.py` | Maps signals to LeetCode-specific modes |

### L3: Context Engine

Gathers workspace context from VS Code, Chrome, and terminal to inform LLM interventions.

- **App Classifier** — detects workspace mode: `coding_debugging`, `reading_docs`, `browsing`, `terminal_errors`, `mixed`
- **Editor Adapter** — VS Code extension WebSocket → file path, diagnostics, cursor symbol, visible code
- **Browser Adapter** — Chrome extension WebSocket → active tab, all tabs, content excerpt (≤2000 tokens), tab classification with type-diverse sampling (30 cap from 150+ tabs)
- **Terminal Adapter** — captures recent output, detects error blocks, identifies root-cause

Output: `TaskContext` with complexity score (0.0–1.0).

### L4: LLM Scaffolding Engine

Sends state + context to the LLM and parses structured intervention plans.

- **Azure OpenAI** (primary) — Azure OpenAI API with `max_completion_tokens`, supports reasoning deployments
- **Ollama** (local fallback) — Ollama REST API on localhost:11434
- **Rule-based fallback** — built-in guidance when no LLM is available
- **Prompt templates** — 10 mode-specific templates: `debug_error_summary`, `code_focus_reduction`, `browser_tab_reduction`, `micro_step_planner`, `calm_overlay_writer`, `breathing_overlay`, `active_recall`, `rabbit_hole`, `alignment_summary`, `deep_bottleneck_diagnosis`
- **Parser** — fault-tolerant JSON parsing with 2-retry, handles missing braces, trailing commas, unescaped quotes
- **Cache** — LRU by context hash, 5-minute TTL

### L5: Intervention Engine

Validates LLM plans and applies workspace modifications.

1. **Consent Ladder** — 5-level progressive trust per action type (OBSERVE → SUGGEST → PREVIEW → REVERSIBLE_ACT → AUTONOMOUS_ACT). 5 approvals escalate, 3 rejections de-escalate.
2. **Trigger** — evaluates whether to intervene based on state, confidence, complexity, cooldown, and dismissal history
3. **Snapshot** — captures pre-intervention workspace state (fold states, tab visibility, scroll positions)
4. **Planner** — validates plan constraints (no destructive actions, headline <15 words, 1-3 steps)
5. **Executor** — applies fold commands to VS Code, tab hide/dim to Chrome, desktop overlay
6. **Restore** — restores from snapshot on dismiss/timeout/recovery, auto-timeout at 5 minutes

### Learning Loop (v2.0)

- **Helpfulness Tracker** — pre/post state snapshots → reward signal. Weights: recovery (40%), complexity reduction (15%), explicit rating (30%), implicit signals (15%).
- **Contextual Bandit** (LinUCB) — selects best intervention arm from 7 options (overlay_only, simplified_workspace, guided_mode, breathing, active_recall, circuit_breaker, none) using 8-dimensional context features.
- **Tab Relevance Tracker** — per-domain EMA learning (alpha=0.3, 90-day TTL) from Keep button feedback.

### Activity Tracker

Tracks learning progress across YouTube, Bilibili, Coursera, LeetCode, PDFs, Jupyter, and more. On return, shows a one-click resume card that seeks video, scrolls to position, or pastes saved code.

### Handover

- **Shutdown Detector** — compound fatigue signals (posture slump + HRV drop + error rate + late hour)
- **Handover Snapshot** — captures editor, terminal, browser, git diff at end-of-day
- **Morning Briefing** — LLM recap of yesterday with action items

### API Gateway

FastAPI REST API on port 9472, WebSocket on port 9473.

- **Service Registry** — dependency injection for all services
- **REST endpoints** — feature submission, state inference, context building, LLM planning, intervention control, stress integral, helpfulness summary, project launcher
- **WebSocket** — real-time bidirectional communication with extensions: `STATE_UPDATE` (500ms), `INTERVENTION_TRIGGER`, `USER_ACTION`, `SETTINGS_SYNC`, `ACTIVITY_SYNC`, `CONTEXT_REQUEST`

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
                                                    v2.0 Detectors (stress, zombie, rabbit hole, ...)
                                                             │
                                                             ▼
                                                    EMA Smoother ──▶ StateEstimate
                                                             │
                                    ┌────────────────────────┤
                                    ▼                        ▼
                            WebSocket Broadcast      Trigger Policy + Consent Ladder
                            (STATE_UPDATE, 500ms)         │
                                                          ▼
                                                  Context Engine ──▶ TaskContext
                                                          │
                                                          ▼
                                                  Bandit arm selection
                                                          │
                                                          ▼
                                                  LLM Engine ──▶ InterventionPlan
                                                          │
                                                          ▼
                                                  Intervention Engine
                                                  (snapshot → execute → restore)
                                                          │
                                                          ▼
                                                  Helpfulness Tracker → Bandit update
```

## Repository Structure

```
cortex/
├── libs/
│   ├── adapters/        # CortexAdapter protocol, registry, LeetCode adapter
│   ├── config/          # Settings, defaults.yaml
│   ├── schemas/         # Pydantic models (features, state, context, intervention, consent, eval, activity, leetcode)
│   ├── signal/          # DSP: bandpass filters, peak detection, windowing
│   ├── store/           # Persistence: Redis + in-memory fallback
│   ├── logging/         # Structured JSON logging
│   └── utils/           # Platform detection, async helpers, secrets (macOS Keychain)
├── services/
│   ├── capture_service/     # Webcam, face tracker, quality gate
│   ├── physio_engine/       # ROI extraction, rPPG, pulse estimation
│   ├── kinematics_engine/   # Blink, head pose, posture
│   ├── telemetry_engine/    # Input hooks, window tracker, feature aggregation
│   ├── state_engine/        # Feature fusion, rule scorer, smoother, trigger, v2.0 detectors
│   ├── context_engine/      # Editor/browser/terminal adapters, app classifier
│   ├── llm_engine/          # Azure OpenAI, Ollama, prompts, parser, cache
│   ├── intervention_engine/ # Trigger, snapshot, planner, executor, restore
│   ├── consent/             # ConsentLadder, ConsentPolicy (5-level trust)
│   ├── eval/                # HelpfulnessTracker, ContextualBandit, TabRelevanceTracker
│   ├── handover/            # ShutdownDetector, HandoverSnapshot, MorningBriefing
│   ├── activity_tracker/    # ActivityAggregator, ActivitySummarizer
│   ├── launcher/            # ProjectConfig, ProjectLauncher
│   ├── throttle/            # CopilotThrottle (VS Code inline suggestions)
│   ├── api_gateway/         # FastAPI app, REST routes, WebSocket server
│   └── runtime_daemon.py    # Main orchestrator — wires all services together
├── scripts/               # Dev tools: run_dev, calibrate, replay, seed_config, native_host
├── apps/
│   ├── browser_extension/ # Chrome + Edge extension (Plasmo/React MV3)
│   ├── vscode_extension/  # VS Code extension (TypeScript)
│   └── desktop_shell/     # PySide6 desktop app (tray, dashboard, overlay, onboarding)
├── tests/
│   ├── unit/              # Per-module unit tests (40+)
│   └── integration/       # Cross-service integration tests
└── docs/                  # This documentation
```

## Performance Targets

| Metric | Budget |
|--------|--------|
| Frame processing (capture → landmarks) | < 50 ms |
| Feature fusion (3 channels → vector) | < 10 ms |
| State classification (vector → state) | < 5 ms |
| Full signal-to-state pipeline | < 200 ms |
| LLM response (Azure OpenAI) | < 10 s |
| Intervention apply/restore | < 500 ms |

## Privacy Architecture

- No video frames are saved to disk
- No face images leave the device
- No biometric data (HR, HRV, blink rate) is sent to the LLM
- LLM receives only workspace context (file paths, error messages, tab titles)
- Chrome extension permissions: `activeTab`, `scripting`, `tabs`, `tabGroups`, `storage`, `alarms`, `bookmarks`, `webNavigation`, plus `<all_urls>` host permission
- All sensing runs locally; only LLM inference calls the remote API
- Azure API key can be stored in macOS Keychain instead of `.env`
- Consent-gated autonomy: no action executes without earned trust
