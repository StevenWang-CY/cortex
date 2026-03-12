# Cortex — The Somatic Workspace Engine

Real-time biofeedback engine that detects cognitive overwhelm via webcam and autonomously restructures your digital workspace.

## What It Does

Cortex monitors you through your webcam (no video saved) and input devices to detect when you're overwhelmed — stuck in a debugging spiral, thrashing between tabs, or disengaged. When it detects trouble, it uses an LLM to generate a focused intervention: simplifying your workspace, folding irrelevant code, hiding distracting tabs, and showing you the one thing to focus on.

**Three states detected:**
- **FLOW** — productive, focused work
- **HYPER** — overwhelmed, thrashing, stuck
- **HYPO** — disengaged, drifting, bored

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Configure
cp .env.example .env

# Initialize storage
python -m cortex.scripts.seed_config

# Calibrate (2 minutes, or --simulate for testing)
cortex-calibrate --simulate

# Run
cortex-dev
```

API: `http://127.0.0.1:9472` | WebSocket: `ws://127.0.0.1:9473`

## Architecture

Five-layer pipeline processing at 30 FPS with <200ms signal-to-state latency:

```
Webcam → Bio-Extraction → State Classification → Context Engine → LLM → Intervention
```

| Layer | Purpose | Key Tech |
|-------|---------|----------|
| L1: Bio-Extraction | rPPG heart rate, blink detection, posture, input telemetry | OpenCV, MediaPipe, pynput |
| L2: State Engine | Fuse features → classify FLOW/HYPER/HYPO/RECOVERY | Rule scoring, EMA smoothing, hysteresis |
| L3: Context Engine | Gather workspace context from VS Code, Chrome, terminal | WebSocket adapters |
| L4: LLM Scaffolding | Generate structured intervention plans | Qwen-3-8B (remote), Ollama (fallback) |
| L5: Intervention | Apply/restore workspace modifications | Snapshot/restore, fold, tab management |

## Overwhelm Scoring

Seven weighted signals combined into a cognitive state estimate:

| Signal | Weight | Source |
|--------|--------|--------|
| Pulse elevation | 20% | rPPG via webcam |
| HRV drop | 15% | Inter-beat interval analysis |
| Blink suppression | 12% | Eye aspect ratio tracking |
| Posture collapse | 8% | Shoulder/lean detection |
| Mouse thrashing | 15% | Velocity variance + jerk |
| Window switching | 15% | App/tab switch frequency |
| Workspace complexity | 15% | Error count + tab count + context |

## Tech Stack

**Core:** Python 3.11+, FastAPI, Pydantic, WebSocket

**Computer Vision:** OpenCV, MediaPipe FaceMesh/Pose

**Signal Processing:** SciPy (Butterworth filters, Welch PSD), NumPy

**LLM:** Qwen-3-8B via vLLM (remote GPU), Ollama (local fallback)

**Telemetry:** pynput (keyboard/mouse), pyobjc (macOS window tracking)

**Desktop UI:** PySide6 (tray, dashboard, overlay)

**Extensions:** TypeScript (VS Code), Plasmo/React (Chrome)

## Privacy

Cortex is privacy-first by design:

- **No video recording** — frames are processed in-memory and immediately discarded
- **No face images leave the device** — all computer vision runs locally
- **No biometrics in LLM requests** — the LLM receives only workspace context (file paths, error messages, tab titles), never heart rate, HRV, blink data, or any physiological measurements
- **Minimal permissions** — Chrome extension uses only `activeTab` and `scripting`
- **Local-first sensing** — only LLM inference uses the network (SSH tunnel to GPU)
- **No cloud dependencies** — everything runs on your machine and your GPU

## Project Structure

```
cortex/
├── libs/                    # Shared libraries
│   ├── config/              # Settings, defaults.yaml
│   ├── schemas/             # Pydantic models
│   ├── signal/              # DSP filters, peak detection
│   ├── logging/             # Structured JSON logging
│   └── utils/               # Platform, async helpers
├── services/                # Core services
│   ├── capture_service/     # Webcam + face tracking
│   ├── physio_engine/       # rPPG heart rate + HRV
│   ├── kinematics_engine/   # Blink, head pose, posture
│   ├── telemetry_engine/    # Mouse, keyboard, windows
│   ├── state_engine/        # Feature fusion + classification
│   ├── context_engine/      # Workspace adapters
│   ├── llm_engine/          # LLM client + prompts
│   ├── intervention_engine/ # Trigger, execute, restore
│   └── api_gateway/         # REST + WebSocket server
├── scripts/                 # Developer tools
├── apps/                    # Extension apps
├── tests/                   # Unit + integration tests
└── docs/                    # Documentation
```

## Documentation

- [Setup Guide](docs/setup.md) — installation, SSH, configuration
- [Architecture](docs/architecture.md) — system design, data flow, layer details
- [API Reference](docs/apis.md) — REST endpoints, WebSocket protocol
- [Calibration](docs/calibration.md) — baseline capture process
- [Writing Adapters](docs/adapters.md) — how to add new workspace adapters

## Development

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=cortex --cov-report=html

# Type checking
mypy cortex/

# Linting
ruff check cortex/

# Standalone webcam test
cortex-capture

# Replay a recorded session
python -m cortex.scripts.replay_session storage/sessions/latest.jsonl
```

## License

MIT
