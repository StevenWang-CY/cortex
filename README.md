# Ralph

**Ralph** is a monorepo containing **Cortex** — a real-time biofeedback engine that watches you work through your webcam and input devices, detects cognitive overwhelm, and actively restructures your digital workspace so you can stay focused.

## What's Inside

| Directory | Description |
|-----------|-------------|
| [`cortex/`](cortex/) | Core engine — bio-extraction, state classification, LLM-powered interventions, LeetCode mode, learning loop |
| [`cortex/apps/browser_extension/`](cortex/apps/browser_extension/) | Chrome extension (Plasmo/React) — intervention overlay, ambient feedback, focus sessions, LeetCode observer |
| [`cortex/apps/vscode_extension/`](cortex/apps/vscode_extension/) | VS Code extension — context provider, code folding, morning briefing, copilot throttle |
| [`cortex/apps/desktop_shell/`](cortex/apps/desktop_shell/) | PySide6 desktop app — system tray, dashboard, onboarding |

## Quick Start

```bash
cd cortex
pip install -e ".[dev]"
cp .env.example .env   # Edit with your Azure OpenAI config
python -m cortex.scripts.seed_config --root .
cortex-calibrate        # 2-min baseline capture
cortex-dev              # Start all services
```

See [`cortex/README.md`](cortex/README.md) for full documentation.

## License

MIT
