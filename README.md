# Cortex

**Cortex** is a real-time biofeedback engine that watches you work through your webcam and input devices, detects cognitive overwhelm, and actively restructures your digital workspace so you can stay focused. Unlike timer-based productivity tools, Cortex uses your biology to decide when you need help, and uses LLMs to decide how to help.

> **Platform:** macOS only (requires AVFoundation for camera, TCC for permissions, and macOS-specific frameworks). Linux and Windows are not supported.

---

## Key Features

- **Bio-extraction at 30 FPS** — heart rate, HRV, and respiratory rate via rPPG from your face (no video stored); blink rate, head pose, and posture via MediaPipe; mouse/keyboard patterns via pynput. Gracefully degrades to telemetry-only mode in poor lighting
- **Cognitive state classification** — fuses signals into state estimates every 500ms as FLOW, HYPER (overwhelmed), HYPO (disengaged), or RECOVERY using rule-based scoring with EMA smoothing and hysteresis
- **LLM-powered interventions** — workspace context (never biometrics) is sent to the LLM, which returns executable actions: close distraction tabs, group related tabs, surface error fixes, decompose tasks into micro-steps. Smart tab algorithm protects recently-visited tabs, AI assistants, and goal-relevant content from being closed
- **Activity tracking and resume** — tracks learning progress across YouTube, Bilibili, Coursera, LeetCode, PDFs, Jupyter, and more. On return, shows a one-click resume card that seeks video, scrolls to position, or pastes saved code
- **LeetCode mode** — DOM observer, stage inference (READ/PLAN/IMPLEMENT/DEBUG/REFLECT), amygdala hijack lockout, pattern ladder hints, submission discipline guard
- **Biology-driven breaks** — cumulative HRV suppression integral replaces arbitrary Pomodoro timers; you can ride deep FLOW until your body says stop
- **Progressive consent** — 5-level trust ladder (OBSERVE → SUGGEST → PREVIEW → REVERSIBLE_ACT → AUTONOMOUS_ACT) per action type; Cortex earns autonomy through repeated approvals
- **Learning loop** — contextual bandit (LinUCB) selects intervention type; helpfulness tracker computes reward from user engagement and explicit ratings; per-tab relevance tracker learns individual tab preferences from Keep button feedback
- **Ambient somatic feedback** — sub-threshold color vignettes, weather particles, and flow shield that fades distraction elements during sustained focus
- **Chrome + Edge** — Plasmo/React Manifest V3 extension with popup dashboard, one-click daemon launch (via native messaging + Terminal.app for camera access), camera restart, intervention overlay, Pulse Room new tab, and focus sessions with distraction blocking

---

## How It Works

```
Webcam (30 FPS)
     │
     ▼
L1: Bio-Extraction ─── rPPG · Respiration · Blink · Pose · Telemetry
     │
     ▼  FeatureVector (500ms)
L2: State Engine ────── Fusion · Focus Graph · Scoring · Detectors
     │
     ▼  StateEstimate + stress_integral
L3: Context Engine ──── VS Code · Chrome · Terminal · Adapter Registry
     │
     ▼  TaskContext
L4: LLM Engine ──────── Azure OpenAI · Qwen-3 · Ollama · Bandit
     │
     ▼  InterventionPlan
L5: Intervention ────── Consent · Validate · Execute · Undo · Learn
     │
     ▼
Store (Redis / In-Memory)
```

All layers communicate via FastAPI (port 9472) and WebSocket (port 9473). The desktop shell, VS Code extension, and Chrome/Edge extension are all clients.

---

## What's Inside

| Directory | Description |
|-----------|-------------|
| [`cortex/`](cortex/) | Core engine — bio-extraction, state classification, LLM interventions, consent ladder, learning loop, v2.0 detectors, LeetCode mode, activity tracker, smart camera selection |
| [`cortex/apps/browser_extension/`](cortex/apps/browser_extension/) | Chrome + Edge extension (Plasmo/React) — one-click daemon launch/stop, intervention overlay, ambient feedback, focus sessions, LeetCode observer, activity tracker, resume cards, Pulse Room |
| [`cortex/apps/vscode_extension/`](cortex/apps/vscode_extension/) | VS Code extension — context provider, code folding, morning briefing, copilot throttle |
| [`cortex/apps/desktop_shell/`](cortex/apps/desktop_shell/) | PySide6 desktop app — system tray, dashboard, onboarding, settings |
| [`cortex/scripts/`](cortex/scripts/) | Daemon entry point, native messaging host, launcher agent, calibration, install scripts |

---

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| **Backend** | Python 3.11+, FastAPI, MediaPipe, OpenCV, pynput, PySide6 |
| **Browser Extension** | TypeScript, React, Plasmo (Manifest V3), Chrome + Edge |
| **VS Code Extension** | TypeScript, VS Code Extension API |
| **LLM** | Azure OpenAI, Qwen-3-8B (remote via SSH tunnel), Ollama (local) |
| **Storage** | Redis 7+ with automatic in-memory fallback |
| **Testing** | pytest (47 test files), mypy (strict), ruff |

---

## Setup

### Prerequisites

| Requirement | How to install |
|-------------|----------------|
| **macOS 13+** | Required (Ventura or later) |
| **Python 3.11 or 3.12** | `brew install python@3.11` or [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | `brew install node` or [nodejs.org](https://nodejs.org/) |
| **pnpm** | `npm install -g pnpm` (after installing Node.js) |
| **LLM backend** | One of: Azure OpenAI API key, local [Ollama](https://ollama.com), or `rule_based` mode (no LLM needed) |
| **Redis** (optional) | `brew install redis && brew services start redis` — falls back to in-memory if not running |

> **Apple Silicon note:** Use native ARM Python, not Rosetta. Verify with: `python3 -c "import platform; print(platform.machine())"` — should print `arm64`.

### Step 1: Python Backend

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex   # this is the repo root (Ralph/)

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install all dependencies
pip install -e "./cortex[dev]"
```

### Step 2: Configuration

```bash
# Copy example config
cp cortex/.env.example .env
```

Edit `.env` and set your LLM backend. Pick ONE:

**Option A — Azure OpenAI** (recommended):
```bash
CORTEX_LLM__MODE=azure
CORTEX_LLM__AZURE__ENDPOINT=https://your-resource.openai.azure.com/
CORTEX_LLM__AZURE__API_KEY=your-key-here
CORTEX_LLM__AZURE__DEPLOYMENT_NAME=gpt-4o-mini
```

**Option B — Local Ollama** (free, no API key):
```bash
# Install and start Ollama first:
#   brew install ollama && ollama pull llama3.1:8b && ollama serve
CORTEX_LLM__MODE=local
```

**Option C — Rule-based only** (no LLM, limited interventions):
```bash
CORTEX_LLM__MODE=rule_based
```

Then initialize storage directories:
```bash
python -m cortex.scripts.seed_config --root .
```

### Step 3: macOS Permissions

Cortex needs two macOS permissions. Both are prompted automatically on first use:

1. **Camera** — macOS will ask when the daemon first opens the webcam. Click **Allow**.
2. **Accessibility (Input Monitoring)** — required for keyboard/mouse telemetry. Go to: `System Settings → Privacy & Security → Input Monitoring → add your terminal app (Terminal.app or iTerm)`

### Step 4: Browser Extension

```bash
cd cortex/apps/browser_extension
pnpm install
```

Build for your browser:

| Browser | Build command | Load from |
|---------|--------------|-----------|
| **Chrome** | `npx plasmo build` | `chrome://extensions` → Developer mode → Load unpacked → `build/chrome-mv3-prod/` |
| **Edge** | `npx plasmo build --target=edge-mv3` | `edge://extensions` → Developer mode → Load unpacked → `build/edge-mv3-prod/` |

### Step 5: Native Messaging (enables one-click Start/Stop from browser)

```bash
cd /path/to/repo-root
python -m cortex.scripts.install_native_host
```

This automatically:
- Detects all installed Chromium browsers (Chrome, Edge, Brave, Arc, Vivaldi, Opera)
- Registers the native messaging host for each
- No extension ID needed — the extension uses a fixed manifest key

**Then fully restart your browser** (Cmd+Q, reopen). Native messaging only loads at browser startup.

The first time you click "Start Cortex" from the extension, macOS will ask: *"Google Chrome (or Edge) wants to control Terminal. Allow?"* — click **Allow** once.

### Step 6: Start Cortex

**From browser** — click **Start Cortex** in the extension popup. A Terminal window opens and the daemon runs there (Terminal has camera permission).

**From terminal** — `source .venv/bin/activate && cortex-dev`

### Step 7: Calibrate (optional but recommended)

Sit relaxed for 2 minutes while Cortex learns your resting heart rate, blink rate, and posture:

```bash
cortex-calibrate --duration 120
```

---

## What To Expect

Cortex watches you through your webcam while you study — not to record you, but to read your pulse and breathing from subtle color changes in your face (a technique called remote photoplethysmography). It combines those biological signals with what's happening on your screen — which tabs are open, what errors your code is throwing, how fast you're switching between windows — to figure out whether you're in a productive flow, spiraling into overwhelm, or zoning out. When it detects you're struggling, it uses an AI model to figure out *how* to help: closing distraction tabs, surfacing the error fix you need, breaking your task into smaller steps, or just telling you to take a break because your body's stress accumulator says so. It also has a dedicated LeetCode mode that detects panic-coding patterns and tries to get you to slow down before you submit your fifth wrong answer in a row.

What works well today: the state classification system is conservative and well-tuned — in testing, it correctly avoids false alarms for caffeinated studying, debugging sessions, and deep reading. The biological break timer (which replaces arbitrary Pomodoro intervals with actual HRV-based fatigue tracking) is a genuinely novel feature that works as designed. The LeetCode mode's multi-selector DOM strategy is resilient to LeetCode's frequent UI changes, and the intervention matrix covers real failure modes students hit. The context-aware fallback system means you still get useful help even when the AI model is slow or unavailable. The progressive consent system lets Cortex earn your trust gradually — it starts by just observing, and only takes actions after you've approved similar ones multiple times.

Cortex asks for your webcam (for pulse and posture — no video is saved or sent anywhere), broad browser permissions (to read tab titles and URLs for context — the AI model never sees your biometrics), and a 2-minute baseline calibration session where you sit still so it can learn your resting heart rate. It runs a local daemon on your machine that communicates with a Chrome extension and optionally a VS Code extension. The AI model (Azure OpenAI, Qwen, or a local Ollama instance) sees only workspace context: file paths, error messages, and tab titles. Your physiological data stays on your machine.

Cortex is not a study planner, a to-do app, or a replacement for actually understanding the material. The heart rate signal from a webcam is noisier than a chest strap — in dim lighting or if you move a lot, the biological signals degrade and the system falls back to behavioral-only detection. The HRV measurement at 30 FPS is at the edge of what's physiologically meaningful and works best as a trend indicator over minutes, not a precise beat-by-beat measurement. The AI-generated interventions are sometimes generic or slightly off-target, especially early on before the learning system has calibrated to your preferences. And if you're the kind of student who studies past midnight, you'll want to adjust the wind-down hour from its default — it was set for an earlier bedtime than most college students keep.

---

## Troubleshooting

### "Start Cortex" shows "Native host not found"
- Run `python -m cortex.scripts.install_native_host` and **fully restart your browser** (Cmd+Q, reopen)
- Native messaging manifests are only loaded at browser startup — reloading the extension is not enough

### Camera opens iPhone instead of MacBook camera
- Cortex auto-skips Continuity Camera devices (iPhone/iPad). If it still picks the wrong one, set `CORTEX_CAPTURE__DEVICE_ID=0` in `.env` (or try `1`, `2`, etc.)
- Moving your iPhone away or locking it removes the Continuity Camera from the device list

### Camera permission denied
- If the daemon was launched from the browser extension, it runs via Terminal.app. Grant camera access to **Terminal.app** (not Chrome) in: `System Settings → Privacy & Security → Camera`
- If running from terminal directly, grant camera to your terminal app (Terminal.app or iTerm)

### Webcam not detected
```bash
python3 -c "import cv2; [print(f'Device {i}: {cv2.VideoCapture(i).isOpened()}') for i in range(5)]"
```

### "Stop Cortex" doesn't stop the camera
- Click Stop again — the extension uses a multi-layer kill chain (WebSocket → HTTP → process kill)
- If the camera light stays on: `pkill -f "cortex.scripts.run_dev"`

### Azure LLM errors
- Verify your `.env` has `CORTEX_LLM__AZURE__ENDPOINT`, `API_KEY`, and `DEPLOYMENT_NAME` set
- Check API version is `2025-01-01-preview`
- Use `CORTEX_LLM__MODE=rule_based` to run without any LLM

### MediaPipe import errors on Apple Silicon
```bash
pip install --force-reinstall mediapipe
```
Ensure you're using native ARM Python (not Rosetta): `python3 -c "import platform; print(platform.machine())"` should print `arm64`.

### Accessibility / pynput errors
Add your terminal app to: `System Settings → Privacy & Security → Input Monitoring`

---

## Privacy

- **No video is ever saved.** Frames are processed in memory and immediately discarded.
- **No biometrics reach the LLM.** The model sees only workspace context: file paths, error messages, tab titles.
- **Consent-gated autonomy.** No action executes without earned trust. Users control the maximum autonomy level.

---

## License

MIT
