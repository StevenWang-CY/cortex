# Troubleshooting

Cortex ships as a macOS app (`Cortex.app` from the release DMG) whose daemon
runs in-process. Most people never need a terminal. The first half of this
page is for the installed app; the second half is for developers running the
source checkout. When a symptom looks the same in both, the installed-app
guidance comes first.

---

## Installed app (DMG)

### The app bounces in the Dock and disappears

Do not remove quarantine attributes or bypass Gatekeeper. First confirm the
app came from the current GitHub release, was dragged to `/Applications`, and
the DMG checksum matches its attached `SHA256SUMS-<arch>` file. Then run:

```bash
spctl -a -vv --type execute /Applications/Cortex.app
codesign --verify --deep --strict --verbose=2 /Applications/Cortex.app
tail -n 200 "$HOME/Library/Logs/Cortex/startup.log"
cat "$HOME/Library/Logs/Cortex/last-startup-error.txt" 2>/dev/null
```

An official signed and notarized build passes Gatekeeper without `xattr -cr`.
If signature verification or Gatekeeper fails, delete that app, redownload the
release asset, verify its checksum and GitHub attestation, and report the exact
output. If those checks pass, attach the two startup logs and the newest
`Cortex` crash report from `~/Library/Logs/DiagnosticReports/` to a bug report.
One failed launch plus its logs is enough; repeated relaunches only re-acquire
the camera.

### No window appears, or the window ignores the keyboard

Cortex is a normal foreground app: it has a Dock icon, appears in Cmd-Tab,
and its windows accept keyboard focus. If you see the menu-bar icon but no
window, click the icon and choose **Dashboard**. If a window is visible but
typing does nothing, you are running a pre-0.4.0 build that was packaged as a
background-only process; update to the current release.

### Camera permission

Grant camera access to **Cortex** in `System Settings → Privacy & Security →
Camera`. If Cortex is not in the list, open the app once so macOS registers
the request. Never run `tccutil reset Camera` without a bundle id: that clears
camera access for every app on the Mac. To reset only Cortex:

```bash
tccutil reset Camera com.cortex.daemon
```

### iPhone camera activates unexpectedly

Cortex excludes Continuity Camera using AVFoundation's explicit device
property both before opening a camera and again after warm-up, and it never
caches device indices because an iPhone waking or sleeping reshuffles them.
If the iPhone still lights up:

- Stop the session from the menu bar or dashboard.
- Remove any `CORTEX_CAPTURE__DEVICE_ID` override and restart only when
  ready. A configured index is refused if it resolves to Continuity Camera or
  cannot be verified.
- Save `~/Library/Logs/Cortex/startup.log` and file a bug with the Cortex
  version and the privacy-safe descriptor output from the developer section
  below. Do not include localized device names, credentials, or private page
  content.

### Camera light stays on after stopping

Choose **Quit Cortex** from the menu-bar icon. The app releases the camera as
part of a bounded shutdown (recap, receipts, database close). If the app does
not respond within about half a minute, force-quit **Cortex** from Activity
Monitor. Do not kill by port number: several processes hold sockets on the
Cortex ports (the browser's network service, the editor extension host), and
port-based kills terminate them too.

### "Reading your pulse…" never resolves

Webcam pulse is experimental and does not affect support estimates. The
dashboard states why a pulse is unavailable rather than showing a permanent
warm-up: the window is still filling, coverage is below the gate, the light
is too low, there is too much motion, or the face was lost. Sit facing the
camera at arm's length with even light and no strong backlight. A steady
low-frame-rate camera (for example in low light) is supported; the estimate
just takes longer to become available.

### Status stays "Still gathering" or "Not enough evidence"

Support estimates come from input and window telemetry (mouse, keyboard,
focus transitions), not from the camera. They require:

- **Input Monitoring / Accessibility** permission for Cortex, otherwise
  mouse and keyboard aggregates are unavailable and the estimate stays
  `unknown`. Grant both in `System Settings → Privacy & Security`.
- **Enough evidence coverage**: at least three input channels with fresh
  data. A quiet stretch of reading or watching legitimately yields
  "Not enough evidence".

There is no camera-only mode and the camera cannot raise or lower a support
score. Calibration only sets your personal mouse-variance baseline.

### Too many or too few suggestions

- Adjust the **suggestion threshold** in Settings. Cortex 0.4.0 honours the
  configured value; earlier builds silently clamped anything below 0.75.
- Use **Snooze**, **Quiet for this session**, or **Pause** from the menu bar
  or dashboard. Weekly schedule "quiet" slots are honoured.
- Dismissing several suggestions in a row pauses suggestions for a bounded
  period that grows with repeated dismissals and resumes on its own. Nothing
  is locked permanently and no reset is needed.

### Cannot connect the browser extension

1. In the Cortex app, open **Connections** and click **Connect Chrome** or
   **Connect Edge**. This installs the native-messaging manifest for the
   app's own native host (no terminal, no Python) and shows a checklist with
   the folder to load.
2. In the browser, open `chrome://extensions` (or `edge://extensions`),
   enable Developer mode, choose **Load unpacked**, and pick the folder the
   checklist names.
3. **Fully quit the browser (Cmd+Q) and reopen it.** Native-messaging
   manifests are read only at browser startup; reloading the extension is not
   enough. This is the most common cause of "it still doesn't work".
4. Click **Verify connection** in the Connections window.

If the app was installed somewhere other than `/Applications`, move it there
and repeat step 1. The native host is registered at the canonical path so
that a translocated first launch cannot leave a broken manifest behind.

### The extension says Cortex isn't running

Open the Cortex app (or click **Open Cortex** in the popup). The extension
reconnects on its own once the app is up. If the popup says the app needs an
update, install the current release; a version skew between the app and the
extension is shown as a dismissible banner, not a dead end.

### Stop from the extension

**Stop Cortex** in the popup asks for confirmation, because it shuts down the
camera and the app. It then asks the app to stop gracefully with the local
capability token, waits for the app to finish its bounded shutdown, and only
signals the Cortex process itself if the app has not exited. Browser and
editor processes are never signalled.

### History is empty

Session reports are kept for 180 days by default
(`CORTEX_STORAGE__SESSION_RETENTION_DAYS`) inside a bounded storage budget.
Builds before 0.4.0 deleted them after seven days.

### Claude access (optional)

Cortex makes no model network request by default. External planning requires
all of the following, which the onboarding wizard and Settings walk through:

1. `CORTEX_LLM__PRIVACY__PLANNER_MODE=external_redacted` and
   `CORTEX_LLM__PRIVACY__EXTERNAL_CONTEXT_ENABLED=true`;
2. a credential for the chosen provider (Bedrock bearer token in Keychain,
   Vertex application-default credentials, or `ANTHROPIC_API_KEY`);
3. for each request, an exact redacted preview that you confirm once.

If the review sheet says no credential is saved, add the token under Claude
access in Settings; no restart is needed. "Test connection" reports the
provider's real error in its status pill. When the provider is unavailable,
Cortex falls back to its offline rule-based suggestions and labels them as
such.

### Logs

The app writes to `~/Library/Logs/Cortex/` (`startup.log`,
`last-startup-error.txt`, and the daemon log). Logs contain identifiers,
health, counts, and reasons, never frames, prompts, page bodies, source code,
key text, or credentials.

---

## Source checkout (developers)

Follow the locked toolchain in [Setup](Setup) (`uv sync --locked`, pinned Node
and pnpm). `pip install -e` outside the lock is not a supported path.

### "ModuleNotFoundError" on startup

The locked environment is not active:

```bash
uv sync --project cortex --locked --extra dev --extra codegen
uv run --project cortex --locked --extra dev python -m cortex.scripts.run_dev
```

### Daemon starts then exits

Check the log output for the specific error. Common causes:

- Missing `.env`: `cp cortex/.env.example .env`
- Missing storage directory: `python -m cortex.scripts.seed_config --root .`
- A hard-coded `CORTEX_CAPTURE__DEVICE_ID` in `.env`; leave it unset.

### Camera permission for a source run

Camera permission belongs to the process you launched from: grant it to
Terminal, iTerm, or your editor. When the browser extension launches a source
daemon, it does so through Terminal.app, because processes spawned by the
browser inherit the browser's (absent) camera grant, so grant Terminal.app
camera access for that path.

### Enumerate cameras without opening any

```bash
uv run --project cortex --locked --extra dev python - <<'PY'
from cortex.services.capture_service.webcam import _list_macos_video_devices

for device in _list_macos_video_devices():
    print({
        "index": device.index,
        "built_in": device.is_builtin,
        "continuity": device.is_continuity,
        "connected": device.is_connected,
        "type": device.device_type,
    })
PY
```

Do not sweep numeric OpenCV indices: probing an index opens the device and may
wake Continuity Camera. `system_profiler` order does not match AVFoundation.

### Camera light stays on after a source daemon exits

```bash
pkill -f "cortex.scripts.run_dev"
```

If a listener is still bound, find only the listening process (never both
socket ends):

```bash
lsof -ti tcp:9473 -sTCP:LISTEN | xargs kill
```

### Native host for a source checkout

```bash
uv run --project cortex --locked --extra dev python -m cortex.scripts.install_native_host
```

The installer patches the host's shebang to the absolute virtual-environment
interpreter (a bare `python3` resolves to the system interpreter inside the
browser's restricted environment) and auto-detects extension ids. Fully quit
and reopen the browser afterwards.

### MediaPipe or OpenCV import errors on Apple silicon

Verify you are running native ARM Python, not Rosetta:

```bash
python3 -c "import platform; print(platform.machine())"   # arm64
```

Then re-run `uv sync --locked`; MediaPipe's capped OpenCV dependency is the
sole `cv2` provider in the lock, so mixing a separately installed OpenCV
breaks imports.

### pynput / Accessibility errors

Add your terminal app to `System Settings → Privacy & Security → Input
Monitoring`. Without it the daemon starts, but input telemetry is unavailable
and the support estimate stays `unknown`; there is no camera-only fallback.

### Verbose logs

```bash
CORTEX_LOGGING__LEVEL=DEBUG uv run --project cortex --locked --extra dev python -m cortex.scripts.run_dev 2>&1 | tee ~/cortex.log
```

---

## VS Code extension

- The status bar shows a Cortex item; **Disconnected** with a warning
  background means the daemon is not reachable at `ws://127.0.0.1:9473`.
- If the auth token file is missing, the extension shows one warning naming
  the token path and an **Open Cortex** action; start the app and it
  reconnects.
- `cortex.daemonUrl` is a machine-scoped setting and only loopback hosts are
  accepted; a workspace cannot redirect the extension.
- If the panel is empty after "Why this?", the daemon did not answer within
  five seconds; the panel says so and offers **Retry**.
- Reload the window (`Cmd+Shift+P → Reload Window`) after installing a new
  VSIX.
