"""Closing a proposal in suggest_only mode must free the intervention slot.

In the default ``suggest_only`` mode a delivered proposal sets
``_active_intervention_id`` but registers no ``RestoreManager`` session:
that only happens after a verified workspace receipt. When the user then
dismisses (or engages) the proposal, ``RestoreManager.dismiss`` finds no
session and returns ``None``, and ``_handle_user_action`` returns early on
``outcome is None``. The slot stays occupied, so every later standard and
special proposal is refused ("Don't stack interventions") for the rest of the
session, no INTERVENTION_RESTORE reaches the clients, and helpfulness
tracking for the dismissed proposal is never closed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
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
    instance._ws_server.send_message = AsyncMock(return_value=1)
    instance._ws_server.send_restore = AsyncMock(return_value=1)
    instance._consent_ladder.record_rejection = AsyncMock(return_value=None)
    instance._policy_lifecycle.observe_intervention = AsyncMock(return_value=None)
    try:
        yield instance
    finally:
        settings_mod.get_config.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["dismissed", "engaged"])
async def test_closing_a_proposal_frees_the_slot(daemon: Any, action: str) -> None:
    assert daemon.intervention_execution_mode == "suggest_only"
    iid = "iv-proposal-only"
    daemon._active_intervention_id = iid
    daemon._active_plan = SimpleNamespace(intervention_id=iid, micro_steps=[])
    assert daemon._restore_manager.get_active(iid) is None  # proposal, no effect

    await daemon._handle_user_action({"intervention_id": iid, "action": action})

    assert daemon._active_intervention_id is None, "later proposals stay blocked"
    assert daemon._active_plan is None
    daemon._ws_server.send_restore.assert_awaited()


@pytest.mark.asyncio
async def test_a_transaction_that_may_carry_an_effect_keeps_the_slot(daemon: Any) -> None:
    # Once an authorization was consumed, only receipts and the exact
    # restore path may close it; a dismissal must not relabel it.
    iid = "iv-applying"
    daemon._active_intervention_id = iid
    daemon._active_plan = SimpleNamespace(intervention_id=iid, micro_steps=[])
    daemon._transaction_coordinator.abandon = AsyncMock(return_value=False)
    daemon._transaction_coordinator.get_transaction = AsyncMock(
        return_value=SimpleNamespace(state="applying")
    )

    await daemon._handle_user_action({"intervention_id": iid, "action": "dismissed"})

    assert daemon._active_intervention_id == iid
    daemon._ws_server.send_restore.assert_not_awaited()


@pytest.mark.asyncio
async def test_closing_a_proposal_ends_its_helpfulness_tracking(daemon: Any) -> None:
    iid = "iv-helpfulness"
    daemon._active_intervention_id = iid
    daemon._active_plan = SimpleNamespace(intervention_id=iid, micro_steps=[])
    daemon._close_helpfulness_tracking = AsyncMock(return_value=None)

    await daemon._handle_user_action({"intervention_id": iid, "action": "dismissed"})

    daemon._close_helpfulness_tracking.assert_awaited_once_with(iid, "dismissed")
