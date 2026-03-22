# CLAUDE.md — Cortex Project Guidelines

## Project Overview

Cortex is a real-time biofeedback engine (webcam + input devices) that detects cognitive overwhelm and restructures the user's digital workspace. It runs as a Python daemon (FastAPI on port 9472, WebSocket on 9473) with a Chrome/Edge extension (Plasmo/React MV3) and optional VS Code extension.

## Key File Paths

- `cortex/services/capture_service/webcam.py` — Camera selection, capture, Continuity Camera filtering
- `cortex/services/runtime_daemon.py` — Main daemon loop, shutdown orchestration
- `cortex/services/api_gateway/routes.py` — FastAPI routes including `/shutdown`
- `cortex/scripts/native_host.py` — Chrome native messaging host (launches daemon)
- `cortex/scripts/install_native_host.py` — Installs native messaging manifest + patches shebang
- `cortex/scripts/launcher_agent.py` — HTTP launcher server on port 9471
- `cortex/apps/browser_extension/background.ts` — Extension service worker (launch/stop/connect)
- `cortex/apps/browser_extension/popup.tsx` — Extension popup UI
- `.cortex_launcher.c` — C source for CortexDaemon.app (TCC camera wrapper)

---

## Critical Rules (From Past Bugs)

### macOS TCC (Camera Permission)

1. **Processes spawned from Chrome inherit Chrome's TCC context.** Chrome does not have camera permission, so any subprocess it creates — even with `start_new_session=True`, `setsid`, `nohup`, or `osascript 'do shell script'` — will be denied camera access. `start_new_session=True` does NOT break TCC lineage.

2. **The working solution is Terminal.app.** Use `osascript -e 'tell application "Terminal" to do script "..."'` to launch the daemon. Terminal.app has its own TCC camera grant. The daemon MUST run in Terminal's **foreground** (not `nohup & disown; exit`) to keep Terminal's TCC context.

3. **Never run `tccutil reset Camera`.** This clears camera permission for ALL apps on the Mac (Zoom, FaceTime, etc.), not just Cortex. If you need to reset TCC for debugging, target only the specific bundle ID: `tccutil reset Camera com.cortex.daemon`.

4. **CortexDaemon.app needs Desktop folder access.** If the project lives under `~/Desktop`, macOS prompts for `kTCCServiceSystemPolicyDesktopFolder`. The C launcher retries `chdir()` for 15 seconds to give the user time to click Allow.

### Camera Selection (webcam.py)

5. **iPhone Continuity Camera can change device indices at any time.** Camera order is not stable — an iPhone waking/sleeping reshuffles AVFoundation device indices. Never cache device-to-index mappings. Always re-enumerate live cameras during post-open verification.

6. **Post-open re-verification is mandatory.** After `cv2.VideoCapture(idx)` succeeds and returns a frame, re-enumerate cameras with `_list_macos_video_device_names()` and check if the camera at that index is actually a Continuity Camera. If so, `release()` and try the next candidate. Do NOT trust the pre-open enumeration.

7. **MacBook camera needs ~2 seconds warmup.** `cv2.VideoCapture.read()` returns `(False, None)` for the first 1-2 seconds after opening. Always retry in a loop (4 retries x 0.5s) instead of a single attempt.

8. **Check `.env` for hardcoded `CORTEX_CAPTURE__DEVICE_ID`.** If set to `0` (or any value), it bypasses ALL smart camera selection logic. When debugging camera issues, check `.env` first — a hardcoded device ID silently overrides everything.

9. **`system_profiler SPCameraDataType` device order does NOT match AVFoundation.** Never use system_profiler output to determine camera indices for OpenCV/AVFoundation.

### Chrome Native Messaging

10. **Chrome requires a full restart (Cmd+Q) after installing or updating a native messaging host manifest.** Reloading the extension does NOT work. Simply closing tabs does NOT work. Must fully quit and relaunch Chrome. This is the #1 source of "it still doesn't work" when debugging native messaging.

11. **Use absolute paths in the native host shebang.** `#!/usr/bin/env python3` resolves to system Python in Chrome's restricted environment, not the project venv. `install_native_host.py` must patch the shebang to the absolute venv Python path.

12. **The extension uses a fixed manifest key for a deterministic ID.** `install_native_host.py` also auto-detects existing extension IDs from browser profiles and includes them in `allowed_origins`. No manual ID configuration is needed. If you add a new browser or change the key, re-run the installer.

### Daemon Stop Flow

13. **Stopping the daemon requires multiple kill mechanisms.** A single approach is never enough. The proven kill chain:
    1. WebSocket `SHUTDOWN` message (with 300ms flush delay before disconnect)
    2. HTTP `POST /shutdown`
    3. Wait 1 second
    4. Native messaging `stop` command (finds PIDs via `lsof` + `pgrep`)
    5. `SIGTERM` all found PIDs
    6. Wait 3 seconds, then `SIGKILL` any survivors

14. **Find daemon PIDs by BOTH port AND process name.** `lsof -ti tcp:9473` misses orphaned processes that lost their port binding but still hold the camera open. Always also use `pgrep -f "cortex.scripts.run_dev"`.

15. **`webcam.stop()` must ALWAYS call `cap.release()`.** Never early-return from `stop()` based on a `_running` flag without releasing the camera. An inconsistent flag state will leak the camera handle.

16. **`asyncio.get_event_loop()` silently fails in Python 3.10+.** Use `asyncio.get_running_loop()` with a fallback to direct `os.kill(os.getpid(), signal.SIGTERM)`. The old API returns a new (non-running) loop instead of the current one, so scheduled callbacks never execute.

17. **WebSocket messages are lost if you disconnect immediately after sending.** Always add a delay (200-300ms) between `send(SHUTDOWN)` and `disconnect()` to allow the message to flush.

### Plasmo / Browser Extension

18. **Files in `.plasmo/` are auto-generated.** Never edit them — they are overwritten on every build. TypeScript errors in `.plasmo/` files (like `HTMLElement = null` strict mode warnings) are harmless noise from Plasmo's codegen.

19. **Plasmo tab pages have a white `<body>` by default.** To prevent white flash on dark-themed pages, use a three-layer approach: (1) imported CSS file setting `html, body, #__plasmo` background, (2) inline `<style>` tag injected in the component for immediate paint, (3) canvas `fillRect` on resize. `useEffect` alone is too late — it runs after first paint.

20. **Always add required Chrome permissions to `package.json`.** When implementing features that need `nativeMessaging`, `tabs`, `activeTab`, etc., add them to the Plasmo permissions array. Missing permissions fail silently or with cryptic errors.

### General Patterns

21. **Environment config silently overrides code logic.** When debugging "my code isn't running," always check `.env`, config files, and CLI arguments first. A hardcoded value in config can make hours of code changes irrelevant.

22. **Design token values must be cross-referenced against the spec.** When implementing visual changes, verify every value (particle counts, alpha values, timing, spacing, colors) against the design guide. Don't assume reasonable defaults.

23. **Wire up new functions.** After implementing a new function (e.g., LLM camera classifier), verify it's actually called from the relevant code path. Defined-but-never-called functions are dead code that waste effort.

24. **When a bug takes 3+ fix attempts, step back and enumerate ALL possible causes.** Cascading single-fix attempts often miss the real problem. The stop button needed 7 separate fixes. The white rectangle needed 4. List every possible cause upfront, then fix them all.

25. **Test stop/cleanup paths as thoroughly as start paths.** Start flows get tested naturally during development. Stop flows accumulate bugs silently because they're tested less frequently and have more edge cases (orphaned processes, race conditions, partial shutdowns).

26. **Never hardcode personal info in tracked files.** No usernames, server hostnames, absolute paths to user home directories, or institution-specific defaults. Use empty strings or environment variables. The shebang in `native_host.py` is `#!/usr/bin/env python3` in git — `install_native_host.py` patches it to the absolute path at install time.

27. **Don't track generated artifacts.** `.plasmo/`, `build/`, `CortexDaemon.app/`, `*.vsix`, and IDE configs (`.vscode/`) must stay in `.gitignore`. If they get accidentally committed, `git rm --cached` to untrack without deleting.

---

## Build & Run

```bash
# Backend
pip install -e "./cortex[dev]"
python -m cortex.scripts.run_dev          # Start daemon from terminal

# Browser extension
cd cortex/apps/browser_extension
pnpm install && npx plasmo dev            # Dev mode
npx plasmo build                          # Production build

# Install native messaging (one-time, auto-detects all browsers)
python -m cortex.scripts.install_native_host
# Then RESTART your browser completely (Cmd+Q)

# Tests
pytest cortex/
mypy cortex/ --strict
ruff check cortex/
```

## Ports

| Port | Service |
|------|---------|
| 9472 | FastAPI HTTP API |
| 9473 | WebSocket server |
| 9471 | Launcher agent (optional HTTP launcher) |
