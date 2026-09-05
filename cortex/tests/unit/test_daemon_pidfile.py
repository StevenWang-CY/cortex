"""D1 — the daemon pidfile handshake between the daemon and its launchers."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from cortex.scripts import launcher_agent
from cortex.services.launcher.daemon_process import (
    DAEMON_PIDFILE_NAME,
    daemon_pidfile_path,
    read_daemon_pidfile,
    remove_daemon_pidfile,
    write_daemon_pidfile,
)


def test_write_read_remove_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "nested" / DAEMON_PIDFILE_NAME
    written = write_daemon_pidfile(target)
    assert written == target
    assert read_daemon_pidfile(target) == os.getpid()
    assert target.read_text(encoding="utf-8") == f"{os.getpid()}\n"
    if not sys.platform.startswith("win"):
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob(".daemon.pid.*")), "temp file must not linger"
    assert remove_daemon_pidfile(target) is True
    assert not target.exists()
    assert remove_daemon_pidfile(target) is False


def test_remove_refuses_to_delete_another_processes_pidfile(tmp_path: Path) -> None:
    target = tmp_path / DAEMON_PIDFILE_NAME
    write_daemon_pidfile(target, pid=99_999)
    assert read_daemon_pidfile(target) == 99_999
    assert remove_daemon_pidfile(target) is False, "our PID is not the one recorded"
    assert target.exists()
    assert remove_daemon_pidfile(target, pid=99_999) is True
    assert not target.exists()


def test_garbage_pidfile_reads_as_absent(tmp_path: Path) -> None:
    target = tmp_path / DAEMON_PIDFILE_NAME
    target.write_text("not a pid\n", encoding="utf-8")
    assert read_daemon_pidfile(target) is None
    target.write_text("0\n", encoding="utf-8")
    assert read_daemon_pidfile(target) is None
    assert read_daemon_pidfile(tmp_path / "missing.pid") is None


def test_daemon_and_launcher_agree_on_the_pidfile_location() -> None:
    """The launcher resolves the path without importing cortex; both must
    land on ``<config_dir>/daemon.pid`` (under the test HOME sandbox)."""
    assert Path(launcher_agent._daemon_pid_path()) == daemon_pidfile_path()  # noqa: SLF001
    assert daemon_pidfile_path().name == DAEMON_PIDFILE_NAME
    assert Path(launcher_agent._auth_token_path()).parent == daemon_pidfile_path().parent  # noqa: SLF001


def test_launcher_finds_daemon_through_the_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / DAEMON_PIDFILE_NAME
    write_daemon_pidfile(target, pid=31_337)
    monkeypatch.setattr(launcher_agent, "_daemon_pid_path", lambda: str(target))
    monkeypatch.setattr(launcher_agent, "_pid_alive", lambda pid: pid == 31_337)
    monkeypatch.setattr(
        launcher_agent,
        "_process_command_line",
        lambda pid: "/Users/dev/.venv/bin/python -m cortex.scripts.run_dev" if pid == 31_337 else None,
    )
    monkeypatch.setattr(launcher_agent, "_listening_pids", lambda _port: set())
    monkeypatch.setattr(launcher_agent, "_pgrep", lambda _pattern: set())
    assert launcher_agent.find_daemon_pids() == {31_337}
