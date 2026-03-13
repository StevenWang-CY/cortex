# Setup Guide

## Prerequisites

- **Python 3.11+** (3.12 also supported)
- **macOS** (primary supported target for Cortex v1)
- **Webcam** (built-in or USB; 640x480 @ 30 FPS minimum)
- **Azure OpenAI deployment** (recommended production backend)
- **Node.js 18+** (for VS Code extension development)
- **pnpm** (for Chrome extension development)

## 1. Clone & Install Python Backend

```bash
git clone <repo-url>
cd <repo-dir>

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e "./cortex[dev]"
export PYTHONPATH="$PWD"
```

Verify the installation:

```bash
python -c "from cortex.libs.config.settings import get_config; print(get_config().llm.mode)"
```

### macOS Permissions

Cortex requires two macOS permissions:

1. **Camera access** — prompted automatically on first webcam use
2. **Accessibility (Input Monitoring)** — required for keyboard/mouse telemetry via `pynput`

To grant accessibility access:
`System Settings → Privacy & Security → Accessibility → Add your terminal app`

## 2. Configuration

Copy the example environment file:

```bash
cp cortex/.env.example .env
```

Edit `.env` to match your setup. Cortex uses nested environment variables with `__`, not the old flat names.

Recommended Azure configuration:

| Variable | Default | Description |
|----------|---------|-------------|
| `CORTEX_LLM__MODE` | `azure` | `azure`, `local`, `rule_based`, or `remote` |
| `CORTEX_LLM__AZURE__ENDPOINT` | empty | Azure OpenAI resource endpoint |
| `CORTEX_LLM__AZURE__API_KEY` | empty | Azure API key for dev use |
| `CORTEX_LLM__AZURE__API_VERSION` | `2025-01-01-preview` | Azure API version |
| `CORTEX_LLM__AZURE__DEPLOYMENT_NAME` | empty | Primary deployment for overlay/simplified plans |
| `CORTEX_LLM__AZURE__REASONING_DEPLOYMENT_NAME` | empty | Optional deeper-reasoning deployment |
| `CORTEX_LLM__AZURE__MAX_COMPLETION_TOKENS` | `1024` | Azure-compatible response token limit |
| `CORTEX_API__PORT` | `9472` | REST API port |
| `CORTEX_API__WS_PORT` | `9473` | WebSocket port |
| `CORTEX_CAPTURE__DEVICE_ID` | `0` | Webcam device index |

Configuration loads from `libs/config/defaults.yaml` first, then environment variables override with the `CORTEX_` prefix. See [`.env.example`](../.env.example) for all options.

For packaged macOS installs, store the Azure API key in Keychain instead of `.env`. Cortex will check Keychain first when `CORTEX_LLM__AZURE__USE_KEYCHAIN=true`.

### Initialize Storage

```bash
python -m cortex.scripts.seed_config --root .
```

This creates:
- `storage/` directory tree (`sessions/`, `cache/`, `baselines/`, `logs/`, `exports/`)
- Default `.env` from configuration defaults
- Default baseline profile at `storage/baselines/default.json`
- Cortex entries in `.gitignore`

Use `--dry-run` to preview without writing, or `--force` to overwrite existing files.

## 3. Azure OpenAI Setup

Cortex v1 is designed to use Azure OpenAI as the primary planner, then fall back to local Ollama, then to built-in rule-based guidance.

Example:

```bash
CORTEX_LLM__MODE=azure
CORTEX_LLM__AZURE__ENDPOINT=https://your-resource.openai.azure.com
CORTEX_LLM__AZURE__API_KEY=...
CORTEX_LLM__AZURE__API_VERSION=2025-01-01-preview
CORTEX_LLM__AZURE__DEPLOYMENT_NAME=gpt-5-mini
CORTEX_LLM__AZURE__REASONING_DEPLOYMENT_NAME=gpt-5-mini
```

Important:
- Azure `gpt-5-mini` rejects `max_tokens`; Cortex now sends `max_completion_tokens`.
- Cortex only sends workspace text context, current state label/confidence, and allowed intervention constraints to Azure. Raw camera frames and biometrics stay local.

### Optional Local Fallback (Ollama)

```bash
brew install ollama
ollama pull llama3.1:8b
ollama serve
```

Set `CORTEX_LLM__MODE=local` in `.env` to use Ollama exclusively.

## 4. Calibration

Cortex uses personal baselines for accurate state detection. Run the calibration script in a relaxed environment:

```bash
# Full 2-minute calibration with webcam and live telemetry
cortex-calibrate --duration 120

# Simulated calibration (no webcam required, for testing)
cortex-calibrate --simulate
```

Calibration measures:
- Resting heart rate and HRV
- Baseline blink rate
- Neutral posture reference
- Normal mouse/keyboard patterns

Results are saved to `storage/baselines/`. See [calibration.md](calibration.md) for details.
The calibration command now also refreshes the active baseline file at `storage/baselines/default.json`.

## 5. Running Cortex

### Development Server

Start all services with hot reload:

```bash
cortex-dev
```

This starts:
- API Gateway on `http://127.0.0.1:9472`
- WebSocket server on `ws://127.0.0.1:9473`
- Capture, physio, kinematics, telemetry, state, context, and intervention services

### Standalone Webcam Test

Verify your webcam and face tracking work:

```bash
cortex-capture
```

This opens a window showing the webcam feed with face detection overlays, FPS counter, and quality metrics. Press `q` to quit.

## 6. Running Tests

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

## 7. Desktop App

Run the PySide desktop shell:

```bash
python -m cortex.apps.desktop_shell.main
```

On first launch, complete onboarding:
- grant camera and Accessibility/Input Monitoring permissions
- confirm Azure endpoint and deployment
- install VS Code and Chrome extensions
- run calibration

## 8. VS Code Extension

The VS Code extension provides editor context, diagnostics, integrated terminal context, fold/apply, and restore:

```bash
cd apps/vscode_extension
npm install
npm run compile

# Install in VS Code
code --install-extension cortex-vscode-0.1.0.vsix
```

## 9. Chrome Extension

The Chrome extension provides browser context, PDF/paper classification, tab hide/restore, and research overlays:

```bash
cd apps/browser_extension
pnpm install
pnpm build

# Load unpacked extension in Chrome
# chrome://extensions → Developer mode → Load unpacked → dist/
```

## 10. Package a macOS App

Build the bundled desktop app:

```bash
./scripts/build_macos_app.sh
```

This produces a PyInstaller-based macOS app bundle in `dist/`. Code signing and DMG wrapping are the final release steps for distribution.

## Troubleshooting

### Webcam not detected

```bash
# List available devices
python -c "import cv2; [print(f'Device {i}: {cv2.VideoCapture(i).isOpened()}') for i in range(5)]"
```

Set `CORTEX_CAPTURE_DEVICE_ID` to the correct device index.

### Azure connection failures

Check:
- `CORTEX_LLM__MODE=azure`
- endpoint matches your Azure resource exactly
- deployment names exist in Azure
- API version is `2025-01-01-preview`
- API key is valid or present in Keychain

### Accessibility permission denied

If `pynput` raises `PermissionError`, add your terminal application to:
`System Settings → Privacy & Security → Accessibility`

### MediaPipe import errors

```bash
pip install --force-reinstall mediapipe
```

On Apple Silicon, ensure you're using a native ARM Python (not Rosetta).
