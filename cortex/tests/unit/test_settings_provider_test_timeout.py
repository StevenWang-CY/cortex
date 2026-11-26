"""Settings → "Test connection" must not say "Testing…" forever.

The click sets the pill to "Testing…" and emits ``test_provider_requested``.
Nothing ever answers when no session is running (the in-process controller
returns early without a daemon) or in the WebSocket host (the signal is not
connected there at all), so the pill claimed a test was in flight for the
rest of the app's life. The daemon caps its own probe at 5 s; past a bounded
wait the pill now says that no answer came.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell import settings as settings_mod


@pytest.fixture()
def dialog(monkeypatch: pytest.MonkeyPatch) -> Iterator[settings_mod.SettingsDialog]:
    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(mac_native, "apply_unified_titlebar", lambda *a, **kw: False)
    monkeypatch.setattr(settings_mod, "_PROVIDER_TEST_TIMEOUT_MS", 50, raising=False)
    widget = settings_mod.SettingsDialog()
    try:
        yield widget
    finally:
        widget.deleteLater()


def _pump(seconds: float) -> None:
    app = QApplication.instance()
    assert app is not None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_unanswered_test_stops_claiming_to_be_in_flight(
    dialog: settings_mod.SettingsDialog,
) -> None:
    dialog._on_test_provider_clicked()
    assert dialog._provider_status_pill.text() == "Testing…"

    _pump(0.5)

    text = dialog._provider_status_pill.text()
    assert text != "Testing…"
    assert "no answer" in text.lower()


def test_a_real_result_is_not_overwritten_by_the_timeout(
    dialog: settings_mod.SettingsDialog,
) -> None:
    dialog._on_test_provider_clicked()
    dialog.apply_provider_test_result(
        {"provider": "bedrock", "ok": True, "latency_ms": 142, "error": None}
    )

    _pump(0.5)

    assert dialog._provider_status_pill.text() == "Bedrock ✓ 142 ms"
