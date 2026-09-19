"""The Connections panel must not block the Qt main thread.

Every subprocess and network probe in ``connections.py`` ran synchronously on
the GUI thread. The worst case is the editor install —
``subprocess.run(..., timeout=30)`` invoked straight from a click — so a single
click could freeze the entire UI for half a minute. The
``"Installing into …"`` status written immediately before it could not even
repaint, because the event loop never got control back: the window simply went
unresponsive with no explanation.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture()
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def panel(qapp):
    from cortex.apps.desktop_shell.connections import ConnectionsPanel

    widget = ConnectionsPanel()
    try:
        yield widget
    finally:
        widget.deleteLater()


def _drain(qapp, predicate, timeout_s: float = 5.0) -> bool:
    """Pump the Qt event loop until *predicate* holds or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_editor_install_returns_immediately(
    panel, qapp, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """The click handler must hand back control long before the work finishes."""
    vsix = tmp_path / "cortex-somatic-1.0.0.vsix"
    vsix.write_bytes(b"vsix")
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.connections.Path.glob",
        lambda self, pattern: [vsix] if pattern.startswith("cortex-somatic") else [],
    )

    # The work releases itself on a timer rather than waiting for this test to
    # let it go. A regression that puts the call back on the Qt thread then
    # fails on the elapsed assertion in ~1.5 s instead of hanging until the
    # suite timeout.
    release = threading.Event()
    threading.Timer(1.5, release.set).start()

    def _slow_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        release.wait(timeout=10.0)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _slow_run)

    started = time.monotonic()
    panel._connect_editor("/usr/local/bin/code", "VS Code")
    elapsed = time.monotonic() - started

    # The blocking version only returned once subprocess.run had finished.
    assert elapsed < 1.0, f"click handler blocked for {elapsed:.2f}s"

    # And the pending status is visible while the work is still running, which
    # is the whole point — the old code could not repaint it.
    assert panel._status_model["VS Code"]["note"].startswith("Installing into")
    assert _drain(
        qapp,
        lambda: panel._status_model["VS Code"]["note"] == "Installed into VS Code.",
    ), f"status never settled: {panel._status_model['VS Code']}"


def test_editor_install_failure_reaches_the_card(
    panel, qapp, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """A worker exception must surface, not vanish into the thread pool."""
    vsix = tmp_path / "cortex-somatic-1.0.0.vsix"
    vsix.write_bytes(b"vsix")
    monkeypatch.setattr(
        "cortex.apps.desktop_shell.connections.Path.glob",
        lambda self, pattern: [vsix] if pattern.startswith("cortex-somatic") else [],
    )

    def _timeout(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="code", timeout=30)

    monkeypatch.setattr(subprocess, "run", _timeout)

    panel._connect_editor("/usr/local/bin/code", "VS Code")

    assert _drain(
        qapp,
        lambda: panel._status_model["VS Code"]["note"] == "Installation timed out.",
    ), f"failure never surfaced: {panel._status_model['VS Code']}"
