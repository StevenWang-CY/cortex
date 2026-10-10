# Setup

## Install a published release

Use this path for a release that actually includes the matching artifact and
verification records. A source checkout or locally built DMG is not evidence
that a signed release was published. The October review produced a verified
local ad-hoc arm64 build; notarization and Intel release validation remain
separate requirements.

1. Choose the available arm64 or x86_64 DMG matching your Mac from the GitHub release.
2. Verify its matching `SHA256SUMS-<arch>` line and GitHub attestation; optionally run `stapler`
   and `spctl` as shown in the [release guide](https://github.com/StevenWang-CY/cortex/blob/main/docs/release/README.md).
3. Mount the DMG and drag Cortex to `/Applications`.
4. Launch the installed copy and complete onboarding. A properly notarized
   release should not require removing quarantine attributes.
5. Grant only the permissions you want. Denial must degrade visibly; it should
   not produce synthetic sensor values.
6. Install browser/editor integrations from Connections. Fully quit Chrome or
   Edge with Cmd+Q and reopen after native-host installation or update.

The optional model-provider step is BYOK. The default planner is local and
network-off; Cortex remains usable without a provider credential.

## Developer prerequisites

| Tool | Supported input |
| --- | --- |
| macOS | 13 or later |
| Python | 3.11.15 primary; 3.12.13 compatibility builder |
| uv | 0.10.12 |
| Node | 22.23.2 (`.node-version`) |
| pnpm | 9.15.9 |
| Browser | Chrome or Edge for the MV3 client |

Redis is not required for authoritative storage. SQLite under the configured
storage root owns v2 transactions; Redis/in-memory paths are compatibility or
ephemeral caches.

## Source setup

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex
make setup
make precommit
cp cortex/.env.example .env  # optional; every generated setting is commented
```

`make setup` uses `uv sync --locked`, `pnpm install --frozen-lockfile`, and
`npm ci`. Do not replace them with unconstrained installs when diagnosing a
release issue.

Choose one runtime arrangement. For the same in-process daemon ownership used
by the packaged app:

```bash
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.apps.desktop_shell.main --in-process
```

Alternatively, start the daemon with `make dev`, then run its desktop client in
a second terminal:

```bash
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.apps.desktop_shell.main
```

The unflagged desktop command connects over WebSocket; it does not own a second
daemon. Do not run `make dev` alongside the in-process host on the same ports.
Close hides the dashboard while the session continues; explicit Quit performs
shutdown. Ending a focus session leaves the app running.

Browser and native host:

```bash
make ext
make ext-edge
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.scripts.install_native_host
```

Load the corresponding `cortex/apps/browser_extension/build/chrome-mv3-prod`
or `edge-mv3-prod` directory with **Load unpacked** on the browser's extensions
page. After installing/updating the native host, fully quit that browser with
Cmd+Q and reopen it; an extension reload is insufficient.

## Configuration

The generated [configuration reference](https://github.com/StevenWang-CY/cortex/blob/main/cortex/docs/configuration-reference.md)
lists every runtime setting, type, default, and environment name. Edit only the
overrides you need. Important safe defaults are:

```text
CORTEX_INTERVENTION__EXECUTION_MODE=suggest_only
CORTEX_LLM__PRIVACY__PLANNER_MODE=no_llm
CORTEX_LLM__PRIVACY__EXTERNAL_CONTEXT_ENABLED=false
CORTEX_EVAL__POLICY=deterministic
```

Secrets are not `CortexConfig` fields. Prefer macOS Keychain for Bedrock or
direct Anthropic API BYOK; use provider-standard ADC for Vertex. Select the
transport with `CORTEX_LLM__PROVIDER=bedrock`, `vertex`, or `direct`. Never
commit a populated `.env`.

## Permissions and TCC

- Camera: experimental pulse/acquisition; optional for telemetry-only use.
- Input Monitoring/Accessibility: aggregate input/window support and optional
  authorized effects.
- Automation: only for an explicitly authorized OS capability.
- Notifications: optional user-facing proposal fallback.

Processes launched directly by Chrome inherit Chrome's camera permission
context. The development native host therefore uses a foreground Terminal.app
launch. `start_new_session`, `nohup`, or `setsid` do not fix TCC lineage. Never
run a global `tccutil reset Camera`; if a developer reset is unavoidable,
target only `com.cortex.daemon`.

## Verify

```bash
make contracts
make ci
(cd cortex/apps/browser_extension && pnpm exec tsc --noEmit && pnpm test)
(cd cortex/apps/vscode_extension && npm run lint && npm run compile && npm test)
```

The Python gate isolates the Qt desktop test process because loading PySide6
after other native scientific/macOS libraries can crash the interpreter during
collection. See [Troubleshooting](Troubleshooting) for lifecycle issues.

Tests use an isolated HOME before importing Cortex; keep that isolation intact.
They must not overwrite a real profile, calibration or native-host registration.
Build locally with `make dmg`; the release guide governs Developer ID signing,
notarization, provenance and publication separately.
