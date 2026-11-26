"""macOS notification action buttons must reach a live daemon.

``send_intervention_notification`` posts every banner in the
``cortex_intervention`` category, which renders "Open" and "Snooze 15"
buttons. The delegate forwards a click to ``_user_action_handler``, which
only ``set_user_action_handler`` assigns, and nothing in the product called
it: both buttons were dead, so "Snooze 15" never snoozed anything.

Separately, ``CortexDaemon.stop`` latches ``mark_shutting_down()`` for the
process. The in-process desktop shell hot-restarts by composing a *new*
``CortexDaemon`` in the same process (Stop session, then Start), and nothing
cleared the latch, so after the first Stop every later click was discarded
and foreground banners were suppressed for the rest of the app's life.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cortex.libs.utils import macos_notifications as mn


@pytest.fixture()
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    settings_mod.get_config.cache_clear()
    mn.set_user_action_handler(None)
    mn.reset_shutdown_state_for_tests()
    from cortex.services.runtime_daemon import CortexDaemon

    instance = CortexDaemon(config=settings_mod.get_config())
    instance._ws_server.send_message = AsyncMock(return_value=1)
    instance._broadcast_quiet_mode_state = AsyncMock(return_value=None)
    try:
        yield instance
    finally:
        mn.set_user_action_handler(None)
        mn.reset_shutdown_state_for_tests()
        settings_mod.get_config.cache_clear()


async def _settle(daemon: Any) -> None:
    # Wait only for the click handlers; a snooze also schedules its 15-minute
    # decay task, which must not be awaited here.
    for _ in range(20):
        await asyncio.sleep(0)
    clicks = [
        task
        for task in list(getattr(daemon, "_background_tasks", ()) or ())
        if task.get_name() == "cortex-notification-action"
    ]
    if clicks:
        await asyncio.wait_for(asyncio.gather(*clicks, return_exceptions=True), 5.0)
    for task in list(getattr(daemon, "_background_tasks", ()) or ()):
        task.cancel()


@pytest.mark.asyncio
async def test_start_binding_registers_a_receiver_and_rearms_the_latch(daemon: Any) -> None:
    mn.mark_shutting_down()  # what the previous in-process session's stop() left
    daemon._bind_os_notification_actions()

    assert mn._user_action_handler is not None, "Open/Snooze buttons have no receiver"
    assert mn.is_shutting_down() is False


@pytest.mark.asyncio
async def test_snooze_button_click_from_the_appkit_thread_snoozes(daemon: Any) -> None:
    daemon._bind_os_notification_actions()
    handler = mn._user_action_handler
    assert handler is not None

    clicker = threading.Thread(target=handler, args=("iv-1", "snooze"))
    clicker.start()
    clicker.join()
    await _settle(daemon)

    assert daemon._quiet_mode_kind == "snooze_15"
    assert daemon._quiet_mode_source == "os_notification"


@pytest.mark.asyncio
async def test_open_button_click_asks_the_desktop_to_raise(daemon: Any) -> None:
    daemon._bind_os_notification_actions()
    handler = mn._user_action_handler
    assert handler is not None

    handler("iv-2", "open")
    await _settle(daemon)

    calls = [call.args[0] for call in daemon._ws_server.send_message.await_args_list]
    assert "RAISE_DASHBOARD" in calls
    assert daemon._quiet_mode_kind == "off"


@pytest.mark.asyncio
async def test_click_during_stop_is_not_dispatched(daemon: Any) -> None:
    daemon._bind_os_notification_actions()
    handler = mn._user_action_handler
    assert handler is not None
    daemon._stop_started = True

    handler("iv-3", "snooze")
    await _settle(daemon)

    assert daemon._quiet_mode_kind == "off"
