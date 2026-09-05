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
from pathlib import Path

_LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "Cortex"
LOG_FILE = _LOG_DIRECTORY / "native-host.log"
_LOG_MAX_BYTES = 512 * 1024
_LOG_BACKUP_COUNT = 2


# P1 (audit Phase 4d): centralise the port literals so a future port
# migration only touches ``cortex/libs/config/ports.py``. Import is
# wrapped in try/except so source-mode diagnostic invocations remain usable
# even if the checkout is incomplete. Packaged releases execute this module
# inside the self-contained ``CortexNativeHost`` binary.
try:
    from cortex.libs.config.ports import HTTP_API_PORT, WEBSOCKET_PORT
except Exception:  # pragma: no cover - import-path dependent
    HTTP_API_PORT = 9472
    WEBSOCKET_PORT = 9473


def log(msg: str) -> None:
    """Append one bounded diagnostic line outside the signed app bundle.

    Native messaging starts a fresh host for every ``sendNativeMessage`` call.
    A persistent import failure therefore used to append full tracebacks forever
    (the v0.3.14 failure produced a 61 MB file). Keep two 512 KiB backups and
    never let logging interfere with the stdio protocol.
    """
    try:
        _LOG_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            _LOG_DIRECTORY.chmod(0o700)
        except OSError:
            pass
        if LOG_FILE.exists() and LOG_FILE.stat().st_size >= _LOG_MAX_BYTES:
            oldest = LOG_FILE.with_name(f"{LOG_FILE.name}.{_LOG_BACKUP_COUNT}")
            try:
                oldest.unlink()
            except FileNotFoundError:
                pass
            for index in range(_LOG_BACKUP_COUNT - 1, 0, -1):
                source = LOG_FILE.with_name(f"{LOG_FILE.name}.{index}")
                target = LOG_FILE.with_name(f"{LOG_FILE.name}.{index + 1}")
                try:
                    source.replace(target)
                except FileNotFoundError:
                    pass
            LOG_FILE.replace(LOG_FILE.with_name(f"{LOG_FILE.name}.1"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        try:
            LOG_FILE.chmod(0o600)
        except OSError:
            pass
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
        except Exception as drain_exc:
            log(f"oversized-payload drain failed: {drain_exc}")
        return b""
    return sys.stdin.buffer.read(length)


def send_message(msg: dict[str, object]) -> None:
    """Send a native messaging response to stdout."""
    encoded = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _native_error(
    error: object,
    *,
    request_command: str | None = None,
    detail: object | None = None,
) -> dict[str, object]:
    """Build a bounded canonical native-host error response."""

    response: dict[str, object] = {
        "command": "error",
        "status": "error",
        "error": str(error)[:4096] or "unknown_error",
    }
    if request_command:
        response["request_command"] = request_command[:64]
    if detail is not None:
        response["detail"] = str(detail)[:8192]
    return response


def _send_validated_response(response: dict[str, object]) -> None:
    """Validate against the Pydantic response union, then frame it.

    Keeping validation immediately adjacent to stdout ensures no dispatch
    branch can silently drift from the generated TypeScript contract.
    """

    from cortex.libs.schemas.native_messaging import validate_native_response

    validated = validate_native_response(response)
    send_message(validated.model_dump(mode="json", exclude_none=True))


def is_daemon_running(port: int = WEBSOCKET_PORT) -> bool:
    """Check if the Cortex daemon is already listening on its WebSocket port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False


CORTEX_APP_PATH = "/Applications/Cortex.app"
#: ``CFBundleIdentifier`` of Cortex.app (see ``cortex/scripts/cortex.spec``).
#: ``open -b`` resolves the bundle through LaunchServices wherever the
#: user actually put it, which is the only sane fallback for a packaged
#: host when the app is not at the canonical ``/Applications`` path.
CORTEX_BUNDLE_ID = "com.cortex.daemon"
#: How long a freshly opened Cortex.app gets to bring its in-process
#: daemon up (the desktop shell starts it lazily).
_APP_READY_TIMEOUT_S = 20.0
_NOT_INSTALLED_MESSAGE = (
    "Cortex.app is not installed in /Applications. Move Cortex.app into "
    "/Applications (or reinstall from the DMG) and try again."
)


def _is_installed_app() -> bool:
    """True when Cortex.app is installed in /Applications (DMG users)."""
    return os.path.isdir(CORTEX_APP_PATH)


def _is_frozen() -> bool:
    """True inside the PyInstaller-built ``CortexNativeHost`` binary."""
    return bool(getattr(sys, "frozen", False))


def _wait_for_daemon_ready(timeout_s: float) -> dict:
    """Poll the WS port until the daemon listens or ``timeout_s`` elapses."""
    polls = max(1, int(timeout_s / 0.5))
    for i in range(polls):
        time.sleep(0.5)
        if is_daemon_running():
            log(f"Daemon ready after {(i + 1) * 0.5}s")
            return {"status": "launched"}
    log(f"Daemon did not become ready in {timeout_s:.0f}s")
    return {
        "status": "timeout",
        "error": f"Daemon started but port {WEBSOCKET_PORT} not yet ready",
    }


def _open_app(argv: list[str], *, label: str) -> dict | None:
    """Run an ``open`` command; return an error envelope or ``None`` on success."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:
        log(f"{label} failed: {e}")
        return {"status": "error", "error": str(e)}
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        log(f"{label} failed rc={result.returncode} stderr={stderr}")
        return {"status": "error", "error": stderr or f"Failed to launch via {label}"}
    log(f"Launched daemon via {label}")
    return None


def launch_daemon() -> dict:
    """Launch the Cortex daemon as a detached background process.

    Three launch modes:

    * **DMG mode** — Cortex.app is installed in /Applications. Use
      ``open /Applications/Cortex.app`` so the bundled Python and
      in-process daemon start with the app's own TCC camera identity.
      This is the path end users hit after installing the DMG.
    * **Packaged host, app elsewhere** — the frozen ``CortexNativeHost``
      never takes the dev path (that would open Terminal running
      ``CortexNativeHost -m cortex.scripts.run_dev``, which cannot work).
      It asks LaunchServices for the bundle by id (``open -b``) and
      otherwise returns a structured error telling the user to install
      the app into /Applications.
    * **Dev mode** — source checkout, no installed .app. Run
      ``python -m cortex.scripts.run_dev`` via Terminal.app so the
      dev-checkout daemon inherits Terminal's camera permission.
    """
    if is_daemon_running():
        return {"status": "already_running"}

    # --- DMG path: open the installed .app ---------------------------------
    if _is_installed_app():
        failure = _open_app(["open", CORTEX_APP_PATH], label="open Cortex.app")
        if failure is not None:
            return failure
        return _wait_for_daemon_ready(_APP_READY_TIMEOUT_S)

    # --- Packaged host without /Applications/Cortex.app --------------------
    if _is_frozen():
        failure = _open_app(
            ["open", "-b", CORTEX_BUNDLE_ID],
            label=f"open -b {CORTEX_BUNDLE_ID}",
        )
        if failure is not None:
            return {"status": "error", "error": _NOT_INSTALLED_MESSAGE}
        return _wait_for_daemon_ready(_APP_READY_TIMEOUT_S)

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
                "osascript",
                "-e",
                f'tell application "Terminal" to do script "{cmd}"',
            ],
            capture_output=True,
            text=True,
            timeout=5,
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
    return _wait_for_daemon_ready(12.0)


def _find_all_daemon_pids() -> set[int]:
    """Find every PID that is provably the Cortex daemon (D1).

    Delegates to :func:`cortex.scripts.launcher_agent.find_daemon_pids`
    — the single dependency-free implementation shared by both
    launchers: listening socket owners only (``lsof -sTCP:LISTEN``),
    anchored ``pgrep`` patterns that can never match this host's own
    ``.../MacOS/CortexNativeHost`` argv, the daemon-written pidfile, and
    never our own PID or Chrome (our parent).
    """
    from cortex.scripts import launcher_agent

    return launcher_agent.find_daemon_pids()


def stop_daemon() -> dict:
    """Stop the Cortex daemon: graceful first, escalate only when needed (D2).

    Delegates to :func:`cortex.scripts.launcher_agent.stop_daemon`:
    ``POST /shutdown`` with ``X-Cortex-Auth-Token``, poll ``/health`` for
    connection-refused for up to 20 s, then SIGTERM, then (5 s later)
    SIGKILL. The native ``StopResponse`` schema is closed
    (``extra="forbid"``), so the launcher's ``method`` diagnostic is
    logged rather than returned.
    """
    from cortex.scripts import launcher_agent

    result = launcher_agent.stop_daemon(log=log)
    log(f"stop chain finished: {result}")
    # A daemon that was never running is "stopped" from the extension's
    # point of view; only an exception maps to ``error`` (see ``main``).
    return {"status": "stopped"}


def _get_auth_token_response() -> dict[str, object]:
    """Return the daemon's capability token to the extension.

    Loads or creates the token via :func:`cortex.libs.auth.load_or_create_token`.
    On import failure (e.g. running this script outside an installed
    venv) returns a structured error so the extension can degrade
    gracefully rather than blocking on a missing token.
    """
    try:
        from cortex.libs.auth import load_or_create_token

        return {
            "command": "get_auth_token",
            "status": "ok",
            "auth_token": load_or_create_token(),
        }
    except Exception as exc:  # pragma: no cover - import-path dependent
        return _native_error(
            f"auth_token_unavailable: {exc}",
            request_command="get_auth_token",
        )


def _read_auth_token() -> str:
    """Best-effort *pure* read of the capability token for HTTP header use.

    Used by the ``raise_dashboard`` branch to authenticate against
    ``POST /dashboard/raise`` (Phase 4b). Returns an empty string on
    failure — the daemon route will then return 401, which surfaces to
    the extension as ``{ok: false, error: "..."}``. We never propagate
    a stack trace into the response body.

    Never mints a token: if the file is absent the daemon has not started
    (it provisions the token on boot), so there is nothing to talk to
    anyway, and a host-minted token would race the daemon's own.
    """
    try:
        from cortex.libs.auth.local_token import load_token_or_none

        return load_token_or_none() or ""
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
                # auth.py only accepts ``X-Cortex-Auth-Token`` or
                # ``Authorization: Bearer``; the legacy
                # ``X-Cortex-Auth`` header silently failed authentication
                # and caused /dashboard/raise to bounce as 401.
                "X-Cortex-Auth-Token": _read_auth_token(),
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

        parsed = parse_native_message(raw)
        if parsed.error is not None:
            log(f"rejected: error={parsed.error} detail={parsed.detail}")
            _send_validated_response(_native_error(parsed.error, detail=parsed.detail))
            return

        msg = parsed.message
        assert msg is not None  # narrow for type-checkers
        log(f"received: command={msg.command}")

        if msg.command == "launch":
            result = {"command": "launch", **launch_daemon()}
        elif msg.command == "stop":
            result = {"command": "stop", **stop_daemon()}
        elif msg.command == "status":
            result = {
                "command": "status",
                "status": "running" if is_daemon_running() else "stopped",
            }
        elif msg.command == "get_auth_token":
            # F07b/F08: extension cannot read mode-0600 files directly;
            # the native host runs as the user and CAN. The browser↔host
            # boundary is already OS-authenticated (chrome.runtime.host
            # is provisioned per-profile), so returning the token here
            # does not widen the attack surface — it just reaches the
            # capability gates we added on WS SHUTDOWN and launcher /stop.
            result = _get_auth_token_response()
        elif msg.command == "raise_dashboard":
            dashboard_result = _raise_dashboard(msg.target)
            if dashboard_result.get("ok") is True:
                result = {
                    "command": "raise_dashboard",
                    "status": "ok",
                    "http_status": int(dashboard_result["status"]),
                }
            else:
                result = _native_error(
                    dashboard_result.get("error", "raise_dashboard_failed"),
                    request_command="raise_dashboard",
                )
        else:
            # Unreachable: the schema's discriminated union exhausts the
            # legitimate command set. Surfaced for defence in depth.
            result = _native_error(
                "unknown_command",
                request_command=str(msg.command),
            )

        # Never log the full response: the get_auth_token payload contains
        # the capability secret. Command/status are sufficient diagnostics.
        log(f"sending: command={result.get('command')} status={result.get('status')}")
        _send_validated_response(result)
    except Exception as e:
        log(f"CRASH: {traceback.format_exc()}")
        # Always try to send a response even on crash
        try:
            _send_validated_response(_native_error(e))
        except Exception:
            pass


if __name__ == "__main__":
    main()
