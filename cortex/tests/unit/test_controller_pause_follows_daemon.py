"""The in-process controller's pause flag follows the daemon's quiet state.

``CortexAppController._paused`` decides two things: which way the tray's
Pause/Resume item toggles, and whether STATE_UPDATE frames and
interventions reach the dashboard and tray at all. It was only ever
flipped by the tray itself, while a pause or resume can also come from
the dashboard capsule, the browser popup, the VS Code status bar, a
Cmd+Shift+/ shortcut or a timed quiet mode decaying. After any of those:

* pausing from the tray and resuming anywhere else left the dashboard
  frozen on its last reading, because every later state frame was
  dropped as "paused";
* the tray item's title (which follows the daemon since 499f491) and
  its action disagreed: it read "Resume" and requested another pause,
  or read "Pause" and requested a resume.

"Turn off suggestions this session" is not a daemon quiet kind, so it
keeps the desktop paused through a later ``kind: off`` broadcast until
the user picks Resume.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import Any  # noqa: E402

import pytest  # noqa: E402

from cortex.apps.desktop_shell.controller import CortexAppController  # noqa: E402


class _Tray:
    def __init__(self) -> None:
        self.paused: bool | None = None
        self.kind: str | None = None
        self.states: list[str] = []

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def set_quiet_mode_kind(self, kind: str) -> None:
        # Mirrors tray.py: the daemon's kind also retitles Pause/Resume.
        self.kind = kind
        self.set_paused(kind == "pause")

    def update_state(self, state: str, *_args: Any) -> None:
        self.states.append(state)


class _Dashboard:
    def __init__(self) -> None:
        self.states: list[str] = []

    def update_state(self, payload: dict[str, Any]) -> None:
        self.states.append(str(payload.get("state")))


@pytest.fixture()
def ctrl() -> CortexAppController:
    controller = CortexAppController.__new__(CortexAppController)
    controller._paused = False
    controller._tray = _Tray()  # type: ignore[assignment]
    controller._dashboard = _Dashboard()  # type: ignore[assignment]
    controller._overlay = None
    controller._suggestion_review = None  # type: ignore[attr-defined]
    controller._daemon = None
    controller._daemon_loop = None
    requests: list[str] = []
    controller.request_quiet_mode = (  # type: ignore[method-assign]
        lambda kind, **_kw: requests.append(kind)
    )
    controller.requests = requests  # type: ignore[attr-defined]
    return controller


def _broadcast(ctrl: CortexAppController, kind: str) -> None:
    ctrl._on_quiet_mode_state_to_tray({"kind": kind})


def test_resume_elsewhere_unfreezes_the_dashboard(ctrl: CortexAppController) -> None:
    ctrl._toggle_pause()
    assert ctrl.requests == ["pause"]  # type: ignore[attr-defined]
    _broadcast(ctrl, "pause")

    # Resumed from the dashboard, the popup or VS Code.
    _broadcast(ctrl, "off")
    ctrl._on_state_update({"state": "FLOW", "confidence": 0.8})

    assert ctrl._dashboard.states == ["FLOW"]  # type: ignore[union-attr]
    assert ctrl._tray.states == ["FLOW"]  # type: ignore[union-attr]


def test_tray_click_does_what_its_title_says(ctrl: CortexAppController) -> None:
    # Paused from the dashboard: the tray reads "Resume" ...
    _broadcast(ctrl, "pause")
    assert ctrl._tray.paused is True  # type: ignore[union-attr]
    # ... so clicking it resumes.
    ctrl._toggle_pause()
    assert ctrl.requests == ["off"]  # type: ignore[attr-defined]

    # Resumed elsewhere: the tray reads "Pause", so clicking it pauses.
    _broadcast(ctrl, "off")
    assert ctrl._tray.paused is False  # type: ignore[union-attr]
    ctrl._toggle_pause()
    assert ctrl.requests == ["off", "pause"]  # type: ignore[attr-defined]


def test_turn_off_for_session_survives_a_quiet_off_until_resume(
    ctrl: CortexAppController,
) -> None:
    ctrl._disable_for_session()
    # A snooze ending, or any other surface, broadcasts ``off``.
    _broadcast(ctrl, "off")
    ctrl._on_state_update({"state": "HYPER"})
    assert ctrl._dashboard.states == []  # type: ignore[union-attr]
    assert ctrl._tray.paused is True  # type: ignore[union-attr]

    # The user picks Resume; the daemon confirms with ``off``.
    ctrl._toggle_pause()
    assert ctrl.requests == ["off"]  # type: ignore[attr-defined]
    _broadcast(ctrl, "off")
    ctrl._on_state_update({"state": "FLOW"})
    assert ctrl._dashboard.states == ["FLOW"]  # type: ignore[union-attr]
    assert ctrl._tray.paused is False  # type: ignore[union-attr]
