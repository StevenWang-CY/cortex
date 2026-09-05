## Anthropic SDK Deployment Checklist

Use this flow to experience Cortex as a real product on macOS. Cortex talks to Claude exclusively through the Anthropic SDK (`anthropic` 0.125.x, locked); pick one of three transports.

### 1. Install and configure

```bash
cd /path/to/cortex-repo
uv sync --project cortex --locked --extra dev --extra codegen
cp cortex/.env.example .env
uv run --project cortex --locked python -m cortex.scripts.seed_config --root .
```

Set the provider in `.env`. Pick one option below.

Provider credentials alone do not enable network calls. Cortex defaults to the
local deterministic planner. After reading [`privacy.md`](privacy.md), enable
the external preview path explicitly:

```bash
CORTEX_LLM__PRIVACY__PLANNER_MODE=external_redacted
CORTEX_LLM__PRIVACY__EXTERNAL_CONTEXT_ENABLED=true
CORTEX_LLM__PRIVACY__CONSENT_REVISION=context-disclosure-v1
CORTEX_LLM__PRIVACY__PROVIDER_RETENTION_MODE=unverified
```

Every provider request still needs a fresh source selection, exact redacted
preview, and one-time confirmation. Cortex does not verify provider retention
configuration or contractual terms.

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

The daemon reads the token from Keychain when it builds the planner and passes
it **explicitly** to `anthropic.AsyncAnthropicBedrockMantle(aws_region=...,
api_key=<token>)` — the Messages-API Bedrock endpoint, which sends it as
`Authorization: Bearer`. The token is never written to `os.environ`, the `.env`,
or the .app bundle, so no child process (capture worker, native host, launcher
terminals) can inherit it. `AWS_BEARER_TOKEN_BEDROCK` is honoured only as an
operator-supplied fallback when the Keychain entry is absent.

First-run BYOK needs no restart: if the daemon started in external mode without
a token, the planner is created without a transport and the onboarding "save
token" step calls `reload_credentials()`, which constructs the transport
lazily. Until then the privacy status reports `credentials_missing` (distinct
from `external_context_disabled`).

#### Option B — Google Vertex AI

```bash
CORTEX_LLM__PROVIDER=vertex
gcloud auth application-default login
```

`anthropic.AsyncAnthropicVertex(region=...)` reads the standard Application
Default Credentials; the region defaults to `GOOGLE_CLOUD_REGION` or
`us-east5`.

#### Option C — Direct Anthropic API

```bash
CORTEX_LLM__PROVIDER=direct
export ANTHROPIC_API_KEY=sk-ant-...
```

#### Model tiers (all providers)

```bash
CORTEX_LLM__MODEL_DEFAULT=claude-sonnet-5
CORTEX_LLM__MODEL_FAST=claude-haiku-4-5
CORTEX_LLM__MODEL_DEEP=claude-opus-5
CORTEX_LLM__FALLBACK_MODE=rule_based   # default — deterministic plan if all else fails
```

Valid logical ids are `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`
and the still-served legacy tiers `claude-opus-4-7`, `claude-sonnet-4-6`.
`cortex/libs/llm/anthropic_client.resolve_anthropic_model_id` maps each logical
id to the provider-specific identifier:

| Logical id          | Bedrock (Mantle)              | Vertex                     | Direct              |
| ------------------- | ----------------------------- | -------------------------- | ------------------- |
| `claude-opus-5`     | `anthropic.claude-opus-5`     | `claude-opus-5`            | `claude-opus-5`     |
| `claude-sonnet-5`   | `anthropic.claude-sonnet-5`   | `claude-sonnet-5`          | `claude-sonnet-5`   |
| `claude-haiku-4-5`  | `anthropic.claude-haiku-4-5`  | `claude-haiku-4-5@20251001`| `claude-haiku-4-5`  |
| `claude-opus-4-7`   | `anthropic.claude-opus-4-7`   | `claude-opus-4-7`          | `claude-opus-4-7`   |
| `claude-sonnet-4-6` | `anthropic.claude-sonnet-4-6` | `claude-sonnet-4-6`        | `claude-sonnet-4-6` |

Pricing (`cortex/libs/llm/pricing.py`, USD per million tokens, input/output):
Opus 5 and Opus 4.7 $5/$25, Sonnet 5 $2/$10, Sonnet 4.6 $3/$15, Haiku 4.5
$1/$5; cache reads bill at 0.1x input, five-minute cache writes at 1.25x. The
daily cost ledger also keeps per-day prompt/completion token counters.

#### Request shape, effort, and `max_tokens`

Every planner call is a structured-output request:
`output_config={"format": {"type": "json_schema", "schema": <PlanDraft>}}`
(`cortex/services/llm_engine/plan_draft.py`). The model may author only the
narrative and proposal fields; daemon-owned fields (`intervention_id`,
`metadata`, `consent_level`, `trigger_url`, `causal_signals`, `plan_warnings`,
`action_id`, `catalog_id`) are not in the schema and are stamped locally. No
tools, no forced `tool_choice`, and no sampling parameters are sent —
`temperature` / `top_p` / `top_k` return HTTP 400 on Opus 4.7/5 and Sonnet 5.

```bash
CORTEX_LLM__EFFORT=medium        # low | medium | high | xhigh | max
CORTEX_LLM__MAX_TOKENS=8192      # minimum 1024
CORTEX_LLM__TIMEOUT_SECONDS=30   # per attempt
```

- `effort` is sent as `output_config.effort` only to models that accept it
  (Opus 4.7/5, Sonnet 5, Sonnet 4.6); it is omitted for Haiku 4.5, which
  rejects it. `medium` balances latency and depth for interventions; raise it
  for the deep tier via a per-template override if diagnoses feel shallow.
- Opus 5 and Sonnet 5 run adaptive thinking by default when `thinking` is
  omitted, so `max_tokens` caps thinking **plus** the JSON plan. Plans carry
  per-tab recommendations for up to 30 tabs and an error analysis; 8192 is the
  floor that avoids `stop_reason == "max_tokens"` truncation. A truncated or
  refused response (`stop_reason == "refusal"`, HTTP 200 from the safety
  classifiers) falls back to the deterministic plan immediately — neither is
  retried.
- The system prompt is marked `cache_control: ephemeral`; it caches on models
  whose minimum cacheable prefix it exceeds (Opus 5: 512 tokens, Sonnet 5 /
  Sonnet 4.6: 1024, Opus 4.7: 2048) and silently does not on Haiku 4.5 (4096).

#### Worst-case timing

The SDK client is built with `max_retries=0`, so the planner's own loop is the
only retry policy: 3 attempts × `timeout_seconds`, plus capped jittered
backoff (2 s + 3 s), retrying only `RateLimitError`, `APITimeoutError`,
`APIConnectionError`, and HTTP 408/409/429/5xx. 401/403 (`auth_error`),
400/422 (`bad_request`), and 404 (`model_unavailable`) are not retried and
trip the affected tier's circuit breaker. With the defaults the bound is
`LLMConfig.planner_worst_case_seconds` = 3 × 30 s + 5 s = 95 s; the daemon
should size any outer `wait_for` from that value (also exposed as
`llm_client.worst_case_seconds`), not from `timeout_seconds` alone. A caller
that stops waiting does not orphan the HTTP call: it completes in the
background, its real usage is billed, and its concurrency slot is released.

### 2. Optional research calibration

```bash
uv run --project cortex --locked cortex-calibrate --duration 120
```

This writes the active baseline to `storage/baselines/default.json`.

### 3. Install clients

VS Code:

```bash
cd cortex/apps/vscode_extension
npm ci
npm run compile
```

Chrome:

```bash
cd cortex/apps/browser_extension
pnpm install --frozen-lockfile
pnpm exec plasmo build
```

Load the browser extension from `chrome://extensions`.

### 4. Start Cortex

**Option A — Desktop App (recommended):**
Open **Cortex.app** from `/Applications` (installed via DMG). The app starts the daemon and dashboard automatically.

**Option B — Terminal (developer setup):**

Terminal 1:

```bash
uv run --project cortex --locked cortex-dev
```

Terminal 2:

```bash
uv run --project cortex --locked python -m cortex.apps.desktop_shell.main
```

### 5. Experience the product

For coding recovery:
- open VS Code with a real assignment or debugging task
- keep the VS Code extension connected
- let Cortex see diagnostics and terminal output

For research recovery:
- keep Chrome open with docs, PDFs, and paper tabs
- let the browser extension classify the active research context

When the legacy heuristic support gate is reached, Cortex can show a focused
proposal with 1–3 next steps. The shipping `suggest_only` mode does not fold
code, hide tabs, or otherwise restructure the workspace. Do not interpret the
gate as a diagnosis of overwhelm.

### 6. Package a macOS app

```bash
./cortex/scripts/build_macos_app.sh
```

Use code signing and DMG packaging for distribution after local verification. Bedrock bearer tokens, Anthropic API keys, and Vertex ADC files are NOT bundled — every user supplies their own credentials during onboarding (BYOK).
