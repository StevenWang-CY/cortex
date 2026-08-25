"""Desktop action requests must cross the exact transaction boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SuggestedAction,
    UIPlan,
)
from cortex.libs.schemas.intervention_transaction import (
    InterventionApplyCommand,
    InterventionLifecycleState,
)
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import ConsentPolicy
from cortex.services.intervention_engine.transaction import (
    InterventionTransactionCoordinator,
    build_action_manifest,
)
from cortex.services.runtime_daemon import CortexDaemon


class _RecordingWS:
    def __init__(self, recipients: int = 1) -> None:
        self.recipients = recipients
        self.commands: list[InterventionApplyCommand] = []

    async def send_apply_command(
        self,
        command: InterventionApplyCommand,
    ) -> int:
        self.commands.append(command)
        return self.recipients

    async def dispatch_apply_command(
        self,
        command: InterventionApplyCommand,
    ) -> SimpleNamespace:
        self.commands.append(command)
        return SimpleNamespace(
            expected_targets=1,
            attempted_targets=1 if self.recipients else 0,
            delivered_targets=self.recipients,
        )

    async def send_restore_command(self, _command: Any) -> int:
        return 1


def _valid_action() -> dict[str, Any]:
    return {
        "action_id": "act-123",
        "action_type": "open_url",
        "label": "Open exact reference",
        "reason": "Keep relevant context nearby",
        "target": "https://example.com/reference",
    }


async def _daemon(*, recipients: int = 1) -> CortexDaemon:
    clock = FakeClock(
        wall_unix_ms=1_900_000_000_000,
        mono_ns=5_000_000_000,
        _boot_id=UUID("00000000-0000-0000-0000-000000000061"),
    )
    policy = ConsentPolicy()
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=policy, clock=clock),
        clock=clock,
        execution_mode="authorized",
    )
    plan = InterventionPlan(
        intervention_id="iv_active",
        level="overlay_only",
        situation_summary="A noisy tab is open.",
        headline="Review the tab suggestion",
        primary_focus="Current task",
        micro_steps=["Review the proposed action"],
        ui_plan=UIPlan(show_overlay=True, intervention_type="overlay_only"),
        suggested_actions=[SuggestedAction.model_validate(_valid_action())],
        consent_level="preview",
    )
    manifest = build_action_manifest(
        plan,
        [],
        consent_policy=policy,
        clock=clock,
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)

    daemon = CortexDaemon.__new__(CortexDaemon)
    daemon.config = SimpleNamespace(
        intervention=SimpleNamespace(execution_mode="authorized"),
    )
    daemon._clock = clock
    daemon._transaction_coordinator = coordinator
    daemon._ws_server = _RecordingWS(recipients=recipients)
    daemon._active_intervention_id = "iv_active"
    daemon._recorder = type("_R", (), {"append": lambda *_a, **_k: None})()
    daemon._helpfulness = type(
        "_H",
        (),
        {"record_rating": lambda *_a, **_k: None},
    )()
    return daemon


@pytest.mark.asyncio
async def test_missing_config_fails_closed() -> None:
    daemon = CortexDaemon.__new__(CortexDaemon)
    daemon._ws_server = _RecordingWS()
    daemon._active_intervention_id = "iv_active"
    assert await daemon.dispatch_intervention_action(
        "iv_active",
        _valid_action(),
    ) == 0
    assert daemon._ws_server.commands == []


@pytest.mark.asyncio
async def test_desktop_gesture_issues_exact_apply_command() -> None:
    daemon = await _daemon()
    assert await daemon.dispatch_intervention_action(
        "iv_active",
        _valid_action(),
    ) == 1
    [command] = daemon._ws_server.commands
    assert command.authorization.source_surface == "desktop"
    assert command.authorization.authorized_action_ids == ("act-123",)
    assert command.actions == (command.manifest.body.actions[0],)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", ["iv_OLD", "__pending__", None])
async def test_stale_or_pending_desktop_gesture_is_rejected(
    active: str | None,
) -> None:
    daemon = await _daemon()
    daemon._active_intervention_id = active
    assert await daemon.dispatch_intervention_action(
        "iv_active",
        _valid_action(),
    ) == 0
    assert daemon._ws_server.commands == []


@pytest.mark.asyncio
async def test_invalid_or_non_manifest_action_is_rejected() -> None:
    daemon = await _daemon()
    assert await daemon.dispatch_intervention_action(
        "iv_active",
        {"action_type": "definitely_not_a_real_type"},
    ) == 0
    irreversible = {
        "action_id": "timer",
        "action_type": "start_timer",
        "label": "Start timer",
        "reason": "Take a pause",
    }
    assert await daemon.dispatch_intervention_action(
        "iv_active",
        irreversible,
    ) == 0
    assert daemon._ws_server.commands == []


@pytest.mark.asyncio
async def test_tampered_desktop_presentation_cannot_authorize_same_action_id() -> None:
    daemon = await _daemon()
    tampered = {
        **_valid_action(),
        "label": "Open a different production dashboard",
        "reason": "Different displayed effect",
    }
    assert await daemon.dispatch_intervention_action(
        "iv_active",
        tampered,
    ) == 0
    assert daemon._ws_server.commands == []


@pytest.mark.asyncio
async def test_no_executor_persists_failed_consumed_grant() -> None:
    daemon = await _daemon(recipients=0)
    assert await daemon.dispatch_intervention_action(
        "iv_active",
        _valid_action(),
    ) == 0
    [command] = daemon._ws_server.commands
    transaction = await daemon._transaction_coordinator.get_transaction(
        "iv_active"
    )
    assert transaction is not None
    assert transaction.state == InterventionLifecycleState.FAILED.value
    assert any(
        entry.authorization.authorization_id
        == command.authorization.authorization_id
        for entry in transaction.authorizations
    )


@pytest.mark.asyncio
async def test_legacy_browser_facade_is_transactional() -> None:
    daemon = await _daemon()
    assert await daemon.dispatch_action_to_browser(
        "iv_active",
        _valid_action(),
    ) == 1
    assert len(daemon._ws_server.commands) == 1


@pytest.mark.asyncio
async def test_non_desktop_peer_cannot_forge_dispatch_request() -> None:
    daemon = await _daemon()
    await daemon._handle_user_action({
        "action_id": "act-123",
        "action_type": "open_url",
        "intervention_id": "iv_active",
        "request_dispatch": True,
        "_source_client_type": "chrome",
    })
    assert daemon._ws_server.commands == []


@pytest.mark.asyncio
async def test_legacy_desktop_request_is_lifted_into_exact_authority() -> None:
    daemon = await _daemon()
    await daemon._handle_user_action({
        "action_id": "act-123",
        "action_type": "open_url",
        "intervention_id": "iv_active",
        "request_dispatch": True,
        "_source_client_type": "desktop",
        "action": _valid_action(),
    })
    [command] = daemon._ws_server.commands
    assert command.authorization.authorized_action_ids == ("act-123",)
