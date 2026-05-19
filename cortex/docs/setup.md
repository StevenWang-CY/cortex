# Setup Guide

## Quick Install (DMG)

The fastest way to get Cortex running. No Python, Node.js, or terminal setup required.

1. Download `Cortex.dmg` from [GitHub Releases](https://github.com/StevenWang-CY/cortex/releases/latest)
2. Drag `Cortex.app` to Applications
3. Strip quarantine:
   ```bash
   xattr -cr /Applications/Cortex.app
   ```
4. Open Cortex
5. Follow the 4-step onboarding wizard:
   - **Camera** -- Grant camera permission when prompted
   - **Accessibility** -- Grant Accessibility permission (for keyboard/mouse telemetry)
   - **LLM backend** -- Choose your LLM provider and enter your API key (stored securely in macOS Keychain)
   - **Connect tools** -- Connect browser extension (Chrome/Edge) and editor (VS Code/Cursor)
6. You're done. The DMG bundles everything -- no Python, Node.js, or terminal setup required.

---

## Developer Setup (from source)

For developers who want to modify Cortex or contribute to the project.

### Prerequisites

- **macOS 13+ (Ventura or later)** — Linux and Windows are not supported
- **Python 3.11 or 3.12** — `brew install python@3.11` or [python.org](https://www.python.org/downloads/)
- **Node.js 18+** — `brew install node` or [nodejs.org](https://nodejs.org/)
- **pnpm** — `npm install -g pnpm` (after installing Node.js)
- **Webcam** (built-in MacBook camera or USB; 640x480 @ 30 FPS minimum)
- **LLM backend** (one Anthropic SDK transport):
  - **AWS Bedrock** (default) — bearer token stored in macOS Keychain, no AWS CLI needed
  - **Google Vertex AI** — via standard `gcloud auth application-default login`
  - **Direct Anthropic API** — via `ANTHROPIC_API_KEY`
  - **Rule-based fallback** — `CORTEX_LLM__FALLBACK_MODE=rule_based` (default) keeps the daemon running with a deterministic plan if every provider fails. Set to `direct_anthropic` to retry via the direct API instead.
- **Optional:** Redis 7+ (`brew install redis`) — falls back to in-memory automatically

> **Apple Silicon:** Use native ARM Python, not Rosetta. Verify: `python3 -c "import platform; print(platform.machine())"` should print `arm64`.

### 1. Clone & Create Virtual Environment

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex   # repo root directory

python3 -m venv .venv
source .venv/bin/activate
```

### 2. Configuration (before installing)

Copy and edit the config file first, so the daemon has valid settings from the start:

```bash
cp cortex/.env.example .env
```

Edit `.env` and set your LLM backend. Cortex uses nested environment variables with `__` as separator.

#### LLM Options

Cortex uses the Anthropic SDK with three swappable transports. Pick one provider — credentials never bundle into the .app, and the configured provider is mirrored into `ANTHROPIC_PROVIDER` at startup so the SDK picks the right transport.

**Option A — AWS Bedrock** (default, recommended):

```bash
CORTEX_LLM__PROVIDER=bedrock
CORTEX_LLM__BEDROCK__AWS_REGION=us-east-2
CORTEX_LLM__USE_KEYCHAIN=true   # default; reads the bearer token from macOS Keychain
                                # and exports AWS_BEARER_TOKEN_BEDROCK for the SDK
```

Store the Bedrock bearer token in macOS Keychain (one-time):

```bash
security add-generic-password -s cortex.bedrock -a bearer_token -w YOUR_TOKEN
```

**Option B — Google Vertex AI:**

```bash
CORTEX_LLM__PROVIDER=vertex
gcloud auth application-default login
```

**Option C — Direct Anthropic API:**

```bash
CORTEX_LLM__PROVIDER=direct
export ANTHROPIC_API_KEY=sk-ant-...
```

**Model tiers** (logical IDs resolved per provider by `cortex.libs.llm.anthropic_client.resolve_anthropic_model_id`):

```bash
CORTEX_LLM__MODEL_DEFAULT=claude-sonnet-4-6
CORTEX_LLM__MODEL_FAST=claude-haiku-4-5
CORTEX_LLM__MODEL_DEEP=claude-opus-4-7
```

**Fallback when every provider fails:**

```bash
CORTEX_LLM__FALLBACK_MODE=rule_based   # default — deterministic plan keeps daemon running
# CORTEX_LLM__FALLBACK_MODE=direct_anthropic  # retry via direct API instead
```

Notes:
- Cortex only sends workspace text context, current state label/confidence, and allowed intervention constraints to the LLM. Raw camera frames and biometrics stay local.
- Legacy env vars (`CORTEX_LLM__MODE`, `CORTEX_LLM__AZURE__*`, `CORTEX_LLM__REMOTE__*`, `CORTEX_LLM__LOCAL__*`, `CORTEX_LLM__MODEL_NAME`) are silently ignored by the validator — a 0.1.x `.env` will not crash on first launch.

#### Camera Configuration

Leave `CORTEX_CAPTURE__DEVICE_ID` commented out (the default) for automatic camera selection. Cortex will:
- Enumerate cameras via AVFoundation
- Skip iPhone/iPad Continuity Camera devices
- Prefer the MacBook's built-in camera
- Probe only non-Continuity indices as fallback (never probes iPhone/iPad indices)
- Reject any camera it cannot verify by name (prevents accidentally opening a Continuity Camera that disappeared from AVFoundation mid-enumeration)

Camera selection runs once at daemon startup. If you turn off your iPhone after the daemon is already running, restart the daemon to re-run selection.

Only set it manually if auto-detection picks the wrong camera:
```bash
CORTEX_CAPTURE__DEVICE_ID=0   # or 1, 2, etc.
```

### 3. Install Python Dependencies

```bash
pip install -e "./cortex[dev]"
```

Verify:
```bash
python -c "from cortex.libs.config.settings import get_config; print(f'LLM provider: {get_config().llm.provider}')"
```

### 4. Initialize Storage

```bash
python -m cortex.scripts.seed_config --root .
```

This creates the `storage/` directory tree and a default baseline profile. Use `--dry-run` to preview.

### 5. macOS Permissions

Cortex needs two macOS permissions. Both are prompted on first use:

1. **Camera access** — macOS prompts when the daemon first opens the webcam. Click **Allow**.
2. **Input Monitoring (Accessibility)** — required for keyboard/mouse telemetry via `pynput`.

To grant Input Monitoring:
`System Settings → Privacy & Security → Input Monitoring → add your terminal app`

> **Which terminal app?** If you start the daemon from the browser extension, grant permission to **Terminal.app** (the daemon launches via Terminal.app for camera access). If you start from iTerm or another terminal, add that app instead.

### 6. Start the Daemon

#### From terminal (simplest)

```bash
source .venv/bin/activate
cortex-dev
```

This starts:
- API Gateway on `http://127.0.0.1:9472`
- WebSocket server on `ws://127.0.0.1:9473`
- All capture, signal processing, state engine, and intervention services

#### Standalone webcam test

Verify your webcam and face tracking work before running the full daemon:

```bash
cortex-capture
```

Opens a window showing the webcam feed with face detection overlays, FPS counter, and quality metrics. Press `q` to quit.

### 7. Browser Extension

#### Build & Load

```bash
cd cortex/apps/browser_extension
pnpm install
```

| Browser | Build | Load from |
|---------|-------|-----------|
| Chrome | `npx plasmo build` | `chrome://extensions` → Developer mode → Load unpacked → `build/chrome-mv3-prod/` |
| Edge | `npx plasmo build --target=edge-mv3` | `edge://extensions` → Developer mode → Load unpacked → `build/edge-mv3-prod/` |

For development with hot reload: `pnpm dev` (Chrome) or `pnpm dev:edge` (Edge).

#### Native Messaging (one-click Start/Stop from browser)

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

### 8. Calibration (recommended)

Sit relaxed for 2 minutes while Cortex learns your personal baselines:

```bash
cortex-calibrate --duration 120
```

Measures resting heart rate, HRV, baseline blink rate, and neutral posture. Results are saved to `storage/baselines/`. Calibration improves state detection accuracy but is not required to start.

For testing without a webcam:
```bash
cortex-calibrate --simulate
```

### 9. VS Code Extension (optional)

```bash
cd cortex/apps/vscode_extension
npm install
npm run compile
code --install-extension cortex-somatic-0.1.0.vsix
```

### 10. Running Tests

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
- Cortex auto-skips Continuity Camera (iPhone/iPad) and rejects any camera it cannot verify by name
- **Restart the daemon** after turning off or disconnecting your iPhone — camera selection only runs at startup
- If it still picks wrong: set `CORTEX_CAPTURE__DEVICE_ID=0` in `.env` (or `1`, `2`, etc.) to hardcode the index

### Camera permission denied
- If launched from browser: grant camera to **Terminal.app** in `System Settings → Privacy & Security → Camera`
- If launched from terminal: grant camera to your terminal app

### "Stop Cortex" doesn't stop the camera
- Click Stop again — uses multi-layer kill chain
- Manual kill: `pkill -f "cortex.scripts.run_dev"`

### LLM provider errors
- **Bedrock:** confirm the bearer token is in Keychain — `security find-generic-password -s cortex.bedrock -a bearer_token` should print the entry. Re-run `security add-generic-password -s cortex.bedrock -a bearer_token -w YOUR_TOKEN -U` to overwrite. Verify `CORTEX_LLM__BEDROCK__AWS_REGION` matches a region your account is provisioned for (default `us-east-2`).
- **Vertex:** re-run `gcloud auth application-default login`. The SDK reads the standard ADC location; no extra env var needed.
- **Direct:** confirm `ANTHROPIC_API_KEY` is exported in the same shell that runs `cortex-dev`.
- **Fallback:** if all providers are down, the planner serves the rule-based deterministic plan and tags responses with `metadata["budget_killed"] = True` when the daily USD rail is hit. Set `CORTEX_LLM__FALLBACK_MODE=direct_anthropic` to retry the direct API instead.

### MediaPipe import errors on Apple Silicon
```bash
pip install --force-reinstall mediapipe
```
Verify native ARM Python: `python3 -c "import platform; print(platform.machine())"` → `arm64`

### Accessibility / pynput errors
Add your terminal to: `System Settings → Privacy & Security → Input Monitoring`

---

## Building the DMG

```bash
# Prerequisites: Python venv with cortex[dev] installed, pnpm, browser extensions built
./cortex/scripts/build_macos_app.sh
# Output: dist/Cortex.dmg
```

For production distribution, set `CORTEX_SIGN_IDENTITY` to your Developer ID and `CORTEX_NOTARIZE_PROFILE` to your notarytool keychain profile.
