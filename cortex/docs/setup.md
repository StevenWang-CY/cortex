# Setup Guide

## Prerequisites

- **macOS 13+ (Ventura or later)** — Linux and Windows are not supported
- **Python 3.11 or 3.12** — `brew install python@3.11` or [python.org](https://www.python.org/downloads/)
- **Node.js 18+** — `brew install node` or [nodejs.org](https://nodejs.org/)
- **pnpm** — `npm install -g pnpm` (after installing Node.js)
- **Webcam** (built-in MacBook camera or USB; 640x480 @ 30 FPS minimum)
- **LLM backend** (one of):
  - Azure OpenAI API key (recommended)
  - [Ollama](https://ollama.com) running locally (free)
  - `rule_based` mode (no LLM needed, limited interventions)
- **Optional:** Redis 7+ (`brew install redis`) — falls back to in-memory automatically

> **Apple Silicon:** Use native ARM Python, not Rosetta. Verify: `python3 -c "import platform; print(platform.machine())"` should print `arm64`.

## 1. Clone & Create Virtual Environment

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex   # repo root directory

python3 -m venv .venv
source .venv/bin/activate
```

## 2. Configuration (before installing)

Copy and edit the config file first, so the daemon has valid settings from the start:

```bash
cp cortex/.env.example .env
```

Edit `.env` and set your LLM backend. Cortex uses nested environment variables with `__` as separator.

### LLM Options

**Azure OpenAI** (recommended):

```bash
CORTEX_LLM__MODE=azure
CORTEX_LLM__AZURE__ENDPOINT=https://your-resource.openai.azure.com/
CORTEX_LLM__AZURE__API_KEY=your-key-here
CORTEX_LLM__AZURE__API_VERSION=2025-01-01-preview
CORTEX_LLM__AZURE__DEPLOYMENT_NAME=gpt-4o-mini
CORTEX_LLM__AZURE__REASONING_DEPLOYMENT_NAME=gpt-4o-mini
CORTEX_LLM__AZURE__MAX_COMPLETION_TOKENS=1024
```

Notes:
- Azure `gpt-4o-mini` and `gpt-5-mini` reject `max_tokens`; Cortex sends `max_completion_tokens` instead.
- Cortex only sends workspace text context, current state label/confidence, and allowed intervention constraints to Azure. Raw camera frames and biometrics stay local.
- For production macOS installs, store the API key in Keychain: set `CORTEX_LLM__AZURE__USE_KEYCHAIN=true` and add the key via `security add-generic-password -s cortex.azure_openai -a default -w YOUR_KEY`.

**Local Ollama** (free, no API key):

```bash
# Install Ollama first:
brew install ollama
ollama pull llama3.1:8b
ollama serve   # keep running in a separate terminal

# Then in .env:
CORTEX_LLM__MODE=local
```

**Rule-based only** (no LLM, no API key needed):

```bash
CORTEX_LLM__MODE=rule_based
```

This mode uses built-in heuristics instead of LLM-generated interventions. Good for testing the biofeedback pipeline without any LLM setup.

### Camera Configuration

Leave `CORTEX_CAPTURE__DEVICE_ID` commented out (the default) for automatic camera selection. Cortex will:
- Enumerate cameras via AVFoundation
- Skip iPhone/iPad Continuity Camera devices
- Prefer the MacBook's built-in camera
- Probe indices 0–4 as fallback

Only set it manually if auto-detection picks the wrong camera:
```bash
CORTEX_CAPTURE__DEVICE_ID=0   # or 1, 2, etc.
```

## 3. Install Python Dependencies

```bash
pip install -e "./cortex[dev]"
```

Verify:
```bash
python -c "from cortex.libs.config.settings import get_config; print(f'LLM mode: {get_config().llm.mode}')"
```

## 4. Initialize Storage

```bash
python -m cortex.scripts.seed_config --root .
```

This creates the `storage/` directory tree and a default baseline profile. Use `--dry-run` to preview.

## 5. macOS Permissions

Cortex needs two macOS permissions. Both are prompted on first use:

1. **Camera access** — macOS prompts when the daemon first opens the webcam. Click **Allow**.
2. **Input Monitoring (Accessibility)** — required for keyboard/mouse telemetry via `pynput`.

To grant Input Monitoring:
`System Settings → Privacy & Security → Input Monitoring → add your terminal app`

> **Which terminal app?** If you start the daemon from the browser extension, grant permission to **Terminal.app** (the daemon launches via Terminal.app for camera access). If you start from iTerm or another terminal, add that app instead.

## 6. Start the Daemon

### From terminal (simplest)

```bash
source .venv/bin/activate
cortex-dev
```

This starts:
- API Gateway on `http://127.0.0.1:9472`
- WebSocket server on `ws://127.0.0.1:9473`
- All capture, signal processing, state engine, and intervention services

### Standalone webcam test

Verify your webcam and face tracking work before running the full daemon:

```bash
cortex-capture
```

Opens a window showing the webcam feed with face detection overlays, FPS counter, and quality metrics. Press `q` to quit.

## 7. Browser Extension

### Build & Load

```bash
cd cortex/apps/browser_extension
pnpm install
```

| Browser | Build | Load from |
|---------|-------|-----------|
| Chrome | `npx plasmo build` | `chrome://extensions` → Developer mode → Load unpacked → `build/chrome-mv3-prod/` |
| Edge | `npx plasmo build --target=edge-mv3` | `edge://extensions` → Developer mode → Load unpacked → `build/edge-mv3-prod/` |

For development with hot reload: `pnpm dev` (Chrome) or `pnpm dev:edge` (Edge).

### Native Messaging (one-click Start/Stop from browser)

This lets you start and stop the daemon by clicking a button in the extension popup, without touching the terminal.

```bash
cd /path/to/repo-root
python -m cortex.scripts.install_native_host
```

The script automatically:
- Detects all installed Chromium browsers and installs for each
- Patches `native_host.py` with the absolute venv Python path
- Auto-detects existing extension IDs — no manual ID needed

**Then fully restart your browser** (Cmd+Q, reopen). Native messaging manifests are only loaded at browser startup.

**First-time dialogs:**
1. macOS will ask: *"Chrome/Edge wants to control Terminal. Allow?"* — click **Allow** (one-time)
2. A Terminal window opens when the daemon starts — this is normal (Terminal provides camera access)

## 8. Calibration (recommended)

Sit relaxed for 2 minutes while Cortex learns your personal baselines:

```bash
cortex-calibrate --duration 120
```

Measures resting heart rate, HRV, baseline blink rate, and neutral posture. Results are saved to `storage/baselines/`. Calibration improves state detection accuracy but is not required to start.

For testing without a webcam:
```bash
cortex-calibrate --simulate
```

## 9. VS Code Extension (optional)

```bash
cd cortex/apps/vscode_extension
npm install
npm run compile
code --install-extension cortex-somatic-0.1.0.vsix
```

## 10. Running Tests

```bash
# All tests
pytest

# Unit tests only
pytest tests/unit/

# Integration tests
pytest tests/integration/

# With coverage
pytest --cov=cortex --cov-report=html

# Skip tests requiring hardware
pytest -m "not requires_webcam and not requires_gpu"
```

---

## Troubleshooting

### "Start Cortex" shows "Native host not found" or "Access forbidden"
1. Run `python -m cortex.scripts.install_native_host`
2. **Fully restart your browser** (Cmd+Q, reopen) — reloading the extension is not enough
3. Chrome/Edge only reads native messaging manifests at startup

### Camera opens iPhone instead of MacBook camera
- Cortex auto-skips Continuity Camera (iPhone/iPad). If it still picks wrong: set `CORTEX_CAPTURE__DEVICE_ID=0` in `.env`
- Lock your iPhone or move it away to remove it from the device list

### Camera permission denied
- If launched from browser: grant camera to **Terminal.app** in `System Settings → Privacy & Security → Camera`
- If launched from terminal: grant camera to your terminal app

### "Stop Cortex" doesn't stop the camera
- Click Stop again — uses multi-layer kill chain
- Manual kill: `pkill -f "cortex.scripts.run_dev"`

### Azure LLM errors
- Verify `.env` has `ENDPOINT`, `API_KEY`, and `DEPLOYMENT_NAME` set
- Switch to `CORTEX_LLM__MODE=rule_based` to run without LLM

### MediaPipe import errors on Apple Silicon
```bash
pip install --force-reinstall mediapipe
```
Verify native ARM Python: `python3 -c "import platform; print(platform.machine())"` → `arm64`

### Accessibility / pynput errors
Add your terminal to: `System Settings → Privacy & Security → Input Monitoring`
