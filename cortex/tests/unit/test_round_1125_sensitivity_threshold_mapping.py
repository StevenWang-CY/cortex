"""The Settings sensitivity slider must move the trigger threshold around its default.

The desktop dialog sends ``entry_threshold = 0.85 + (3 - sensitivity) * 0.05``
in *every* Apply (slider 1..5, neutral 3 → 0.85), a scale built for the
retired ``StateConfig.entry_threshold`` default of 0.85. ``apply_settings``
writes that value straight into ``intervention.overlay_threshold``, whose
configured default is 0.70 and which is the base of the HYPER trigger gate.

So pressing Apply for any unrelated setting with the slider untouched raised
the trigger threshold from 0.70 to 0.85, and even the *most* sensitive slider
position (0.75) was less sensitive than the untouched default.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


@pytest.fixture()
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    settings_mod.get_config.cache_clear()
    from cortex.services.runtime_daemon import CortexDaemon

    instance = CortexDaemon(config=settings_mod.get_config())
    instance._ws_server.broadcast_settings = AsyncMock(return_value=1)
    instance._ws_server.send_message = AsyncMock(return_value=1)
    try:
        yield instance
    finally:
        settings_mod.get_config.cache_clear()


def _dialog_payload(sensitivity: int) -> dict[str, Any]:
    # Exactly what ``SettingsDialog.get_settings`` emits for these two keys.
    return {
        "sensitivity": sensitivity,
        "entry_threshold": 0.85 + (3 - sensitivity) * 0.05,
    }


@pytest.mark.asyncio
async def test_neutral_slider_apply_keeps_the_default_trigger_threshold(daemon: Any) -> None:
    default = float(daemon.config.intervention.overlay_threshold)

    await daemon.apply_settings(_dialog_payload(3))

    assert daemon.config.intervention.overlay_threshold == pytest.approx(default)


@pytest.mark.asyncio
async def test_most_sensitive_slider_is_at_least_as_sensitive_as_the_default(daemon: Any) -> None:
    default = float(daemon.config.intervention.overlay_threshold)

    await daemon.apply_settings(_dialog_payload(5))

    assert daemon.config.intervention.overlay_threshold < default


@pytest.mark.asyncio
async def test_slider_offsets_do_not_accumulate_across_applies(daemon: Any) -> None:
    default = float(daemon.config.intervention.overlay_threshold)

    for _ in range(3):
        await daemon.apply_settings(_dialog_payload(1))

    assert daemon.config.intervention.overlay_threshold == pytest.approx(default + 0.10)
    await daemon.apply_settings(_dialog_payload(3))
    assert daemon.config.intervention.overlay_threshold == pytest.approx(default)
