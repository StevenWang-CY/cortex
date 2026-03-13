## Azure Deployment Checklist

Use this flow to experience Cortex as a real product on macOS.

### 1. Install and configure

```bash
cd /path/to/Ralph
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e "./cortex[dev]"
export PYTHONPATH="$PWD"
cp cortex/.env.example .env
python -m cortex.scripts.seed_config --root .
```

Set these values in `.env`:

```bash
CORTEX_LLM__MODE=azure
CORTEX_LLM__AZURE__ENDPOINT=https://YOUR-RESOURCE.openai.azure.com
CORTEX_LLM__AZURE__API_KEY=YOUR_KEY
CORTEX_LLM__AZURE__API_VERSION=2025-01-01-preview
CORTEX_LLM__AZURE__DEPLOYMENT_NAME=gpt-5-mini
CORTEX_LLM__AZURE__REASONING_DEPLOYMENT_NAME=gpt-5-mini
```

### 2. Calibrate

```bash
cortex-calibrate --duration 120
```

This writes the active baseline to `storage/baselines/default.json`.

### 3. Install clients

VS Code:

```bash
cd cortex/apps/vscode_extension
npm install
npm run compile
```

Chrome:

```bash
cd cortex/apps/browser_extension
pnpm install
pnpm build
```

Load the browser extension from `chrome://extensions`.

### 4. Start Cortex

Terminal 1:

```bash
cortex-dev
```

Terminal 2:

```bash
python -m cortex.apps.desktop_shell.main
```

### 5. Experience the product

For coding recovery:
- open VS Code with a real assignment or debugging task
- keep the VS Code extension connected
- let Cortex see diagnostics and terminal output

For research recovery:
- keep Chrome open with docs, PDFs, and paper tabs
- let the browser extension classify the active research context

When overwhelm is detected, Cortex can:
- fold unrelated code
- hide non-active research tabs
- show a focused overlay with 1-3 next steps
- restore the workspace when you dismiss, snooze, or recover

### 6. Package a macOS app

```bash
cd cortex
./scripts/build_macos_app.sh
```

Use code signing and DMG packaging for distribution after local verification.
