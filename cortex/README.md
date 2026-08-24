<p align="center">
  <img src="assets/banner.svg?v=2" alt="Cortex Banner" width="450" />
</p>

# Cortex

Cortex is a macOS research prototype for local sensing and conservative
workspace support. It combines webcam-derived observations, input/window
telemetry, browser and editor context, and schema-validated planning. Its
shipping authority boundary is deliberately narrow: proposals may be shown,
but the default product does not restructure the workspace.

The authoritative redesign and evidence audit is
[`../IMPLEMENTATION.md`](../IMPLEMENTATION.md). That document distinguishes
observed behavior, design decisions, research constraints, open risks, and
promotion gates. This README describes the current product boundary.

## Safety and evidence status

Cortex is alpha research software, not a medical device, diagnostic system,
or validated estimator of a person's cognitive state.

- `intervention.execution_mode` defaults to `suggest_only` for fresh and
  migrated installations. Proposal and legacy trigger messages are
  presentation-only in the daemon, browser extension, VS Code extension, and
  desktop shell.
- Consent has exact outcomes. A request above the earned or global ceiling is
  a downgrade/denial and cannot execute the original action manifest.
- Webcam pulse is an experimental wellness estimate and is published only
  after signal-quality checks. Accuracy varies with lighting, motion, camera,
  occlusion, and individual characteristics.
- Camera-derived HRV, LF/HF, pNN50, nonlinear HRV, respiration, breath-pause
  detection, the former stress integral, and physiology-triggered breaks are
  unavailable in product mode pending metric-specific reference-sensor
  validation.
- FLOW/HYPER/HYPO/RECOVERY are legacy heuristic support labels. Their scores
  are not calibrated probabilities and must not be interpreted as diagnoses.
- The legacy adaptive-policy and “causal report” modules are research
  diagnostics. They do not establish causal effects and do not receive
  workspace mutation authority in the default product.
- No raw camera frames are intentionally persisted. Workspace context selected
  for an LLM request can leave the machine; review provider configuration and
  the privacy documentation before enabling cloud planning.

## Current runtime

```text
camera + local activity + client context
                 |
                 v
        quality-gated observations
                 |
                 v
       heuristic support estimates
                 |
                 v
      deterministic eligibility gates
                 |
                 v
      schema-validated LLM/rule plan
                 |
                 v
        non-mutating proposal UI
```

The daemon runs FastAPI on `127.0.0.1:9472` and WebSocket on
`127.0.0.1:9473`. An optional launcher listens on `127.0.0.1:9471`. The
PySide6 desktop shell can host the daemon in-process. Chrome/Edge and VS Code
clients connect over authenticated local transports.

### Main components

| Component | Responsibility |
|---|---|
| `services/capture_service` | camera selection, timestamped frames, face/ROI and quality observations |
| `services/physio_engine` | experimental rPPG pulse estimation; unsupported derived physiology is contained |
| `services/kinematics_engine` | blink and head/neck proxy features |
| `services/telemetry_engine` | local input/window and focus-transition telemetry |
| `services/state_engine` | legacy heuristic scoring, smoothing, eligibility, and detector research code |
| `services/context_engine` | browser/editor/terminal context assembly |
| `services/llm_engine` | Anthropic transport, prompt construction, parsing, and deterministic fallback |
| `services/intervention_engine` | plan validation and the currently contained execution path |
| `services/api_gateway` | authenticated HTTP/WebSocket boundary |
| `apps/browser_extension` | Plasmo MV3 client and proposal UI |
| `apps/vscode_extension` | editor context client and proposal notifications |
| `apps/desktop_shell` | macOS application shell, onboarding, health, and proposal UI |

The schema package under `libs/schemas` is the source of truth for Python and
generated TypeScript boundary models. Native-messaging requests and responses
use a length-prefixed JSON discriminated union and the canonical
`auth_token` field.

## Supported and compatibility-only signals

| Surface | Product status |
|---|---|
| camera pulse/BPM | experimental, quality-gated wellness estimate |
| blink rate and PERCLOS | heuristic local feature; elapsed-time redesign is in progress |
| head/neck pose proxy | heuristic local feature; not a shoulder/posture diagnosis |
| input/window telemetry | local behavioral context |
| HRV/RMSSD/SDNN/pNN50/LF-HF/entropy | unavailable in product mode |
| respiration or “screen apnea” | unavailable in product mode |
| stress integral / biology-driven break | unavailable and cannot trigger an action |
| state “confidence” | legacy score only; not a calibrated probability |

Compatibility fields and messages may remain decodable for one migration
window. They are emitted as unavailable or ignored, never silently
reinterpreted as evidence.

## Workspace proposals and authority

The planner can describe tab organization, error investigation, micro-steps,
or other workspace changes. Descriptions are not commands. In the shipping
default:

- receiving `INTERVENTION_TRIGGER`, `INTERVENTION_PROPOSED`,
  `BREAK_RECOMMENDATION`, `PRE_BREAK_WARNING`, or `BREATHING_OVERLAY` cannot
  close/hide/group tabs, fold editors, disable suggestions, or mutate files;
- a consent downgrade is non-executable;
- clients reject apply traffic while in suggestion-only mode;
- the emergency stop path remains independent of the LLM and policy modules.

The repository still contains experimental adapters and legacy action types so
old data can be decoded and transactional execution can be built behind a
feature boundary. Their presence is not a claim that restart-safe restoration
or autonomous execution is release-ready.

## LLM configuration and privacy

Planning supports the Anthropic SDK through AWS Bedrock, Google Vertex AI, or
the direct Anthropic API. `CORTEX_LLM__PROVIDER` selects the transport. The
default fallback is deterministic rule-based planning.

LLM requests may contain selected workspace material such as file paths,
diagnostics, terminal excerpts, tab titles/URLs, page excerpts, and the focus
goal. They do not intentionally include raw camera frames or biometric feature
values. Treat workspace text as potentially sensitive. Provider credentials
belong in environment configuration or macOS Keychain; never commit or bundle
them.

See [`docs/deploy_anthropic.md`](docs/deploy_anthropic.md) for provider setup.
The context-preview/redaction broker and per-category controls are tracked in
WP-9 of the implementation plan; until those gates pass, cloud planning should
be enabled only with informed test data.

## Install and run

Requirements: macOS 13+, Python 3.11+, Node.js 18+, pnpm, and a webcam for
camera features. Redis is optional; the daemon can use its in-memory fallback.

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools
pip install -e '.[dev]'
cp .env.example .env
python -m cortex.scripts.run_dev
```

Desktop shell:

```bash
python -m cortex.apps.desktop_shell.main
python -m cortex.apps.desktop_shell.main --in-process
```

Browser extension:

```bash
cd apps/browser_extension
pnpm install --frozen-lockfile
pnpm exec plasmo build
```

Load `build/chrome-mv3-prod/` from `chrome://extensions`. For Edge, run
`pnpm exec plasmo build --target=edge-mv3` and load the Edge output directory.

VS Code extension:

```bash
cd apps/vscode_extension
npm ci
npm run compile
npm test
npm run package:vsix
```

### Native messaging on macOS

```bash
python -m cortex.scripts.install_native_host
```

Fully quit the Chromium browser with Cmd+Q and relaunch it after installing or
updating the host. The installer patches the tracked portable shebang to the
active interpreter in the installed copy. The host launches the daemon in the
foreground through Terminal.app because processes directly spawned by Chrome
inherit Chrome's camera-permission context.

Do not use a broad `tccutil reset Camera`; if debugging requires a reset, scope
it to Cortex's bundle identifier.

## Local API boundary

HTTP mutation routes require `Authorization: Bearer <auth_token>`; the legacy
`X-Cortex-Auth-Token` header is accepted only for compatibility. WebSocket
clients must send an `AUTH` message with `auth_token` before `IDENTIFY` or
other traffic. The token is stored with owner-only permissions in the Cortex
application-support directory.

Important endpoints:

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | readiness/degradation summary; token is optional |
| GET | `/status/current` | current legacy support estimate |
| GET | `/api/stress-integral` | compatibility response: unavailable, never a break trigger |
| GET | `/api/helpfulness/summary` | legacy diagnostic summary |
| POST | `/consent/reset` | reset consent records |
| POST | `/shutdown` | graceful daemon shutdown |
| WS | `ws://127.0.0.1:9473` | authenticated client protocol |

See [`docs/apis.md`](docs/apis.md) for payloads and compatibility notes.

## Camera and lifecycle constraints

- Leave `CORTEX_CAPTURE__DEVICE_ID` unset to use smart camera selection. A
  configured value bypasses it.
- AVFoundation indices can change when Continuity Camera appears. The runtime
  re-enumerates after open and rejects iPhone/iPad devices.
- Built-in cameras can need roughly two seconds of warm-up; capture retries
  failed initial reads.
- Shutdown releases the camera regardless of internal running flags and uses a
  bounded WebSocket → HTTP → native-host → SIGTERM → SIGKILL chain.

## Schema and version governance

Pydantic models in `libs/schemas` are canonical for daemon/client payloads.

```bash
python -m cortex.scripts.generate_ts_schemas
python -m cortex.scripts.generate_ts_schemas --check
```

`pyproject.toml` is the hand-edited version source. Generated/runtime/package
surfaces are synchronized and checked with:

```bash
python -m cortex.scripts.sync_versions
python -m cortex.scripts.sync_versions --check
```

## Verification

```bash
make lint
make typecheck
make test
make codegen-check
make version-check
make audit

cd apps/browser_extension
pnpm exec tsc --noEmit
pnpm test
pnpm exec plasmo build

cd ../vscode_extension
npm ci
npm run compile
npm test
npm run package:vsix
```

CI uses frozen Node dependency inputs, produces dependency-audit artifacts,
checks schema/version drift, and packages both clients. Security exceptions
must identify an exact advisory and dependency path, include mitigation, and
expire; path drift or severity escalation fails the gate.

## Build the macOS application

```bash
./scripts/build_macos_app.sh
```

The output is `dist/Cortex-<version>.dmg`. Ad-hoc builds intentionally avoid
hardened runtime because it conflicts with differently ad-hoc-signed bundled
frameworks. Production distribution requires a real Developer ID identity and
notarization. API keys are never bundled.

## Documentation

- [`../IMPLEMENTATION.md`](../IMPLEMENTATION.md) — full audit, target design,
  evidence basis, work packages, risks, and definition of done
- [`docs/setup.md`](docs/setup.md) — source and app setup
- [`docs/apis.md`](docs/apis.md) — HTTP/WebSocket contracts
- [`docs/architecture.md`](docs/architecture.md) — current and target
  architecture boundary
- [`docs/calibration.md`](docs/calibration.md) — current calibration limits
- [`docs/adapters.md`](docs/adapters.md) — adapter development and authority
  constraints

## License

MIT
