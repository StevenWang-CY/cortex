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
import logging
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

PORT = 9471
DAEMON_WS_PORT = 9473
DAEMON_HTTP_PORT = 9472


# F08: capability-token gate on destructive endpoints. The full helper
# lives in ``cortex.libs.auth.local_token``; this file's docstring
# mandates zero cortex imports so the launcher survives a broken
# package install. We therefore inline a minimal path resolver and a
# constant-time compare. Token-file format must stay in sync with
# ``cortex/libs/auth/local_token.py``.

_AUTH_TOKEN_HEADER = "X-Cortex-Auth-Token"

# CORS lockdown (audit fix): the previous ``*`` wildcard echoed
# ``Access-Control-Allow-Origin`` on every request, which let any open
# tab read ``/status`` and exfiltrate the project root, Python path and
# daemon PID via XHR. Browser extensions present an Origin of the form
# ``chrome-extension://<id>`` (or ``extension://`` for some Firefox-like
# builds); we echo the Origin only when it matches one of these
# patterns, otherwise we omit the CORS header entirely so the browser
# blocks the cross-origin response.
_ALLOWED_ORIGIN_PATTERNS = (
    re.compile(r"^chrome-extension://[a-zA-Z0-9_-]+$"),
    re.compile(r"^extension://[a-zA-Z0-9_-]+$"),
)


def _allowed_origin(origin: str | None) -> str | None:
    """Return ``origin`` iff it matches an extension scheme, else None."""
    if not origin:
        return None
    for pat in _ALLOWED_ORIGIN_PATTERNS:
        if pat.match(origin):
            return origin
    return None


def _auth_token_path() -> str:
    """Resolve the auth-token file path without importing cortex."""
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Cortex/auth.token"
        )
    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "cortex", "auth.token")
    if sys.platform in ("win32", "cygwin"):
        base = os.environ.get("APPDATA") or os.path.expanduser(
            "~\\AppData\\Roaming"
        )
        return os.path.join(base, "Cortex", "auth.token")
    return os.path.expanduser("~/.cortex/auth.token")


def _verify_auth_token(presented: str | None) -> bool:
    """Constant-time check against the on-disk token. Falls closed on
    any read/compare error so a missing or unreadable file results in
    a 401 rather than open access."""
    if not presented:
        return False
    try:
        with open(_auth_token_path(), encoding="utf-8") as fp:
            stored = fp.read().strip()
    except OSError:
        return False
    if not stored:
        return False
    import hmac
    try:
        return hmac.compare_digest(stored, presented.strip())
    except Exception:
        logger.debug("auth token compare_digest raised", exc_info=True)
        return False


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
    """Return True iff at least one daemon PID is alive.

    Audit fix: the previous implementation only TCP-probed the WS port.
    That returned True for any process binding 127.0.0.1:9473 — including
    orphaned daemons whose camera handle is stale, or unrelated tools
    that grabbed the port. Combining port + pgrep (via
    ``_find_all_daemon_pids``) closes the "already_running" false
    positive that bounced the extension's Launch button.
    """
    return bool(_find_all_daemon_pids())


def _find_daemon_pid() -> int | None:
    """Return one daemon PID (port + pgrep), or None.

    Used by /status. Delegates to ``_find_all_daemon_pids`` so a daemon
    that lost its port binding but still holds the camera is still
    reported.
    """
    pids = _find_all_daemon_pids()
    if not pids:
        return None
    # Stable ordering for log/UI purposes.
    return min(pids)


CORTEX_APP_PATH = "/Applications/Cortex.app"


def _launch_daemon() -> dict:
    """Spawn the Cortex daemon as a detached background process.

    Prefers ``open -a Cortex.app`` when the DMG install is present so end
    users who don't have a dev checkout can still use the extension's
    Start button. Falls back to ``python -m cortex.scripts.run_dev`` for
    developers.
    """
    if _is_daemon_running():
        pid = _find_daemon_pid()
        return {"status": "already_running", "pid": pid}

    # DMG path: launch the installed .app (in-process daemon).
    if os.path.isdir(CORTEX_APP_PATH):
        try:
            result = subprocess.run(
                ["open", CORTEX_APP_PATH],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                return {
                    "status": "error",
                    "error": stderr or "Failed to launch Cortex.app",
                }
            return {"status": "starting", "message": "Cortex.app launched"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # Dev path: run the cortex.scripts.run_dev module from the checkout.
    #
    # CLAUDE.md rule #1: a subprocess.Popen launched here would inherit
    # the launcher's TCC context (or, if the launcher was started from a
    # browser handoff, the browser's TCC context). With ``start_new_session
    # =True`` macOS still tags the new process tree with the launcher's
    # entitlements, so the daemon ends up without camera permission and
    # silently fails the first ``cv2.VideoCapture.read()``.
    #
    # The proven fix (also used by ``native_host.py``) is to delegate the
    # spawn to Terminal.app via ``osascript``. Terminal has its own TCC
    # camera grant, the daemon runs in Terminal's foreground, and the
    # full stdout/stderr is visible to the developer.
    project_root = _project_root()
    python = _python_path()

    try:
        # User-writable launch script. ~/Desktop is sandboxed (CLAUDE.md
        # rule #4) and writing into the project root spams the dev's
        # checkout with a runtime artefact, so we use the macOS standard
        # support directory.
        support_dir = pathlib.Path.home() / "Library" / "Application Support" / "Cortex"
        support_dir.mkdir(parents=True, exist_ok=True)
        launcher_sh = support_dir / "launch.sh"
        log_path = support_dir / "cortex_daemon.log"

        launcher_sh.write_text(
            "#!/bin/bash\n"
            f"cd {shlex.quote(project_root)}\n"
            f"exec {shlex.quote(python)} -m cortex.scripts.run_dev "
            f"2>&1 | tee -a {shlex.quote(str(log_path))}\n"
        )
        launcher_sh.chmod(0o755)

        # osascript -> Terminal.app -> bash launch.sh.
        # ``do script`` opens a new Terminal window/tab and runs the
        # command in Terminal's TCC context. The daemon stays attached
        # to Terminal's foreground so it does not lose the camera grant
        # the way a backgrounded process would.
        terminal_cmd = f"/bin/bash {shlex.quote(str(launcher_sh))}"
        result = subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "Terminal" to do script "{terminal_cmd}"',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return {
                "status": "error",
                "error": stderr or "osascript failed to launch Terminal",
            }
        return {"status": "starting", "message": "Daemon launched via Terminal.app"}
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
            logger.debug("lsof probe failed (port=%d)", port, exc_info=True)
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
        logger.debug("pgrep run_dev failed", exc_info=True)
    # Bundled app process name/path for DMG installs.
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
        logger.debug("pgrep Cortex.app failed", exc_info=True)
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
        logger.debug("HTTP /shutdown probe failed", exc_info=True)

    # Step 2: SIGTERM all daemon PIDs
    pids = _find_all_daemon_pids()
    if not pids:
        return {"status": "not_running"}
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            logger.debug("SIGTERM target pid %d already gone", pid)

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
            logger.debug("SIGKILL target pid %d already gone", pid)
    return {"status": "stopped"}


class LauncherHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests for the launcher agent."""

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS lockdown: echo Origin only when it matches an extension
        # scheme (see ``_allowed_origin``). The previous ``*`` wildcard
        # let any tab read /status and exfiltrate the project root.
        origin = _allowed_origin(self.headers.get("Origin"))
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                f"Content-Type, {_AUTH_TOKEN_HEADER}",
            )
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight — extension origins only."""
        origin = _allowed_origin(self.headers.get("Origin"))
        self.send_response(204)
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                f"Content-Type, {_AUTH_TOKEN_HEADER}",
            )
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
            # Audit-2 fix (CSRF): require the same capability token as
            # ``/stop``. With CORS at ``Access-Control-Allow-Origin: *``,
            # any open browser tab could previously force-launch the
            # daemon — the daemon then opened the camera and began
            # capturing biometrics without user consent. The legitimate
            # extension fetches the token via native messaging (see
            # ``native_host.py:get_auth_token``) and attaches the
            # ``X-Cortex-Auth-Token`` header.
            presented = self.headers.get(_AUTH_TOKEN_HEADER)
            if not _verify_auth_token(presented):
                self._send_json(
                    {"error": "unauthorized", "reason": "missing or invalid auth token"},
                    401,
                )
                return
            result = _launch_daemon()
            self._send_json(result)
        elif self.path == "/stop":
            # F08: require the capability token. Any localhost origin
            # (malicious tab, hostile extension on the same browser
            # profile) can reach this port; without the gate, any such
            # origin could enumerate PIDs and SIGTERM the daemon at
            # will. The token is supplied via the X-Cortex-Auth-Token
            # header; the legitimate extension fetches it from the
            # native host (see native_host.py:get_auth_token).
            presented = self.headers.get(_AUTH_TOKEN_HEADER)
            if not _verify_auth_token(presented):
                self._send_json(
                    {"error": "unauthorized", "reason": "missing or invalid auth token"},
                    401,
                )
                return
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
        logger.debug("existing-launcher probe failed", exc_info=True)
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
