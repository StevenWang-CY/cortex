# Setup and development

This page describes the supported, locked setup path. Cortex is an alpha
macOS research prototype, not a medical device. Camera pulse is experimental;
HRV, respiration, stress-integral, and physiology-triggered actions are
unavailable. See [limitations](../../docs/limitations.md) before interpreting
any signal or support label.

## Install a published application

1. For a published release, choose an available artifact matching your Mac:
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
The local October 2026 review produced an ad-hoc Apple Silicon build; that
verification does not establish a published, notarized, or Intel release. See
the [current validation record](../../docs/implementation-2026-10.md#packaged-artifact-and-completion).

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
Install the pinned toolchain before running `make setup`. The locked Python
environment lives at **`cortex/.venv`**, not a repository-root `.venv`. Use the
`uv run --project cortex --locked` commands below to avoid stale dependencies
or installed package metadata from another interpreter.

The daemon binds HTTP to `127.0.0.1:9472` and WebSocket to
`127.0.0.1:9473`. Choose one of these desktop arrangements:

```bash
# Attach the desktop client to a daemon already running via make dev.
uv run --project cortex --locked python -m cortex.apps.desktop_shell.main

# Or start one desktop app that owns its daemon; stop make dev first.
uv run --project cortex --locked python -m cortex.apps.desktop_shell.main --in-process
```

The packaged application uses the in-process arrangement. Do not launch a
second daemon on the same ports. The optional launcher service uses port
9471; it is separate from the daemon's HTTP and WebSocket listeners.

### Desktop session controls

- Closing the dashboard with the red close control or Cmd+W hides it and
  leaves the session running. Reopen it from Cortex's menu-bar item.
- **End session** stops sensing and allows the recap to be read while Cortex
  remains open. The in-process app then offers **Start session**; the
  WebSocket-only client cannot start its external daemon itself.
- **Quit Cortex**, the menu-bar Quit item, and Cmd+Q finish the quit/recap
  flow and exit the desktop application. The final quit must release the Qt
  event loop after daemon cleanup.
- An unsolicited suggestion leaves keyboard focus where it is. Choose
  **Review suggestion** in the dashboard to focus its card, then Tab through
  its controls or press Escape to dismiss. Reviewing does not authorize a
  workspace change. Pending suggestions disappear on expiry, dismissal,
  action, quiet mode, end of session, or disconnect.

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

Leave `CORTEX_CAPTURE__DEVICE_ID` unset for automatic camera preference and
candidate ordering. A configured index overrides that preference, but still
requires a verified live AVFoundation identity and post-open verification;
Continuity, disconnected or unverified cameras are rejected in either mode.
Use an index only for a deliberate local override.

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
native-host installation. In source mode the installer discovers supported
profiles and patches the copied host's shebang to the absolute locked
interpreter; the tracked source shebang remains portable. Packaged users should
instead use Cortex's **Connect Extensions** panel, which registers and probes
the signed self-contained helper without requiring Python or Terminal.

Build the optional VS Code extension with:

```bash
npm --prefix cortex/apps/vscode_extension ci
npm --prefix cortex/apps/vscode_extension run compile
npm --prefix cortex/apps/vscode_extension test
npm --prefix cortex/apps/vscode_extension run package:vsix
```

## Verification

Run the full Python gate plus the applicable client and repository checks:

```bash
./cortex/scripts/run_python_gate.sh
make contracts
pnpm --dir cortex/apps/browser_extension exec tsc --noEmit
pnpm --dir cortex/apps/browser_extension test
pnpm --dir cortex/apps/browser_extension exec plasmo build
pnpm --dir cortex/apps/browser_extension exec plasmo build --target=edge-mv3
npm --prefix cortex/apps/vscode_extension run compile
npm --prefix cortex/apps/vscode_extension test
npm --prefix cortex/apps/vscode_extension run lint
npm --prefix cortex/apps/vscode_extension run package:vsix
```

`make ci` is a convenient local Python/contract subset. It does not run the
canonical coverage/wheel checks, browser/editor gates or full release pipeline.

The real Qt tests run offscreen in the canonical suite. Only the legacy
`test_desktop_shell.py` harness, which installs process-global Qt stubs, runs
in a subprocess through `test_desktop_shell_isolation.py`; `make ci` already
includes that wrapper. Tests isolate HOME and settings before importing
Cortex. Do not disable that isolation for routine verification or reuse a
personal application profile. Dataset replay requires a manifest with licensing,
provenance, condition, checksum, and reference-timing metadata; participant
data is never committed. See
[`tests/physio/DATASETS.md`](../tests/physio/DATASETS.md).

## Build and release

```bash
make dmg
```

The local outputs are `dist/Cortex.app` and
`dist/Cortex-<version>-macos-<arch>.dmg`; verification and checksums go under
`dist/evidence-<arch>/` unless `CORTEX_RELEASE_EVIDENCE_DIR` selects another
ignored directory. The build uses `cortex/.venv/bin/python`, checks the Node
pin, bundles Chrome/Edge/VS Code clients, and runs the mounted-artifact probes.
An ad-hoc local build is not a distributable release. The tag workflow is
configured to build separate arm64
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
- **Camera remains occupied after stop:** let the bounded shutdown complete,
  then use the product stop/recovery path. Cleanup identifies the daemon by
  its pidfile, listening socket, or anchored executable identity; a browser or
  editor merely connected to the port is not a daemon. Treat a surviving camera
  handle as a cleanup defect, not a normal state.
- **Window disappeared but Cortex is still running:** ordinary window close
  hides the dashboard. Use **Quit Cortex** when you intend to exit.
- **Blank pulse after reconnect:** the dashboard waits for a new valid state
  update. Disconnected, stale, malformed, or non-flowing camera data must not
  appear as a live measurement; opening the dashboard does not revive an old
  reading.
- **External planner unavailable:** confirm the selected provider credential,
  privacy mode, disclosure revision, per-source selection, and one-time
  confirmation. Local `no_llm` planning remains available without a key.
