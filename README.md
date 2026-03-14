# Ralph

**Ralph** is a monorepo containing **Cortex** — a real-time biofeedback engine that watches you work through your webcam and input devices, detects cognitive overwhelm, and actively restructures your digital workspace so you can stay focused. Unlike timer-based productivity tools, Cortex uses your biology to decide when you need help, and uses LLMs to decide how to help.

---

## Key Features

- **Bio-extraction at 30 FPS** — heart rate, HRV, and respiratory rate via rPPG from your face (no video stored); blink rate, head pose, and posture via MediaPipe; mouse/keyboard patterns via pynput
- **Cognitive state classification** — fuses signals every 500ms into FLOW, HYPER (overwhelmed), HYPO (disengaged), or RECOVERY using rule-based scoring with EMA smoothing and hysteresis
- **LLM-powered interventions** — workspace context (never biometrics) is sent to the LLM, which returns executable actions: close distraction tabs, group related tabs, surface error fixes, decompose tasks into micro-steps. Smart tab algorithm protects recently-visited tabs, AI assistants, and goal-relevant content from being closed
- **Activity tracking and resume** — tracks learning progress across YouTube, Bilibili, Coursera, LeetCode, PDFs, Jupyter, and more. On return, shows a one-click resume card that seeks video, scrolls to position, or pastes saved code
- **LeetCode mode** — DOM observer, stage inference (READ/PLAN/IMPLEMENT/DEBUG/REFLECT), amygdala hijack lockout, pattern ladder hints, submission discipline guard
- **Biology-driven breaks** — cumulative HRV suppression integral replaces arbitrary Pomodoro timers; you can ride deep FLOW until your body says stop
- **Progressive consent** — 5-level trust ladder per action type; Cortex earns autonomy through repeated approvals
- **Learning loop** — contextual bandit (LinUCB) selects intervention type; helpfulness tracker computes reward from user engagement and explicit ratings; per-tab relevance tracker learns individual tab preferences from Keep button feedback
- **Ambient somatic feedback** — sub-threshold color vignettes, weather particles, and flow shield that fades distraction elements during sustained focus
- **Chrome + Edge** — Plasmo/React Manifest V3 extension with popup dashboard, one-click daemon launch and camera restart, intervention overlay, Pulse Room new tab, and focus sessions with distraction blocking

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
| [`cortex/`](cortex/) | Core engine — bio-extraction, state classification, LLM interventions, consent ladder, learning loop, v2.0 detectors, LeetCode mode, activity tracker |
| [`cortex/apps/browser_extension/`](cortex/apps/browser_extension/) | Chrome + Edge extension (Plasmo/React) — intervention overlay, ambient feedback, focus sessions, LeetCode observer, activity tracker, resume cards, Pulse Room |
| [`cortex/apps/vscode_extension/`](cortex/apps/vscode_extension/) | VS Code extension — context provider, code folding, morning briefing, copilot throttle |
| [`cortex/apps/desktop_shell/`](cortex/apps/desktop_shell/) | PySide6 desktop app — system tray, dashboard, onboarding, settings |

---

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| **Backend** | Python 3.11+, FastAPI, MediaPipe, OpenCV, pynput, PySide6 |
| **Browser Extension** | TypeScript, React, Plasmo (Manifest V3), Chrome + Edge |
| **VS Code Extension** | TypeScript, VS Code Extension API |
| **LLM** | Azure OpenAI, Qwen-3-8B (remote via SSH tunnel), Ollama (local) |
| **Storage** | Redis 7+ with automatic in-memory fallback |
| **Testing** | pytest (48 test files), mypy (strict), ruff |

---

## Quick Start

```bash
cd cortex
pip install -e ".[dev]"
cp .env.example .env   # Edit with your Azure OpenAI config
python -m cortex.scripts.seed_config --root .
cortex-calibrate        # 2-min baseline capture
cortex-dev              # Start all services
```

```bash
# Chrome extension
cd cortex/apps/browser_extension
pnpm install && npx plasmo build
# Load build/chrome-mv3-prod/ as unpacked extension
```

See [`cortex/README.md`](cortex/README.md) for full documentation — setup, architecture, all features, API reference, and development guide.

---

## Privacy

- **No video is ever saved.** Frames are processed in memory and immediately discarded.
- **No biometrics reach the LLM.** The model sees only workspace context: file paths, error messages, tab titles.
- **Consent-gated autonomy.** No action executes without earned trust. Users control the maximum autonomy level.

---

## License

MIT
