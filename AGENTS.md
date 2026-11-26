# AGENTS.md — Cortex Project Guidelines

## Project Overview

Cortex is a macOS research prototype for local behavioral support and optional experimental camera estimates. Production support inference is driven by observed input/window behavior; camera physiology (heart rate and SDNN, qualified against ECG; breathing rate, blink rate and eye-closure share, admitted without a reference check) can only corroborate an eligible behavioral reading, with bounded weight (ADR 0008). The default planner is local and the default execution mode is `suggest_only`; proposals cannot restructure the workspace without a separate exact authorization transaction. It ships as a **macOS .app** (DMG installer) that runs the daemon in-process via PySide6, with a Chrome/Edge extension (Plasmo/React MV3) and optional VS Code extension. The daemon exposes FastAPI on port 9472 and WebSocket on port 9473.

## Key File Paths

- `cortex/services/capture_service/webcam.py` — Camera selection, capture, Continuity Camera filtering
- `cortex/services/runtime_daemon.py` — Main daemon loop, shutdown orchestration, UI callback hooks
- `cortex/services/api_gateway/routes.py` — FastAPI routes including `/shutdown`
- `cortex/scripts/native_host.py` — Chrome native messaging host (launches daemon)
- `cortex/scripts/install_native_host.py` — Installs native messaging manifest + patches shebang
- `cortex/scripts/launcher_agent.py` — HTTP launcher server on port 9471
- `cortex/scripts/cortex.spec` — PyInstaller spec for macOS .app bundle
- `cortex/scripts/build_macos_app.sh` — Full build pipeline (extensions → sign → DMG)
- `cortex/scripts/cortex_entitlements.plist` — Camera + automation entitlements for .app
- `cortex/apps/desktop_shell/controller.py` — In-process daemon controller (Qt + asyncio bridge)
- `cortex/apps/desktop_shell/dashboard.py` — Dashboard, History and Advanced tabs; compact layouts and explicit suggestion review
- `cortex/apps/desktop_shell/connections.py` — One-click browser/editor extension setup
- `cortex/apps/desktop_shell/tokens.py` — Design tokens (warm palette, typography, spacing)
- `cortex/apps/desktop_shell/onboarding.py` — 6-step first-run wizard (camera, accessibility, BYOK, calibration, extensions, macOS notifications)
- `cortex/apps/browser_extension/background.ts` — Extension service worker (launch/stop/connect)
- `cortex/apps/browser_extension/popup.tsx` — Extension popup UI
- `.cortex_launcher.c` — C source for CortexDaemon.app (TCC camera wrapper, legacy)

---

## Critical Rules (From Past Bugs)

### macOS TCC (Camera Permission)

1. **Processes spawned from Chrome inherit Chrome's TCC context.** Chrome does not have camera permission, so any subprocess it creates — even with `start_new_session=True`, `setsid`, `nohup`, or `osascript 'do shell script'` — will be denied camera access. `start_new_session=True` does NOT break TCC lineage.

2. **The source-development camera launch path uses Terminal.app.** Packaged Cortex uses LaunchServices to open `/Applications/Cortex.app` and owns its own camera context. For the development host, use `osascript -e 'tell application "Terminal" to do script "..."'` to launch the daemon. Terminal.app has its own TCC camera grant. The daemon MUST run in Terminal's **foreground** (not `nohup & disown; exit`) to keep Terminal's TCC context.

3. **Never run `tccutil reset Camera`.** This clears camera permission for ALL apps on the Mac (Zoom, FaceTime, etc.), not just Cortex. If you need to reset TCC for debugging, target only the specific bundle ID: `tccutil reset Camera com.cortex.daemon`.

4. **CortexDaemon.app needs Desktop folder access.** If the project lives under `~/Desktop`, macOS prompts for `kTCCServiceSystemPolicyDesktopFolder`. The C launcher retries `chdir()` for 15 seconds to give the user time to click Allow.

### Camera Selection (webcam.py)

5. **iPhone Continuity Camera can change device indices at any time.** Camera order is not stable — an iPhone waking/sleeping reshuffles AVFoundation device indices. Never cache device-to-index mappings. Always re-enumerate live cameras during post-open verification.

6. **Post-open re-verification is mandatory.** After `cv2.VideoCapture(idx)` succeeds and returns a frame, re-enumerate cameras with `_list_macos_video_device_names()` and check if the camera at that index is actually a Continuity Camera. If so, `release()` and try the next candidate. Do NOT trust the pre-open enumeration.

7. **MacBook camera needs ~2 seconds warmup.** `cv2.VideoCapture.read()` returns `(False, None)` for the first 1-2 seconds after opening. Always retry in a loop (4 retries x 0.5s) instead of a single attempt.

8. **Check `.env` for hardcoded `CORTEX_CAPTURE__DEVICE_ID`.** An explicit index overrides automatic camera preference/order. It does not bypass live identity, Continuity Camera rejection or post-open verification. When debugging camera issues, check `.env` first; a stale configured index can prevent selection of an otherwise available built-in camera.

9. **`system_profiler SPCameraDataType` device order does NOT match AVFoundation.** Never use system_profiler output to determine camera indices for OpenCV/AVFoundation.

### Chrome Native Messaging

10. **Chrome requires a full restart (Cmd+Q) after installing or updating a native messaging host manifest.** Reloading the extension does NOT work. Simply closing tabs does NOT work. Must fully quit and relaunch Chrome. This is the #1 source of "it still doesn't work" when debugging native messaging.

11. **Use absolute paths in the native host shebang.** `#!/usr/bin/env python3` resolves to system Python in Chrome's restricted environment, not the project venv. `install_native_host.py` must patch the shebang to the absolute venv Python path.

12. **The extension uses a fixed manifest key for a deterministic ID.** `install_native_host.py` also auto-detects existing extension IDs from browser profiles and includes them in `allowed_origins`. No manual ID configuration is needed. If you add a new browser or change the key, re-run the installer.

### Daemon Stop Flow

13. **Stopping the daemon is a bounded chain that must never signal a client.** The proven order:
    1. WebSocket `SHUTDOWN` message (300ms flush delay; retain the authenticated socket during graceful receipt/restore work)
    2. HTTP `POST /shutdown` **with the capability token** (`X-Cortex-Auth-Token` read from `<config_dir>/auth.token`; without it the route answers 401 and the graceful step is dead code)
    3. Wait for the daemon's own graceful shutdown (recap, receipts, database close): allow a 20-second grace window while polling `/health`; an unavailable endpoint ends polling but does not prove process exit
    4. Native messaging `stop` command, which identifies the daemon by its pidfile or its **listening** socket (rule 14)
    5. `SIGTERM` that process only
    6. Wait, then `SIGKILL` any survivor

14. **Identify the daemon by pidfile, listening socket, or an anchored process pattern, never by "anything on the port".** `lsof -ti tcp:9473` lists **both ends** of every socket (empirically verified): it returns Chrome's network service, the VS Code extension host, and the WebSocket desktop shell, and the v0.3.15 kill chain SIGKILLed them. Use `lsof -ti tcp:9473 -sTCP:LISTEN`, prefer the daemon-written `<config_dir>/daemon.pid`, anchor `pgrep` to the GUI executable (`^/Applications/Cortex.app/Contents/MacOS/Cortex( |$)`) so it can never match `CortexNativeHost`, and exclude the caller's own PID and parent PID. A daemon that lost its port but holds the camera is found through the pidfile or the anchored pattern, not through a port sweep.

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

27. **Don't track generated artifacts.** `.plasmo/`, `build/`, `CortexDaemon.app/`, `dist/`, `*.vsix`, and IDE configs (`.vscode/`) must stay in `.gitignore`. If they get accidentally committed, `git rm --cached` to untrack without deleting.

### macOS .app Bundling (PyInstaller)

28. **Ad-hoc signing MUST NOT use `--options runtime` (hardened runtime).** Hardened runtime enforces library validation, which rejects ad-hoc-signed libraries with different Team IDs — this breaks `Python.framework` loading. Only use `--options runtime` with a real Developer ID certificate. For ad-hoc builds, sign all nested `.dylib`, `.so`, and framework binaries individually with `codesign --force --sign -` before signing the outer bundle.

29. **MediaPipe requires `collect_data_files('mediapipe')` in the PyInstaller spec.** MediaPipe ships hidden binary graphs, calculators, and OpenCV `.dylib`s that PyInstaller's default analyzer cannot see. Without this, the bundled app crashes on launch with `FileNotFoundError` for MediaPipe internals.

30. **The `keyring` library needs `copy_metadata('keyring')` + explicit `keyring.backends.macOS` hidden import.** `keyring` uses `importlib.metadata` entry_points to discover OS backends at runtime. PyInstaller strips metadata, so without these additions, `keyring.get_password()` raises `NoKeyringError` in the frozen app.

31. **Never bundle API keys in the .app.** PyInstaller bundles are trivially extractable (`pyinstxtractor`) and `.pyc` bytecode is trivially decompilable (`uncompyle6`). Use BYOK (Bring Your Own Key) with macOS Keychain storage during onboarding instead.

32. **App Translocation breaks native messaging paths.** Downloaded ad-hoc-signed apps are secretly copied to `/private/var/folders/.../AppTranslocation/...` on first launch. Always use `canonical_app_path()` (hardcoded `/Applications/Cortex.app`) for native messaging manifest paths, never `sys.executable`. Proper code signing + notarization eliminates translocation entirely.

33. **GUI apps don't inherit shell `$PATH`.** When launched from Finder/Dock, the app gets only `/usr/bin:/bin:/usr/sbin:/sbin`. Use absolute paths to find `code`, `cursor`, `codium` binaries (e.g., `/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code`). `shutil.which()` is a last-resort fallback only.

34. **PyInstaller `COLLECT` inherits `console` from the LAST `EXE`, and the macOS bundler turns `console=True` into `LSBackgroundOnly=1`.** The two-executable bundle (GUI + `CortexNativeHost`) shipped v0.3.15 as a background-only app: no Dock icon, no Cmd-Tab, and no window could become key, so the keyboard was dead. Always set `"LSBackgroundOnly": False` in the `BUNDLE` `info_plist`, and keep the release verifier's plist check that rejects any truthy `LSBackgroundOnly`/`LSUIElement`.

35. **Qt parses 8-digit hex as `#AARRGGBB`; CSS uses `#RRGGBBAA`.** Design tokens are authored in CSS order in `tokens.yaml`; the emitter (`sync_design_tokens.py`) must reorder the channels for the Python/Qt output. Emitting CSS order into Qt made every hairline a 24 % olive line and the UNKNOWN/HYPO state dots nearly invisible. The contrast contract test pins the emitted order.

36. **Tests must never touch the real user profile.** `cortex/tests/conftest.py` installs a session `HOME` sandbox (with `XDG_*` variables) before any Cortex import; before it existed, the suite overwrote the real goals file, cost ledger, onboarding marker, and native-host log under `~/Library/Application Support/Cortex`. Do not add fixtures that bypass it; opt out only with `CORTEX_TEST_REAL_HOME=1` for a deliberate local experiment.

37. **Current Claude models reject sampling parameters and forced tool choice.** `temperature`/`top_p`/`top_k` return HTTP 400 on Opus 4.7+ and Sonnet 5, and forced `tool_choice` is refused on the newest models. The planner uses structured outputs (`output_config.format`) with no sampling parameters, treats 400/404/422 and `stop_reason` `refusal`/`max_tokens` as terminal, and keeps one circuit breaker per model tier so a broken tier cannot silence the others.

### Evidence, presentation and task lifetime

38. **Unavailable input is not observed inactivity.** Preserve per-device listener availability and restart exposure after gaps; calibration must reject unavailable mouse samples. Typing variance requires two positive regular-key intervals, independently of backspace count.

39. **Missing time cannot earn inference dwell.** Ignore nonincreasing timestamps; gaps above the documented 3-second continuity bound reset scores and dwell. Smoothing weights scale with elapsed monotonic time from a 500 ms nominal cadence; boot changes reset continuity. Reject NaN/infinity before transport. Regenerate model identity after changing a declared component; changing a threshold also requires reviewing model version and card.

40. **Stop intent is not confirmed shutdown.** Coalesce Stop requests, invalidate/drain an already-issued launch, and prevent stale continuations from enabling the camera. Only a verified native/launcher result can report stopped. Clear stale proposal presentation while preserving durable restoration receipts and snapshots.

41. **Final Qt quit must reach the event loop.** Intercept explicit Quit for the recap flow, then remove the filter before the final `QApplication.quit()`. Ordinary dashboard close must not stop the session in either desktop mode. Test actual Qt event loops; mocked `quit()` cannot prove exit.

42. **Presentation must share evidence lifetime.** Desktop/popup/new-tab signal display expires after ten seconds without updates and clears on disconnect or invalid data. Hidden windows still expire, and queued state cannot repopulate an ended session. Dashboard Review explicitly focuses the current suggestion; arrival itself must not steal focus. Keep completed task diagnostics bounded and value-only, while retaining ownership of unfinished cleanup tasks.

### Camera physiology (0.6.0)

43. **An estimator change re-runs the qualification in the same change.** A change to a pulse, HRV or respiration estimator, gate or window must re-run `python -m cortex.scripts.qualify_physiology` (`synthetic` always, through `make qualify-physiology`, which fails when an admitted metric misses its criterion and is a step of `make verify`; `dataset-4tu` when the dataset is available) and update [the qualification report](docs/research/physiology-qualification.md) and [the model card](docs/model-cards/deterministic-support-v3.md) in the same change. A metric's admission to the rules rests on that report; the 0.5.1 estimator, which no reference gate had ever measured, published about 8 % of windows when replayed against ECG and could not publish a breathing rate at all.

44. **The publication gate is harmonic SNR, and the second harmonic counts only up to half the fundamental.** Uncapped, a weak line at half the true rate was confirmed by the real pulse counted as its harmonic: recording P2LC3 published 51 BPM for a 101 BPM heart. The in-band to out-of-band ratio it replaced had the same median for right and wrong rates.

45. **Window quality is signal strength times acquisition quality.** Never multiply raw per-frame scores (exposure, sharpness, landmark confidence) into it: they rarely exceed 0.6, and the product rejected about half of the windows that were otherwise ready.

46. **A window requirement is a share of the configured window, never its full length.** A 30 fps buffer holds 44.97 s of a 45 s window, so "require 45.0 s" meant no breathing rate was ever published.

47. **RMSSD is not qualified; no rule, suggestion or screen may read it. SDNN is the variability input.** Against ECG, RMSSD reached 22.8 ms mean error with 9 % of values within 15 ms; beat-to-beat differences at 30 fps are dominated by timing noise. SDNN, which averages that noise over a window, reached 8.2 ms.

48. **Physiology is fused only into a state whose own behavior rule is eligible.** Camera evidence alone must never produce an estimate: before this rule, camera evidence alone could become the estimated dominant state. Every physiology value is read against the personal reference, and a physical camera change discards that reference, because a different camera carries a different bias.

49. **The 4TU dataset license forbids redistribution.** Never commit the videos or any trace derived from them (color traces, caches, per-window values); only aggregate agreement may enter the repository.

50. **`jest.requireMock("vscode")` returns a separate automock.** Assert on the module-mapped `vscode` import, which is the object the extension calls. The editor's "legacy recommendation is dropped" test asserted on the automock and could never fail.

51. **A breathing rate needs head movement and a pulse-derived channel to agree.** The envelope and beat-interval channels are both read from the pulse and fail together: on simulated recordings they agreed with each other on a rate 5 /min too low while head movement read the true one, and the wrong rate was published. Two pulse-derived channels are one mechanism, not two witnesses.

52. **A listener thread must never call HIToolbox (`TIS*`/`TSM*`).** pynput's macOS keyboard listener reads the keyboard layout at the top of its thread; on current macOS that traps off the main thread (`SIGTRAP` in `dispatch_assert_queue`). The packaged 0.6.0 build crashed when a session was ended and started again, although the first start had worked. `input_hooks.keyboard_listener_class()` skips the read, which the listening path never uses; build the listener only through it, and re-check `_event_to_key` when pynput is upgraded.

53. **An ad-hoc rebuild is a new app to TCC.** Its signature changes, so camera permission returns to "not determined" and macOS lists no row for it under Privacy & Security → Camera. Guidance for an offline camera must lead to Cortex's own Settings, whose link raises the first-use prompt and falls back to System Settings only for a denied grant. Granting Accessibility always needs the user's Touch ID or password and cannot be scripted.

54. **Test stop-then-start on the installed build, not only start and stop.** Every defect the 0.6.0 live run found (the restart crash, the dead-end camera guidance, Pause left enabled) was invisible to the source test suite and to a single launch.

55. **Physiology may lower any eligible score, and raise only one that behavior alone already carried past the entry threshold.** Mixing with a fixed weight let a 0.35 behavior score reach 0.58 on full camera evidence (0.65 × 0.35 + 0.35) and become the estimate: a weak or ambiguous reading that the behavior rules alone would have left at `unknown`. "Eligible" (score above zero) is not "established".

56. **A derived value ends with the signal it came from.** The SDNN window was anchored at the newest accepted beat, so the last value was re-published unchanged for minutes after the person left the camera, and a strain load built earlier could offer a break to an empty chair. Anchor windows at the current time, refuse to publish when the newest input is stale, and require a recent reading before acting on an accumulated quantity.

57. **An input no reference sensor checked may only accompany one that was.** Blink rate and eye-closure share enter the physiology block only while heart rate or SDNN is usable in the same evaluation. Before this rule they moved scores with no pulse at all, which made "no pulse means behavior-only" false.

58. **A check that cannot fail is not a gate.** `qualify_physiology` returned 0 whatever it measured; the documents called it a required check. `--check` now exits 1 on a missed criterion or any publication on signal-free input, and a unit test pins that decision.

### Intel and hosted CI (0.6.0)

59. **An early CI step that fails hides every step after it.** The Intel audit exception expired on 2026-10-15; the audit step failed from then on, and the Intel Python gate, which follows it, did not run for six weeks. Nothing was tested on Intel in that time, and this branch could not learn that six of its tests failed there. When a job is red, read which steps did not run.

60. **To change one release's declared dependency, restate that release's metadata.** `override-dependencies` with a platform marker replaces the requirement everywhere: `"protobuf>=5.29.6,<6 ; <Intel marker>"` removed Protobuf from every non-Intel platform. `[[tool.uv.dependency-metadata]]` for `mediapipe` 0.10.21 changes only the release Intel installs. After any lock change, diff the exported requirements; the intended change here was one line.

61. **Tests must not assume `time.tzset`.** The hosted Intel runner's Python build has none. Call it only where it exists: on macOS, libc's `localtime` re-reads `TZ` on every call, and that is what `datetime` uses. Run a zone-dependent fixture with `time.tzset` deleted as well as present.

62. **The Intel Python graph can be reproduced on Apple silicon.** MediaPipe 0.10.21 ships universal2 wheels. Export the lock, keep the lines whose markers hold for `darwin`/`x86_64`/Python 3.12, and install them with `uv pip install --no-deps` into a Python 3.12 environment. That environment has NumPy 1.26 and the Intel Protobuf, runs the real-model test, and audits with `pip-audit -r … --no-deps --disable-pip`. It is not Intel hardware: the hosted Intel job is still the check of record.

Current verification: [0.6.0 validation](docs/release/0.6.0-validation.md).
Implementation history: [October refinement](docs/implementation-2026-10.md).

---

## Build & Run

```bash
# Run from the repository root with the pinned Python/Node/pnpm toolchain.
make setup

# DMG: dist/Cortex-<version>-macos-<arch>.dmg (ad-hoc by default)
make dmg

# Developer modes
make dev
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.apps.desktop_shell.main
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.apps.desktop_shell.main --in-process

# Browser extension
make ext-dev
make ext
make ext-edge

# Development native host; packaged users use Connect Extensions instead.
uv run --project cortex --locked --extra dev --extra codegen python -m cortex.scripts.install_native_host
# Fully quit the browser with Cmd+Q, then reopen. Reloading the extension is insufficient.

# Canonical Python gate includes lint, strict types, wheel, coverage and Qt isolation.
./cortex/scripts/run_python_gate.sh
make codegen-check
make contracts
(cd cortex/apps/browser_extension && pnpm exec tsc --noEmit && pnpm test)
(cd cortex/apps/vscode_extension && npm run lint && npm run compile && npm test)
```

## Ports

| Port | Service |
|------|---------|
| 9472 | FastAPI HTTP API |
| 9473 | WebSocket server |
| 9471 | Launcher agent (optional HTTP launcher) |

## Schema Codegen (audit Debt-1)

The Pydantic models in `cortex/libs/schemas/` are the single source of truth for every shape that crosses the daemon ↔ browser-extension boundary. Hand-written TypeScript interfaces in `cortex/apps/browser_extension/` previously drifted from these models (audit findings F42 / F43 / F44 / F45); the generator below detects committed declaration drift; runtime validation remains required.

### Regenerate after editing any Pydantic schema

```bash
make codegen
```

Output goes to `cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts` and `cortex/apps/vscode_extension/src/generated/cortex_schemas.d.ts`. The file ships with an `AUTOGENERATED — DO NOT EDIT BY HAND` header; hand-editing it is rejected by both the pre-commit hook and the CI gate.

### Verify in sync (used by CI and pre-commit)

```bash
make codegen-check
```

`--check` regenerates into a tempfile, diffs against the committed copy, prints the diff on drift, and exits with code 1 if the file is stale.

### One-time local install

```bash
make setup       # locked Python codegen extra and local json2ts dependency
make precommit   # installs pre-commit hooks
```

### Gates

- **`.pre-commit-config.yaml`** runs `--check` on every commit that touches `cortex/libs/schemas/`, the generator script, or the committed `.d.ts`.
- **`.github/workflows/ci.yml`** runs the same check on every PR. The job is `schema-codegen-check (Debt-1 drift gate)` and is required for merge.

### Adding a new WS message type

1. Add a member to `cortex/libs/schemas/ws_message_types.py::MessageType` with the literal string.
2. If the daemon dispatches on it, add a case in `cortex/services/api_gateway/websocket_server.py::_process_message` and update the `test_message_type_catalog_covers_*` tests.
3. Run `make codegen` and commit both regenerated `.d.ts` files alongside your Python change.
4. The TypeScript dispatch sites in `background.ts` / `popup.tsx` now type-check against the new union; their switch's `never` default flags any forgotten case.

## Git conventions

- Use descriptive feature or milestone branch names; never include `codex` or `agent`.
- Preserve the user's author identity and required DCO sign-off. Do not add an AI or agent coauthor trailer.
- Stage selected source, tests and documentation; keep local builds, coverage databases and review evidence ignored.
