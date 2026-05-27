#!/usr/bin/env python3
"""
Cortex Native Messaging Host for Chrome Extension.

Chrome calls this script via Native Messaging when the extension needs
to launch the Cortex daemon. It reads a JSON request, spawns the daemon
as a detached background process (if not already running), and replies
with status.

Protocol: Chrome native messaging uses 4-byte little-endian length prefix
followed by JSON payload.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import time
import traceback

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_host_debug.log")


# P1 (audit Phase 4d): centralise the port literals so a future port
# migration only touches ``cortex/libs/config/ports.py``. Import is
# wrapped in try/except because this script may be invoked by Chrome
# native-messaging with a Python interpreter that doesn't have the
# project installed (the installer copies the script out of the .app
# but does NOT carry the package); the fallback defaults match the
# constants in that module verbatim.
try:
    from cortex.libs.config.ports import HTTP_API_PORT, WEBSOCKET_PORT
except Exception:  # pragma: no cover - import-path dependent
    HTTP_API_PORT = 9472
    WEBSOCKET_PORT = 9473


def log(msg: str) -> None:
    """Append a debug line to the log file."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def read_message_bytes() -> bytes | None:
    """Read a native messaging request from stdin as raw bytes.

    Returns ``None`` when stdin is closed before a full length prefix
    arrives. Length-prefix-only validation lives here so callers can
    defer schema parsing to :func:`parse_native_message` (audit F14).
    The legacy ``read_message()`` returned a parsed ``dict`` and used an
    8 MB cap; the new contract is 64 KB and a bytes return so the
    schema layer can reject oversized payloads alongside malformed
    JSON in one place.
    """
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        return None
    length = struct.unpack("<I", raw_length)[0]
    # Tight cap (64 KB) lives in the schema module; reject earlier here
    # so we never allocate megabytes for an obviously-bogus prefix.
    # The schema layer rejects again — defense in depth.
    if length > 64 * 1024:
        # Drain whatever bytes are available so we don't desync the
        # protocol for the next message; cap at 1 MB to bound work.
        try:
            sys.stdin.buffer.read(min(length, 1024 * 1024))
        except Exception:
            pass
        return b""
    return sys.stdin.buffer.read(length)


def send_message(msg: dict) -> None:
    """Send a native messaging response to stdout."""
    encoded = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def is_daemon_running(port: int = WEBSOCKET_PORT) -> bool:
    """Check if the Cortex daemon is already listening on its WebSocket port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False


CORTEX_APP_PATH = "/Applications/Cortex.app"


def _is_installed_app() -> bool:
    """True when Cortex.app is installed in /Applications (DMG users)."""
    return os.path.isdir(CORTEX_APP_PATH)


def launch_daemon() -> dict:
    """Launch the Cortex daemon as a detached background process.

    Two launch modes:

    * **DMG mode** — Cortex.app is installed in /Applications. Use
      ``open -a Cortex`` so the bundled Python and in-process daemon
      start with the app's own TCC camera identity. This is the path
      end users hit after installing the DMG.
    * **Dev mode** — No installed .app. Fall back to running
      ``python -m cortex.scripts.run_dev`` via Terminal.app so the
      dev-checkout daemon inherits Terminal's camera permission.
    """
    if is_daemon_running():
        return {"status": "already_running"}

    # --- DMG path: open the installed .app ---------------------------------
    if _is_installed_app():
        try:
            result = subprocess.run(
                ["open", CORTEX_APP_PATH],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                log(f"open failed rc={result.returncode} stderr={stderr}")
                return {
                    "status": "error",
                    "error": stderr or "Failed to launch Cortex.app",
                }
            log("Launched daemon via open -a Cortex.app")
        except Exception as e:
            log(f"open -a failed: {e}")
            return {"status": "error", "error": str(e)}

        # The desktop shell starts its daemon lazily — allow up to 20s.
        for i in range(40):
            time.sleep(0.5)
            if is_daemon_running():
                log(f"Daemon ready after {(i+1)*0.5}s")
                return {"status": "launched"}
        log("Daemon did not become ready in 20s")
        return {"status": "timeout", "error": f"Daemon started but port {WEBSOCKET_PORT} not yet ready"}

    # --- Dev path: python -m cortex.scripts.run_dev via Terminal.app -------
    # Find the project root (this script is at cortex/scripts/native_host.py)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))

    log(f"project_root={project_root}")
    log(f"sys.executable={sys.executable}")

    try:
        log_path = os.path.join(project_root, "cortex_daemon.log")
        python = os.path.abspath(sys.executable)

        # Launch via Terminal.app — Terminal has its own TCC context for
        # camera and file access.  The daemon runs in the foreground of
        # Terminal so it inherits Terminal's camera permission.
        cmd = (
            f"cd {shlex.quote(project_root)} && "
            f"{shlex.quote(python)} -m cortex.scripts.run_dev "
            f"2>&1 | tee -a {shlex.quote(log_path)}"
        )
        result = subprocess.run(
            [
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd}"',
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            log(f"osascript failed rc={result.returncode} stderr={stderr}")
            return {"status": "error", "error": stderr or "Failed to launch via Terminal"}
        log("Launched daemon via Terminal.app")
    except Exception as e:
        log(f"Popen failed: {e}")
        return {"status": "error", "error": str(e)}

    # Wait for the daemon to start listening (up to 12 seconds —
    # camera warmup alone takes ~2s on Mac builtin camera)
    for i in range(24):
        time.sleep(0.5)
        if is_daemon_running():
            log(f"Daemon ready after {(i+1)*0.5}s")
            return {"status": "launched"}

    log("Daemon did not become ready in 12s")
    return {"status": "timeout", "error": "Daemon started but port 9473 not yet ready"}


def _find_all_daemon_pids() -> set[int]:
    """Find ALL Cortex daemon PIDs — by port AND by process name.

    The daemon can lose its port binding while the process (and camera)
    keeps running, so we must also search by command name.
    """
    pids: set[int] = set()

    # Method 1: lsof on known ports
    for port in (WEBSOCKET_PORT, HTTP_API_PORT):
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
        except Exception:
            pass

    # Method 2: pgrep by command — catches orphaned processes that lost
    # their port binding but still hold the camera open
    try:
        result = subprocess.run(
            ["pgrep", "-f", "cortex.scripts.run_dev"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    except Exception:
        pass
    # Bundled app executable path for DMG installs.
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"{CORTEX_APP_PATH}/Contents/MacOS/Cortex"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    except Exception:
        pass

    return pids


def stop_daemon() -> dict:
    """Stop the Cortex daemon — guaranteed kill."""
    import signal as _signal

    # Step 1: Try HTTP shutdown (cleanest — triggers graceful stop chain)
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{HTTP_API_PORT}/shutdown", method="POST", data=b"",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass

    # Step 2: Find ALL daemon PIDs and send SIGTERM
    pids = _find_all_daemon_pids()
    for pid in pids:
        try:
            os.kill(pid, _signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        log(f"Sent SIGTERM to pids: {pids}")

    # Step 3: Wait for graceful shutdown
    for _ in range(6):
        time.sleep(0.5)
        remaining = _find_all_daemon_pids()
        if not remaining:
            return {"status": "stopped"}

    # Step 4: SIGKILL anything still alive
    remaining = _find_all_daemon_pids()
    for pid in remaining:
        try:
            os.kill(pid, _signal.SIGKILL)
            log(f"SIGKILL sent to {pid}")
        except ProcessLookupError:
            pass

    return {"status": "stopped"}


def _get_auth_token_response() -> dict:
    """Return the daemon's capability token to the extension.

    Loads or creates the token via :func:`cortex.libs.auth.load_or_create_token`.
    On import failure (e.g. running this script outside an installed
    venv) returns a structured error so the extension can degrade
    gracefully rather than blocking on a missing token.
    """
    try:
        from cortex.libs.auth import load_or_create_token

        return {"status": "ok", "token": load_or_create_token()}
    except Exception as exc:  # pragma: no cover - import-path dependent
        return {"status": "error", "error": f"auth_token_unavailable: {exc}"}


def _read_auth_token() -> str:
    """Best-effort load of the capability token for HTTP header use.

    Used by the ``raise_dashboard`` branch to authenticate against
    ``POST /dashboard/raise`` (Phase 4b). Returns an empty string on
    failure — the daemon route will then return 401, which surfaces to
    the extension as ``{ok: false, error: "..."}``. We never propagate
    a stack trace into the response body.
    """
    try:
        from cortex.libs.auth import load_or_create_token

        return load_or_create_token()
    except Exception:
        return ""


def _raise_dashboard(target: str) -> dict:
    """Ask the daemon to bring its desktop dashboard to the front.

    P1 (audit Phase 4d, Task E): the browser extension popup needs a
    way to "open Cortex" without juggling AppleScript or relying on the
    user finding the menu bar icon. We POST to
    ``http://127.0.0.1:<HTTP_API_PORT>/dashboard/raise`` (added in
    Phase 4b-retry-1) and surface the daemon's response status. If the
    route isn't deployed yet, urllib raises ``HTTPError(404)``; we
    return ``{ok: false, error: "..."}`` rather than crashing the host.
    """
    import urllib.request

    try:
        url = f"http://127.0.0.1:{HTTP_API_PORT}/dashboard/raise"
        req = urllib.request.Request(
            url,
            data=json.dumps({"target": target}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Cortex-Auth": _read_auth_token(),
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=2)
        return {"ok": True, "status": resp.status}
    except Exception as exc:  # noqa: BLE001 — extension wants the string
        return {"ok": False, "error": str(exc)}


def main() -> None:
    log("--- invoked ---")
    try:
        # Audit F14 + F37: schema-validate the inbound payload before
        # dispatching. Out-of-band failures (oversized, unparseable,
        # unknown command, project_root outside the allowlist) return a
        # structured ``error`` envelope rather than crashing the host
        # or — worse — invoking ``launch_daemon`` with attacker-shaped
        # arguments.
        from cortex.libs.schemas.native_messaging import parse_native_message

        raw = read_message_bytes()
        if raw is None:
            # stdin closed without a full prefix; nothing we can usefully
            # reply to. The Chrome native-messaging protocol expects no
            # further output in this case.
            log("stdin closed before payload arrived")
            return

        # P1 (audit Phase 4d, Task E): ``raise_dashboard`` lives
        # outside the Pydantic discriminated-union in
        # :mod:`cortex.libs.schemas.native_messaging` (owned by another
        # phase). To avoid blocking on that schema bump, peek at the
        # raw command and short-circuit when it matches. The payload is
        # tiny (a single ``target`` string) so we validate inline
        # rather than via a Pydantic model.
        try:
            _peek: dict = json.loads(raw.decode("utf-8"))
        except Exception:
            _peek = {}
        if isinstance(_peek, dict) and _peek.get("command") == "raise_dashboard":
            target_raw = _peek.get("target", "dashboard")
            target = target_raw if isinstance(target_raw, str) else "dashboard"
            # Guard against absurd payloads (Pydantic would do this for us
            # in the standard path).
            if len(target) > 64:
                target = target[:64]
            log(f"received: command=raise_dashboard target={target}")
            result = _raise_dashboard(target)
            log(f"sending: {result}")
            send_message(result)
            return

        parsed = parse_native_message(raw)
        if parsed.error is not None:
            log(f"rejected: error={parsed.error} detail={parsed.detail}")
            send_message({
                "status": "error",
                "error": parsed.error,
                "detail": parsed.detail,
            })
            return

        msg = parsed.message
        assert msg is not None  # narrow for type-checkers
        log(f"received: command={msg.command}")

        if msg.command == "launch":
            result = launch_daemon()
        elif msg.command == "stop":
            result = stop_daemon()
        elif msg.command == "status":
            result = {"status": "running" if is_daemon_running() else "stopped"}
        elif msg.command == "get_auth_token":
            # F07b/F08: extension cannot read mode-0600 files directly;
            # the native host runs as the user and CAN. The browser↔host
            # boundary is already OS-authenticated (chrome.runtime.host
            # is provisioned per-profile), so returning the token here
            # does not widen the attack surface — it just reaches the
            # capability gates we added on WS SHUTDOWN and launcher /stop.
            result = _get_auth_token_response()
        else:
            # Unreachable: the schema's discriminated union exhausts the
            # legitimate command set. Surfaced for defence in depth.
            result = {
                "status": "error",
                "error": "unknown_command",
                "detail": str(msg.command),
            }

        log(f"sending: {result}")
        send_message(result)
    except Exception as e:
        log(f"CRASH: {traceback.format_exc()}")
        # Always try to send a response even on crash
        try:
            send_message({"status": "error", "error": str(e)})
        except Exception:
            pass


if __name__ == "__main__":
    main()
