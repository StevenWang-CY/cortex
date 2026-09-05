from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from cortex.scripts import install_native_host, launcher_agent, native_host


def test_find_python_rejects_external_runtime_for_app_bundle(monkeypatch):
    monkeypatch.delenv("CORTEX_NATIVE_HOST_PYTHON", raising=False)
    with pytest.raises(RuntimeError, match="CortexNativeHost"):
        install_native_host._find_python(project_root="/Applications/Cortex.app")


def test_find_python_prefers_dev_venv(monkeypatch):
    monkeypatch.delenv("CORTEX_NATIVE_HOST_PYTHON", raising=False)
    venv_suffix = os.path.join(".venv", "bin", "python")

    def fake_isfile(path: str) -> bool:
        return path.endswith(venv_suffix)

    def fake_access(path: str, _mode: int) -> bool:
        return path.endswith(venv_suffix)

    monkeypatch.setattr(install_native_host.os.path, "isfile", fake_isfile)
    monkeypatch.setattr(install_native_host.os, "access", fake_access)

    found = install_native_host._find_python()
    assert found.endswith(venv_suffix)


def test_launcher_launch_daemon_surfaces_open_failure(monkeypatch):
    monkeypatch.setattr(launcher_agent, "_is_daemon_running", lambda: False)
    monkeypatch.setattr(
        launcher_agent.os.path,
        "isdir",
        lambda path: path == launcher_agent.CORTEX_APP_PATH,
    )

    def fake_run(cmd, **_kwargs):
        assert cmd[0] == "open"
        return SimpleNamespace(returncode=1, stdout="", stderr="open failed")

    monkeypatch.setattr(launcher_agent.subprocess, "run", fake_run)

    result = launcher_agent._launch_daemon()
    assert result["status"] == "error"
    assert "open failed" in result["error"]


def test_launcher_pid_scan_includes_bundled_app(monkeypatch):
    """D1: the bundled app is found through an *anchored* ``pgrep`` pattern.

    The previous expectation enshrined the unanchored
    ``/Applications/Cortex.app/Contents/MacOS/Cortex`` pattern, which also
    matched ``.../MacOS/CortexNativeHost`` — i.e. the native host killed
    itself mid-reply.
    """
    monkeypatch.setattr(launcher_agent, "_pidfile_daemon_pid", lambda: None)

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["pgrep", "-f"] and cmd[2] == launcher_agent.PGREP_APP_PATTERN:
            return SimpleNamespace(returncode=0, stdout="1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launcher_agent.subprocess, "run", fake_run)

    assert launcher_agent.PGREP_APP_PATTERN == (
        r"^/Applications/Cortex\.app/Contents/MacOS/Cortex( |$)"
    )
    assert launcher_agent._find_all_daemon_pids() == {1234}


def test_native_host_launch_daemon_surfaces_open_failure(monkeypatch):
    monkeypatch.setattr(native_host, "is_daemon_running", lambda *_, **__: False)
    monkeypatch.setattr(native_host, "_is_installed_app", lambda: True)

    def fake_run(cmd, **_kwargs):
        assert cmd[0] == "open"
        return SimpleNamespace(returncode=1, stdout="", stderr="open failed")

    monkeypatch.setattr(native_host.subprocess, "run", fake_run)

    result = native_host.launch_daemon()
    assert result["status"] == "error"
    assert "open failed" in result["error"]


def test_native_host_pid_scan_includes_bundled_app(monkeypatch):
    """D1: the native host shares the launcher's anchored discovery."""
    monkeypatch.setattr(launcher_agent, "_pidfile_daemon_pid", lambda: None)

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["pgrep", "-f"] and cmd[2] == launcher_agent.PGREP_APP_PATTERN:
            return SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(native_host.subprocess, "run", fake_run)

    assert native_host.CORTEX_APP_PATH == launcher_agent.CORTEX_APP_PATH
    assert native_host._find_all_daemon_pids() == {4321}
