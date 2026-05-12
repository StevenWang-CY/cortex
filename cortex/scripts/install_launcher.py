#!/usr/bin/env python3
"""
Start the Cortex Launcher Agent (HTTP server on port 9471).

Usage:
    python -m cortex.scripts.install_launcher          # Start in foreground
    python -m cortex.scripts.install_launcher --bg     # Start in background
    python -m cortex.scripts.install_launcher --stop   # Stop background instance

The launcher agent is a lightweight HTTP server that the browser extension
can call to start/stop the Cortex daemon. It does NOT auto-start on login —
you must run this manually or use native messaging instead.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request

LAUNCHER_PORT = 9471


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _python_path() -> str:
    venv_python = os.path.join(_project_root(), ".venv", "bin", "python")
    if os.path.isfile(venv_python):
        return venv_python
    return sys.executable


def _check_launcher_health() -> bool:
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{LAUNCHER_PORT}/health", timeout=2,
        )
        data = json.loads(resp.read())
        return data.get("ok") is True
    except Exception:
        return False


def _find_launcher_pid() -> int | None:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{LAUNCHER_PORT}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def start_foreground() -> None:
    """Start the launcher agent in the foreground (blocking)."""
    if _check_launcher_health():
        print(f"Launcher agent already running on port {LAUNCHER_PORT}")
        return

    python = _python_path()
    project_root = _project_root()
    print(f"Starting Cortex Launcher Agent on port {LAUNCHER_PORT}...")
    print(f"  Project root: {project_root}")
    print(f"  Python: {python}")
    print("  Press Ctrl+C to stop\n")
    os.execv(python, [python, "-m", "cortex.scripts.launcher_agent"])


def start_background() -> None:
    """Start the launcher agent as a background process."""
    if _check_launcher_health():
        print(f"Launcher agent already running on port {LAUNCHER_PORT}")
        return

    python = _python_path()
    project_root = _project_root()
    log_path = os.path.join(project_root, "cortex_launcher.log")

    log_file = open(log_path, "a")
    proc = subprocess.Popen(
        [python, "-m", "cortex.scripts.launcher_agent"],
        cwd=project_root,
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    print(f"Launcher agent started in background (pid {proc.pid})")
    print(f"  Log: {log_path}")
    print("  Stop: python -m cortex.scripts.install_launcher --stop")


def stop() -> None:
    """Stop the background launcher agent."""
    pid = _find_launcher_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to launcher agent (pid {pid})")
    else:
        print("Launcher agent is not running")


def main() -> None:
    if "--stop" in sys.argv:
        stop()
    elif "--bg" in sys.argv:
        start_background()
    else:
        start_foreground()


if __name__ == "__main__":
    main()
