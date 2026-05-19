## Anthropic SDK Deployment Checklist

Use this flow to experience Cortex as a real product on macOS. Cortex talks to Claude exclusively through the Anthropic SDK; pick one of three transports.

### 1. Install and configure

```bash
cd /path/to/cortex-repo
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e "./cortex[dev]"
export PYTHONPATH="$PWD"
cp cortex/.env.example .env
python -m cortex.scripts.seed_config --root .
```

Set the provider in `.env`. Pick one option below.

#### Option A — AWS Bedrock (default)

```bash
CORTEX_LLM__PROVIDER=bedrock
CORTEX_LLM__BEDROCK__AWS_REGION=us-east-2
CORTEX_LLM__USE_KEYCHAIN=true   # default
```

Store the bearer token in macOS Keychain (one-time):

```bash
security add-generic-password -s cortex.bedrock -a bearer_token -w YOUR_TOKEN
```

The daemon reads the token from Keychain at startup and exports it as `AWS_BEARER_TOKEN_BEDROCK` for the SDK; the secret never lands in the `.env` or the .app bundle.

#### Option B — Google Vertex AI

```bash
CORTEX_LLM__PROVIDER=vertex
gcloud auth application-default login
```

The SDK reads the standard Application Default Credentials.

#### Option C — Direct Anthropic API

```bash
CORTEX_LLM__PROVIDER=direct
export ANTHROPIC_API_KEY=sk-ant-...
```

#### Model tiers (all providers)

```bash
CORTEX_LLM__MODEL_DEFAULT=claude-sonnet-4-6
CORTEX_LLM__MODEL_FAST=claude-haiku-4-5
CORTEX_LLM__MODEL_DEEP=claude-opus-4-7
CORTEX_LLM__FALLBACK_MODE=rule_based   # default — deterministic plan if all else fails
```

`cortex/libs/llm/anthropic_client.resolve_anthropic_model_id` maps each logical id to the provider-specific identifier (Bedrock inference profile, Vertex revision, or direct API name).

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

**Option A — Desktop App (recommended):**
Open **Cortex.app** from `/Applications` (installed via DMG). The app starts the daemon and dashboard automatically.

**Option B — Terminal (developer setup):**

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

Use code signing and DMG packaging for distribution after local verification. Bedrock bearer tokens, Anthropic API keys, and Vertex ADC files are NOT bundled — every user supplies their own credentials during onboarding (BYOK).
