"""WP6 transactional intervention authority and recovery tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from cortex.application.clock import FakeClock
from cortex.libs.schemas.intervention import (
    AdapterCommand,
    InterventionPlan,
    SuggestedAction,
    UIPlan,
)
from cortex.libs.schemas.intervention_transaction import (
    ActionManifest,
    ActionReceipt,
    InterventionAuthorizationRequest,
    InterventionLifecycleState,
    InterventionReceiptBatch,
    ManifestAction,
    ReceiptPhase,
    ReceiptStatus,
    VerificationStatus,
)
from cortex.services.api_gateway.websocket_server import (
    WebSocketClient,
    WebSocketServer,
)
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import ConsentPolicy
from cortex.services.intervention_engine.executor import InterventionExecutor
from cortex.services.intervention_engine.transaction import (
    InterventionTransactionCoordinator,
    build_action_manifest,
)
from cortex.services.intervention_engine.transaction_store import (
    InMemoryInterventionTransactionStore,
    JsonInterventionTransactionStore,
)


def _clock() -> FakeClock:
    return FakeClock(
        wall_unix_ms=1_900_000_000_000,
        mono_ns=15_000_000_000,
        _boot_id=UUID("00000000-0000-0000-0000-000000000006"),
    )


def _plan(*, two_actions: bool = False) -> InterventionPlan:
    actions = [
        SuggestedAction(
            action_id="act_close",
            action_type="open_url",
            target="https://example.com/reference",
            label="Open the exact reference",
            reason="Keep the relevant reference nearby",
            reversible=True,
        )
    ]
    if two_actions:
        actions.append(
            SuggestedAction(
                action_id="act_open",
                action_type="search_error",
                target="TypeError exact synthetic query",
                label="Search the exact error",
                reason="Open a separate Cortex-owned result tab",
                reversible=True,
            )
        )
    return InterventionPlan(
        intervention_id="int_transaction",
        level="overlay_only",
        situation_summary="There are unrelated tabs in the current window.",
        headline="Review the proposed tab changes",
        primary_focus="The active task",
        micro_steps=["Review each proposed action"],
        ui_plan=UIPlan(show_overlay=True, intervention_type="overlay_only"),
        suggested_actions=actions,
        consent_level="preview",
    )


def _mixed_executor_plan() -> InterventionPlan:
    plan = _plan()
    plan.suggested_actions.append(
        SuggestedAction(
            action_id="act_editor",
            action_type="resume_last_active_file",
            target="/tmp/cortex-workspace/main.py:12",
            label="Resume the active file",
            reason="Return to the exact editor location",
            reversible=True,
        )
    )
    return plan


def _request(
    manifest: ActionManifest,
    action_ids: tuple[str, ...],
    clock: FakeClock,
    *,
    request_id: str = "req_1",
) -> InterventionAuthorizationRequest:
    return InterventionAuthorizationRequest(
        authorization_request_id=request_id,
        intervention_id=manifest.intervention_id,
        manifest_sha256=manifest.manifest_sha256,
        approved_action_ids=tuple(sorted(action_ids)),
        source_surface="browser",
        requested_at_unix_ms=clock.unix_ms(),
        requested_at_mono_ns=clock.monotonic_ns(),
        boot_id=clock.boot_id,
    )


def _receipt(
    *,
    manifest: ActionManifest,
    authorization_id: str,
    action_id: str,
    clock: FakeClock,
    phase: ReceiptPhase = ReceiptPhase.APPLY,
    status: ReceiptStatus = ReceiptStatus.SUCCEEDED,
    verification: VerificationStatus = VerificationStatus.VERIFIED,
    receipt_id: str | None = None,
    inverse: dict[str, Any] | None = None,
    attempt: int = 1,
) -> ActionReceipt:
    start_wall = clock.unix_ms()
    start_mono = clock.monotonic_ns()
    clock.advance(wall_ms=7, monotonic_ns=7_000_000)
    return ActionReceipt(
        **({"receipt_id": receipt_id} if receipt_id is not None else {}),
        intervention_id=manifest.intervention_id,
        authorization_id=authorization_id,
        manifest_sha256=manifest.manifest_sha256,
        action_id=action_id,
        phase=phase,
        attempt=attempt,
        idempotency_key=f"{authorization_id}:{action_id}:{phase.value}:{attempt}",
        status=status,
        started_at_unix_ms=start_wall,
        ended_at_unix_ms=clock.unix_ms(),
        started_at_mono_ns=start_mono,
        ended_at_mono_ns=clock.monotonic_ns(),
        duration_ms=7,
        boot_id=clock.boot_id,
        inverse_payload_json=(
            json.dumps(
                (
                    inverse
                    if inverse is not None
                    else {}
                ),
                sort_keys=True,
                separators=(",", ":"),
            )
            if inverse is not None
            or status
            in {ReceiptStatus.SUCCEEDED, ReceiptStatus.ALREADY_COMPLETE}
            else None
        ),
        verification=verification,
        after_fingerprint=(
            hashlib.sha256(
                f"{action_id}:{phase.value}:{attempt}".encode()
            ).hexdigest()
            if status
            in {ReceiptStatus.SUCCEEDED, ReceiptStatus.ALREADY_COMPLETE}
            else None
        ),
        error_code="adapter_failed" if status == ReceiptStatus.FAILED else None,
        error_message="synthetic failure" if status == ReceiptStatus.FAILED else None,
    )


async def _coordinator(
    *,
    clock: FakeClock,
    store: InMemoryInterventionTransactionStore | JsonInterventionTransactionStore | None = None,
    two_actions: bool = False,
) -> tuple[InterventionTransactionCoordinator, ActionManifest]:
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, clock=clock)
    manifest = build_action_manifest(
        _plan(two_actions=two_actions),
        [],
        consent_policy=policy,
        clock=clock,
    )
    coordinator = InterventionTransactionCoordinator(
        ladder,
        store=store,
        clock=clock,
        execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    return coordinator, manifest


def test_manifest_hash_rejects_tampering() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    manifest = build_action_manifest(
        _plan(), [], consent_policy=policy, clock=clock,
    )
    payload = manifest.model_dump(mode="json")
    body = json.loads(payload["canonical_json"])
    body["actions"][0]["capability"] = "search_error"
    payload["canonical_json"] = json.dumps(
        body, sort_keys=True, separators=(",", ":"),
    )
    with pytest.raises(ValidationError, match="manifest_sha256"):
        ActionManifest.model_validate(payload)


def test_manifest_is_stable_and_excludes_presentation_commands() -> None:
    from cortex.libs.schemas.intervention import AdapterCommand

    clock = _clock()
    policy = ConsentPolicy()
    plan = _plan()
    commands = [
        AdapterCommand(adapter="overlay", action="show_overlay"),
        AdapterCommand(adapter="editor", action="fold_except_current"),
    ]
    first = build_action_manifest(plan, commands, consent_policy=policy, clock=clock)
    second = build_action_manifest(plan, commands, consent_policy=policy, clock=clock)
    assert first.manifest_sha256 == second.manifest_sha256
    assert [action.capability for action in first.body.actions] == ["open_url"]
    assert any("fold_except_current" in warning for warning in plan.plan_warnings)


def test_irreversible_suggestions_remain_presentation_only() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    plan = _plan()
    plan.suggested_actions = [
        SuggestedAction(
            action_id="act_save",
            action_type="save_session",
            label="Save this session",
            reason="Keep a reference for later",
            reversible=False,
        ),
        SuggestedAction(
            action_id="act_copy",
            action_type="copy_to_clipboard",
            target="A short reminder",
            label="Copy reminder",
            reason="Keep the reminder nearby",
            reversible=False,
        ),
    ]
    manifest = build_action_manifest(
        plan,
        [],
        consent_policy=policy,
        clock=clock,
    )
    assert manifest.action_count == 0
    assert manifest.body.actions == ()
    assert any("save_session" in warning for warning in plan.plan_warnings)
    assert any("copy_to_clipboard" in warning for warning in plan.plan_warnings)


@pytest.mark.asyncio
async def test_semantically_inconsistent_manifest_is_rejected_before_delivery() -> None:
    clock = _clock()
    action = ManifestAction.from_parameters(
        action_id="invalid_close",
        ordinal=0,
        executor="browser",
        capability="open_url",
        parameters={},
        reverse_capability="close_created_tab",
        workspace_mutation=False,
        required_consent_level=2,
        source="system",
    )
    manifest = ActionManifest.create(
        intervention_id="int_invalid_semantics",
        actions=[action],
        created_at_unix_ms=clock.unix_ms(),
        created_at_mono_ns=clock.monotonic_ns(),
        boot_id=clock.boot_id,
    )
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=ConsentPolicy(), clock=clock),
        clock=clock,
        execution_mode="authorized",
    )
    with pytest.raises(ValueError, match="workspace classification"):
        await coordinator.register_proposal(manifest)


@pytest.mark.asyncio
async def test_exact_authorization_consumed_before_any_receipt() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    result = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    assert not hasattr(result, "reason_code")
    assert result.authorization.authorized_action_ids == ("act_close",)  # type: ignore[union-attr]
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.state == InterventionLifecycleState.APPLYING.value
    assert transaction.authorizations[0].state == "consumed"
    assert transaction.receipts == []


@pytest.mark.asyncio
async def test_adapter_boundary_is_unreachable_without_consumed_exact_auth() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, clock=clock)
    plan = _plan()
    manifest = build_action_manifest(
        plan, [], consent_policy=policy, clock=clock,
    )
    coordinator = InterventionTransactionCoordinator(
        ladder, clock=clock, execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    manifest_action = manifest.body.actions[0]
    effect = AdapterCommand(
        adapter=manifest_action.executor,
        action=manifest_action.capability,
        params=manifest_action.parameters,
    )
    system_action_id = manifest_action.action_id

    class _Adapter:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, action: str, params: dict[str, Any]) -> bool:
            self.calls += 1
            return True

    adapter = _Adapter()
    executor = InterventionExecutor(execution_mode="authorized")
    executor.register_adapter("browser", adapter)
    executor.set_authorization_verifier(coordinator.validate_consumed)

    denied = await executor.apply(plan, [effect])
    assert denied[0].reason == "exact_authorization_required"
    assert adapter.calls == 0

    command = await coordinator.authorize_and_consume(
        _request(manifest, (system_action_id,), clock),
        source_client_id="client_browser",
    )
    applied = await executor.apply_authorized(plan, command)  # type: ignore[arg-type]
    assert applied[0].success is True
    assert adapter.calls == 1

    fabricated = command.model_copy(  # type: ignore[union-attr]
        update={
            "authorization": command.authorization.model_copy(  # type: ignore[union-attr]
                update={"authorization_id": "authz_fabricated"}
            )
        }
    )
    rejected = await executor.apply_authorized(plan, fabricated)  # type: ignore[arg-type]
    assert rejected[0].reason == "authorization_not_consumed"
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_manifest_mismatch_and_action_injection_are_denied() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    mismatched = _request(manifest, ("act_close",), clock).model_copy(
        update={"manifest_sha256": "0" * 64}
    )
    result = await coordinator.authorize_and_consume(
        mismatched, source_client_id="client_browser",
    )
    assert result.reason_code == "manifest_mismatch"  # type: ignore[union-attr]

    injected = _request(
        manifest, ("act_not_in_manifest",), clock, request_id="req_2",
    )
    result = await coordinator.authorize_and_consume(
        injected, source_client_id="client_browser",
    )
    assert result.reason_code == "action_mismatch"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_suggest_only_never_issues_authority() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, clock=clock)
    manifest = build_action_manifest(_plan(), [], consent_policy=policy, clock=clock)
    coordinator = InterventionTransactionCoordinator(
        ladder, clock=clock, execution_mode="suggest_only",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    result = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    assert result.reason_code == "execution_mode_denied"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_duplicate_authorization_request_is_not_replayable() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    request = _request(manifest, ("act_close",), clock)
    first = await coordinator.authorize_and_consume(
        request, source_client_id="client_browser",
    )
    assert hasattr(first, "authorization")
    second = await coordinator.authorize_and_consume(
        request, source_client_id="client_browser",
    )
    assert second.reason_code == "transaction_closed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_receipt_source_must_own_the_manifest_executor() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    await coordinator.bind_dispatch_targets(
        command.authorization.authorization_id,  # type: ignore[union-attr]
        {"act_close": "client_browser"},
    )
    receipt = _receipt(
        manifest=manifest,
        authorization_id=command.authorization.authorization_id,  # type: ignore[union-attr]
        action_id="act_close",
        clock=clock,
        inverse={"url": "https://example.com"},
    )
    batch = InterventionReceiptBatch(
        intervention_id=manifest.intervention_id,
        manifest_sha256=manifest.manifest_sha256,
        authorization_id=command.authorization.authorization_id,  # type: ignore[union-attr]
        receipts=(receipt,),
    )
    with pytest.raises(ValueError, match="does not own"):
        await coordinator.record_receipts(
            batch,
            source_client_type="vscode",
            source_client_id="client_editor",
        )


@pytest.mark.asyncio
async def test_receipt_source_must_match_exact_bound_client() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="selected_browser",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "selected_browser"},
    )
    batch = InterventionReceiptBatch(
        intervention_id=manifest.intervention_id,
        manifest_sha256=manifest.manifest_sha256,
        authorization_id=auth_id,
        receipts=(
            _receipt(
                manifest=manifest,
                authorization_id=auth_id,
                action_id="act_close",
                clock=clock,
                inverse={"url": "https://example.com"},
            ),
        ),
    )
    with pytest.raises(ValueError, match="bound executor client"):
        await coordinator.record_receipts(
            batch,
            source_client_type="chrome",
            source_client_id="peer_browser_that_saw_state",
        )


@pytest.mark.asyncio
async def test_apply_dispatch_owner_cannot_be_rebound_after_first_binding() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="requesting_surface",
    )
    authorization_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        authorization_id,
        {"act_close": "browser_profile_a"},
    )

    await coordinator.bind_dispatch_targets(
        authorization_id,
        {"act_close": "browser_profile_a"},
    )
    with pytest.raises(ValueError, match="ownership is immutable"):
        await coordinator.bind_dispatch_targets(
            authorization_id,
            {"act_close": "browser_profile_b"},
        )


@pytest.mark.asyncio
async def test_workspace_receipt_requires_server_owned_source_and_observed_proof() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id, {"act_close": "browser_owner"},
    )
    invalid = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        inverse={"url": "https://example.com"},
    ).model_copy(
        update={
            "after_fingerprint": None,
            "source_client_id": "self-asserted",
        }
    )
    batch = InterventionReceiptBatch(
        intervention_id=manifest.intervention_id,
        manifest_sha256=manifest.manifest_sha256,
        authorization_id=auth_id,
        receipts=(invalid,),
    )
    with pytest.raises(ValueError, match="self-assert"):
        await coordinator.record_receipts(
            batch,
            source_client_type="chrome",
            source_client_id="browser_owner",
        )

    no_fingerprint = invalid.model_copy(
        update={"source_client_id": None},
    )
    with pytest.raises(ValueError, match="fingerprint"):
        await coordinator.record_receipts(
            batch.model_copy(update={"receipts": (no_fingerprint,)}),
            source_client_type="chrome",
            source_client_id="browser_owner",
        )


@pytest.mark.asyncio
async def test_multi_executor_apply_waits_for_every_bound_owner() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, clock=clock)
    manifest = build_action_manifest(
        _mixed_executor_plan(), [], consent_policy=policy, clock=clock,
    )
    coordinator = InterventionTransactionCoordinator(
        ladder, clock=clock, execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close", "act_editor"), clock),
        source_client_id="browser_owner",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "browser_owner", "act_editor": "editor_owner"},
    )

    browser_batch = InterventionReceiptBatch(
        intervention_id=manifest.intervention_id,
        manifest_sha256=manifest.manifest_sha256,
        authorization_id=auth_id,
        receipts=(
            _receipt(
                manifest=manifest,
                authorization_id=auth_id,
                action_id="act_close",
                clock=clock,
                inverse={"url": "https://example.com"},
            ),
        ),
    )
    state, compensation = await coordinator.record_receipts(
        browser_batch,
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLYING
    assert compensation is None

    editor_batch = InterventionReceiptBatch(
        intervention_id=manifest.intervention_id,
        manifest_sha256=manifest.manifest_sha256,
        authorization_id=auth_id,
        receipts=(
            _receipt(
                manifest=manifest,
                authorization_id=auth_id,
                action_id="act_editor",
                clock=clock,
                inverse={"priorPath": "/tmp/cortex-workspace/old.py"},
            ),
        ),
    )
    state, compensation = await coordinator.record_receipts(
        editor_batch,
        source_client_type="vscode",
        source_client_id="editor_owner",
    )
    assert state == InterventionLifecycleState.APPLIED
    assert compensation is None


@pytest.mark.asyncio
async def test_apply_dispatch_binding_requires_every_executor() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    manifest = build_action_manifest(
        _mixed_executor_plan(), [], consent_policy=policy, clock=clock,
    )
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=policy, clock=clock),
        clock=clock,
        execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close", "act_editor"), clock),
        source_client_id="browser_owner",
    )

    with pytest.raises(ValueError, match="action set"):
        await coordinator.bind_dispatch_targets(
            command.authorization.authorization_id,  # type: ignore[union-attr]
            {"act_close": "browser_owner"},
        )

@pytest.mark.asyncio
async def test_transport_preflights_and_binds_all_executors_before_send() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=policy, clock=clock),
        clock=clock,
        execution_mode="authorized",
    )
    manifest = build_action_manifest(
        _mixed_executor_plan(), [], consent_policy=policy, clock=clock,
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close", "act_editor"), clock),
        source_client_id="browser_owner",
    )

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    browser_socket = _Socket()
    editor_socket = _Socket()
    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    browser = WebSocketClient(
        "browser_owner",
        browser_socket,
        client_type="chrome",
        client_instance_id="browser_owner",
        authenticated=True,
    )
    server._clients[browser.client_id] = browser  # noqa: SLF001

    # Missing editor means no partial dispatch and no binding is persisted.
    assert await server.send_apply_command(command) == 0  # type: ignore[arg-type]
    assert browser_socket.sent == []
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.dispatch_history == []

    editor = WebSocketClient(
        "editor_owner",
        editor_socket,
        client_type="vscode",
        client_instance_id="editor_owner",
        authenticated=True,
    )
    server._clients[editor.client_id] = editor  # noqa: SLF001
    assert await server.send_apply_command(command) == 2  # type: ignore[arg-type]
    assert len(browser_socket.sent) == 1
    assert len(editor_socket.sent) == 1
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.dispatch_history[-1].action_client_instance_ids == {
        "act_close": "browser_owner",
        "act_editor": "editor_owner",
    }


@pytest.mark.asyncio
async def test_socket_send_failure_is_delivery_ambiguous_and_compensated() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )

    class _FailingSocket:
        async def send(self, _payload: str) -> None:
            raise ConnectionError("socket closed during write")

    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    server._clients["browser_socket"] = WebSocketClient(  # noqa: SLF001
        "browser_socket",
        _FailingSocket(),
        client_type="chrome",
        client_instance_id="browser_owner",
        authenticated=True,
    )
    report = await server.dispatch_apply_command(command)  # type: ignore[arg-type]
    assert report.expected_targets == 1
    assert report.attempted_targets == 1
    assert report.delivered_targets == 0

    compensation = await coordinator.compensate_partial_dispatch(
        command.authorization.authorization_id,  # type: ignore[union-attr]
        reason="socket_write_outcome_ambiguous",
    )
    assert compensation is not None
    assert compensation.actions[0].action_id == "act_close"
    assert (
        compensation.actions[0].owner_client_instance_id
        == "browser_owner"
    )


@pytest.mark.asyncio
async def test_inverse_wire_write_waits_for_inflight_forward_write() -> None:
    """Reset/restore cannot arrive before the apply it is compensating."""

    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )

    class _BlockingSocket:
        def __init__(self) -> None:
            self.types: list[str] = []
            self.apply_started = asyncio.Event()
            self.release_apply = asyncio.Event()
            self.restore_started = asyncio.Event()

        async def send(self, payload: str) -> None:
            message_type = str(json.loads(payload).get("type"))
            self.types.append(message_type)
            if message_type == "INTERVENTION_APPLY":
                self.apply_started.set()
                await self.release_apply.wait()
            elif message_type == "INTERVENTION_RESTORE":
                self.restore_started.set()

    socket = _BlockingSocket()
    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    server._clients["browser_socket"] = WebSocketClient(  # noqa: SLF001
        "browser_socket",
        socket,
        client_type="chrome",
        client_instance_id="browser_owner",
        authenticated=True,
    )

    apply_task = asyncio.create_task(
        server.dispatch_apply_command(command),  # type: ignore[arg-type]
    )
    await asyncio.wait_for(socket.apply_started.wait(), timeout=1.0)
    restores = await coordinator.request_restore_all(
        reason="system_cancelled",
    )
    assert len(restores) == 1
    restore_task = asyncio.create_task(
        server.send_restore_command(restores[0]),
    )
    await asyncio.sleep(0)
    assert not socket.restore_started.is_set()

    socket.release_apply.set()
    assert (await asyncio.wait_for(apply_task, timeout=1.0)).delivered_targets == 1
    assert await asyncio.wait_for(restore_task, timeout=1.0) == 1
    assert socket.types == ["INTERVENTION_APPLY", "INTERVENTION_RESTORE"]


@pytest.mark.asyncio
async def test_missing_apply_receipt_enters_exact_compensation_at_deadline() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    socket = _Socket()
    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    server.set_intervention_partial_dispatch_callback(
        coordinator.compensate_partial_dispatch
    )
    server._clients["browser_socket"] = WebSocketClient(  # noqa: SLF001
        "browser_socket",
        socket,
        client_type="chrome",
        client_instance_id="browser_owner",
        authenticated=True,
    )
    report = await server.dispatch_apply_command(command)  # type: ignore[arg-type]
    assert report.delivered_targets == 1
    clock.advance(
        wall_ms=command.authorization.ttl_ms,  # type: ignore[union-attr]
        monotonic_ns=(  # type: ignore[union-attr]
            command.authorization.ttl_ms * 1_000_000
        ),
    )
    await server._watch_intervention_receipt_deadline(command)  # type: ignore[arg-type]  # noqa: SLF001

    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.state == InterventionLifecycleState.RESTORING.value
    assert transaction.active_restore is not None
    assert transaction.active_restore.reason == "partial_compensation"
    assert len(socket.sent) == 3  # apply, exact restore, transaction state


@pytest.mark.asyncio
async def test_partial_delivery_compensates_reachable_owner_and_retries_peer() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=policy, clock=clock),
        clock=clock,
        execution_mode="authorized",
    )
    manifest = build_action_manifest(
        _mixed_executor_plan(), [], consent_policy=policy, clock=clock,
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close", "act_editor"), clock),
        source_client_id="browser_owner",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "browser_owner", "act_editor": "editor_owner"},
    )
    restore = await coordinator.compensate_partial_dispatch(
        auth_id,
        reason="editor socket failed after browser send",
    )
    assert restore is not None
    assert {action.executor for action in restore.actions} == {
        "browser",
        "editor",
    }

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    browser_socket = _Socket()
    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    server._clients["browser_owner"] = WebSocketClient(  # noqa: SLF001
        "browser_owner",
        browser_socket,
        client_type="chrome",
        client_instance_id="browser_owner",
        authenticated=True,
    )
    assert await server.send_restore_command(restore) == 1

    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.dispatch_history[-1].action_client_instance_ids == {
        "act_close": "browser_owner",
    }

    # An apply receipt already in flight cannot rewind RESTORING to APPLIED.
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=auth_id,
                    action_id="act_close",
                    clock=clock,
                    inverse={"url": "https://example.com/closed"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.RESTORING

    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=restore.restore_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=restore.restore_id,
                    action_id="act_close",
                    clock=clock,
                    phase=ReceiptPhase.COMPENSATE,
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.RESTORING

    editor_socket = _Socket()
    server._clients["editor_owner"] = WebSocketClient(  # noqa: SLF001
        "editor_owner",
        editor_socket,
        client_type="vscode",
        client_instance_id="editor_owner",
        authenticated=True,
    )
    assert await server.send_restore_command(restore) == 2
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.dispatch_history[-1].action_client_instance_ids == {
        "act_close": "browser_owner",
        "act_editor": "editor_owner",
    }
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=restore.restore_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=restore.restore_id,
                    action_id="act_editor",
                    clock=clock,
                    phase=ReceiptPhase.COMPENSATE,
                ),
            ),
        ),
        source_client_type="vscode",
        source_client_id="editor_owner",
    )
    assert state == InterventionLifecycleState.RESTORED


@pytest.mark.asyncio
async def test_restore_routes_only_to_original_stable_client_instance() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_profile_a",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "browser_profile_a"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=auth_id,
                    action_id="act_close",
                    clock=clock,
                    inverse={"url": "https://example.com/closed"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_profile_a",
    )
    assert state == InterventionLifecycleState.APPLIED
    restore = await coordinator.request_restore(
        manifest.intervention_id,
        reason="user_undo",
    )
    assert restore is not None
    assert restore.actions[0].owner_client_instance_id == "browser_profile_a"

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    wrong_socket = _Socket()
    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    server._clients["wrong_socket"] = WebSocketClient(  # noqa: SLF001
        "wrong_socket",
        wrong_socket,
        client_type="chrome",
        client_instance_id="browser_profile_b",
        authenticated=True,
    )
    assert await server.send_restore_command(restore) == 0
    assert wrong_socket.sent == []

    owner_socket = _Socket()
    server._clients["owner_socket"] = WebSocketClient(  # noqa: SLF001
        "owner_socket",
        owner_socket,
        client_type="chrome",
        client_instance_id="browser_profile_a",
        authenticated=True,
    )
    assert await server.send_restore_command(restore) == 1
    assert len(owner_socket.sent) == 1
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.dispatch_history[-1].action_client_instance_ids == {
        "act_close": "browser_profile_a",
    }


@pytest.mark.asyncio
async def test_restore_dispatch_binding_must_preserve_original_owner() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="requesting_surface",
    )
    authorization_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        authorization_id,
        {"act_close": "browser_profile_a"},
    )
    await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=authorization_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=authorization_id,
                    action_id="act_close",
                    clock=clock,
                    inverse={"tabId": 9},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_profile_a",
    )
    restore = await coordinator.request_restore(
        manifest.intervention_id,
        reason="user_undo",
    )
    assert restore is not None

    with pytest.raises(ValueError, match="invalid action set"):
        await coordinator.bind_dispatch_targets(
            restore.restore_id,
            {"act_close": "browser_profile_b"},
        )


@pytest.mark.asyncio
async def test_restore_supports_two_owners_for_the_same_executor() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock, two_actions=True)
    owner_by_action = {
        "act_close": "browser_profile_a",
        "act_open": "browser_profile_b",
    }
    for index, (action_id, owner) in enumerate(owner_by_action.items()):
        command = await coordinator.authorize_and_consume(
            _request(
                manifest,
                (action_id,),
                clock,
                request_id=f"req_owner_{index}",
            ),
            source_client_id=owner,
        )
        auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
        await coordinator.bind_dispatch_targets(
            auth_id,
            {action_id: owner},
        )
        state, _ = await coordinator.record_receipts(
            InterventionReceiptBatch(
                intervention_id=manifest.intervention_id,
                manifest_sha256=manifest.manifest_sha256,
                authorization_id=auth_id,
                receipts=(
                    _receipt(
                        manifest=manifest,
                        authorization_id=auth_id,
                        action_id=action_id,
                        clock=clock,
                        inverse={"ownedBy": owner},
                    ),
                ),
            ),
            source_client_type="chrome",
            source_client_id=owner,
        )
        assert state == InterventionLifecycleState.APPLIED

    restore = await coordinator.request_restore(
        manifest.intervention_id,
        reason="emergency_restore",
    )
    assert restore is not None
    assert {
        action.action_id: action.owner_client_instance_id
        for action in restore.actions
    } == owner_by_action

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    sockets: dict[str, _Socket] = {}
    for owner in owner_by_action.values():
        socket = _Socket()
        sockets[owner] = socket
        server._clients[owner] = WebSocketClient(  # noqa: SLF001
            owner,
            socket,
            client_type="chrome",
            client_instance_id=owner,
            authenticated=True,
        )
    assert await server.send_restore_command(restore) == 2
    assert all(len(socket.sent) == 1 for socket in sockets.values())

    for index, action in enumerate(restore.actions):
        state, _ = await coordinator.record_receipts(
            InterventionReceiptBatch(
                intervention_id=manifest.intervention_id,
                manifest_sha256=manifest.manifest_sha256,
                authorization_id=restore.restore_id,
                receipts=(
                    _receipt(
                        manifest=manifest,
                        authorization_id=restore.restore_id,
                        action_id=action.action_id,
                        clock=clock,
                        phase=ReceiptPhase.RESTORE,
                    ),
                ),
            ),
            source_client_type="chrome",
            source_client_id=action.owner_client_instance_id,
        )
        assert state == (
            InterventionLifecycleState.RESTORED
            if index == len(restore.actions) - 1
            else InterventionLifecycleState.RESTORING
        )


@pytest.mark.asyncio
async def test_partial_apply_creates_reverse_order_compensation() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock, two_actions=True)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close", "act_open"), clock),
        source_client_id="client_browser",
    )
    await coordinator.bind_dispatch_targets(
        command.authorization.authorization_id,  # type: ignore[union-attr]
        {"act_close": "client_browser", "act_open": "client_browser"},
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    succeeded = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        inverse={"url": "https://example.com/closed"},
    )
    failed = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_open",
        clock=clock,
        status=ReceiptStatus.FAILED,
        verification=VerificationStatus.FAILED,
    )
    state, compensation = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(succeeded, failed),
        ),
        source_client_type="chrome",
        source_client_id="client_browser",
    )
    assert state == InterventionLifecycleState.PARTIAL
    assert compensation is not None
    assert [action.action_id for action in compensation.actions] == ["act_close"]
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.state == InterventionLifecycleState.RESTORING.value

    await coordinator.bind_dispatch_targets(
        compensation.restore_id,
        {"act_close": "client_browser"},
    )
    compensated = _receipt(
        manifest=manifest,
        authorization_id=compensation.restore_id,
        action_id="act_close",
        clock=clock,
        phase=ReceiptPhase.COMPENSATE,
        inverse={"url": "https://example.com/closed"},
    )
    state, followup = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=compensation.restore_id,
            receipts=(compensated,),
        ),
        source_client_type="chrome",
        source_client_id="client_browser",
    )
    assert state == InterventionLifecycleState.RESTORED
    assert followup is None


@pytest.mark.asyncio
async def test_partial_compensation_preserves_prior_authorization_effects() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    plan = _plan(two_actions=True)
    plan.suggested_actions.append(
        SuggestedAction(
            action_id="act_editor",
            action_type="resume_last_active_file",
            target="/tmp/cortex-workspace/main.py:12",
            label="Resume the active file",
            reason="Return to the exact editor location",
            reversible=True,
        )
    )
    manifest = build_action_manifest(plan, [], consent_policy=policy, clock=clock)
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=policy, clock=clock),
        clock=clock,
        execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)

    first = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock, request_id="req_first"),
        source_client_id="browser_owner",
    )
    first_auth = first.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        first_auth, {"act_close": "browser_owner"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=first_auth,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=first_auth,
                    action_id="act_close",
                    clock=clock,
                    inverse={"url": "https://example.com/closed"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLIED

    second = await coordinator.authorize_and_consume(
        _request(
            manifest,
            ("act_editor", "act_open"),
            clock,
            request_id="req_second",
        ),
        source_client_id="browser_owner",
    )
    second_auth = second.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        second_auth,
        {"act_open": "browser_owner", "act_editor": "editor_owner"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=second_auth,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=second_auth,
                    action_id="act_open",
                    clock=clock,
                    inverse={"tabId": 44, "url": "https://example.com/reference"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLYING
    state, compensation = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=second_auth,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=second_auth,
                    action_id="act_editor",
                    clock=clock,
                    status=ReceiptStatus.FAILED,
                    verification=VerificationStatus.FAILED,
                ),
            ),
        ),
        source_client_type="vscode",
        source_client_id="editor_owner",
    )
    assert state == InterventionLifecycleState.PARTIAL
    assert compensation is not None
    assert [action.action_id for action in compensation.actions] == ["act_open"]

    await coordinator.bind_dispatch_targets(
        compensation.restore_id, {"act_open": "browser_owner"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=compensation.restore_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=compensation.restore_id,
                    action_id="act_open",
                    clock=clock,
                    phase=ReceiptPhase.COMPENSATE,
                    inverse={"tabId": 44, "url": "https://example.com/reference"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLIED

    # A delayed, indeterminate apply receipt predating compensation is audit
    # evidence only; it cannot reactivate the already-inverted action.
    late = _receipt(
        manifest=manifest,
        authorization_id=second_auth,
        action_id="act_open",
        clock=clock,
        status=ReceiptStatus.FAILED,
        verification=VerificationStatus.FAILED,
        inverse={
            "tabId": 44,
            "url": "https://example.com/reference",
            "cortexEffectMayExist": True,
        },
        attempt=2,
    )
    late_state, late_compensation = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=second_auth,
            receipts=(late,),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert late_state == InterventionLifecycleState.APPLIED
    assert late_compensation is None

    restore = await coordinator.request_restore(
        manifest.intervention_id,
        reason="user_undo",
    )
    assert restore is not None
    assert [action.action_id for action in restore.actions] == ["act_close"]


@pytest.mark.asyncio
async def test_failed_action_requires_a_fresh_proposal_id_for_retry() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id, {"act_close": "browser_owner"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=auth_id,
                    action_id="act_close",
                    clock=clock,
                    status=ReceiptStatus.FAILED,
                    verification=VerificationStatus.FAILED,
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.FAILED

    retry = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock, request_id="req_retry"),
        source_client_id="browser_owner",
    )
    assert retry.reason_code == "action_mismatch"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_unverified_possible_effect_is_compensated_not_called_failed() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "client_browser"},
    )
    uncertain = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        status=ReceiptStatus.FAILED,
        verification=VerificationStatus.FAILED,
        inverse={
            "url": "https://example.com/closed",
            "cortexEffectMayExist": True,
        },
    )
    state, compensation = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(uncertain,),
        ),
        source_client_type="chrome",
        source_client_id="client_browser",
    )
    assert state == InterventionLifecycleState.PARTIAL
    assert compensation is not None
    assert compensation.actions[0].inverse_payload_json == (
        uncertain.inverse_payload_json
    )


@pytest.mark.asyncio
async def test_restore_failure_remains_retryable_and_idempotent() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    await coordinator.bind_dispatch_targets(
        command.authorization.authorization_id,  # type: ignore[union-attr]
        {"act_close": "client_browser"},
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    apply_receipt = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        inverse={"url": "https://example.com/closed"},
    )
    batch = InterventionReceiptBatch(
        intervention_id=manifest.intervention_id,
        manifest_sha256=manifest.manifest_sha256,
        authorization_id=auth_id,
        receipts=(apply_receipt,),
    )
    state, _ = await coordinator.record_receipts(
        batch, source_client_type="chrome", source_client_id="client_browser",
    )
    assert state == InterventionLifecycleState.APPLIED
    # Exact duplicate receipt is a no-op, not a second state mutation.
    await coordinator.record_receipts(
        batch, source_client_type="chrome", source_client_id="client_browser",
    )

    restore = await coordinator.request_restore(
        manifest.intervention_id, reason="user_undo",
    )
    assert restore is not None
    duplicate_restore = await coordinator.request_restore(
        manifest.intervention_id, reason="dismissed",
    )
    assert duplicate_restore is not None
    assert duplicate_restore.restore_id == restore.restore_id
    await coordinator.bind_dispatch_targets(
        restore.restore_id,
        {"act_close": "client_browser"},
    )
    failed_restore = _receipt(
        manifest=manifest,
        authorization_id=restore.restore_id,
        action_id="act_close",
        clock=clock,
        phase=ReceiptPhase.RESTORE,
        status=ReceiptStatus.FAILED,
        verification=VerificationStatus.FAILED,
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=restore.restore_id,
            receipts=(failed_restore,),
        ),
        source_client_type="chrome",
        source_client_id="client_browser",
    )
    assert state == InterventionLifecycleState.RESTORE_FAILED

    retry = await coordinator.request_restore(
        manifest.intervention_id, reason="user_undo",
    )
    assert retry is not None
    assert retry.restore_id != restore.restore_id
    await coordinator.bind_dispatch_targets(
        retry.restore_id,
        {"act_close": "client_browser"},
    )
    success = _receipt(
        manifest=manifest,
        authorization_id=retry.restore_id,
        action_id="act_close",
        clock=clock,
        phase=ReceiptPhase.RESTORE,
        status=ReceiptStatus.SUCCEEDED,
        verification=VerificationStatus.VERIFIED,
        attempt=2,
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=retry.restore_id,
            receipts=(success,),
        ),
        source_client_type="chrome",
        source_client_id="client_browser",
    )
    assert state == InterventionLifecycleState.RESTORED


@pytest.mark.asyncio
async def test_receipt_idempotency_key_replay_cannot_change_outcome() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "client_browser"},
    )
    first = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        receipt_id="rcpt_first",
        inverse={"url": "https://example.com"},
    )
    await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(first,),
        ),
        source_client_type="chrome",
        source_client_id="client_browser",
    )
    conflicting = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        receipt_id="rcpt_conflict",
        status=ReceiptStatus.FAILED,
        verification=VerificationStatus.FAILED,
    )
    with pytest.raises(ValueError, match="idempotency_key replayed"):
        await coordinator.record_receipts(
            InterventionReceiptBatch(
                intervention_id=manifest.intervention_id,
                manifest_sha256=manifest.manifest_sha256,
                authorization_id=auth_id,
                receipts=(conflicting,),
            ),
            source_client_type="chrome",
            source_client_id="client_browser",
        )


@pytest.mark.asyncio
async def test_delayed_older_receipt_cannot_replace_newer_inverse_evidence() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "browser_owner"},
    )
    newer = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        inverse={"url": "https://example.com/newer"},
        attempt=2,
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(newer,),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLIED

    delayed_older = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        status=ReceiptStatus.FAILED,
        verification=VerificationStatus.FAILED,
        inverse={
            "url": "https://example.com/older",
            "cortexEffectMayExist": True,
        },
        attempt=1,
    )
    late_state, followup = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(delayed_older,),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert late_state == InterventionLifecycleState.APPLIED
    assert followup is None

    restore = await coordinator.request_restore(
        manifest.intervention_id,
        reason="user_undo",
    )
    assert restore is not None
    assert restore.actions[0].inverse_receipt_id == newer.receipt_id
    assert json.loads(restore.actions[0].inverse_payload_json) == {
        "url": "https://example.com/newer",
    }


@pytest.mark.asyncio
async def test_verified_consent_evidence_is_claimed_once_across_restart() -> None:
    clock = _clock()
    store = InMemoryInterventionTransactionStore()
    coordinator, manifest = await _coordinator(clock=clock, store=store)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "browser_owner"},
    )
    receipt = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        inverse={"url": "https://example.com/closed"},
    )
    await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(receipt,),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert await coordinator.claim_consent_evidence(receipt.receipt_id) is True
    assert await coordinator.claim_consent_evidence(receipt.receipt_id) is False

    restarted = InterventionTransactionCoordinator(
        ConsentLadder(policy=ConsentPolicy(), clock=clock),
        store=store,
        clock=clock,
        execution_mode="authorized",
    )
    assert await restarted.claim_consent_evidence(receipt.receipt_id) is False


@pytest.mark.asyncio
async def test_verified_noop_is_not_active_or_consent_escalation_evidence() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    authorization_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        authorization_id,
        {"act_close": "browser_owner"},
    )
    receipt = _receipt(
        manifest=manifest,
        authorization_id=authorization_id,
        action_id="act_close",
        clock=clock,
        status=ReceiptStatus.ALREADY_COMPLETE,
        inverse={"noEffect": True},
    )
    state, compensation = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=authorization_id,
            receipts=(receipt,),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLIED
    assert compensation is None
    assert await coordinator.claim_consent_evidence(receipt.receipt_id) is False
    assert await coordinator.request_restore(
        manifest.intervention_id,
        reason="user_undo",
    ) is None

    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    payload = transaction.model_dump(mode="json")
    payload["consent_evidence_receipt_ids"] = [receipt.receipt_id]
    with pytest.raises(
        ValidationError,
        match="claimed consent evidence is not a verified apply",
    ):
        type(transaction).model_validate(payload)


@pytest.mark.asyncio
async def test_noop_plus_failure_does_not_enter_unnecessary_compensation() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock, two_actions=True)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close", "act_open"), clock),
        source_client_id="browser_owner",
    )
    authorization_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        authorization_id,
        {"act_close": "browser_owner", "act_open": "browser_owner"},
    )
    noop = _receipt(
        manifest=manifest,
        authorization_id=authorization_id,
        action_id="act_close",
        clock=clock,
        status=ReceiptStatus.ALREADY_COMPLETE,
        inverse={"noEffect": True},
    )
    failed = _receipt(
        manifest=manifest,
        authorization_id=authorization_id,
        action_id="act_open",
        clock=clock,
        status=ReceiptStatus.FAILED,
        verification=VerificationStatus.FAILED,
    )
    state, compensation = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=authorization_id,
            receipts=(noop, failed),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.FAILED
    assert compensation is None


@pytest.mark.asyncio
async def test_active_verified_action_cannot_be_authorized_twice() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "client_browser"},
    )
    await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=auth_id,
                    action_id="act_close",
                    clock=clock,
                    inverse={"url": "https://example.com"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="client_browser",
    )
    denied = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock, request_id="req_again"),
        source_client_id="client_browser",
    )
    assert denied.reason_code == "action_mismatch"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_manifest_expiry_and_dispatch_failure_fail_closed() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    clock.advance(
        wall_ms=manifest.ttl_ms,
        monotonic_ns=manifest.ttl_ms * 1_000_000,
    )
    expired = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    assert expired.reason_code == "transaction_closed"  # type: ignore[union-attr]

    fresh_clock = _clock()
    fresh, fresh_manifest = await _coordinator(clock=fresh_clock)
    command = await fresh.authorize_and_consume(
        _request(fresh_manifest, ("act_close",), fresh_clock),
        source_client_id="client_browser",
    )
    state = await fresh.record_dispatch_failure(
        command.authorization.authorization_id,  # type: ignore[union-attr]
        reason="no_complete_executor_route",
    )
    assert state == InterventionLifecycleState.FAILED


@pytest.mark.asyncio
async def test_consumed_authorization_expires_at_exact_boundary() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    assert await coordinator.validate_consumed(command) is True  # type: ignore[arg-type]
    clock.advance(
        wall_ms=command.authorization.ttl_ms,  # type: ignore[union-attr]
        monotonic_ns=(  # type: ignore[union-attr]
            command.authorization.ttl_ms * 1_000_000
        ),
    )
    assert await coordinator.validate_consumed(command) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_authorization_gesture_is_fresh_and_cannot_outlive_manifest() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    stale = _request(manifest, ("act_close",), clock).model_copy(
        update={"requested_at_unix_ms": clock.unix_ms() - 15_001}
    )
    denied = await coordinator.authorize_and_consume(
        stale,
        source_client_id="browser_owner",
    )
    assert denied.reason_code == "invalid_request"  # type: ignore[union-attr]

    remaining_ms = 4_000
    clock.advance(
        wall_ms=manifest.ttl_ms - remaining_ms,
        monotonic_ns=(manifest.ttl_ms - remaining_ms) * 1_000_000,
    )
    command = await coordinator.authorize_and_consume(
        _request(
            manifest,
            ("act_close",),
            clock,
            request_id="req_near_manifest_expiry",
        ),
        source_client_id="browser_owner",
    )
    assert command.authorization.ttl_ms == remaining_ms  # type: ignore[union-attr]
    assert (  # type: ignore[union-attr]
        command.authorization.expires_at_unix_ms
        == manifest.expires_at_unix_ms
    )


@pytest.mark.asyncio
async def test_wall_clock_rollback_cannot_extend_manifest_or_dispatch_lease() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    near_expiry_ms = manifest.ttl_ms - 2_000
    clock.advance(monotonic_ns=near_expiry_ms * 1_000_000)
    clock.jump_wall(-120_000)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    assert command.authorization.ttl_ms == 2_000  # type: ignore[union-attr]

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    socket = _Socket()
    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    server._clients["browser_socket"] = WebSocketClient(  # noqa: SLF001
        "browser_socket",
        socket,
        client_type="chrome",
        client_instance_id="browser_owner",
        authenticated=True,
    )
    clock.advance(monotonic_ns=2_000 * 1_000_000)
    clock.jump_wall(-120_000)
    report = await server.dispatch_apply_command(command)  # type: ignore[arg-type]
    assert report.attempted_targets == 0
    assert socket.sent == []


@pytest.mark.asyncio
async def test_expired_authorization_is_never_attempted_on_socket() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    clock.advance(
        wall_ms=command.authorization.ttl_ms,  # type: ignore[union-attr]
        monotonic_ns=command.authorization.ttl_ms * 1_000_000,  # type: ignore[union-attr]
    )

    class _Socket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    socket = _Socket()
    server = WebSocketServer(clock=clock)
    server.set_intervention_dispatch_binding_callback(
        coordinator.bind_dispatch_targets
    )
    server._clients["browser_socket"] = WebSocketClient(  # noqa: SLF001
        "browser_socket",
        socket,
        client_type="chrome",
        client_instance_id="browser_owner",
        authenticated=True,
    )
    report = await server.dispatch_apply_command(command)  # type: ignore[arg-type]
    assert report.attempted_targets == 0
    assert report.delivered_targets == 0
    assert socket.sent == []
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.dispatch_history == []


@pytest.mark.asyncio
async def test_later_dispatch_failure_preserves_prior_active_effect() -> None:
    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock, two_actions=True)

    first = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock, request_id="req_first"),
        source_client_id="browser_owner",
    )
    first_auth = first.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        first_auth,
        {"act_close": "browser_owner"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=first_auth,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=first_auth,
                    action_id="act_close",
                    clock=clock,
                    inverse={"url": "https://example.com/closed"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLIED

    second = await coordinator.authorize_and_consume(
        _request(manifest, ("act_open",), clock, request_id="req_second"),
        source_client_id="browser_owner",
    )
    state = await coordinator.record_dispatch_failure(
        second.authorization.authorization_id,  # type: ignore[union-attr]
        reason="browser_disconnected_before_send",
    )
    assert state == InterventionLifecycleState.APPLIED

    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.transitions[-1].reason == (
        "dispatch_failed_prior_effects_remain_active"
    )
    restore = await coordinator.request_restore(
        manifest.intervention_id,
        reason="user_undo",
    )
    assert restore is not None
    assert [action.action_id for action in restore.actions] == ["act_close"]


@pytest.mark.asyncio
async def test_later_partial_dispatch_compensates_only_that_authorization() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    plan = _mixed_executor_plan()
    plan.suggested_actions.insert(
        1,
        SuggestedAction(
            action_id="act_open",
            action_type="open_url",
            target="https://example.com/reference",
            label="Open reference",
            reason="Keep the relevant reference nearby",
            reversible=True,
        ),
    )
    manifest = build_action_manifest(plan, [], consent_policy=policy, clock=clock)
    coordinator = InterventionTransactionCoordinator(
        ConsentLadder(policy=policy, clock=clock),
        clock=clock,
        execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)

    first = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock, request_id="req_first"),
        source_client_id="browser_owner",
    )
    first_auth = first.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        first_auth,
        {"act_close": "browser_owner"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=first_auth,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=first_auth,
                    action_id="act_close",
                    clock=clock,
                    inverse={"url": "https://example.com/closed"},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.APPLIED

    second = await coordinator.authorize_and_consume(
        _request(
            manifest,
            ("act_open", "act_editor"),
            clock,
            request_id="req_second",
        ),
        source_client_id="browser_owner",
    )
    second_auth = second.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        second_auth,
        {"act_open": "browser_owner", "act_editor": "editor_owner"},
    )
    compensation = await coordinator.compensate_partial_dispatch(
        second_auth,
        reason="editor disconnected after browser delivery",
    )
    assert compensation is not None
    assert {action.action_id for action in compensation.actions} == {
        "act_open",
        "act_editor",
    }
    assert "act_close" not in {
        action.action_id for action in compensation.actions
    }
    await coordinator.bind_dispatch_targets(
        compensation.restore_id,
        {"act_open": "browser_owner", "act_editor": "editor_owner"},
    )
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=compensation.restore_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=compensation.restore_id,
                    action_id="act_open",
                    clock=clock,
                    phase=ReceiptPhase.COMPENSATE,
                    status=ReceiptStatus.ALREADY_COMPLETE,
                    inverse={},
                ),
            ),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )
    assert state == InterventionLifecycleState.RESTORING
    state, _ = await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=compensation.restore_id,
            receipts=(
                _receipt(
                    manifest=manifest,
                    authorization_id=compensation.restore_id,
                    action_id="act_editor",
                    clock=clock,
                    phase=ReceiptPhase.COMPENSATE,
                    status=ReceiptStatus.ALREADY_COMPLETE,
                    inverse={},
                ),
            ),
        ),
        source_client_type="vscode",
        source_client_id="editor_owner",
    )
    assert state == InterventionLifecycleState.APPLIED
    final = await coordinator.get_transaction(manifest.intervention_id)
    assert final is not None
    assert final.active_restore is None


@pytest.mark.asyncio
async def test_presentation_only_close_is_trivially_safe_and_abandoned() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, clock=clock)
    plan = _plan()
    plan.suggested_actions = []
    manifest = build_action_manifest(plan, [], consent_policy=policy, clock=clock)
    coordinator = InterventionTransactionCoordinator(
        ladder, clock=clock, execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    assert await coordinator.request_restore(
        manifest.intervention_id,
        reason="dismissed",
    ) is None
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.state == InterventionLifecycleState.ABANDONED.value


@pytest.mark.asyncio
async def test_global_restore_abandons_proposals_and_retains_exact_effect_inverse() -> None:
    clock = _clock()
    policy = ConsentPolicy()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    auth_id = command.authorization.authorization_id  # type: ignore[union-attr]
    await coordinator.bind_dispatch_targets(
        auth_id,
        {"act_close": "browser_owner"},
    )
    receipt = _receipt(
        manifest=manifest,
        authorization_id=auth_id,
        action_id="act_close",
        clock=clock,
        inverse={"url": "https://example.com/closed"},
    )
    await coordinator.record_receipts(
        InterventionReceiptBatch(
            intervention_id=manifest.intervention_id,
            manifest_sha256=manifest.manifest_sha256,
            authorization_id=auth_id,
            receipts=(receipt,),
        ),
        source_client_type="chrome",
        source_client_id="browser_owner",
    )

    pending_plan = _plan()
    pending_plan.intervention_id = "int_pending_proposal"
    pending_manifest = build_action_manifest(
        pending_plan,
        [],
        consent_policy=policy,
        clock=clock,
    )
    await coordinator.register_proposal(pending_manifest)
    await coordinator.mark_delivered(pending_manifest.intervention_id)

    restores = await coordinator.request_restore_all(
        reason="emergency_restore",
    )
    assert len(restores) == 1
    assert restores[0].actions[0].inverse_receipt_id == receipt.receipt_id
    assert restores[0].actions[0].owner_client_instance_id == "browser_owner"
    pending = await coordinator.get_transaction(pending_manifest.intervention_id)
    assert pending is not None
    assert pending.state == InterventionLifecycleState.ABANDONED.value

    replay = await coordinator.request_restore_all(reason="emergency_restore")
    assert len(replay) == 1
    assert replay[0].restore_id == restores[0].restore_id


@pytest.mark.asyncio
async def test_crash_during_apply_recovers_from_durable_journal(tmp_path: Path) -> None:
    clock = _clock()
    path = tmp_path / "transactions.json"
    store = JsonInterventionTransactionStore(path)
    coordinator, manifest = await _coordinator(clock=clock, store=store)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="client_browser",
    )
    await coordinator.bind_dispatch_targets(
        command.authorization.authorization_id,  # type: ignore[union-attr]
        {"act_close": "client_browser"},
    )
    assert path.exists()

    # New boot/coordinator sees APPLYING with no receipt and emits an exact
    # startup restore. The client uses its own operation journal to decide
    # whether the Cortex-owned action actually happened.
    clock.reboot(monotonic_ns=1_000_000)
    recovered = InterventionTransactionCoordinator(
        ConsentLadder(policy=ConsentPolicy(), clock=clock),
        store=JsonInterventionTransactionStore(path),
        clock=clock,
        execution_mode="authorized",
    )
    restores = await recovered.recover_unfinished()
    assert len(restores) == 1
    assert restores[0].reason == "startup_recovery"
    assert [action.action_id for action in restores[0].actions] == ["act_close"]
    assert restores[0].actions[0].owner_client_instance_id == "client_browser"


@pytest.mark.asyncio
async def test_applying_transaction_cannot_be_relabelled_abandoned() -> None:
    """An ambiguous client write always retains an exact restore obligation."""

    clock = _clock()
    coordinator, manifest = await _coordinator(clock=clock)
    command = await coordinator.authorize_and_consume(
        _request(manifest, ("act_close",), clock),
        source_client_id="browser_owner",
    )
    await coordinator.bind_dispatch_targets(
        command.authorization.authorization_id,  # type: ignore[union-attr]
        {"act_close": "browser_owner"},
    )

    assert await coordinator.abandon(
        manifest.intervention_id,
        "presentation_transport_failed",
    ) is False
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.state == InterventionLifecycleState.APPLYING.value

    restore = await coordinator.request_restore(
        manifest.intervention_id,
        reason="system_cancelled",
    )
    assert restore is not None
    assert restore.actions[0].action_id == "act_close"
    assert restore.actions[0].owner_client_instance_id == "browser_owner"


@pytest.mark.asyncio
async def test_corrupt_journals_are_uniquely_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "transactions.json"
    store = JsonInterventionTransactionStore(path)
    first_bytes = "{not-json-one"
    path.write_text(first_bytes, encoding="utf-8")
    assert (await store.load()).transactions == {}

    second_bytes = "{not-json-two"
    path.write_text(second_bytes, encoding="utf-8")
    assert (await store.load()).transactions == {}

    quarantined = sorted(tmp_path.glob("transactions.json.corrupt.*"))
    assert len(quarantined) == 2
    assert {item.read_text(encoding="utf-8") for item in quarantined} == {
        first_bytes,
        second_bytes,
    }


class _BlockingStore(InMemoryInterventionTransactionStore):
    def __init__(self) -> None:
        super().__init__()
        self.block = False
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()

    async def save(self, journal: Any) -> None:
        if self.block:
            self.save_started.set()
            await self.release_save.wait()
        await super().save(journal)


@pytest.mark.asyncio
async def test_concurrent_reset_cannot_split_check_from_commit() -> None:
    clock = _clock()
    store = _BlockingStore()
    policy = ConsentPolicy()
    ladder = ConsentLadder(policy=policy, clock=clock)
    manifest = build_action_manifest(_plan(), [], consent_policy=policy, clock=clock)
    coordinator = InterventionTransactionCoordinator(
        ladder,
        store=store,
        clock=clock,
        execution_mode="authorized",
    )
    await coordinator.register_proposal(manifest)
    await coordinator.mark_delivered(manifest.intervention_id)
    store.block = True

    authorize_task = asyncio.create_task(
        coordinator.authorize_and_consume(
            _request(manifest, ("act_close",), clock),
            source_client_id="client_browser",
        )
    )
    await asyncio.wait_for(store.save_started.wait(), timeout=1.0)
    reset_task = asyncio.create_task(ladder.reset("open_url"))
    await asyncio.sleep(0)
    assert not reset_task.done(), "reset interleaved before auth commit"
    store.release_save.set()
    result = await asyncio.wait_for(authorize_task, timeout=1.0)
    assert hasattr(result, "authorization")
    await asyncio.wait_for(reset_task, timeout=1.0)
    assert await ladder.revision() > result.authorization.consent_revision  # type: ignore[union-attr]

    # Durable consumption alone is not enough to race a reset. The final
    # client-owner binding rechecks the captured consent revision immediately
    # before the WebSocket layer is allowed to write the command.
    with pytest.raises(
        ValueError,
        match="consent revision changed before executor binding",
    ):
        await coordinator.bind_dispatch_targets(
            result.authorization.authorization_id,  # type: ignore[union-attr]
            {"act_close": "client_browser"},
        )
    assert await coordinator.request_restore_all(reason="system_cancelled") == []
    transaction = await coordinator.get_transaction(manifest.intervention_id)
    assert transaction is not None
    assert transaction.state == InterventionLifecycleState.RESTORED.value
