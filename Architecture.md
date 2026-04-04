# Architecture

## System Diagram

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
│                         │  Ollama (local fallback)          │   │
│                         │  Prompt Templates · JSON Parser   │   │
│                         │  LRU Cache · Contextual Bandit    │   │
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

---

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
                                                    v2.0 Detectors
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

---

## L1: Bio-Extraction Components

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
| `kinematics_engine/posture.py` | Pose landmarks | Slump, forward lean, shoulder drop | 30 FPS |
| `telemetry_engine/input_hooks.py` | OS events | Mouse/keyboard events | 60→10 Hz |
| `telemetry_engine/window_tracker.py` | OS APIs | Window switch events | On change |
| `telemetry_engine/feature_aggregator.py` | Raw events | TelemetryFeatures | 1 Hz |

---

## L2: State Engine Components

**Feature Fusion** — merges PhysioFeatures, KinematicFeatures, and TelemetryFeatures into a 12-dimensional FeatureVector every 500ms with confidence weighting for missing channels.

**Rule Scorer** — weighted sub-scorers:

| Sub-scorer | Weight |
|------------|--------|
| Pulse elevation | 0.20 |
| HRV drop | 0.15 |
| Blink suppression | 0.12 |
| Posture collapse | 0.08 |
| Mouse thrashing | 0.15 |
| Window switching | 0.15 |
| Workspace complexity | 0.15 |

**Smoother** — EMA (α=0.3), hysteresis (entry=0.85, exit=0.70), dwell enforcement (HYPER: 8s, HYPO: 15s).

**v2.0 Specialized Detectors:**

| Detector | Purpose |
|----------|---------|
| `stress_integral` | Cumulative HRV suppression for biology-driven breaks |
| `zombie_detector` | HYPO + browser + low mouse + high blink for 90+ sec |
| `rabbit_hole` | Goal drift detection (<30% alignment after 10+ min) |
| `longitudinal` | Daily HRV baseline tracking via linear regression |
| `amygdala_hijack` | Acute stress spike detection (LeetCode mode) |
| `destructive_struggle` | Productive→destructive transition (LeetCode mode) |
| `parasympathetic_rebound` | Optimal learning window post-accept (LeetCode mode) |

---

## L4: LLM Engine

- **10 prompt templates**: `debug_error_summary`, `code_focus_reduction`, `browser_tab_reduction`, `micro_step_planner`, `calm_overlay_writer`, `breathing_overlay`, `active_recall`, `rabbit_hole`, `alignment_summary`, `deep_bottleneck_diagnosis`
- **Fault-tolerant parser** — 2-retry JSON parsing with recovery for missing braces, trailing commas, and unescaped quotes
- **LRU cache** — keyed by context hash, 5-minute TTL

---

## L5: Intervention Engine

1. **Consent Ladder** — per-action-type trust tracking. 5 approvals escalate, 3 rejections de-escalate.
2. **Trigger** — evaluates state, confidence, complexity, cooldown (60s), and dismissal history
3. **Snapshot** — captures fold states, tab visibility, scroll positions before acting
4. **Planner** — validates plan constraints (no destructive actions, headline ≤15 words, 1-3 steps)
5. **Executor** — applies fold commands to VS Code, tab operations to Chrome, desktop overlay
6. **Restore** — restores from snapshot on dismiss/timeout/recovery (auto-timeout: 5 min)

---

## Learning Loop

| Component | Purpose |
|-----------|---------|
| **HelpfulnessTracker** | Computes reward: recovery (40%) + complexity reduction (15%) + explicit rating (30%) + implicit (15%) |
| **ContextualBandit** (LinUCB) | Selects best intervention arm from 7 options using 8-dimensional context features |
| **TabRelevanceTracker** | Per-domain EMA learning (α=0.3, 90-day TTL) from Keep button feedback |

---

## Repository Layout

```
cortex/
├── libs/
│   ├── adapters/        # CortexAdapter protocol, registry, LeetCode adapter
│   ├── config/          # Settings, defaults.yaml
│   ├── schemas/         # Pydantic models (features, state, context, intervention, activity)
│   ├── signal/          # DSP: bandpass filters, peak detection, windowing
│   ├── store/           # Persistence: Redis + in-memory fallback
│   ├── logging/         # Structured JSON logging
│   └── utils/           # Platform detection, async helpers, macOS Keychain
├── services/
│   ├── capture_service/     # Webcam, face tracker, quality gate
│   ├── physio_engine/       # ROI extraction, rPPG, pulse estimation
│   ├── kinematics_engine/   # Blink, head pose, posture
│   ├── telemetry_engine/    # Input hooks, window tracker, feature aggregation
│   ├── state_engine/        # Feature fusion, rule scorer, smoother, v2.0 detectors
│   ├── context_engine/      # Editor/browser/terminal adapters, app classifier
│   ├── llm_engine/          # Azure OpenAI, Ollama, prompts, parser, cache
│   ├── intervention_engine/ # Trigger, snapshot, planner, executor, restore
│   ├── consent/             # ConsentLadder, ConsentPolicy (5-level trust)
│   ├── eval/                # HelpfulnessTracker, ContextualBandit, TabRelevanceTracker
│   ├── handover/            # ShutdownDetector, HandoverSnapshot, MorningBriefing
│   ├── activity_tracker/    # ActivityAggregator, ActivitySummarizer
│   ├── api_gateway/         # FastAPI app, REST routes, WebSocket server
│   └── runtime_daemon.py    # Main orchestrator
├── scripts/               # run_dev, calibrate, native_host, install_native_host, seed_config
│   └── cortex.spec        # PyInstaller spec for macOS .app bundle
├── apps/
│   ├── browser_extension/ # Chrome + Edge (Plasmo/React MV3)
│   ├── vscode_extension/  # VS Code (TypeScript)
│   └── desktop_shell/     # PySide6 tray, two-tab dashboard, overlay, in-process daemon
│       └── controller.py  # In-process daemon controller for bundled .app
└── tests/
    ├── unit/              # 40+ per-module unit tests
    └── integration/       # Cross-service integration tests
```

---

## Performance Targets

| Metric | Budget |
|--------|--------|
| Frame processing (capture → landmarks) | < 50 ms |
| Feature fusion (3 channels → vector) | < 10 ms |
| State classification (vector → state) | < 5 ms |
| Full signal-to-state pipeline | < 200 ms |
| LLM response (Azure OpenAI) | < 10 s |
| Intervention apply/restore | < 500 ms |

---

## Ports

| Port | Service |
|------|---------|
| 9471 | Launcher agent (optional HTTP launcher from browser) |
| 9472 | FastAPI REST API |
| 9473 | WebSocket server |
