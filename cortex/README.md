<p align="center">
  <img src="assets/banner.svg?v=2" alt="Cortex Banner" width="450" />
</p>

# Cortex

Cortex is a macOS research prototype for local sensing and conservative
workspace support. It combines webcam-derived observations, input/window
telemetry, browser and editor context, and schema-validated planning. Its
shipping authority boundary is deliberately narrow: proposals may be shown,
but the default product does not restructure the workspace.

The current [implementation and verification record](../docs/implementation-2026-10.md)
covers the completed evidence-continuity, runtime and interface refinement.
[Earlier implementation history](../IMPLEMENTATION.md) remains available.
This README describes the current package and product boundary.

Application version remains `0.5.0`; behavioral model `2.5.0` uses
`support-features-v2.2.0`. The October local arm64 build passed mounted-artifact
startup/shutdown and same-profile restart checks. It is an ad-hoc development
artifact, not a new published or notarized release.

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
- Production policy selection is deterministic and does not learn online.
  Retired adaptive-policy logs and former “causal reports” are legacy
  diagnostics only. Separately consented research randomization has its own
  frozen protocol and never receives workspace mutation authority merely by
  selecting an arm.
- No raw camera frames are intentionally persisted. Workspace context selected
  for an LLM request can leave the machine; review provider configuration and
  the privacy documentation before enabling cloud planning.

## Current runtime

```text
observed input/window activity → availability + exposure + aggregation
                                             |
                                             v
                             behavioral scoring + continuous dwell
                                             |
                                             v
                                deterministic eligibility gates
                                             |
current workspace context ────────────────→ local plan
                               or exact-preview external request
                                             |
                                             v
                                  suggestion for user review

optional camera → signal-quality gates → experimental diagnostic display
```

The daemon runs FastAPI on `127.0.0.1:9472` and WebSocket on
`127.0.0.1:9473`. An optional launcher listens on `127.0.0.1:9471`. The
packaged PySide6 desktop shell hosts the daemon in-process and receives queued
UI callbacks. The development shell can also connect as a WebSocket client.
Chrome/Edge and VS Code connect over authenticated local transports. Internal
services use the application kernel and ports; they do not require a network
hop between every layer. Camera observations are excluded from production
behavioral inference and cannot drive reading-reminder eligibility.

### Main components

| Component | Responsibility |
|---|---|
| `services/capture_service` | camera selection, timestamped frames, face/ROI and quality observations |
| `services/physio_engine` | experimental rPPG pulse estimation; unsupported derived physiology is contained |
| `services/kinematics_engine` | blink and head/neck proxy features |
| `services/telemetry_engine` | per-source availability, input exposure/intervals, current foreground window and focus transitions |
| `application` | typed kernel/ports, deadline-bounded cancellation, live task ownership and bounded value-only diagnostic history |
| `services/state_engine` | deterministic evidence-aware scoring, smoothing, eligibility, and compatibility decoders |
| `services/context_engine` | browser/editor/terminal context assembly |
| `services/llm_engine` | default local planner, privacy broker, preview-gated Anthropic transport, validation and deterministic fallback |
| `services/intervention_engine` | manifest validation, exact authorization, receipts, verification, compensation, and restore |
| `services/api_gateway` | authenticated HTTP/WebSocket boundary |
| `apps/browser_extension` | Plasmo MV3 client and proposal UI |
| `apps/vscode_extension` | editor context client and proposal notifications |
| `apps/desktop_shell` | macOS application shell, onboarding, health, and proposal UI |

The schema package under `libs/schemas` is the source of truth for Python and
generated TypeScript boundary models. Native-messaging requests and responses
use a length-prefixed JSON discriminated union and the canonical
`auth_token` field.

Policy evidence is governed by the
[`docs/research/policy-evaluation-protocol.md`](docs/research/policy-evaluation-protocol.md):
action and no-action decisions share one durable follow-up window and reward,
product logs never masquerade as propensity data, and research export/analysis
requires an exact checksummed study epoch.

## Supported and compatibility-only signals

| Surface | Product status |
|---|---|
| camera pulse/BPM | experimental, quality-gated wellness estimate |
| blink rate and PERCLOS | local diagnostics only; not support-inference inputs or externally validated |
| head/neck pose proxy | local diagnostics only; not support-inference inputs or a posture diagnosis |
| input/window telemetry | observed local behavioral evidence; unavailable channels abstain |
| HRV/RMSSD/SDNN/pNN50/LF-HF/entropy | unavailable in product mode |
| respiration or “screen apnea” | unavailable in product mode |
| stress integral / biology-driven break | unavailable and cannot trigger an action |
| state “confidence” | legacy score only; not a calibrated probability |

Compatibility fields and messages may remain decodable for one migration
window. They are emitted as unavailable or ignored, never silently
reinterpreted as evidence.

Input silence is evidence only while the corresponding listener is available.
Typing variance requires at least two positive regular-key intervals;
backspaces do not satisfy that count. Nonincreasing timestamps are ignored by
smoothing; gaps above three seconds reset scores and dwell. The model identity
includes eleven telemetry/schema/fusion/smoothing/scoring components; it does
not hash the entire runtime, configuration or intervention policy. See the
[model card](../docs/model-cards/deterministic-support-v2.md) for precise scope.

## Interface behavior

- Dashboard, History and Advanced separate everyday controls from diagnostics.
  Status rows wrap, compact content scrolls, and session controls stay reachable.
- A pending suggestion offers **Review suggestion** from the dashboard. It
  activates the exact current card only after the gesture, expires within its
  original bounded lifetime, and clears on terminal lifecycle events.
- Popup and Pulse Room share normalized fields and sample age; invalid values,
  disconnects and ten seconds without updates clear current signal display.
  This is a UI continuity bound, not physiological validation.
- Pulse Room motion is decorative, with persisted pause and hidden/reduced-motion
  cleanup. It is neither an ECG nor a heartbeat-paced breathing instruction.
- Closing the dashboard keeps the menu-bar app running. **End session** stops
  sensing and leaves Cortex open; explicit **Quit Cortex** completes cleanup
  and exits. Browser **Stop Cortex** reports the verified helper outcome.

The [UI contract](docs/ui-design.md) records tokens, keyboard/focus behavior,
motion preferences and the application of Emil Kowalski's design principles.

## Workspace proposals and authority

The planner can describe tab organization, error investigation, micro-steps,
or other workspace changes. Descriptions are not commands. In the shipping
default:

- receiving `INTERVENTION_TRIGGER`, `INTERVENTION_PROMPT`,
  `BREAK_RECOMMENDATION`, `PRE_BREAK_WARNING`, or `BREATHING_OVERLAY` cannot
  close/hide/group tabs, fold editors, disable suggestions, or mutate files;
- a consent downgrade is non-executable;
- clients reject apply traffic while in suggestion-only mode;
- the emergency stop path remains independent of the LLM and policy modules.

The repository still contains experimental adapters and legacy action fields
so old data can be decoded. Their presence is not authority: mutation requires
the implemented manifest-bound transaction and explicit release enablement.

## LLM configuration and privacy

Planning supports the Anthropic SDK through AWS Bedrock, Google Vertex AI, or
the direct Anthropic API. `CORTEX_LLM__PROVIDER` selects the transport, while
the default planning mode remains local and makes no model-network call.
Credentials do not grant permission to send workspace context. Deterministic
rule-based planning is also the configured provider-failure fallback.

Confirmed external requests may contain selected and minimized workspace
material, such as file basenames, diagnostic summaries, selected code/page excerpts,
tab titles and the focus goal. The exact redacted preview is authoritative;
raw camera frames and biometric measurements are excluded. Treat workspace
text as potentially sensitive. Provider credentials
belong in environment configuration or macOS Keychain; never commit or bundle
them.

See [`docs/deploy_anthropic.md`](docs/deploy_anthropic.md) for provider setup.
The implemented privacy broker requires per-category selection, exact redacted
preview, a short-lived one-time confirmation, and conservative provider
retention disclosure. Cloud planning should still use only data the user is
authorized to disclose.

## Install and run

Requirements: macOS 13+, uv 0.10.12, Python 3.11.15/3.12.13, Node.js 22.23.2, pnpm 9.15.9,
and a webcam only for experimental camera features.

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex
uv sync --project cortex --locked --extra dev --extra codegen
cp cortex/.env.example .env  # optional overrides; generated lines are commented
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.scripts.run_dev
```

Choose one desktop arrangement; do not run two daemons on the same ports:

```bash
# Attach to the daemon already running above.
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.apps.desktop_shell.main

# Or stop that daemon first, then start a desktop app that owns its daemon.
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.apps.desktop_shell.main --in-process
```

Browser extension:

Run each integration block from the repository root:

```bash
(cd cortex/apps/browser_extension && pnpm install --frozen-lockfile && pnpm exec plasmo build)
```

Load `cortex/apps/browser_extension/build/chrome-mv3-prod/` from
`chrome://extensions`. For Edge, run `make ext-edge` from the repository root
and load `cortex/apps/browser_extension/build/edge-mv3-prod/`.

VS Code extension:

```bash
(cd cortex/apps/vscode_extension && npm ci && npm run compile && npm test && npm run package:vsix)
```

### Native messaging on macOS

Packaged DMG users should open **Cortex → Connect Extensions**, choose Chrome
or Edge, load the copied unpacked extension folder, then fully quit that
browser with Cmd+Q and reopen it. The app registers and protocol-verifies its
signed self-contained native host; no Terminal command or separate Python
installation is required.

For a source checkout:

```bash
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.scripts.install_native_host
```

Fully quit the Chromium browser with Cmd+Q and relaunch it after installing or
updating the host. Development installation copies the tracked portable script
to Application Support and patches its shebang to the active interpreter. The
development host launches the daemon in the foreground through Terminal.app
because processes directly spawned by Chrome inherit Chrome's camera-permission
context.

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

- Leave `CORTEX_CAPTURE__DEVICE_ID` unset for automatic camera preference and
  ordering. An explicit index still requires live identity and post-open
  verification; it cannot bypass Continuity Camera rejection.
- AVFoundation indices can change when Continuity Camera appears. The runtime
  re-enumerates after open and rejects iPhone/iPad devices.
- Built-in cameras can need roughly two seconds of warm-up; capture retries
  failed initial reads.
- Shutdown releases the camera regardless of internal running flags and uses a
  bounded WebSocket → HTTP → native-host → SIGTERM → SIGKILL chain.
- Stop drains an already-issued launch request, coalesces duplicate requests,
  retains the authenticated WebSocket through graceful receipt/restore work,
  and confirms success only after process verification. Native timeout remains
  uncertain rather than starting an overlapping signal fallback. Recovery
  snapshots persist even when stale suggestion presentation is cleared.
- The desktop Quit filter is removed before final programmatic quit; Qt event
  interception must never keep a stopped app alive.

## Schema and version governance

Pydantic models in `libs/schemas` are canonical for daemon/client payloads.
The generator updates declarations in both browser and VS Code integrations.

```bash
uv run --project cortex --locked --extra codegen python -m cortex.scripts.generate_ts_schemas
uv run --project cortex --locked --extra codegen python -m cortex.scripts.generate_ts_schemas --check
```

`pyproject.toml` is the hand-edited version source. Generated/runtime/package
surfaces are synchronized and checked with:

```bash
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.scripts.sync_versions --apply
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.scripts.sync_versions --check
```

## Verification

The canonical Python gate includes coverage, wheel verification and isolated
Qt tests. `make ci` is a useful subset of the hosted workflow; it does not run
the browser/editor suites, packaging or all release checks.

```bash
./cortex/scripts/run_python_gate.sh
make codegen-check
make version-check
make contracts
make audit

cd cortex/apps/browser_extension
pnpm exec tsc --noEmit
pnpm test
pnpm exec plasmo build
pnpm exec plasmo build --target=edge-mv3

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

The 2026-10-02 implementation run passed 3,305 Python tests, 360 browser tests
and 131 VS Code tests, with 69.32% Python coverage. UBFC/PURE replay needs
separately supplied datasets; physical camera/TCC/VoiceOver validation and
notarization remain separate. Full commands and evidence are in the
[current implementation record](../docs/implementation-2026-10.md).

## Build the macOS application

```bash
./cortex/scripts/build_macos_app.sh
```

The output is `dist/Cortex-<version>-macos-<arch>.dmg`. Ad-hoc builds intentionally avoid
hardened runtime because it conflicts with differently ad-hoc-signed bundled
frameworks. Production distribution requires a real Developer ID identity,
notarization, architecture-specific evidence/SBOMs, and the installed-artifact
protocol in [`../docs/release/README.md`](../docs/release/README.md). API keys
are never bundled.

## Documentation

- [Current implementation](../docs/implementation-2026-10.md) — research decisions,
  completed work, exact bounds, verification and remaining external requirements
- [Implementation history](../IMPLEMENTATION.md) — earlier audit and milestone records
- [`docs/setup.md`](docs/setup.md) — source and app setup
- [`docs/apis.md`](docs/apis.md) — HTTP/WebSocket contracts
- [`docs/architecture.md`](docs/architecture.md) — current ownership and architecture boundaries
- [`docs/calibration.md`](docs/calibration.md) — current calibration limits
- [`docs/adapters.md`](docs/adapters.md) — adapter development and authority
  constraints

## License

MIT
