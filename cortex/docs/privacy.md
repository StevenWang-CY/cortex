# Privacy and context disclosure

Last implementation review: 2026-08-25. Disclosure contract:
`context-disclosure-v1`.

Cortex is local-first and network-off by default. Camera frames, face
landmarks, pulse waveforms, input events, and raw physiological observations
are not fields in the external planner contract. External planning is a
separate, fail-closed mode in which the user previews one exact, redacted
payload and confirms that payload once. Previewing does not contact a model
provider, and a model response is a proposal—not authority to change the
workspace.

## Planner modes

| Mode | Default | Reads local workspace context | Model network request |
| --- | --- | --- | --- |
| `no_llm` | Yes | The deterministic local planner may use it | Never |
| `no_content` | No | No; returns a context-independent local plan | Never |
| `external_redacted` | No | Only explicitly selected, brokered fields | Only after an exact preview and one-time confirmation |

External mode is enabled only when all three configuration values agree:

```text
planner_mode = external_redacted
external_context_enabled = true
consent_revision = context-disclosure-v1
```

Changing the required disclosure revision invalidates an older
acknowledgement. Every source selector defaults to false. Enabling external
mode does not create standing permission to send future context.

## Data flow

```text
local sensors and workspace clients
              |
              v
     current in-memory TaskContext
              |
              v
 per-source selection + field catalog
              |
              v
 normalize -> minimize -> redact -> cap
              |
              v
 exact preview + 15–60 s one-time handle
              |
       explicit confirmation
              |
              v
 configured provider -> validated proposal
              |
              v
 separate action manifest and authorization
```

The desktop privacy sheet requests a preview from the daemon's current
snapshot. It does not copy raw workspace data into the UI request. The response
shows the exact outbound `TaskContext`, generated prompt, byte count, field
dispositions, redaction count, destination, model, and provider-retention
caveat.

## Field catalog and selection

`CONTEXT_FIELD_CATALOG` in
`cortex/services/llm_engine/context_broker.py` is the executable catalog. A
test walks every `TaskContext` leaf and fails if a field is not catalogued.
These are the selectable groups:

| Selector | Included when available | Deliberately excluded or minimized |
| --- | --- | --- |
| Workspace aggregates | mode, active app, complexity aggregate | no process dump or window contents |
| Support estimate | named support state, evidence strength, dwell | no frame, landmark, waveform, IBI, HRV, or raw biometric stream |
| User goal | current goal and browser focus goal | bounded and secret-redacted |
| Editor metadata | file basename, symbol, diagnostic severity/line | absolute path, visible range, diagnostic source/code |
| Editor content | visible code and diagnostic messages | recent-edit history; bounded content only |
| Terminal content | detected error summaries | command history, running command, and raw terminal lines |
| Browser metadata | bounded titles, URL origins, tab type, topic hint, aggregate counts | tab IDs and URL paths/query/fragment/userinfo |
| Browser content | active-page excerpt | only after an exact per-origin browser grant; capped and redacted |
| Learned preferences | bounded relevance preferences | no policy/research event ledger |
| Extra context | the note typed by the user | no implicit clipboard or file read |

The broker also preserves each field's origin and classification in the
disclosure manifest. Text remains untrusted even after redaction; prompt
instructions cannot grant a capability.

## Minimization and redaction

Before a field can enter an outbound prompt, Cortex:

1. normalizes Unicode with NFKC and removes bidi/zero-width controls;
2. replaces every absolute POSIX path with two or more segments (any root —
   `/Users`, `/Applications`, `/etc`, `/usr/local`, `/srv`, `/mnt`, `/root`,
   `/workspace`, …) and every Windows drive or UNC path with its basename;
   URL paths are left to the URL rule below;
3. reduces browser URLs to an HTTP(S) origin;
4. redacts URI credentials, private keys, common provider tokens, JWTs,
   generic secret assignments, and bounded high-entropy token candidates;
5. caps each field, the complete prompt (24,000 characters), and encoded
   request (96,000 bytes).

Redaction is defense in depth, not a proof that arbitrary secrets cannot pass.
Review the exact preview. Do not select content containing credentials,
private keys, regulated records, or third-party confidential data.

## Preview, confirm, cancel

All routes are authenticated loopback routes:

| Route | Effect |
| --- | --- |
| `GET /privacy/context/status` | Reports mode, destination posture, and live preview count; no provider probe |
| `POST /privacy/context/preview/current` | Creates a preview from the daemon's current snapshot; no network call |
| `POST /privacy/context/preview` | Developer/API equivalent with a caller-supplied local snapshot; no network call |
| `POST /privacy/context/confirm` | Requires the exact handle and `SEND PREVIEWED CONTEXT ONCE`; burns the handle before awaiting the provider |
| `DELETE /privacy/context/preview/{id}` | Burns a prepared handle without sending |

Handles are random, memory-only, limited to 16, expire in 15–60 seconds, and
are consumed on success, wrong confirmation, expiry, cancellation, or any
send attempt. A payload digest prevents `/llm/plan` callers from changing a
previewed body. The desktop sheet cancels the old handle when sources change,
the user goes back, the sheet closes, or the preview expires.

## Browser privacy boundary

The extension distinguishes learning-activity metadata from page-body
context:

- Static content scripts run only on the explicit learning-site allowlist in
  their `PlasmoCSConfig`; there is no hidden `<all_urls>` content script.
- Incognito scripts return before initialization, and the background rejects
  any incognito sender.
- Periodic activity records contain bounded title/URL/position metadata. They
  do not contain page excerpts or persisted source code. URLs lose userinfo,
  fragments, tracking fields, and secret-like query parameters before storage.
- “Page context” is off per site. Allowing it records Cortex consent for the
  active HTTP(S) origin and requests that exact browser origin. Extraction
  requires both that explicit record and browser permission. A required
  learning-site content-script host is not mistaken for consent.
- Revocation burns the Cortex consent, attempts browser-permission removal,
  clears the current context snapshot, and scrubs content fields from stored
  activity records. Required content-script hosts may remain visible in the
  browser because they support metadata telemetry; they do not bypass the
  Cortex consent check.

Local activity storage is capped at 200 resume records. It can retain a
sanitized page title, resume-capable URL path, position, topic tags, and dwell
metadata until evicted or local data is deleted. This metadata can itself be
sensitive; do not install the extension in a browser profile where this local
history is inappropriate.

## Local and provider retention

| Data | Cortex retention |
| --- | --- |
| Raw current task context | In daemon/client memory for the active flow; not written by the broker |
| Prepared redacted payload | Memory only, maximum 60 seconds, cancellable, maximum 16 |
| LLM plan cache | Memory only, configurable 0–60 seconds; a plan can echo redacted context |
| Privacy preview/confirmation logs | Event/status metadata only; prompt and raw context are not logged |
| Browser activity metadata | Browser local storage, capped at 200 records; see above |
| Credentials | macOS Keychain or provider-standard credential store; never bundled in the app |

Cortex cannot inspect an account's effective provider contract or retention
configuration. The sheet therefore never asserts zero retention. Provider
documentation reviewed on 2026-08-25:

| Provider | Conservative disclosure | Primary documentation |
| --- | --- | --- |
| AWS Bedrock | Effective account/project mode and model determine retention. `store=false` alone is not ZDR; AWS documents `none` as its durable-storage-off mode. | [Amazon Bedrock data retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html) |
| Google Vertex AI | Model, feature, terms, abuse monitoring, request logging, and cache configuration can change retention. | [Vertex AI zero data retention](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/vertex-ai-zero-data-retention) |
| Direct Anthropic API | Anthropic documents a standard 30-day deletion window with contractual, usage-policy, legal, and covered-model exceptions. | [Anthropic organization data retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data) |

Verify the active account, project, model, region, feature flags, and contract
immediately before use. The `provider_retention_mode` setting is a user
declaration shown in the sheet; it is not provider attestation.

## Authority boundary

Model output is parsed into a strict schema and treated as untrusted proposal
data. It cannot grant browser/editor/file authority, mint an action ID, or
change the execution mode. Workspace effects require a separately materialized
action manifest, exact user authorization, idempotent command, durable receipt,
postcondition verification, and scoped restore path. Prompt injection can
alter a proposed sentence; it cannot satisfy that transaction protocol.

## Verification

```bash
pytest -q cortex/tests/unit/test_context_privacy_broker.py \
  cortex/tests/unit/test_context_privacy_desktop.py \
  cortex/tests/unit/test_api_gateway.py

cd cortex/apps/browser_extension
pnpm exec tsc --noEmit
pnpm exec vitest run __tests__/context_privacy.spec.ts \
  __tests__/site_access.spec.ts __tests__/activity_privacy.spec.ts \
  __tests__/manifest_privacy.spec.ts
```

When adding a `TaskContext` field, add its policy, source selector (or explicit
never-send disposition), preview copy, redaction bound, generated schema, and
tests in the same change.
