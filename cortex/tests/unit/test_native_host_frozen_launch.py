"""Release audit — the frozen ``CortexNativeHost`` must never take the dev path.

``native_host.launch_daemon`` only recognised ``/Applications/Cortex.app``
and otherwise fell through to the source-checkout branch, which opens
Terminal.app running ``<sys.executable> -m cortex.scripts.run_dev``. Inside
the PyInstaller binary ``sys.executable`` *is* ``CortexNativeHost``, so a
user who kept the app elsewhere got a Terminal window running
``CortexNativeHost -m cortex.scripts.run_dev`` and a cryptic failure.

The packaged host now asks LaunchServices for the bundle by id
(``open -b com.cortex.daemon``) and otherwise returns a structured error
telling the user to install into /Applications. The dev path is kept only
when not frozen.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cortex.libs.schemas.native_messaging import validate_native_response
from cortex.scripts import native_host


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_host.time, "sleep", lambda _s: None)


def test_frozen_host_without_app_in_applications_opens_bundle_id_or_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_host, "is_daemon_running", lambda *_a, **_k: False)
    monkeypatch.setattr(native_host, "_is_installed_app", lambda: False)
    monkeypatch.setattr(native_host, "_is_frozen", lambda: True)
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        assert cmd[0] != "osascript", "frozen host must never open Terminal with the dev path"
        return SimpleNamespace(returncode=1, stdout="", stderr="Unable to find application")

    monkeypatch.setattr(native_host.subprocess, "run", fake_run)

    result = native_host.launch_daemon()

    assert calls == [["open", "-b", native_host.CORTEX_BUNDLE_ID]]
    assert native_host.CORTEX_BUNDLE_ID == "com.cortex.daemon"
    assert result["status"] == "error"
    assert "/Applications" in result["error"]
    validated = validate_native_response({"command": "launch", **result})
    assert validated.status == "error"


def test_frozen_host_bundle_id_launch_waits_for_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_host, "_is_installed_app", lambda: False)
    monkeypatch.setattr(native_host, "_is_frozen", lambda: True)
    running = iter([False, False, True])
    monkeypatch.setattr(native_host, "is_daemon_running", lambda *_a, **_k: next(running, True))

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        assert cmd == ["open", "-b", native_host.CORTEX_BUNDLE_ID]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(native_host.subprocess, "run", fake_run)

    assert native_host.launch_daemon() == {"status": "launched"}


def test_source_host_keeps_the_terminal_dev_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_host, "_is_installed_app", lambda: False)
    monkeypatch.setattr(native_host, "_is_frozen", lambda: False)
    running = iter([False, True])
    monkeypatch.setattr(native_host, "is_daemon_running", lambda *_a, **_k: next(running, True))
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(native_host.subprocess, "run", fake_run)

    assert native_host.launch_daemon() == {"status": "launched"}
    assert calls and calls[0][0] == "osascript"
    assert "cortex.scripts.run_dev" in calls[0][2]
    assert not any(call[:2] == ["open", "-b"] for call in calls)


def test_installed_app_path_is_preferred_even_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_host, "_is_installed_app", lambda: True)
    monkeypatch.setattr(native_host, "_is_frozen", lambda: True)
    running = iter([False, True])
    monkeypatch.setattr(native_host, "is_daemon_running", lambda *_a, **_k: next(running, True))
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(native_host.subprocess, "run", fake_run)

    assert native_host.launch_daemon() == {"status": "launched"}
    assert calls == [["open", native_host.CORTEX_APP_PATH]]
