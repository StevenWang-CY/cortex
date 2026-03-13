# Cortex

**Cortex** is a real-time biofeedback engine that watches you work — through your webcam and input devices — and actively intervenes when it detects cognitive overwhelm. It analyzes your workspace, generates executable actions via LLM, and restructures your digital environment so you can get back to focused work with one click.

---

## How It Works

Cortex runs a five-layer pipeline at 30 FPS, entirely on your machine:

1. **Bio-Extraction** — extracts heart rate and HRV from your face via rPPG (no camera storage), tracks blink rate, head pose, and posture via MediaPipe, and monitors mouse/keyboard patterns via pynput.
2. **State Classification** — fuses seven signals into a cognitive state score every 500ms. Uses rule-based scoring with EMA smoothing and hysteresis to classify you as FLOW, HYPER, HYPO, or RECOVERY.
3. **Context Engine** — when an intervention is warranted, gathers workspace context: open file + diagnostics from VS Code, active tab + content from Chrome, recent terminal output. Tabs are pre-filtered (30 cap with type-diverse sampling) to fit LLM context windows.
4. **LLM Engine** — sends workspace context (no biometrics) to Azure OpenAI first, then local Ollama if unavailable. The model returns a structured intervention plan: headline, micro-steps, suggested actions, error analysis, and per-tab recommendations.
5. **Intervention Engine** — validates and executes the plan: closes distraction tabs, groups related tabs, folds irrelevant code in VS Code, shows an overlay with one-click actions. Snapshots workspace state first. All actions are reversible via undo.

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

## Active Interventions

When Cortex detects you need help, the LLM analyzes your full workspace context and generates specific, executable actions. The intervention overlay appears in the bottom-right of your active tab.

### What the LLM generates

**Suggested Actions** — 1-5 concrete actions per intervention:

| Action | What It Does |
|--------|-------------|
| `close_tab` | Closes distraction tabs (saves URL for undo) |
| `group_tabs` | Groups related tabs into named, collapsed groups |
| `bookmark_and_close` | Bookmarks a tab then closes it |
| `open_url` | Opens a URL in a background tab |
| `search_error` | Opens Google with a pre-built error query |
| `highlight_tab` | Switches to a specific tab |
| `save_session` | Saves all tab URLs/titles to storage |
| `copy_to_clipboard` | Copies text to clipboard |
| `start_timer` | Sets a break timer with notification |

**Error Analysis** — when terminal/editor errors are detected:
- Error type classification (syntax, import, runtime, build, test)
- Root cause explanation
- Concrete suggested fix
- Pre-crafted search query

**Tab Recommendations** — when 4+ tabs are open, every tab is assessed:
- `keep` / `close` / `group` / `bookmark_and_close` per tab
- Relevance score against your focus goal
- Group name for related tabs

### Safety

- **Validate-before-execute** — every tab action checks the tab still exists and hasn't navigated away (10-40s can elapse between context snapshot and user click)
- **Tab targeting by index** — LLM references tabs by integer index from the context list, never by URL (prevents hallucination)
- **Context overflow protection** — 150+ tabs are filtered to 30 with type-diverse sampling
- **Full undo stack** — all destructive actions are reversible (FIFO, max 50 entries)

---

## Chrome Extension

Built with Plasmo + React (Manifest V3). Lives in `apps/browser_extension/`.

### Popup Dashboard

Dark, high-end interface showing:
- Connection status with live cognitive state indicator
- Focus session controls with goal input
- Real-time focus metrics (focus minutes, percentage, streak)
- Live biometrics (BPM, HRV, blink rate) in monospace
- Intervention preview with one-click "Close N tabs" button
- Daily stats grid

### Intervention Overlay

Injected via Shadow DOM into the active tab:
- Tab close list with red `x` marks
- "Keeping N you need" count
- Error analysis with monospace suggested fix
- Single CTA button that executes all recommended actions
- Undo link to reverse all changes

### Ambient Somatic Feedback

Sub-threshold content script running on every page:
- **Aura** — barely-visible vignette that shifts color based on state
- **Somatic filter** — color temperature overlay (warm vs cool, 0-4% opacity)
- **Weather particles** — canvas with 6-45 particles (rain when stressed, calm when focused)
- **Flow Shield** — gradually fades distracting page elements (sidebars, recommendation feeds) during focus

### Pulse Room (New Tab)

Replaces new tab with a dark canvas visualization:
- Central orb pulses at your actual heart rate
- Ripple rings expand on each beat
- ECG-style trace with scanning dot
- Monospace BPM readout

### Focus Sessions

- Start with an optional goal ("Studying PyTorch CUDA debugging")
- Tracks real focus minutes, focus percentage, current/best streaks
- Blocks distraction sites with a full-page interceptor showing your stats
- Focus goal flows through the entire pipeline to inform LLM tab relevance scoring

### Health Alerts

- Eye strain detection (low blink rate triggers 20-20-20 rule reminder)
- Posture alerts (forward lean threshold)
- Break recommendations based on session duration and stress

---

## VS Code Extension

Built with TypeScript. Lives in `apps/vscode_extension/`.

Provides active file, diagnostics, and symbol at cursor. Receives fold commands from the intervention engine to collapse irrelevant code sections.

---

## Setup

**Requirements:** Python 3.11+, macOS (primary target), webcam, Azure OpenAI deployment, Node.js 18+, pnpm.

```bash
cd /path/to/Ralph

# Install
pip install -e "./cortex[dev]"
export PYTHONPATH="$PWD"

# Copy and edit config
cp cortex/.env.example .env

# Initialize storage and default config
python -m cortex.scripts.seed_config --root .

# Calibrate personal baselines (2 min)
cortex-calibrate

# Start everything
cortex-dev

# Optional desktop shell
python -m cortex.apps.desktop_shell.main
```

REST API runs at `http://127.0.0.1:9472`. WebSocket runs at `ws://127.0.0.1:9473`.

### Chrome Extension

```bash
cd cortex/apps/browser_extension
pnpm install
npx plasmo build

# Load in Chrome:
# 1. Open chrome://extensions
# 2. Enable Developer mode
# 3. Click "Load unpacked"
# 4. Select build/chrome-mv3-prod/
```

### Testing Interventions

A standalone test script sends a mock intervention without the full daemon:

```bash
python -m cortex.scripts.test_intervention
# Starts ws://127.0.0.1:9473, sends INTERVENTION_TRIGGER on extension connect
```

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
          │  Azure OpenAI · Ollama fallback  │
          └────────────────┬─────────────────┘
                           │  InterventionPlan
          ┌────────────────▼─────────────────┐
          │  L5: Intervention Engine         │
          │  Validate · Execute · Undo       │
          └──────────────────────────────────┘
```

All layers communicate via the FastAPI gateway (`api_gateway/`) and WebSocket server on port 9473. The desktop shell, VS Code extension, and Chrome extension are all clients of this WebSocket.

### LLM Prompt Modes

| Mode | Trigger | Output |
|------|---------|--------|
| `debug_error_summary` | Terminal/editor errors | Error analysis + search action + docs link |
| `code_focus_reduction` | Coding with too much visible code | `close_tab` actions for distractions |
| `browser_tab_reduction` | 5+ tabs open | Per-tab recommendations + close/group actions |
| `micro_step_planner` | Mixed/overwhelmed state | Actions for automatable steps |
| `calm_overlay_writer` | Reading docs / mild state | Actions for obviously irrelevant tabs |

---

## Privacy

- **No video is ever saved.** Frames are processed in memory and immediately discarded.
- **No biometrics reach the LLM.** The model sees only workspace context: file paths, error messages, tab titles. Heart rate, HRV, blink data, and posture angles never leave your machine.
- **Minimal browser permissions.** The Chrome extension requests `activeTab`, `scripting`, `tabs`, `tabGroups`, `storage`, `alarms`, and `bookmarks`. It does not request browsing history.
- **Local sensing, cloud planning.** The only network traffic is the LLM call, and Cortex sends workspace text context only.

---

## Project Structure

```
cortex/
├── libs/
│   ├── config/              # CortexConfig, defaults.yaml, .env loading
│   ├── schemas/             # Pydantic models (state, context, intervention, actions)
│   ├── signal/              # Butterworth filters, Welch PSD, windowing
│   ├── logging/             # structlog JSON event logging
│   └── utils/               # Platform detection, async helpers, secrets
├── services/
│   ├── capture_service/     # Webcam capture, MediaPipe face tracking, quality gating
│   ├── physio_engine/       # POS/CHROM rPPG, BVP peak detection, HR/HRV
│   ├── kinematics_engine/   # EAR blink detection, solvePnP head pose, shoulder posture
│   ├── telemetry_engine/    # pynput input hooks, window tracker, feature aggregation
│   ├── state_engine/        # Feature fusion, rule scorer, EMA smoother, trigger policy
│   ├── context_engine/      # Editor, browser, terminal adapters + app classifier
│   ├── llm_engine/          # Azure OpenAI client, Ollama fallback, prompts, parser, cache
│   ├── intervention_engine/ # Trigger, snapshot, planner, executor, restore
│   ├── api_gateway/         # FastAPI REST routes, WebSocket server
│   └── runtime_daemon.py    # Main orchestrator — ties all services together
├── apps/
│   ├── desktop_shell/       # PySide6: tray, dashboard, overlay, settings, onboarding
│   ├── vscode_extension/    # TypeScript: WS client, context provider, fold controller
│   └── browser_extension/   # Plasmo/React: background SW, content script, popup, newtab,
│                            #   tab manager, ambient engine, action executor, undo stack
├── scripts/
│   ├── run_dev.py           # Start all services
│   ├── calibrate.py         # Capture personal baselines
│   ├── seed_config.py       # Initialize storage and config
│   ├── test_intervention.py # Mock intervention test server
│   └── build_macos_app.sh   # macOS app packaging
├── tests/
│   ├── unit/                # Per-module unit tests
│   └── integration/         # Pipeline integration tests
└── docs/
    ├── setup.md
    ├── deploy_azure.md
    ├── calibration.md
    └── ...
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

# Build Chrome extension
cd cortex/apps/browser_extension && npx plasmo build

# Test intervention overlay without full daemon
python -m cortex.scripts.test_intervention

# Replay a session
python -m cortex.scripts.replay_session storage/sessions/latest.jsonl
```

---

## Docs

- [Setup](docs/setup.md) — installation, Azure config, packaging, troubleshooting
- [Azure Deployment](docs/deploy_azure.md) — deploy-and-experience checklist
- [Calibration](docs/calibration.md) — personal baseline capture and usage

---

## License

MIT
