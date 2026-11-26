"""The SETTINGS_SYNC re-broadcast must carry the daemon's real quiet state.

``apply_settings`` ends by re-broadcasting ``dict(settings)`` — the raw
client payload — "with the values the daemon actually applied". Two
quiet-mode cases break that promise:

* While paused, a Settings Apply carries ``quiet_mode: false`` (the dialog
  always includes the checkbox). The daemon deliberately keeps the pause,
  yet the echo tells every client ``quiet_mode: false``.
* A partial payload such as the browser's launch-time
  ``{webcam_enabled: true}`` has no ``quiet_mode`` key at all, and the
  browser's SETTINGS_SYNC handler does ``quietMode =
  Boolean(msg.payload.quiet_mode)``, so the echo clears its quiet flag while
  the daemon is still snoozed.

Either way the browser stops honouring quiet (its OS-notification/badge
gate reads ``quietMode``) and its view of the daemon is wrong until the
next QUIET_MODE_STATE.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


@pytest.fixture()
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    settings_mod.get_config.cache_clear()
    from cortex.services.runtime_daemon import CortexDaemon

    d = CortexDaemon(config=settings_mod.get_config())
    d._ws_server.send_message = AsyncMock(return_value=1)  # type: ignore[method-assign]
    d._broadcast_quiet_mode_state = AsyncMock(return_value=None)  # type: ignore[method-assign]
    yield d
    settings_mod.get_config.cache_clear()


def _capture_rebroadcast(daemon: Any) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def _broadcast(settings: dict[str, Any]) -> int:
        sent.append(dict(settings))
        return 1

    daemon._ws_server.broadcast_settings = _broadcast
    return sent


def test_browser_settings_handler_coerces_a_missing_quiet_key_to_false() -> None:
    # Pins the consumer behaviour the daemon must account for.
    source = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "browser_extension"
        / "background.ts"
    ).read_text(encoding="utf-8")
    assert "quietMode = Boolean(msg.payload.quiet_mode);" in source


@pytest.mark.asyncio
async def test_rebroadcast_while_paused_reports_quiet(daemon) -> None:  # type: ignore[no-untyped-def]
    await daemon.set_quiet_mode("pause", source="tray")
    sent = _capture_rebroadcast(daemon)

    await daemon.apply_settings({"quiet_mode": False, "cooldown_seconds": 45})

    assert daemon._quiet_mode_kind == "pause"  # the pause was kept ...
    assert sent and sent[-1].get("quiet_mode") is True  # ... and must be reported


@pytest.mark.asyncio
async def test_settings_apply_with_quiet_checked_keeps_an_indefinite_pause(daemon) -> None:  # type: ignore[no-untyped-def]
    # The pause's own SETTINGS_SYNC carries ``quiet_mode: true``; the dialog
    # mirrors it into its checkbox and sends it back on the next Apply. That
    # bare boolean turned the pause into a timed snooze (and resumed capture).
    await daemon.set_quiet_mode("pause", source="tray")
    sent = _capture_rebroadcast(daemon)

    await daemon.apply_settings(
        {"quiet_mode": True, "quiet_duration_minutes": 30, "cooldown_seconds": 45}
    )

    assert daemon._quiet_mode_kind == "pause"
    assert daemon._quiet_mode_ends_at is None
    assert sent and sent[-1].get("quiet_mode_kind") == "pause"


@pytest.mark.asyncio
async def test_settings_apply_still_snoozes_and_clears_outside_a_pause(daemon) -> None:  # type: ignore[no-untyped-def]
    sent = _capture_rebroadcast(daemon)

    await daemon.apply_settings({"quiet_mode": True, "quiet_duration_minutes": 20})
    assert daemon._quiet_mode_kind == "snooze_15"
    assert sent[-1].get("quiet_mode") is True

    await daemon.apply_settings({"quiet_mode": False})
    assert daemon._quiet_mode_kind == "off"
    assert sent[-1].get("quiet_mode") is False


@pytest.mark.asyncio
async def test_partial_settings_rebroadcast_keeps_an_active_snooze(daemon) -> None:  # type: ignore[no-untyped-def]
    await daemon.set_quiet_mode("snooze_15", duration_minutes=15, source="popup")
    sent = _capture_rebroadcast(daemon)

    await daemon.apply_settings({"webcam_enabled": False})

    assert daemon._quiet_mode_kind == "snooze_15"
    assert sent and sent[-1].get("quiet_mode") is True
