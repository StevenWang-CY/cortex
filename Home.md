# Cortex Wiki

**Cortex** is a real-time biofeedback engine that watches you work through your webcam and input devices, detects cognitive overwhelm, and actively restructures your digital workspace so you can stay focused.

> Platform: **macOS only** (requires AVFoundation, TCC, and macOS-specific frameworks)

---

## Pages

| Page | Description |
|------|-------------|
| [Setup](Setup) | Installation, configuration, and first run |
| [How It Works](How-It-Works) | Signal pipeline, state classification, and AI interventions |
| [Architecture](Architecture) | Layer-by-layer technical design and data flow |
| [Browser Extension](Browser-Extension) | Chrome/Edge extension features and usage |
| [Calibration](Calibration) | Personal baseline calibration guide |
| [API Reference](API-Reference) | REST API and WebSocket protocol |
| [Troubleshooting](Troubleshooting) | Common issues and fixes |
| [Privacy](Privacy) | What data is collected, where it goes, and what never leaves your machine |

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex
python3 -m venv .venv && source .venv/bin/activate
pip install -e "./cortex[dev]"

# 2. Configure LLM
cp cortex/.env.example .env
# Edit .env — set CORTEX_LLM__MODE and credentials

# 3. Start daemon
cortex-dev

# 4. Load browser extension
cd cortex/apps/browser_extension
pnpm install && npx plasmo build
# Load build/chrome-mv3-prod/ in chrome://extensions
```

See [Setup](Setup) for the full guide including native messaging, permissions, and calibration.

---

## How Cortex Helps

Cortex watches you through your webcam (no video stored) while you work. It reads your pulse and breathing from subtle color changes in your face, combines those signals with mouse/keyboard patterns and workspace state, and classifies you into one of four cognitive states every 500ms:

| State | Meaning |
|-------|---------|
| **FLOW** | Focused and productive |
| **HYPER** | Overwhelmed, thrashing, stuck |
| **HYPO** | Disengaged, drifting |
| **RECOVERY** | Returning to focus |

When it detects HYPER, it sends your **workspace context** (tab titles, error messages, file paths — never biometrics) to an LLM, which returns specific executable actions: close distraction tabs, surface the error fix you need, break your task into micro-steps, or suggest a biology-driven break.

Everything is opt-in and reversible. Cortex earns autonomy through a 5-level progressive consent system.
