# Setup and development

This page describes the supported, locked setup path. Cortex is an alpha
macOS research prototype, not a medical device. Camera pulse is experimental;
HRV, respiration, stress-integral, and physiology-triggered actions are
unavailable. See [limitations](../../docs/limitations.md) before interpreting
any signal or support label.

## Install a published application

1. In the GitHub release, choose
   `Cortex-<version>-macos-arm64.dmg` for Apple Silicon or
   `Cortex-<version>-macos-x86_64.dmg` for Intel.
2. Download the matching `SHA256SUMS-<arch>` and evidence ZIP. Verify the
   checksum and GitHub attestation using the
   [release guide](../../docs/release/README.md).
3. Mount the DMG, drag `Cortex.app` to `/Applications`, and launch that copy.
4. Complete onboarding and grant only the permissions needed for the features
   you choose. A correctly signed and notarized release does not require
   removing quarantine attributes.
5. After installing or updating a Chromium native host, fully quit the browser
   with Cmd+Q and reopen it. Reloading an extension is insufficient.

The default planner is local and network-off. Provider credentials are
optional BYOK credentials and must never be bundled into the application.

## Locked developer prerequisites

| Input | Supported value |
| --- | --- |
| macOS | 13 or later |
| Python | 3.11.15 primary; 3.12.13 compatibility builder |
| uv | 0.10.12 |
| Node.js | 22.23.2 (`.node-version`) |
| pnpm | 9.15.9 |

A webcam is needed only for experimental camera features. Redis is not needed
for the authoritative v2 storage path; SQLite owns durable transaction and
policy lifecycles.

## Bootstrap from a clean checkout

Run these commands from the repository root:

```bash
git clone https://github.com/StevenWang-CY/cortex.git
cd cortex
make setup
make precommit
cp cortex/.env.example .env  # optional, commented safe template
make dev
```

`make setup` consumes `cortex/uv.lock`, the browser `pnpm-lock.yaml`, and the
VS Code `package-lock.json`. It uses `uv sync --locked`,
`pnpm install --frozen-lockfile`, and `npm ci`; do not replace those with an
unconstrained install when reproducing a defect or release.

The daemon binds HTTP to `127.0.0.1:9472` and WebSocket to
`127.0.0.1:9473`. Start either desktop mode with:

```bash
uv run --project cortex --locked python -m cortex.apps.desktop_shell.main
uv run --project cortex --locked python -m cortex.apps.desktop_shell.main --in-process
```

## Configuration and credentials

The generated [configuration reference](configuration-reference.md) is the
complete catalog of settings, types, defaults, and environment names.
Regenerate it and the safe template after changing `CortexConfig`:

```bash
make config-sync
make contracts
```

Important safe defaults are:

```text
CORTEX_INTERVENTION__EXECUTION_MODE=suggest_only
CORTEX_LLM__PRIVACY__PLANNER_MODE=no_llm
CORTEX_LLM__PRIVACY__EXTERNAL_CONTEXT_ENABLED=false
CORTEX_EVAL__POLICY=deterministic
CORTEX_INTERVENTION__ENABLE_FOCUS_BREAK_REMINDERS=false
```

Leave `CORTEX_CAPTURE__DEVICE_ID` unset so the live AVFoundation selection and
post-open Continuity Camera verification run. A configured device index
bypasses that selection logic and should be used only for a deliberate local
override.

Secrets are operational inputs, not `CortexConfig` values. Prefer macOS
Keychain for Bedrock BYOK, provider-standard Application Default Credentials
for Vertex, or a process-scoped direct-provider key. Never put a real key in a
tracked file or release bundle. External planning additionally requires the
privacy broker's explicit mode, category selection, exact redacted preview,
one-time confirmation, and provider-retention disclosure. See
[privacy](privacy.md).

## macOS permissions and launch lineage

- **Camera:** optional experimental acquisition and pulse estimate.
- **Input Monitoring / Accessibility:** aggregate input/window context and
  explicitly authorized OS capabilities.
- **Automation:** only for a capability the user authorizes.
- **Notifications:** optional proposal fallback.

A process spawned directly by Chrome inherits Chrome's TCC context. The
development native host therefore starts the daemon in the foreground through
Terminal.app. `start_new_session`, `setsid`, `nohup`, and backgrounding do not
break that lineage. Grant permission to the application that actually owns the
process. Never run a global `tccutil reset Camera`; if a developer reset is
unavoidable, target only `com.cortex.daemon`.

Camera indices may change when an iPhone Continuity Camera wakes or sleeps.
Cortex re-enumerates after a successful open, rejects Continuity devices, and
retries warm-up reads for the built-in camera. Restart the daemon to perform a
fresh selection after the device set changes.

## Browser and editor clients

Build the browser clients from the repository root:

```bash
make ext
make ext-edge
uv run --project cortex --locked python -m cortex.scripts.install_native_host
```

Load `cortex/apps/browser_extension/build/chrome-mv3-prod/` or the Edge build
from the browser's extension page. Fully quit and relaunch the browser after
native-host installation. The installer discovers supported profiles and
patches the installed host shebang to the absolute locked interpreter; the
tracked source shebang remains portable.

Build the optional VS Code extension with:

```bash
npm --prefix cortex/apps/vscode_extension ci
npm --prefix cortex/apps/vscode_extension run compile
npm --prefix cortex/apps/vscode_extension test
npm --prefix cortex/apps/vscode_extension run package:vsix
```

## Verification

The canonical local gates are:

```bash
make contracts
make ci
pnpm --dir cortex/apps/browser_extension exec tsc --noEmit
pnpm --dir cortex/apps/browser_extension test
pnpm --dir cortex/apps/browser_extension exec plasmo build
pnpm --dir cortex/apps/browser_extension exec plasmo build --target=edge-mv3
npm --prefix cortex/apps/vscode_extension run compile
npm --prefix cortex/apps/vscode_extension test
```

The desktop-shell suite is intentionally run in a separate process from the
rest of pytest because Qt and native scientific/macOS libraries can interfere
during shared collection. Dataset replay requires a manifest with licensing,
provenance, condition, checksum, and reference-timing metadata; participant
data is never committed. See
[`tests/physio/DATASETS.md`](../tests/physio/DATASETS.md).

## Build and release

```bash
make dmg
```

The local output is `dist/Cortex-<version>-macos-<arch>.dmg`. An ad-hoc local
build is not a distributable release. The tag workflow builds separate arm64
and x86_64 artifacts, requires Developer ID signing and Apple notarization,
mount-verifies the resulting DMGs, scans the frozen bundle, produces SBOMs and
architecture-specific evidence, and creates GitHub attestations. A public
candidate still requires the real-device protocol in the
[release guide](../../docs/release/README.md).

## Focused troubleshooting

- **Native host not found:** rerun the installer, then Cmd+Q and reopen the
  browser.
- **Camera permission denied:** grant permission to `/Applications/Cortex.app`
  for a release or to Terminal.app/the launching terminal for development.
- **Wrong camera:** first remove any hardcoded
  `CORTEX_CAPTURE__DEVICE_ID`, restart, and let live verification run.
- **Camera remains occupied after stop:** use the product Stop flow again; it
  checks both port owners and daemon process names. Treat a surviving camera
  handle as a cleanup defect, not a normal state.
- **External planner unavailable:** confirm the selected provider credential,
  privacy mode, disclosure revision, per-source selection, and one-time
  confirmation. Local `no_llm` planning remains available without a key.
