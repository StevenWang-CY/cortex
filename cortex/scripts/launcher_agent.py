#!/usr/bin/env python3
"""
Cortex Launcher Agent — lightweight HTTP server for starting/stopping the daemon.

Replaces Chrome Native Messaging with a simple HTTP API on 127.0.0.1:9471.
Auto-starts on login via macOS launchd (see install_launcher.py).

Zero cortex imports — must work even if the cortex package is broken.

Endpoints:
    POST /launch   — Start the daemon if not running
    POST /stop     — Stop the daemon gracefully
    GET  /status   — Check daemon status
    GET  /health   — Launcher liveness check
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9471
DAEMON_WS_PORT = 9473
DAEMON_HTTP_PORT = 9472


def _project_root() -> str:
    """Return the project root directory."""
    env = os.environ.get("CORTEX_PROJECT_ROOT")
    if env and os.path.isdir(env):
        return env
    # Fallback: this file is at cortex/scripts/launcher_agent.py
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _python_path() -> str:
    """Return the Python executable to use for the daemon."""
    env = os.environ.get("CORTEX_PYTHON")
    if env and os.path.isfile(env):
        return env
    # Try venv in project root
    venv_python = os.path.join(_project_root(), ".venv", "bin", "python")
    if os.path.isfile(venv_python):
        return venv_python
    return sys.executable


def _is_daemon_running() -> bool:
    """Check if the Cortex daemon is listening on its WebSocket port."""
    try:
        with socket.create_connection(("127.0.0.1", DAEMON_WS_PORT), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _find_daemon_pid() -> int | None:
    """Find the PID of the process listening on the daemon WebSocket port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{DAEMON_WS_PORT}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return None


def _launch_daemon() -> dict:
    """Spawn the Cortex daemon as a detached background process."""
    if _is_daemon_running():
        pid = _find_daemon_pid()
        return {"status": "already_running", "pid": pid}

    project_root = _project_root()
    python = _python_path()

    log_path = os.path.join(project_root, "cortex_daemon.log")

    try:
        # Write a launcher script and spawn via Popen (launcher_agent
        # already runs from terminal context with camera permissions).
        launcher_sh = os.path.join(project_root, ".cortex_launch.sh")
        with open(launcher_sh, "w") as f:
            f.write(f"#!/bin/bash\ncd {project_root}\nexec {python} -m cortex.scripts.run_dev\n")
        os.chmod(launcher_sh, 0o755)

        log_file = open(log_path, "a")
        subprocess.Popen(
            ["/bin/bash", launcher_sh],
            cwd=project_root,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"status": "starting", "message": "Daemon process spawned"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _find_all_daemon_pids() -> set[int]:
    """Find ALL daemon PIDs — by port AND by process name."""
    pids: set[int] = set()
    for port in (DAEMON_WS_PORT, DAEMON_HTTP_PORT):
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


def _stop_daemon() -> dict:
    """Stop the Cortex daemon — guaranteed kill."""
    # Step 1: HTTP shutdown (graceful)
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{DAEMON_HTTP_PORT}/shutdown",
            method="POST", data=b"",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass

    # Step 2: SIGTERM all daemon PIDs
    pids = _find_all_daemon_pids()
    if not pids:
        return {"status": "not_running"}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # Step 3: Wait for graceful shutdown
    for _ in range(6):
        time.sleep(0.5)
        if not _find_all_daemon_pids():
            return {"status": "stopped"}

    # Step 4: SIGKILL stragglers
    for pid in _find_all_daemon_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return {"status": "stopped"}


class LauncherHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests for the launcher agent."""

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS — allow Chrome extension to call us
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({"ok": True})
        elif self.path == "/status":
            running = _is_daemon_running()
            pid = _find_daemon_pid() if running else None
            self._send_json({
                "daemon_running": running,
                "pid": pid,
                "project_root": _project_root(),
                "python": _python_path(),
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path == "/launch":
            result = _launch_daemon()
            self._send_json(result)
        elif self.path == "/stop":
            result = _stop_daemon()
            self._send_json(result)
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, format: str, *args) -> None:
        """Suppress default stderr logging — use stdout instead."""
        print(f"[{time.strftime('%H:%M:%S')}] {format % args}")


def _check_existing_launcher() -> bool:
    """Check if another launcher agent is already running on our port."""
    try:
        import urllib.request
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/health", timeout=2,
        )
        data = json.loads(resp.read())
        return data.get("ok") is True
    except Exception:
        return False


def main() -> None:
    # Check for existing instance
    if _check_existing_launcher():
        print(f"Launcher agent already running on port {PORT}")
        sys.exit(0)

    server = ThreadingHTTPServer(("127.0.0.1", PORT), LauncherHandler)
    print(f"Cortex Launcher Agent started on http://127.0.0.1:{PORT}")
    print(f"  Project root: {_project_root()}")
    print(f"  Python: {_python_path()}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLauncher agent stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
