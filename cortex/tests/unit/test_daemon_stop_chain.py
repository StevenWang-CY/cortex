"""D1 / D2 / D16 — the daemon stop chain: who may be signalled, and when.

D1: ``lsof -ti tcp:<port>`` lists BOTH ends of every socket, and the old
``pgrep -f`` patterns were unanchored, so the kill chain SIGTERM/SIGKILLed
Chrome's network process, the VS Code extension host, the WS-mode desktop
shell (all *clients* of 9473), the native host itself
(``.../MacOS/CortexNativeHost`` contains ``.../MacOS/Cortex``) and any
editor with ``run_dev.py`` open. The discovery now uses ``-sTCP:LISTEN``,
anchored patterns, a validated pidfile, and never includes self/parent.

D2: both killers POSTed ``/shutdown`` without the capability token (always
401), so graceful shutdown was dead code and SIGKILL followed 3 s later,
interrupting the recap and the SQLite close. The chain now sends the
token, waits for ``/health`` to be refused (up to 20 s) before SIGTERM,
and only SIGKILLs after a further grace.

All process/network interaction is faked: no subprocess runs, no socket
is opened, and ``os.kill`` is intercepted.
"""

from __future__ import annotations

import json
import os
import re
import signal
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex.libs.schemas.native_messaging import validate_native_response
from cortex.scripts import install_launcher, launcher_agent, native_host

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProcessTable:
    """Scripted ``subprocess.run`` for ``lsof`` / ``pgrep`` / ``ps``."""

    def __init__(
        self,
        *,
        listen: dict[int, set[int]] | None = None,
        pgrep: dict[str, set[int]] | None = None,
        cmdlines: dict[int, str] | None = None,
    ) -> None:
        self.listen = listen or {}
        self.pgrep = pgrep or {}
        self.cmdlines = cmdlines or {}
        self.calls: list[list[str]] = []

    @staticmethod
    def _pid_lines(pids: set[int]) -> str:
        return "".join(f"{pid}\n" for pid in sorted(pids))

    def run(self, cmd, **_kwargs):  # noqa: ANN001
        self.calls.append(list(cmd))
        if cmd[0] == "lsof":
            port = int(cmd[2].split(":")[1])
            return SimpleNamespace(
                returncode=0, stdout=self._pid_lines(self.listen.get(port, set())), stderr=""
            )
        if cmd[0] == "pgrep":
            assert cmd[1] == "-f"
            return SimpleNamespace(
                returncode=0, stdout=self._pid_lines(self.pgrep.get(cmd[2], set())), stderr=""
            )
        if cmd[0] == "ps":
            pid = int(cmd[2])
            cmdline = self.cmdlines.get(pid)
            if cmdline is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=cmdline + "\n", stderr="")
        raise AssertionError(f"unexpected subprocess invocation: {cmd}")


@pytest.fixture()
def no_pidfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the pidfile resolver at a path that does not exist."""
    path = tmp_path / "daemon.pid"
    monkeypatch.setattr(launcher_agent, "_daemon_pid_path", lambda: str(path))
    return path


# ---------------------------------------------------------------------------
# D1 — discovery
# ---------------------------------------------------------------------------


def test_lsof_probe_requests_listening_owners_only(
    monkeypatch: pytest.MonkeyPatch, no_pidfile: Path
) -> None:
    table = _FakeProcessTable(listen={9473: {4242}, 9472: {4242}})
    monkeypatch.setattr(launcher_agent.subprocess, "run", table.run)

    assert launcher_agent.find_daemon_pids() == {4242}
    lsof_calls = [call for call in table.calls if call[0] == "lsof"]
    assert lsof_calls, "expected lsof probes"
    for call in lsof_calls:
        assert call[1] == "-ti"
        assert call[3] == "-sTCP:LISTEN", call
    assert {call[2] for call in lsof_calls} == {"tcp:9473", "tcp:9472"}


def test_pid_set_never_includes_self_or_parent(
    monkeypatch: pytest.MonkeyPatch, no_pidfile: Path
) -> None:
    """Even if a probe returned us or Chrome (our parent), they are dropped."""
    me, parent = os.getpid(), os.getppid()
    table = _FakeProcessTable(
        listen={9473: {me, parent, 5150}, 9472: {me}},
        pgrep={launcher_agent.PGREP_APP_PATTERN: {parent, 5150}},
    )
    monkeypatch.setattr(launcher_agent.subprocess, "run", table.run)

    assert launcher_agent.find_daemon_pids() == {5150}


@pytest.mark.parametrize(
    ("cmdline", "matches"),
    [
        ("/Applications/Cortex.app/Contents/MacOS/Cortex", True),
        ("/Applications/Cortex.app/Contents/MacOS/Cortex -psn_0_123", True),
        # The native host lives in the same directory with a longer name.
        ("/Applications/Cortex.app/Contents/MacOS/CortexNativeHost chrome-extension://abc/", False),
        ("/Applications/Cortex.app/Contents/MacOS/CortexNativeHost", False),
        # A translocated copy is deliberately NOT matched by pgrep (the
        # pidfile covers it) — never guess on an unanchored path.
        ("/private/var/folders/x/AppTranslocation/y/d/Cortex.app/Contents/MacOS/Cortex", False),
    ],
)
def test_app_pgrep_pattern_is_anchored(cmdline: str, matches: bool) -> None:
    pattern = re.compile(launcher_agent.PGREP_APP_PATTERN)
    assert bool(pattern.search(cmdline)) is matches


@pytest.mark.parametrize(
    ("cmdline", "matches"),
    [
        ("/Users/dev/proj/.venv/bin/python -m cortex.scripts.run_dev", True),
        ("python3.11 -m cortex.scripts.run_dev --profile-startup", True),
        ("/usr/bin/vim cortex/scripts/run_dev.py", False),
        ("grep -r cortex.scripts.run_dev .", False),
        # The WS-mode desktop shell is a CLIENT and must never be killed.
        ("/Users/dev/proj/.venv/bin/python -m cortex.apps.desktop_shell.main", False),
        ("/Users/dev/proj/.venv/bin/python -m cortex.scripts.launcher_agent", False),
        ("/Users/dev/proj/.venv/bin/python -m cortex.scripts.run_dev_helper", False),
    ],
)
def test_dev_pgrep_pattern_is_anchored(cmdline: str, matches: bool) -> None:
    pattern = re.compile(launcher_agent.PGREP_DEV_PATTERN)
    assert bool(pattern.search(cmdline)) is matches


def test_pidfile_pid_is_trusted_only_when_alive_and_daemon_shaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pidfile = tmp_path / "daemon.pid"
    monkeypatch.setattr(launcher_agent, "_daemon_pid_path", lambda: str(pidfile))
    alive: set[int] = set()
    monkeypatch.setattr(launcher_agent, "_pid_alive", lambda pid: pid in alive)

    translocated = "/private/var/folders/x/AppTranslocation/y/d/Cortex.app/Contents/MacOS/Cortex"
    table = _FakeProcessTable(
        cmdlines={
            7001: translocated,
            7002: "/Applications/Cortex.app/Contents/MacOS/CortexNativeHost chrome-extension://abc/",
            7003: "/usr/bin/vim cortex/scripts/run_dev.py",
            7004: "/Users/dev/.venv/bin/python -m cortex.apps.desktop_shell.main --in-process",
        }
    )
    monkeypatch.setattr(launcher_agent.subprocess, "run", table.run)

    def scenario(pid: int, *, is_alive: bool) -> set[int]:
        pidfile.write_text(f"{pid}\n", encoding="utf-8")
        alive.clear()
        if is_alive:
            alive.add(pid)
        return launcher_agent.find_daemon_pids()

    # A translocated GUI daemon is only discoverable through the pidfile.
    assert scenario(7001, is_alive=True) == {7001}
    # The in-process desktop shell wrote the pidfile itself: trusted.
    assert scenario(7004, is_alive=True) == {7004}
    # Never the native host, even when the pidfile claims it.
    assert scenario(7002, is_alive=True) == set()
    # A recycled PID now belonging to an editor: not daemon-shaped.
    assert scenario(7003, is_alive=True) == set()
    # A stale pidfile (process gone) is ignored.
    assert scenario(7001, is_alive=False) == set()
    # Our own PID and our parent's are never candidates.
    assert scenario(os.getpid(), is_alive=True) == set()
    assert scenario(os.getppid(), is_alive=True) == set()
    # Garbage content is ignored.
    pidfile.write_text("not-a-pid\n", encoding="utf-8")
    assert launcher_agent.find_daemon_pids() == set()


def test_native_host_discovery_delegates_to_shared_implementation(
    monkeypatch: pytest.MonkeyPatch, no_pidfile: Path
) -> None:
    table = _FakeProcessTable(listen={9473: {6001}})
    monkeypatch.setattr(launcher_agent.subprocess, "run", table.run)
    assert native_host._find_all_daemon_pids() == {6001}


def test_install_launcher_probe_is_listen_only_and_skips_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=f"{os.getpid()}\n{os.getppid()}\n8080\n", stderr="")

    monkeypatch.setattr(install_launcher.subprocess, "run", fake_run)
    assert install_launcher._find_launcher_pid() == 8080
    assert calls == [["lsof", "-ti", f"tcp:{install_launcher.LAUNCHER_PORT}", "-sTCP:LISTEN"]]


# ---------------------------------------------------------------------------
# D2 — graceful stop ordering
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _World:
    """Fake daemon + clock driving ``launcher_agent.stop_daemon``.

    ``exit_at`` — virtual time at which the daemon finishes on its own
    after accepting ``/shutdown`` (``None`` = never). ``sigterm_exit_delay``
    — how long after SIGTERM the daemon disappears (``None`` = ignores
    SIGTERM). SIGKILL always removes the process.
    """

    def __init__(
        self,
        *,
        accept_shutdown: bool,
        pids: set[int],
        exit_at: float | None = None,
        sigterm_exit_delay: float | None = None,
    ) -> None:
        self.accept_shutdown = accept_shutdown
        self.pids = set(pids)
        self.exit_at = exit_at
        self.sigterm_exit_delay = sigterm_exit_delay
        self.t = 0.0
        self.gone_at: float | None = None
        self.events: list[tuple[float, str]] = []
        self.shutdown_headers: dict[str, str] = {}
        self.health_polls = 0
        self.kills: list[tuple[float, int, signal.Signals]] = []

    # -- daemon liveness ---------------------------------------------------
    def _gone(self) -> bool:
        if not self.pids:
            return True
        if self.gone_at is not None and self.t >= self.gone_at:
            return True
        return self.exit_at is not None and self.t >= self.exit_at

    # -- hooks -------------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def monotonic(self) -> float:
        return self.t

    def urlopen(self, request, timeout=None):  # noqa: ANN001
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        if url.endswith("/shutdown"):
            assert isinstance(request, urllib.request.Request)
            assert request.get_method() == "POST"
            self.shutdown_headers = {k.lower(): v for k, v in request.header_items()}
            self.events.append((self.t, "POST /shutdown"))
            if not self.accept_shutdown:
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
            return _Response(200)
        if url.endswith("/health"):
            self.health_polls += 1
            self.events.append((self.t, "GET /health"))
            if self._gone():
                raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
            return _Response(200)
        raise AssertionError(f"unexpected url {url}")

    def find_pids(self) -> set[int]:
        return set() if self._gone() else set(self.pids)

    def kill(self, pid: int, sig: int) -> None:
        signum = signal.Signals(sig)
        self.events.append((self.t, f"{signum.name} {pid}"))
        self.kills.append((self.t, pid, signum))
        if pid not in self.find_pids():
            raise ProcessLookupError(pid)
        if signum == signal.SIGKILL:
            self.gone_at = self.t
        elif signum == signal.SIGTERM and self.sigterm_exit_delay is not None:
            self.gone_at = self.t + self.sigterm_exit_delay


@pytest.fixture()
def world_factory(monkeypatch: pytest.MonkeyPatch):
    def _install(world: _World, *, token: str = "a" * 64) -> _World:
        monkeypatch.setattr(launcher_agent, "_sleep", world.sleep)
        monkeypatch.setattr(launcher_agent, "_monotonic", world.monotonic)
        monkeypatch.setattr(launcher_agent, "find_daemon_pids", world.find_pids)
        monkeypatch.setattr(launcher_agent, "_read_auth_token", lambda: token)
        monkeypatch.setattr(urllib.request, "urlopen", world.urlopen)
        monkeypatch.setattr(os, "kill", world.kill)
        return world

    return _install


def _names(world: _World) -> list[str]:
    return [name for _t, name in world.events]


def test_graceful_stop_sends_token_and_never_signals(world_factory) -> None:  # noqa: ANN001
    world = world_factory(_World(accept_shutdown=True, pids={4242}, exit_at=3.0))

    result = launcher_agent.stop_daemon()

    assert result == {"status": "stopped", "method": "graceful"}
    assert world.shutdown_headers["x-cortex-auth-token"] == "a" * 64
    assert world.kills == [], "a daemon that exits gracefully must not be signalled"
    assert world.health_polls >= 1
    assert _names(world)[0] == "POST /shutdown"
    assert all(name == "GET /health" for name in _names(world)[1:])


def test_sigterm_only_after_the_graceful_window(world_factory) -> None:  # noqa: ANN001
    world = world_factory(
        _World(accept_shutdown=True, pids={4242}, exit_at=None, sigterm_exit_delay=1.0)
    )

    result = launcher_agent.stop_daemon()

    assert result == {"status": "stopped", "method": "sigterm"}
    assert [sig for _t, _pid, sig in world.kills] == [signal.SIGTERM]
    sigterm_at, pid, _ = world.kills[0]
    assert pid == 4242
    assert sigterm_at >= launcher_agent.GRACEFUL_SHUTDOWN_TIMEOUT_S >= 20.0
    # Every health poll happened before the signal.
    polls = [t for t, name in world.events if name == "GET /health" and t < sigterm_at]
    assert len(polls) >= int(launcher_agent.GRACEFUL_SHUTDOWN_TIMEOUT_S / 0.5) - 1


def test_sigkill_only_after_sigterm_grace(world_factory) -> None:  # noqa: ANN001
    world = world_factory(
        _World(accept_shutdown=True, pids={4242}, exit_at=None, sigterm_exit_delay=None)
    )

    result = launcher_agent.stop_daemon()

    assert result == {"status": "stopped", "method": "sigkill"}
    assert [sig for _t, _pid, sig in world.kills] == [signal.SIGTERM, signal.SIGKILL]
    sigterm_at = world.kills[0][0]
    sigkill_at = world.kills[1][0]
    assert sigterm_at >= launcher_agent.GRACEFUL_SHUTDOWN_TIMEOUT_S
    assert sigkill_at - sigterm_at >= launcher_agent.SIGTERM_GRACE_S


def test_rejected_shutdown_falls_back_to_sigterm_without_the_long_wait(
    world_factory,  # noqa: ANN001
) -> None:
    """A 401 (stale token) must not stall for 20 s: the daemon's own
    SIGTERM handler runs the same graceful chain."""
    world = world_factory(
        _World(accept_shutdown=False, pids={4242}, sigterm_exit_delay=0.5), token=""
    )

    result = launcher_agent.stop_daemon()

    assert result == {"status": "stopped", "method": "sigterm"}
    assert [sig for _t, _pid, sig in world.kills] == [signal.SIGTERM]
    assert world.kills[0][0] == 0.0
    assert "x-cortex-auth-token" not in world.shutdown_headers


def test_nothing_running_reports_not_running_and_signals_nobody(
    world_factory,  # noqa: ANN001
) -> None:
    world = world_factory(_World(accept_shutdown=False, pids=set()))

    assert launcher_agent.stop_daemon() == {"status": "not_running"}
    assert world.kills == []


def test_protected_pids_are_never_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    launcher_agent._signal_pids(
        {os.getpid(), os.getppid(), 9999}, signal.SIGTERM, lambda _m: None
    )
    assert sent == [(9999, signal.SIGTERM)]


def test_native_host_stop_maps_result_onto_closed_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def fake_stop(*, log=None):  # noqa: ANN001
        assert log is native_host.log
        seen.append({"status": "stopped", "method": "graceful"})
        return seen[-1]

    monkeypatch.setattr(launcher_agent, "stop_daemon", fake_stop)
    result = native_host.stop_daemon()
    assert result == {"status": "stopped"}
    validated = validate_native_response({"command": "stop", **result})
    assert validated.status == "stopped"
    assert seen, "native host must delegate to the shared chain"


# ---------------------------------------------------------------------------
# D16 — /status must not leak the checkout or interpreter location
# ---------------------------------------------------------------------------


def test_launcher_status_does_not_expose_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher_agent, "_is_daemon_running", lambda: False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), launcher_agent.LauncherHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as resp:
            payload = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert set(payload) == {"daemon_running", "pid"}
    assert payload == {"daemon_running": False, "pid": None}
