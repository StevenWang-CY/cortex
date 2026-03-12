# Cortex

**Cortex** is a real-time biofeedback engine that watches you work — through your webcam and input devices — and intervenes when it detects cognitive overwhelm. It restructures your digital workspace so you can get back to focused work without having to think about it.

---

## How It Works

Cortex runs a five-layer pipeline at 30 FPS, entirely on your machine:

1. **Bio-Extraction** — extracts heart rate and HRV from your face via rPPG (no camera storage), tracks blink rate, head pose, and posture via MediaPipe, and monitors mouse/keyboard patterns via pynput.
2. **State Classification** — fuses seven signals into a cognitive state score every 500ms. Uses rule-based scoring with EMA smoothing and hysteresis to classify you as FLOW, HYPER, HYPO, or RECOVERY.
3. **Context Engine** — when an intervention is warranted, gathers workspace context: open file + diagnostics from VS Code, active tab + content from Chrome, recent terminal output.
4. **LLM Scaffolding** — sends workspace context (no biometrics) to a Qwen-3-8B model running on a remote GPU via SSH tunnel. The model returns a structured intervention plan: headline, 1–3 micro-steps, and what to hide.
5. **Intervention Engine** — executes the plan: folds irrelevant code in VS Code, collapses non-essential browser tabs, shows a calming overlay with the steps and a 4-7-8 breathing pacer. Snapshots workspace state first. Restores everything on dismiss or recovery.

---

## States

| State | Meaning | Trigger |
|-------|---------|---------|
| **FLOW** | Focused, productive | Baseline |
| **HYPER** | Overwhelmed, thrashing, stuck | High HR + mouse jerk + window switching + errors |
| **HYPO** | Disengaged, drifting | Low blink rate + inactivity + flat telemetry |
| **RECOVERY** | Returning to focus | Transitioning out of HYPER/HYPO |

Interventions trigger on HYPER with confidence > 0.85, workspace complexity > 0.6, and a 60-second cooldown between triggers.

---

## Setup

**Requirements:** Python 3.11+, macOS (primary target), webcam, SSH access to a GPU for LLM inference.

```bash
# Enter the Python project root
cd cortex

# Install
pip install -e ".[dev]"

# Copy and edit config
cp .env.example .env

# Initialize storage and default config
python -m cortex.scripts.seed_config

# Set up SSH tunnel to LLM server
bash scripts/setup_ssh_tunnel.sh --background

# Calibrate personal baselines (2 min)
cortex-calibrate

# Start everything
cortex-dev
```

REST API runs at `http://127.0.0.1:9472`. WebSocket at `ws://127.0.0.1:9473`.

---

## Extensions

Install both for full context gathering. Without them, Cortex still works using input telemetry and posture signals alone.

**VS Code** — built with TypeScript, lives in `apps/vscode_extension/`. Provides active file, diagnostics, and symbol at cursor. Receives fold commands from the intervention engine.

**Chrome** — built with Plasmo + React (Manifest V3), lives in `apps/browser_extension/`. Provides tab titles/URLs and active-tab content excerpt. Receives tab hide/restore commands.

---

## Signals & Weights

| Signal | Weight | How It's Measured |
|--------|--------|-------------------|
| Pulse elevation | 20% | rPPG from forehead/cheek ROI vs. personal baseline |
| HRV drop | 15% | RMSSD from inter-beat intervals |
| Blink suppression | 12% | Eye Aspect Ratio below threshold for extended period |
| Mouse thrashing | 15% | Velocity variance + jerk score |
| Window switching | 15% | App/tab switch rate per minute |
| Workspace complexity | 15% | Diagnostic count + tab count + context density |
| Posture collapse | 8% | Shoulder drop ratio + forward lean angle |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Webcam (30 FPS)                       │
└──────────────────────────┬──────────────────────────────────┘
                           │
          ┌────────────────▼─────────────────┐
          │  L1: Bio-Extraction              │
          │  rPPG · Blink · Pose · Telemetry │
          └────────────────┬─────────────────┘
                           │  FeatureVector (500ms)
          ┌────────────────▼─────────────────┐
          │  L2: State Engine                │
          │  Fusion · Scoring · Smoothing    │
          └────────────────┬─────────────────┘
                           │  StateEstimate
          ┌────────────────▼─────────────────┐
          │  L3: Context Engine              │
          │  VS Code · Chrome · Terminal     │
          └────────────────┬─────────────────┘
                           │  TaskContext
          ┌────────────────▼─────────────────┐
          │  L4: LLM Engine                  │
          │  Qwen-3-8B · Ollama fallback     │
          └────────────────┬─────────────────┘
                           │  InterventionPlan
          ┌────────────────▼─────────────────┐
          │  L5: Intervention Engine         │
          │  Snapshot · Execute · Restore    │
          └──────────────────────────────────┘
```

All layers communicate via the FastAPI gateway (`api_gateway/`) and WebSocket server on port 9473. The desktop shell, VS Code extension, and Chrome extension are all clients of this WebSocket.

---

## Privacy

- **No video is ever saved.** Frames are processed in memory and immediately discarded.
- **No biometrics reach the LLM.** The model sees only workspace context: file paths, error messages, tab titles. Heart rate, HRV, blink data, and posture angles never leave your machine.
- **Minimal browser permissions for the current feature set.** The Chrome extension requests `activeTab`, `scripting`, `tabs`, `tabGroups`, and `storage`. It does not request browsing history.
- **Local sensing, remote inference.** The only network traffic is the LLM call, routed through an SSH tunnel to your own GPU.

---

## Project Structure

```
cortex/
├── libs/
│   ├── config/              # CortexConfig, defaults.yaml, .env loading
│   ├── schemas/             # Pydantic models for all data structures
│   ├── signal/              # Butterworth filters, Welch PSD, windowing
│   ├── logging/             # structlog JSON event logging
│   └── utils/               # Platform detection, async helpers
├── services/
│   ├── capture_service/     # Webcam capture, MediaPipe face tracking, quality gating
│   ├── physio_engine/       # POS/CHROM rPPG, BVP peak detection, HR/HRV
│   ├── kinematics_engine/   # EAR blink detection, solvePnP head pose, shoulder posture
│   ├── telemetry_engine/    # pynput input hooks, window tracker, feature aggregation
│   ├── state_engine/        # Feature fusion, rule scorer, EMA smoother, trigger policy
│   ├── context_engine/      # Editor, browser, terminal adapters + app classifier
│   ├── llm_engine/          # Remote Qwen client, Ollama fallback, prompts, parser, cache
│   ├── intervention_engine/ # Trigger, snapshot, planner, executor, restore
│   └── api_gateway/         # FastAPI REST routes, WebSocket server
├── apps/
│   ├── desktop_shell/       # PySide6: tray, dashboard, overlay, settings
│   ├── vscode_extension/    # TypeScript: WS client, context provider, fold controller, panel
│   └── browser_extension/   # Plasmo/React: background SW, content script, popup, tab manager
├── scripts/
│   ├── run_dev.py           # Start all services
│   ├── calibrate.py         # Capture personal baselines
│   ├── seed_config.py       # Initialize storage and config
│   ├── setup_ssh_tunnel.sh  # SSH tunnel to LLM GPU
│   └── replay_session.py    # Replay recorded sessions
├── tests/
│   ├── unit/                # Per-module unit tests
│   └── integration/         # Pipeline integration tests
└── docs/
    ├── setup.md
    ├── architecture.md
    ├── apis.md
    ├── calibration.md
    └── adapters.md
```

---

## Development

```bash
# Run all tests
pytest

# With coverage
pytest --cov=cortex --cov-report=html

# Type check
mypy cortex/

# Lint
ruff check cortex/

# Test webcam pipeline standalone
cortex-capture

# Replay a session
python -m cortex.scripts.replay_session storage/sessions/latest.jsonl
```

---

## Docs

- [Setup](docs/setup.md) — installation, SSH tunnel, environment config, troubleshooting
- [Architecture](docs/architecture.md) — layer details, data flow, performance targets
- [API Reference](docs/apis.md) — all REST endpoints and WebSocket message types
- [Calibration](docs/calibration.md) — personal baseline capture and usage
- [Writing Adapters](docs/adapters.md) — how to add new workspace adapters

---

## License

MIT
