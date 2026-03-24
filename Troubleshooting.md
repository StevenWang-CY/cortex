# Troubleshooting

## Daemon Won't Start

### "Cannot open webcam device"

The daemon failed to find a usable camera.

1. Check the camera is not in use by another app (Zoom, FaceTime, etc.)
2. Verify camera permission: `System Settings → Privacy & Security → Camera → Terminal.app` should be allowed
3. Test the camera directly:
   ```bash
   python3 -c "import cv2; cap = cv2.VideoCapture(0); print(cap.read()[0]); cap.release()"
   ```
4. If you have multiple cameras, try hardcoding: add `CORTEX_CAPTURE__DEVICE_ID=0` (or `1`, `2`) to `.env`

### "ModuleNotFoundError" on startup

The virtual environment is not activated or the package is not installed:
```bash
source .venv/bin/activate
pip install -e "./cortex[dev]"
```

### Daemon starts then crashes immediately

Check the log output for the specific error. Common causes:
- Missing `.env` file: `cp cortex/.env.example .env`
- Invalid LLM config: set `CORTEX_LLM__MODE=rule_based` to test without a LLM
- Missing storage directory: `python -m cortex.scripts.seed_config --root .`

---

## Camera Issues

### Camera opens iPhone instead of MacBook camera

Cortex automatically skips Continuity Camera (iPhone/iPad) and verifies each camera by name before accepting it.

- **Restart the daemon** after turning off your iPhone — camera selection runs once at startup
- If it still picks wrong: set `CORTEX_CAPTURE__DEVICE_ID=0` in `.env` (or try `1`, `2`)
- Debug what cameras are visible:
  ```bash
  python3 -c "
  from cortex.services.capture_service.webcam import _list_macos_video_device_names
  print(_list_macos_video_device_names())
  "
  ```

### Camera permission denied

- If launched from the browser extension: grant camera to **Terminal.app** in `System Settings → Privacy & Security → Camera`
- If launched from iTerm or another terminal: grant camera to that app instead
- Do NOT reset all camera permissions with `tccutil reset Camera` — this clears camera access for every app on your Mac (Zoom, FaceTime, etc.)

### Camera light stays on after stopping

```bash
pkill -f "cortex.scripts.run_dev"
```

If that doesn't work:
```bash
lsof -ti tcp:9473 | xargs kill -9
```

---

## Browser Extension Issues

### "Native host not found" or "Access forbidden"

1. Run the installer: `python -m cortex.scripts.install_native_host`
2. **Fully restart your browser** (Cmd+Q, then reopen) — reloading the extension tab is not enough
3. Native messaging manifests are only read at browser startup

### Extension popup shows "Not connected"

The daemon is not running. Click **Start Cortex** or run `cortex-dev` from your terminal.

### "Start Cortex" opens Terminal but daemon fails

- Check Terminal.app has Accessibility permission: `System Settings → Privacy & Security → Accessibility`
- Check the Terminal window for the actual error message

### Extension ID changed after rebuild

This should not happen — the extension uses a fixed manifest key for a deterministic ID. If it does change, re-run `python -m cortex.scripts.install_native_host` and restart the browser.

---

## "Stop Cortex" Doesn't Work

The extension uses a multi-layer kill chain:
1. WebSocket `SHUTDOWN` message
2. `POST /shutdown` HTTP request
3. Wait 1 second
4. `lsof` port scan + `pgrep` process search → SIGTERM
5. Wait 3 seconds → SIGKILL survivors

If the stop button fails once, click it again. If the camera light stays on after two attempts:
```bash
pkill -f "cortex.scripts.run_dev"
```

---

## LLM Errors

### Azure OpenAI errors

- Verify `.env` has `ENDPOINT`, `API_KEY`, `DEPLOYMENT_NAME`, and `API_VERSION=2025-01-01-preview`
- Test connectivity:
  ```bash
  python -c "from cortex.services.llm_engine.azure_client import AzureClient; print('ok')"
  ```
- Fallback: set `CORTEX_LLM__MODE=rule_based` to bypass LLM entirely

### Ollama errors ("connection refused")

Ollama is not running:
```bash
ollama serve   # in a separate terminal
```

Verify the model is pulled:
```bash
ollama list
# If llama3.1:8b is missing:
ollama pull llama3.1:8b
```

---

## Signal Quality Issues

### "No face detected" / rPPG not working

- Improve lighting — face should be evenly lit, no strong backlight
- Reduce movement — large head movements corrupt the rPPG signal
- The system falls back to telemetry-only mode when signal quality is low (this is normal behavior, not an error)

### State never leaves FLOW (no interventions)

- Check signal quality at `GET /status/current` — if physio quality is < 0.5, the system is in telemetry-only mode with stricter thresholds
- Run calibration: `cortex-calibrate --duration 120`
- Verify the daemon is receiving webcam frames:
  ```bash
  cortex-capture   # should show video with face detection overlay
  ```

### Too many false HYPER detections

- Run calibration (population defaults may not match your baseline)
- Increase the entry threshold in `.env`: `CORTEX_STATE__ENTRY_THRESHOLD=0.30`
- Check if another app is causing rapid window switching (browser auto-refresh, notifications)

---

## Python / Dependency Issues

### MediaPipe import errors on Apple Silicon

```bash
pip install --force-reinstall mediapipe
```

Verify you're using native ARM Python (not Rosetta):
```bash
python3 -c "import platform; print(platform.machine())"
# Should print: arm64
```

### pynput / Accessibility errors

Add your terminal app to: `System Settings → Privacy & Security → Input Monitoring`

The daemon will start without input monitoring but mouse/keyboard telemetry will be unavailable (state classification uses webcam-only mode).

### OpenCV camera index errors

```bash
python3 -c "
import cv2
for i in range(4):
    cap = cv2.VideoCapture(i)
    print(f'Device {i}: opened={cap.isOpened()}')
    cap.release()
"
```

---

## VS Code Extension Issues

### Context not appearing in interventions

- Verify the VS Code extension is installed and activated (check the status bar for the Cortex indicator)
- The extension connects via WebSocket to `ws://127.0.0.1:9473` — check the daemon is running
- Try reloading the VS Code window: `Cmd+Shift+P → Reload Window`

---

## Logs

Daemon logs are written to stdout. To save them:
```bash
cortex-dev 2>&1 | tee ~/cortex.log
```

Set log level in `.env`:
```bash
CORTEX_LOGGING__LEVEL=DEBUG   # verbose
CORTEX_LOGGING__LEVEL=INFO    # default
CORTEX_LOGGING__LEVEL=WARNING # quiet
```
