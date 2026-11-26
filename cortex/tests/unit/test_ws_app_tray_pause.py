"""WebSocket-mode tray Pause reaches the daemon and follows its quiet state.

In the source-mode shell (``CortexApp``), the tray's Pause/Resume item
only flipped a local ``_paused`` flag: the daemon kept sensing and kept
sending interventions to the browser and editor while the desktop simply
dropped them, and nothing mirrored QUIET_MODE_STATE onto the tray, so a
pause made from the dashboard capsule or an extension left the tray
offering "Pause" and a resume elsewhere left the dashboard frozen. The
in-process controller already routes the tray through
``set_quiet_mode``; this aligns the WebSocket host with it.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Any  # noqa: E402

import pytest  # noqa: E402

from cortex.apps.desktop_shell.main import CortexApp  # noqa: E402


class _Tray:
    def __init__(self) -> None:
        self.paused: bool | None = None

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def set_quiet_mode_kind(self, kind: str) -> None:
        self.set_paused(kind == "pause")

    def update_state(self, *_args: Any) -> None:
        pass


class _Bridge:
    def __init__(self) -> None:
        self.toggles: list[tuple[str, str]] = []
        self.settings: list[dict[str, Any]] = []

    def send_quiet_mode_toggle(
        self, kind: str, duration_minutes: int = 0, *, source: str = "settings_sync",
    ) -> None:
        self.toggles.append((kind, source))

    def send_settings(self, settings: dict[str, Any]) -> None:
        self.settings.append(settings)


class _Dashboard:
    def __init__(self) -> None:
        self.states: list[str] = []

    def update_state(self, payload: dict[str, Any]) -> None:
        self.states.append(str(payload.get("state")))


@pytest.fixture()
def app() -> CortexApp:
    instance = CortexApp()
    instance._tray = _Tray()  # type: ignore[assignment]
    instance._bridge = _Bridge()  # type: ignore[assignment]
    instance._dashboard = _Dashboard()  # type: ignore[assignment]
    instance._overlay = None
    return instance


def test_tray_pause_and_resume_reach_the_daemon(app: CortexApp) -> None:
    app._toggle_pause()
    app._on_quiet_mode_state({"kind": "pause"})
    app._toggle_pause()

    assert app._bridge.toggles == [("pause", "tray"), ("off", "tray")]  # type: ignore[union-attr]


def test_quiet_state_from_elsewhere_drives_the_tray_and_the_flag(app: CortexApp) -> None:
    app._on_quiet_mode_state({"kind": "pause"})
    assert app._tray.paused is True  # type: ignore[union-attr]
    app._toggle_pause()
    assert app._bridge.toggles == [("off", "tray")]  # type: ignore[union-attr]

    app._toggle_pause()
    app._on_quiet_mode_state({"kind": "pause"})
    # Resumed from the dashboard or an extension.
    app._on_quiet_mode_state({"kind": "off"})
    app._on_state_update({"state": "FLOW"})
    assert app._dashboard.states == ["FLOW"]  # type: ignore[union-attr]
    assert app._tray.paused is False  # type: ignore[union-attr]


def test_turn_off_for_session_holds_until_resume(app: CortexApp) -> None:
    app._disable_for_session()
    app._on_quiet_mode_state({"kind": "off"})
    assert app._tray.paused is True  # type: ignore[union-attr]
    app._on_state_update({"state": "HYPER"})
    assert app._dashboard.states == []  # type: ignore[union-attr]

    app._toggle_pause()
    app._on_quiet_mode_state({"kind": "off"})
    app._on_state_update({"state": "FLOW"})
    assert app._dashboard.states == ["FLOW"]  # type: ignore[union-attr]
