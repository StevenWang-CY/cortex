# Setup Guide

## Prerequisites

- **Python 3.11+** (3.12 also supported)
- **macOS** (primary target; Linux and Windows are secondary)
- **Webcam** (built-in or USB; 640x480 @ 30 FPS minimum)
- **SSH access** to `gwhiz1.cis.upenn.edu` (for remote LLM inference)
- **Node.js 18+** (for VS Code extension development)
- **pnpm** (for Chrome extension development)

## 1. Clone & Install Python Backend

```bash
git clone <repo-url>
cd cortex

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

Verify the installation:

```bash
python -c "from cortex.libs.config.settings import get_config; print(get_config())"
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
cp .env.example .env
```

Edit `.env` to match your setup. The most important settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `CORTEX_LLM_MODE` | `remote` | LLM backend: `remote`, `local`, or `openai_compat` |
| `CORTEX_LLM_SSH_USER` | `wangcy07` | SSH username for gwhiz1 |
| `CORTEX_LLM_REMOTE_HOST` | `gwhiz1.cis.upenn.edu` | Remote GPU host |
| `CORTEX_LLM_REMOTE_PORT` | `8800` | vLLM server port on remote host |
| `CORTEX_API_PORT` | `9472` | REST API port |
| `CORTEX_WS_PORT` | `9473` | WebSocket port |
| `CORTEX_CAPTURE_DEVICE_ID` | `0` | Webcam device index |

Configuration loads from `libs/config/defaults.yaml` first, then environment variables override with the `CORTEX_` prefix. See [`.env.example`](../.env.example) for all options.

### Initialize Storage

```bash
python -m cortex.scripts.seed_config
```

This creates:
- `storage/` directory tree (`sessions/`, `cache/`, `baselines/`, `logs/`, `exports/`)
- Default `.env` from configuration defaults
- Default baseline profile at `storage/baselines/default_baselines.json`
- Cortex entries in `.gitignore`

Use `--dry-run` to preview without writing, or `--force` to overwrite existing files.

## 3. Remote LLM Setup (gwhiz1)

Cortex uses Qwen-3-8B running on a remote GPU via vLLM with an OpenAI-compatible API.

### SSH Key Setup

```bash
# Generate a key if you don't have one
ssh-keygen -t ed25519 -C "cortex-dev"

# Copy to remote host
ssh-copy-id wangcy07@gwhiz1.cis.upenn.edu

# Test connection
ssh wangcy07@gwhiz1.cis.upenn.edu "hostname"
```

### SSH Tunnel

The tunnel forwards `localhost:8800` to the remote vLLM server:

```bash
# Start tunnel (background mode with auto-reconnect)
bash scripts/setup_ssh_tunnel.sh --background

# Check tunnel status
bash scripts/setup_ssh_tunnel.sh --check

# Stop tunnel
bash scripts/setup_ssh_tunnel.sh --stop
```

The script uses `ServerAliveInterval=30` and `ExitOnForwardFailure=yes` for reliability.

### Start/Verify Remote vLLM

```bash
# Check if vLLM is running on remote, start if needed
python -m cortex.scripts.run_llm_server start

# Test with a sample inference request
python -m cortex.scripts.run_llm_server test
```

### Local Fallback (Ollama)

If the remote GPU is unavailable, Cortex falls back to local Ollama:

```bash
# Install Ollama (macOS)
brew install ollama

# Pull a model
ollama pull llama3.1:8b

# Start Ollama server
ollama serve
```

Set `CORTEX_LLM_MODE=local` in `.env` to use Ollama exclusively.

## 4. Calibration

Cortex uses personal baselines for accurate state detection. Run the calibration script in a relaxed environment:

```bash
# Full 2-minute calibration with webcam
cortex-calibrate

# Simulated calibration (no webcam required, for testing)
cortex-calibrate --simulate
```

Calibration measures:
- Resting heart rate and HRV
- Baseline blink rate
- Neutral posture reference
- Normal mouse/keyboard patterns

Results are saved to `storage/baselines/`. See [calibration.md](calibration.md) for details.

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

## 7. VS Code Extension (Future)

The VS Code extension provides editor context and intervention UI. Once implemented:

```bash
cd apps/vscode-extension
npm install
npm run compile

# Install in VS Code
code --install-extension cortex-vscode-0.1.0.vsix
```

## 8. Chrome Extension (Future)

The Chrome extension provides browser context and tab management. Once implemented:

```bash
cd apps/chrome-extension
pnpm install
pnpm build

# Load unpacked extension in Chrome
# chrome://extensions → Developer mode → Load unpacked → dist/
```

## Troubleshooting

### Webcam not detected

```bash
# List available devices
python -c "import cv2; [print(f'Device {i}: {cv2.VideoCapture(i).isOpened()}') for i in range(5)]"
```

Set `CORTEX_CAPTURE_DEVICE_ID` to the correct device index.

### SSH tunnel failures

```bash
# Check if port is already in use
lsof -i :8800

# Kill existing tunnel
bash scripts/setup_ssh_tunnel.sh --stop

# Start fresh
bash scripts/setup_ssh_tunnel.sh --background
```

### Accessibility permission denied

If `pynput` raises `PermissionError`, add your terminal application to:
`System Settings → Privacy & Security → Accessibility`

### MediaPipe import errors

```bash
pip install --force-reinstall mediapipe
```

On Apple Silicon, ensure you're using a native ARM Python (not Rosetta).
