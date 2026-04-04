# Setup

## DMG Install (Recommended)

1. Download **Cortex.dmg** from [Releases](https://github.com/StevenWang-CY/cortex/releases/latest)
2. Drag **Cortex.app** to `/Applications`
3. Strip the quarantine attribute:
   ```bash
   xattr -cr /Applications/Cortex.app
   ```
4. Open Cortex from Applications
5. Follow the setup wizard to configure your LLM backend and grant permissions
6. Use the in-app **Connect Chrome/Edge** button to install the browser extension

The desktop app bundles the daemon, dashboard, and system tray. No Python, Node.js, or terminal setup required.

---

## Developer Setup (from source)

### Prerequisites

| Requirement | Install |
|-------------|---------|
| **macOS 13+ (Ventura or later)** | Required — Linux/Windows not supported |
| **Python 3.11 or 3.12** | `brew install python@3.11` or [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | `brew install node` or [nodejs.org](https://nodejs.org/) |
| **pnpm** | `npm install -g pnpm` |
| **LLM backend** | Azure OpenAI key, local Ollama, or `rule_based` mode |
| **Redis** (optional) | `brew install redis && brew services start redis` — auto-falls back to in-memory |

> **Apple Silicon:** Use native ARM Python, not Rosetta. Verify: `python3 -c "import platform; print(platform.machine())"` should print `arm64`.

---

### 1. Clone & Virtual Environment

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex   # repo root

python3 -m venv .venv
source .venv/bin/activate
```

---

### 2. Configuration

Copy the example config:

```bash
cp cortex/.env.example .env
```

Edit `.env` and choose one LLM backend:

### Option A — Azure OpenAI (recommended)

```bash
CORTEX_LLM__MODE=azure
CORTEX_LLM__AZURE__ENDPOINT=https://your-resource.openai.azure.com/
CORTEX_LLM__AZURE__API_KEY=your-key-here
CORTEX_LLM__AZURE__API_VERSION=2025-01-01-preview
CORTEX_LLM__AZURE__DEPLOYMENT_NAME=gpt-4o-mini
CORTEX_LLM__AZURE__MAX_COMPLETION_TOKENS=1024
```

For production: store the key in macOS Keychain instead of `.env`:
```bash
security add-generic-password -s cortex.azure_openai -a default -w YOUR_KEY
# Then set: CORTEX_LLM__AZURE__USE_KEYCHAIN=true
```

### Option B — Local Ollama (free, no API key)

```bash
brew install ollama
ollama pull llama3.1:8b
ollama serve   # keep running in a separate terminal
```

```bash
# In .env:
CORTEX_LLM__MODE=local
```

### Option C — Rule-based only (no LLM)

```bash
CORTEX_LLM__MODE=rule_based
```

Uses built-in heuristics. Good for testing the biofeedback pipeline without any LLM setup.

### Camera Configuration

Leave `CORTEX_CAPTURE__DEVICE_ID` commented out for automatic selection. Cortex will:
- Enumerate cameras via AVFoundation
- Skip iPhone/iPad Continuity Camera devices
- Prefer the MacBook's built-in camera
- Probe only non-Continuity indices as fallback
- Reject any camera it cannot verify by name

Camera selection runs once at daemon startup. Restart the daemon after turning off an iPhone.

To override:
```bash
CORTEX_CAPTURE__DEVICE_ID=0   # or 1, 2, etc.
```

---

### 3. Install Python Dependencies

```bash
pip install -e "./cortex[dev]"
```

Verify:
```bash
python -c "from cortex.libs.config.settings import get_config; print(f'LLM mode: {get_config().llm.mode}')"
```

---

### 4. Initialize Storage

```bash
python -m cortex.scripts.seed_config --root .
```

Creates the `storage/` directory tree and a default baseline profile.

---

### 5. macOS Permissions

Cortex needs two permissions, both prompted automatically on first use:

**Camera** — macOS asks when the daemon first opens the webcam. Click **Allow**.

**Input Monitoring** — required for keyboard/mouse telemetry:
`System Settings → Privacy & Security → Input Monitoring → add your terminal app`

> If you launch the daemon from the browser extension, it runs via **Terminal.app** — grant permission to Terminal.app specifically.

---

### 6. Start the Daemon

### From terminal

```bash
source .venv/bin/activate
cortex-dev
```

Starts:
- REST API on `http://127.0.0.1:9472`
- WebSocket on `ws://127.0.0.1:9473`
- All capture, signal processing, state engine, and intervention services

### Webcam-only test

```bash
cortex-capture
```

Opens a window showing the webcam feed with face detection overlays and FPS counter. Press `q` to quit.

---

### 7. Browser Extension

```bash
cd cortex/apps/browser_extension
pnpm install
```

| Browser | Build | Load from |
|---------|-------|-----------|
| Chrome | `npx plasmo build` | `chrome://extensions` → Developer mode → Load unpacked → `build/chrome-mv3-prod/` |
| Edge | `npx plasmo build --target=edge-mv3` | `edge://extensions` → Developer mode → Load unpacked → `build/edge-mv3-prod/` |

For development with hot reload: `pnpm dev`

### Native Messaging (one-click Start/Stop from browser)

```bash
cd /path/to/repo-root
python -m cortex.scripts.install_native_host
```

This auto-detects all installed Chromium browsers and patches `native_host.py` with the absolute venv Python path. No manual extension ID needed.

**Then fully restart your browser** (Cmd+Q, reopen). Native messaging manifests only load at browser startup.

First-time dialogs:
1. macOS: *"Chrome/Edge wants to control Terminal. Allow?"* — click **Allow** (one-time)
2. A Terminal window opens when the daemon starts — this is normal

---

### 8. Calibration (recommended)

```bash
cortex-calibrate --duration 120
```

Sit relaxed for 2 minutes while Cortex learns your resting heart rate, HRV, blink rate, and posture. Results saved to `storage/baselines/`. See [Calibration](Calibration) for details.

---

### 9. VS Code Extension (optional)

```bash
cd cortex/apps/vscode_extension
npm install && npm run compile
code --install-extension cortex-somatic-0.1.0.vsix
```

Provides editor context (open file, diagnostics, cursor position) to the daemon for more accurate interventions.

---

### 10. Running Tests

```bash
pytest                                      # all tests
pytest tests/unit/                          # unit tests only
pytest tests/integration/                   # integration tests
pytest --cov=cortex --cov-report=html       # with coverage
pytest -m "not requires_webcam"             # skip hardware tests
```
