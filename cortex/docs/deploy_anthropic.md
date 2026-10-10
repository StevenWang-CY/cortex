## Anthropic SDK Deployment Checklist

Use this flow to configure Cortex's optional external planner on macOS. Cortex
uses the Anthropic SDK (`anthropic` 0.125.0 in the lockfile) with one of three
transports. This page describes the repository's integration and model catalog;
provider availability, account access and billing terms require separate
verification. See the [current implementation record](../../docs/implementation-2026-10.md).

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

The repository catalog includes `claude-opus-5`, `claude-sonnet-5`,
`claude-haiku-4-5`, `claude-opus-4-7` and `claude-sonnet-4-6`.
[`resolve_anthropic_model_id`](../libs/llm/anthropic_client.py) maps each logical
id to the configured provider's identifier. A mapping does not establish that
the account or region can serve that model:

| Logical id          | Bedrock (Mantle)              | Vertex                     | Direct              |
| ------------------- | ----------------------------- | -------------------------- | ------------------- |
| `claude-opus-5`     | `anthropic.claude-opus-5`     | `claude-opus-5`            | `claude-opus-5`     |
| `claude-sonnet-5`   | `anthropic.claude-sonnet-5`   | `claude-sonnet-5`          | `claude-sonnet-5`   |
| `claude-haiku-4-5`  | `anthropic.claude-haiku-4-5`  | `claude-haiku-4-5@20251001`| `claude-haiku-4-5`  |
| `claude-opus-4-7`   | `anthropic.claude-opus-4-7`   | `claude-opus-4-7`          | `claude-opus-4-7`   |
| `claude-sonnet-4-6` | `anthropic.claude-sonnet-4-6` | `claude-sonnet-4-6`        | `claude-sonnet-4-6` |

The daily ledger estimates USD cost using the local
[`pricing.py`](../libs/llm/pricing.py) table and records prompt/completion token
counters, including provider-reported cache usage when available. Its totals
are estimates, not invoices or live provider price quotes.

#### Request shape, effort, and `max_tokens`

Every planner call is a structured-output request:
`output_config={"format": {"type": "json_schema", "schema": <PlanDraft>}}`
(`cortex/services/llm_engine/plan_draft.py`). The model may author only the
narrative and proposal fields; daemon-owned fields (`intervention_id`,
`metadata`, `consent_level`, `trigger_url`, `causal_signals`, `plan_warnings`,
`action_id`, `catalog_id`) are not in the schema and are stamped locally. No
tools, forced `tool_choice`, sampling parameters (`temperature`, `top_p`,
`top_k`) or explicit `thinking` configuration are sent. This is the planner's
request policy across its supported transports.

```bash
CORTEX_LLM__EFFORT=medium        # low | medium | high | xhigh | max
CORTEX_LLM__MAX_TOKENS=8192      # minimum 1024
CORTEX_LLM__TIMEOUT_SECONDS=30   # per attempt
```

- `effort` is a global planner setting. It is sent as `output_config.effort`
  only when the local capability catalog enables it: Opus 4.7/5, Sonnet 5 and
  Sonnet 4.6. It is omitted for Haiku 4.5 and unknown model overrides.
  `template_tier_overrides` selects a model tier, not a separate effort value.
- `max_tokens` defaults to 8192 and accepts values of at least 1024. It is an
  output budget, not a guarantee that a plan will fit. A response ending with
  `stop_reason == "max_tokens"` or `"refusal"` is terminal for that call;
  the configured fallback runs without retrying the same response.
- The system prompt requests `cache_control: ephemeral`. A cache request does
  not prove a hit; accounting uses cache token counts from the response.

#### Worst-case timing

The SDK client is built with `max_retries=0`, so the planner's own loop is the
only retry policy: 3 attempts × `timeout_seconds`, plus capped jittered
backoff (2 s + 3 s), retrying only `RateLimitError`, `APITimeoutError`,
`APIConnectionError`, and HTTP 408/409/429/5xx. 401/403 (`auth_error`),
400/422 (`bad_request`), and 404 (`model_unavailable`) are not retried and
trip the affected tier's circuit breaker. With the defaults the retry envelope
is `LLMConfig.planner_worst_case_seconds` = 3 × 30 s + 5 s = 95 s, also exposed
as `llm_client.worst_case_seconds`. An outer deadline must account for that
budget rather than only one `timeout_seconds`; waiting for a concurrency slot
and any fallback work are additional to this calculated envelope. A caller
that stops waiting leaves the shielded SDK call with a completion callback,
which records returned usage and releases its concurrency slot. If event-loop
teardown cancels it before a response, the ledger records an input-token
estimate with zero output tokens; it cannot establish actual provider billing.

### 2. Optional research calibration

```bash
uv run --project cortex --locked cortex-calibrate --duration 120
```

This captures a measured profile and presents it for explicit review and
approval. Approved CLI profiles are considered on the next daemon start;
the desktop calibration flow can apply an approved profile live. Immutable
profiles and the active-calibration store are authoritative; the legacy
`baselines/default.json` is a compatibility projection. See
[Calibration](calibration.md) for storage, identity checks and measurement limits.

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
- provide the current editor context and diagnostic summaries; the shipping
  extension does not collect terminal output

For research recovery:

- keep Chrome open with docs, PDFs, and paper tabs
- let the browser extension classify the active research context

When the behavioral support and policy gates are reached, Cortex can show a focused
proposal with 1–3 next steps. The shipping `suggest_only` mode does not fold
code, hide tabs, or otherwise restructure the workspace. Do not interpret the
gate as a diagnosis of overwhelm.

### 6. Package a macOS app

```bash
./cortex/scripts/build_macos_app.sh
```

Use code signing and DMG packaging for distribution after local verification. Bedrock bearer tokens, Anthropic API keys, and Vertex ADC files are NOT bundled — every user supplies their own credentials during onboarding (BYOK).
