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
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

PORT = 9471
DAEMON_WS_PORT = 9473
DAEMON_HTTP_PORT = 9472

# Indirection points for the stop chain so tests can drive the timing
# without sleeping (see ``stop_daemon``). Production leaves them alone.
_sleep: Callable[[float], None] = time.sleep
_monotonic: Callable[[], float] = time.monotonic

# D2: how long a daemon that ACCEPTED ``POST /shutdown`` gets to finish
# its graceful chain (session recap → WS ack → close ports → DB close)
# before we escalate to SIGTERM. The previous chain sent SIGTERM
# immediately and SIGKILL 3 s later, which interrupted the recap and
# the SQLite close. The daemon's own budget for the recap acknowledgement
# alone is 5 s, so 20 s is a comfortable ceiling.
GRACEFUL_SHUTDOWN_TIMEOUT_S = 20.0
# After SIGTERM (the daemon runs the same graceful chain from its signal
# handler) wait this long before the last-resort SIGKILL.
SIGTERM_GRACE_S = 5.0
_POLL_INTERVAL_S = 0.5


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


def _config_dir() -> str:
    """Resolve the Cortex config directory without importing cortex.

    Mirrors ``cortex.libs.utils.platform.get_config_dir`` — the two must
    stay in sync because the daemon writes ``auth.token`` and
    ``daemon.pid`` there and this module reads them.
    """
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Cortex")
    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        return os.path.join(base, "cortex")
    if sys.platform in ("win32", "cygwin"):
        base = os.environ.get("APPDATA") or os.path.expanduser(
            "~\\AppData\\Roaming"
        )
        return os.path.join(base, "Cortex")
    return os.path.expanduser("~/.cortex")


def _auth_token_path() -> str:
    """Resolve the auth-token file path without importing cortex."""
    return os.path.join(_config_dir(), "auth.token")


def _daemon_pid_path() -> str:
    """Resolve the daemon-written pidfile path (``<config_dir>/daemon.pid``).

    The daemon (or ``run_dev``) writes its own PID there on start and
    removes it on clean exit. The stop chain prefers it because it is
    the only identification that survives App Translocation (the
    executable path is then under ``/private/var/folders/.../AppTranslocation``
    and the anchored ``pgrep`` pattern cannot match it).
    """
    return os.path.join(_config_dir(), "daemon.pid")


def _read_auth_token() -> str:
    """Return the on-disk capability token, or ``""`` when unavailable.

    Pure read — the launcher never mints a token; that is the daemon's
    job at start. An empty return makes ``POST /shutdown`` bounce with
    401, which the stop chain treats as "graceful path unavailable".
    """
    try:
        with open(_auth_token_path(), encoding="utf-8") as fp:
            return fp.read().strip()
    except OSError:
        return ""


def _verify_auth_token(presented: str | None) -> bool:
    """Constant-time check against the on-disk token. Falls closed on
    any read/compare error so a missing or unreadable file results in
    a 401 rather than open access."""
    if not presented:
        return False
    stored = _read_auth_token()
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


# ---------------------------------------------------------------------------
# D1: daemon PID discovery.
#
# The previous chain ran ``lsof -ti tcp:<port>`` — which lists BOTH ends of
# every socket on the port — and an unanchored ``pgrep -f``. Together they
# returned Chrome's network process, the VS Code extension host, the
# WS-mode desktop shell (all *clients* of 9473), the native host itself
# (``.../MacOS/CortexNativeHost`` contains ``.../MacOS/Cortex``) and any
# editor with ``cortex/scripts/run_dev.py`` open, and then SIGTERM/SIGKILLed
# them all. Every candidate below is therefore restricted to a process that
# is provably the daemon:
#
# * ``lsof -sTCP:LISTEN`` — only the socket's listening owner;
# * anchored ``pgrep`` patterns — only the GUI executable or the dev
#   module invocation, never ``CortexNativeHost``;
# * a daemon-written pidfile, cross-checked against the live command line;
# * never our own PID or our parent's (Chrome for the native host, launchd
#   or a terminal for the launcher agent).
# ---------------------------------------------------------------------------

_DAEMON_APP_EXECUTABLE = f"{CORTEX_APP_PATH}/Contents/MacOS/Cortex"

#: ``pgrep -f`` (ERE) pattern for the packaged GUI daemon. Anchored at
#: both ends so ``CortexNativeHost`` (same directory, longer name) can
#: never match; ``( |$)`` allows launch arguments after the executable.
PGREP_APP_PATTERN = rf"^{re.escape(_DAEMON_APP_EXECUTABLE)}( |$)"

#: ``pgrep -f`` pattern for the dev-checkout daemon. Requires the literal
#: ``-m cortex.scripts.run_dev`` module invocation preceded by a python
#: executable so ``vim cortex/scripts/run_dev.py``, ``grep run_dev`` or a
#: ``pgrep`` of our own never match (``.`` is escaped — the old pattern let
#: ``cortex/scripts/run_dev`` match too).
PGREP_DEV_PATTERN = r"(^|/)python[0-9.]* -m cortex\.scripts\.run_dev( |$)"

#: Command lines that a pidfile PID must exhibit to be trusted. Looser
#: than the ``pgrep`` anchors on purpose: the pidfile is written by the
#: daemon itself, so it may legitimately point at a translocated bundle
#: (``/private/var/folders/.../AppTranslocation/.../Cortex.app``), the
#: in-process desktop shell (``-m cortex.apps.desktop_shell.main``) or the
#: ``cortex-dev`` console script. It must still never be the native host.
_PIDFILE_CMDLINE_RE = re.compile(
    r"(Cortex\.app/Contents/MacOS/Cortex( |$))"
    r"|(-m cortex\.scripts\.run_dev( |$))"
    r"|(-m cortex\.apps\.desktop_shell\.main( |$))"
    r"|(/cortex-dev( |$))"
)
_NEVER_KILL_CMDLINE_RE = re.compile(r"CortexNativeHost")


def _protected_pids() -> set[int]:
    """PIDs the stop chain must never signal: ourselves and our parent."""
    protected = {os.getpid()}
    try:
        protected.add(os.getppid())
    except OSError:  # pragma: no cover - platform dependent
        pass
    return protected


def _parse_pid_lines(stdout: str) -> set[int]:
    pids: set[int] = set()
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def _listening_pids(port: int) -> set[int]:
    """PIDs *listening* on ``127.0.0.1:port`` (never the clients)."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        logger.debug("lsof probe failed (port=%d)", port, exc_info=True)
        return set()
    return _parse_pid_lines(result.stdout)


def _pgrep(pattern: str) -> set[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        logger.debug("pgrep failed (pattern=%s)", pattern, exc_info=True)
        return set()
    return _parse_pid_lines(result.stdout)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_command_line(pid: int) -> str | None:
    """Return the full command line of ``pid`` via ``ps``, or ``None``."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        logger.debug("ps probe failed (pid=%d)", pid, exc_info=True)
        return None
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None


def _pidfile_daemon_pid() -> int | None:
    """Return the daemon PID from the pidfile if it is alive and provably ours.

    A stale pidfile (daemon crashed, PID recycled by an unrelated process)
    is the classic way a pidfile kills the wrong process, so the PID is
    accepted only when it is alive, not protected, and its live command
    line matches a daemon shape and is not the native host.
    """
    try:
        with open(_daemon_pid_path(), encoding="utf-8") as fp:
            text = fp.read().strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    pid = int(text)
    if pid <= 1 or pid in _protected_pids() or not _pid_alive(pid):
        return None
    command = _process_command_line(pid)
    if command is None:
        return None
    if _NEVER_KILL_CMDLINE_RE.search(command):
        return None
    if _PIDFILE_CMDLINE_RE.search(command) is None:
        return None
    return pid


def find_daemon_pids() -> set[int]:
    """Find every PID that is provably the Cortex daemon (D1).

    Union of: the daemon-written pidfile (preferred, see
    :func:`_pidfile_daemon_pid`), the *listening* owners of the WS/HTTP
    ports, and the anchored ``pgrep`` patterns. Our own PID and our parent
    are always excluded. Works with or without the pidfile.
    """
    pids: set[int] = set()
    pidfile_pid = _pidfile_daemon_pid()
    if pidfile_pid is not None:
        pids.add(pidfile_pid)
    for port in (DAEMON_WS_PORT, DAEMON_HTTP_PORT):
        pids |= _listening_pids(port)
    pids |= _pgrep(PGREP_DEV_PATTERN)
    pids |= _pgrep(PGREP_APP_PATTERN)
    return pids - _protected_pids()


def _find_all_daemon_pids() -> set[int]:
    """Backward-compatible alias for :func:`find_daemon_pids`."""
    return find_daemon_pids()


# ---------------------------------------------------------------------------
# D2: graceful stop chain.
# ---------------------------------------------------------------------------


def _request_graceful_shutdown(token: str) -> bool:
    """``POST /shutdown`` with the capability token. True iff accepted (2xx).

    The route lives on the token-gated router, so the previous tokenless
    POST always bounced with 401 and graceful shutdown was dead code.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{DAEMON_HTTP_PORT}/shutdown",
        method="POST",
        data=b"",
        headers={_AUTH_TOKEN_HEADER: token} if token else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return 200 <= int(resp.status) < 300
    except urllib.error.HTTPError as exc:
        logger.debug("HTTP /shutdown rejected: %s", exc.code)
        return False
    except Exception:
        logger.debug("HTTP /shutdown probe failed", exc_info=True)
        return False


def _daemon_http_alive() -> bool:
    """False only when ``/health`` is refused (nothing listens any more).

    Any HTTP response — including 5xx — and even a timeout means a
    listener still owns the port, so the daemon has not finished closing.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{DAEMON_HTTP_PORT}/health", timeout=1,
        ):
            return True
    except urllib.error.HTTPError:
        return True
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError):
            return False
        return True
    except ConnectionRefusedError:
        return False
    except Exception:
        return True


def _wait_for_daemon_exit(timeout_s: float) -> bool:
    """Poll until ``/health`` is refused AND no daemon PID remains.

    Returns True when the daemon is fully gone, False when ``timeout_s``
    elapsed first. Uses the module-level ``_sleep``/``_monotonic`` hooks
    so tests can run the chain without wall-clock waits.
    """
    deadline = _monotonic() + timeout_s
    while True:
        if not _daemon_http_alive() and not find_daemon_pids():
            return True
        if _monotonic() >= deadline:
            return False
        _sleep(_POLL_INTERVAL_S)


def _signal_pids(pids: set[int], sig: signal.Signals, log: Callable[[str], None]) -> None:
    protected = _protected_pids()
    for pid in sorted(pids):
        if pid in protected:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            log(f"{sig.name} target pid {pid} already gone")
        except PermissionError:
            log(f"{sig.name} target pid {pid} not permitted")


def stop_daemon(*, log: Callable[[str], None] | None = None) -> dict:
    """Stop the Cortex daemon: graceful first, escalate only when needed.

    Order (D2):

    1. ``POST /shutdown`` carrying ``X-Cortex-Auth-Token`` read from the
       token file.
    2. If accepted, poll ``/health`` for connection-refused (and the PID
       set for emptiness) for up to :data:`GRACEFUL_SHUTDOWN_TIMEOUT_S`.
       No signal is sent while the daemon is still finishing its recap /
       DB close inside that window.
    3. Only then SIGTERM every PID from :func:`find_daemon_pids` (the
       daemon's signal handler runs the same graceful chain).
    4. Wait :data:`SIGTERM_GRACE_S`; SIGKILL any survivor last.

    Returns ``{"status": "stopped", "method": ...}`` or
    ``{"status": "not_running"}`` when nothing was there to stop.
    """
    emit = log or (lambda message: logger.info("%s", message))
    token = _read_auth_token()
    if not token:
        emit("auth token unavailable; graceful /shutdown will be rejected")
    accepted = _request_graceful_shutdown(token)
    if accepted:
        emit("daemon accepted POST /shutdown; waiting for graceful exit")
        if _wait_for_daemon_exit(GRACEFUL_SHUTDOWN_TIMEOUT_S):
            return {"status": "stopped", "method": "graceful"}
        emit(
            f"daemon still alive {GRACEFUL_SHUTDOWN_TIMEOUT_S:.0f}s after "
            "accepting shutdown; escalating to SIGTERM"
        )

    pids = find_daemon_pids()
    if not pids:
        if accepted:
            return {"status": "stopped", "method": "graceful"}
        return {"status": "not_running"}

    _signal_pids(pids, signal.SIGTERM, emit)
    emit(f"sent SIGTERM to pids: {sorted(pids)}")
    if _wait_for_daemon_exit(SIGTERM_GRACE_S):
        return {"status": "stopped", "method": "sigterm"}

    survivors = find_daemon_pids()
    if survivors:
        _signal_pids(survivors, signal.SIGKILL, emit)
        emit(f"sent SIGKILL to pids: {sorted(survivors)}")
    return {"status": "stopped", "method": "sigkill"}


def _stop_daemon() -> dict:
    """Stop the Cortex daemon (handler entry point)."""
    return stop_daemon()


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
            # D16: this endpoint is unauthenticated (it is the extension's
            # liveness poll), so it must not leak the checkout location
            # or the interpreter path to any localhost origin.
            running = _is_daemon_running()
            pid = _find_daemon_pid() if running else None
            self._send_json({
                "daemon_running": running,
                "pid": pid,
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

    def log_message(self, format: str, *args: object) -> None:
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
