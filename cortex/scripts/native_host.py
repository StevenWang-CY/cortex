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
import socket
import struct
import subprocess
import sys
import os
import time
import traceback

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_host_debug.log")


def log(msg: str) -> None:
    """Append a debug line to the log file."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def read_message() -> dict:
    """Read a native messaging request from stdin."""
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        return {}
    length = struct.unpack("<I", raw_length)[0]
    data = sys.stdin.buffer.read(length)
    return json.loads(data.decode("utf-8"))


def send_message(msg: dict) -> None:
    """Send a native messaging response to stdout."""
    encoded = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def is_daemon_running(port: int = 9473) -> bool:
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
            subprocess.run(
                ["open", "-a", CORTEX_APP_PATH],
                capture_output=True, timeout=5,
            )
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
        return {"status": "timeout", "error": "Daemon started but port 9473 not yet ready"}

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
            f"cd {project_root} && "
            f"{python} -m cortex.scripts.run_dev "
            f"2>&1 | tee -a {log_path}"
        )
        subprocess.run(
            [
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd}"',
            ],
            capture_output=True, timeout=5,
        )
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
    for port in (9473, 9472):
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

    return pids


def stop_daemon() -> dict:
    """Stop the Cortex daemon — guaranteed kill."""
    import signal as _signal

    # Step 1: Try HTTP shutdown (cleanest — triggers graceful stop chain)
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:9472/shutdown", method="POST", data=b"",
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


def main() -> None:
    log("--- invoked ---")
    try:
        msg = read_message()
        log(f"received: {msg}")
        command = msg.get("command", "launch")

        if command == "launch":
            result = launch_daemon()
        elif command == "stop":
            result = stop_daemon()
        elif command == "status":
            result = {"status": "running" if is_daemon_running() else "stopped"}
        else:
            result = {"status": "error", "error": f"Unknown command: {command}"}

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
