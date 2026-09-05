"""Manifest construction and transactional intervention coordination.

This module is the only production path that can turn a proposal into
workspace authority.  It deliberately contains no browser, editor, or Qt
calls: adapters receive an :class:`InterventionApplyCommand` only after the
coordinator has durably consumed a one-time authorization.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Literal, cast

from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.libs.ports.intervention_transaction_port import (
    InterventionTransactionStore,
)
from cortex.libs.schemas.intervention import AdapterCommand, InterventionPlan
from cortex.libs.schemas.intervention_transaction import (
    INTERVENTION_ALLOWED_TRANSITIONS,
    ActionAuthorization,
    ActionManifest,
    ActionReceipt,
    AuthorizationDenied,
    AuthorizationLedgerEntry,
    AuthorizationState,
    ExecutorDispatchBinding,
    InterventionApplyCommand,
    InterventionAuthorizationRequest,
    InterventionLifecycleState,
    InterventionReceiptBatch,
    InterventionRestoreCommand,
    InterventionTransaction,
    InterventionTransactionJournal,
    LifecycleTransition,
    ManifestAction,
    ReceiptPhase,
    ReceiptStatus,
    RestoreAction,
    VerificationStatus,
)
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import ConsentPolicy, canonical_action_type
from cortex.services.intervention_engine.transaction_store import (
    InMemoryInterventionTransactionStore,
)

logger = logging.getLogger(__name__)

ExecutionMode = Literal["suggest_only", "authorized", "research_autonomous"]
RestoreReason = Literal[
    "user_undo",
    "dismissed",
    "snoozed",
    "timed_out",
    "natural_recovery",
    "system_cancelled",
    "partial_compensation",
    "startup_recovery",
    "emergency_restore",
]

_CONSENT_LEVELS: dict[str, int] = {
    "observe": 0,
    "suggest": 1,
    "preview": 2,
    "reversible_act": 3,
    "autonomous_act": 4,
}

# Presentation is delivered with the proposal. It is not a workspace effect
# and must never be smuggled into a later mutation manifest.
_PRESENTATION_COMMANDS = frozenset(
    {"show_overlay", "dim_background", "prompt_micro_commit", "suggest_movement_break"}
)

_REVERSE_CAPABILITIES: dict[str, str] = {
    "open_url": "close_created_tab",
    "search_error": "close_created_tab",
    "highlight_tab": "restore_active_tab",
    "resume_last_active_file": "restore_active_file",
}

_SUGGESTED_ACTION_EXECUTORS: dict[str, Literal[
    "browser", "editor", "terminal", "desktop", "daemon"
]] = {
    "close_tab": "browser",
    "group_tabs": "browser",
    "bookmark_and_close": "browser",
    "open_url": "browser",
    "search_error": "browser",
    "highlight_tab": "browser",
    "save_session": "browser",
    "copy_to_clipboard": "browser",
    "start_timer": "browser",
    "resume_last_active_file": "editor",
    "prompt_micro_commit": "browser",
    "suggest_movement_break": "browser",
    "take_biology_break": "desktop",
}

_PLANNER_CAPABILITIES: frozenset[str] = frozenset()
_AUTHORIZATION_REQUEST_MAX_AGE_MS = 15_000
_AUTHORIZATION_REQUEST_MAX_FUTURE_SKEW_MS = 5_000

# Only capabilities with a production implementation of the exact
# apply/receipt/restore protocol may enter an executable manifest. Unsupported
# LLM suggestions remain visible as inert proposal copy and are annotated with
# a warning; they never become adapter authority.
_SUPPORTED_CAPABILITIES_BY_EXECUTOR: dict[str, frozenset[str]] = {
    "browser": frozenset(
        {
            "open_url",
            "search_error",
            "highlight_tab",
        }
    ),
    # Destructive tab closure cannot be reconstructed from a URL: history,
    # form state, scroll position, media state, and the browser-owned session
    # identity are lost. Grouping is also excluded because moving a tab out of
    # a pre-existing/user-raced group can destroy group ownership that Chrome
    # does not let us atomically compare-and-swap. Likewise, VS Code does not
    # expose whether a folding range was already collapsed. Keep all of those
    # proposals visible, but inert, until an ownership-safe adapter exists.
    "editor": frozenset({"resume_last_active_file"}),
}


def _append_plan_warning(plan: InterventionPlan, warning: str) -> None:
    """Add one deterministic warning without accumulating build duplicates."""

    if warning not in plan.plan_warnings:
        plan.plan_warnings.append(warning)


def _validate_manifest_semantics(manifest: ActionManifest) -> None:
    """Reject internally inconsistent authority even with a valid digest."""

    expected_executors = {
        capability: executor
        for capability, executor in _SUGGESTED_ACTION_EXECUTORS.items()
        if capability
        in _SUPPORTED_CAPABILITIES_BY_EXECUTOR.get(executor, frozenset())
    }
    for action in manifest.body.actions:
        expected_executor = expected_executors.get(action.capability)
        if expected_executor is None or action.executor != expected_executor:
            raise ValueError(
                f"manifest capability has invalid executor: {action.capability}"
            )
        expected_reverse = _REVERSE_CAPABILITIES.get(action.capability)
        if action.reverse_capability != expected_reverse:
            raise ValueError(
                f"manifest capability has invalid inverse: {action.capability}"
            )
        # WP-6 deliberately promotes only capabilities with a verified
        # ownership-safe inverse. Clipboard writes, timers, saved sessions,
        # and prompt widgets remain presentation suggestions: calling them
        # "non-workspace" would not make their irreversible side effects
        # transactional.
        if not action.workspace_mutation:
            raise ValueError(
                "manifest capability has invalid workspace classification: "
                f"{action.capability}"
            )
        if (
            action.workspace_mutation
            and action.reverse_capability is None
        ):
            raise ValueError(
                f"workspace capability is not reversible: {action.capability}"
            )
        if (
            action.source == "planner_command"
            and action.capability not in _PLANNER_CAPABILITIES
        ):
            raise ValueError(
                f"planner cannot mint capability {action.capability}"
            )
        if (
            action.source == "suggested_action"
            and action.capability not in _SUGGESTED_ACTION_EXECUTORS
        ):
            raise ValueError(
                f"unknown suggested capability {action.capability}"
            )
        if action.source == "system":
            raise ValueError("system-sourced executable actions are not enabled")

_TERMINAL_STATES = frozenset(
    {
        InterventionLifecycleState.RESTORED.value,
        InterventionLifecycleState.ABANDONED.value,
    }
)

def _lifecycle_value(
    state: InterventionLifecycleState | str,
) -> str:
    return state.value if isinstance(state, InterventionLifecycleState) else state


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _system_action_id(
    intervention_id: str,
    ordinal: int,
    command: AdapterCommand,
) -> str:
    material = _canonical_json(
        {
            "intervention_id": intervention_id,
            "ordinal": ordinal,
            "adapter": command.adapter,
            "action": command.action,
            "params": command.params,
        }
    )
    return f"sys_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def build_action_manifest(
    plan: InterventionPlan,
    commands: list[AdapterCommand],
    *,
    consent_policy: ConsentPolicy,
    clock: Clock | None = None,
    include_suggested_actions: bool = True,
    manifest_ttl_ms: int = 300_000,
) -> ActionManifest:
    """Lower a validated plan into an immutable, presentation-free manifest."""

    active_clock = clock or SYSTEM_CLOCK
    actions: list[ManifestAction] = []
    plan_level = _CONSENT_LEVELS[plan.consent_level]

    # Planner commands are deterministic system effects. Presentation commands
    # stay on the proposal path and are intentionally absent here.
    seen_commands: set[tuple[str, str, str]] = set()
    for command in commands:
        if command.action in _PRESENTATION_COMMANDS:
            continue
        executor: Literal["browser", "editor", "terminal", "desktop", "daemon"]
        if command.adapter == "overlay":
            executor = "desktop"
        elif command.adapter in {"browser", "editor", "terminal"}:
            executor = cast(
                Literal["browser", "editor", "terminal"],
                command.adapter,
            )
        else:
            raise ValueError(f"unsupported manifest adapter {command.adapter!r}")
        if command.action not in _SUPPORTED_CAPABILITIES_BY_EXECUTOR.get(
            executor, frozenset()
        ):
            _append_plan_warning(
                plan,
                f"non-executable capability omitted: {executor}:{command.action}",
            )
            continue
        params_json = _canonical_json(command.params)
        command_key = (executor, command.action, params_json)
        if command_key in seen_commands:
            continue
        seen_commands.add(command_key)
        ordinal = len(actions)
        consent_key = canonical_action_type(command.action)
        actions.append(
            ManifestAction.from_parameters(
                action_id=_system_action_id(plan.intervention_id, ordinal, command),
                ordinal=ordinal,
                executor=executor,
                capability=command.action,
                parameters={str(key): value for key, value in command.params.items()},
                reverse_capability=_REVERSE_CAPABILITIES.get(command.action),
                workspace_mutation=True,
                required_consent_level=max(
                    plan_level,
                    consent_policy.get_minimum_level(consent_key),
                ),
                source="planner_command",
            )
        )

    # Suggested actions are inert descriptions until the user approves their
    # stable action_id. Embed the whole validated SuggestedAction as params so
    # the client never executes a mutable UI copy.
    for suggested in plan.suggested_actions if include_suggested_actions else []:
        suggested_executor = _SUGGESTED_ACTION_EXECUTORS.get(suggested.action_type)
        if suggested_executor is None:
            raise ValueError(
                f"unsupported suggested action capability {suggested.action_type!r}"
            )
        if suggested.action_type not in _SUPPORTED_CAPABILITIES_BY_EXECUTOR.get(
            suggested_executor, frozenset()
        ):
            _append_plan_warning(
                plan,
                "non-executable suggested action omitted: "
                f"{suggested_executor}:{suggested.action_type}",
            )
            continue
        ordinal = len(actions)
        actions.append(
            ManifestAction.from_parameters(
                action_id=suggested.action_id,
                ordinal=ordinal,
                executor=suggested_executor,
                capability=suggested.action_type,
                parameters={
                    "suggested_action": suggested.model_dump(mode="json"),
                },
                reverse_capability=_REVERSE_CAPABILITIES.get(suggested.action_type),
                workspace_mutation=True,
                required_consent_level=max(
                    plan_level,
                    consent_policy.get_minimum_level(suggested.action_type),
                ),
                source="suggested_action",
            )
        )

    return ActionManifest.create(
        intervention_id=plan.intervention_id,
        actions=actions,
        created_at_unix_ms=active_clock.unix_ms(),
        created_at_mono_ns=active_clock.monotonic_ns(),
        boot_id=active_clock.boot_id,
        ttl_ms=manifest_ttl_ms,
    )


class InterventionTransactionCoordinator:
    """Serializes authorization, receipts, compensation, and restoration."""

    def __init__(
        self,
        consent_ladder: ConsentLadder,
        *,
        store: InterventionTransactionStore | None = None,
        clock: Clock | None = None,
        execution_mode: ExecutionMode = "suggest_only",
        authorization_ttl_ms: int = 30_000,
        terminal_retention_days: int = 7,
        max_terminal_transactions: int = 200,
    ) -> None:
        if not 1 <= authorization_ttl_ms <= 300_000:
            raise ValueError("authorization_ttl_ms must be in 1..300000")
        if terminal_retention_days < 1 or max_terminal_transactions < 1:
            raise ValueError("terminal retention bounds must be positive")
        # Audit D9: RESTORED/ABANDONED rows carry no authority and were never
        # evicted, so the store re-mirrored an ever-growing journal on every
        # save. Terminal rows are archived (dropped from the journal, hence
        # from the mirrored store) once older than the retention window, and
        # the retained terminal set is capped in size regardless of age.
        self._terminal_retention_ms = int(terminal_retention_days) * 86_400_000
        self._max_terminal_transactions = int(max_terminal_transactions)
        self._consent_ladder = consent_ladder
        self._store = store or InMemoryInterventionTransactionStore()
        self._clock = clock or SYSTEM_CLOCK
        self._execution_mode = execution_mode
        self._authorization_ttl_ms = authorization_ttl_ms
        self._journal = InterventionTransactionJournal()
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def execution_mode(self) -> ExecutionMode:
        return self._execution_mode

    def set_execution_mode(self, mode: ExecutionMode) -> None:
        self._execution_mode = mode

    async def _ensure_loaded_unlocked(self) -> None:
        if self._loaded:
            return
        self._journal = await self._store.load()
        # Issued-but-unconsumed capabilities never survive a process boot.
        changed = False
        for transaction in self._journal.transactions.values():
            transaction_changed = False
            for entry in transaction.authorizations:
                if entry.state == AuthorizationState.ISSUED.value:
                    entry.state = AuthorizationState.REVOKED
                    entry.state_reason = "daemon_restarted_before_consumption"
                    changed = True
                    transaction_changed = True
            if transaction_changed:
                transaction.revision += 1
                self._touch(transaction)
        self._loaded = True
        if self._archive_terminal_unlocked():
            changed = True
        if changed:
            await self._store.save(self._journal)

    def _archive_terminal_unlocked(self) -> bool:
        """Drop terminal rows past retention or beyond the size cap (audit D9).

        Only RESTORED/ABANDONED transactions are eligible: every other state
        still carries authority or an outstanding restore obligation. Returns
        True when the journal changed.
        """
        now = self._clock.unix_ms()
        terminal = [
            transaction
            for transaction in self._journal.transactions.values()
            if _lifecycle_value(transaction.state) in _TERMINAL_STATES
        ]
        if not terminal:
            return False
        expired = [
            transaction
            for transaction in terminal
            if now - transaction.updated_at_unix_ms > self._terminal_retention_ms
        ]
        retained = [transaction for transaction in terminal if transaction not in expired]
        retained.sort(key=lambda item: (item.updated_at_unix_ms, item.intervention_id))
        overflow = retained[: max(0, len(retained) - self._max_terminal_transactions)]
        evicted = {item.intervention_id for item in expired} | {
            item.intervention_id for item in overflow
        }
        if not evicted:
            return False
        for intervention_id in evicted:
            self._journal.transactions.pop(intervention_id, None)
        logger.info("Archived %d terminal intervention transaction(s)", len(evicted))
        return True

    def _reusable_restore_unlocked(
        self,
        transaction: InterventionTransaction,
    ) -> InterventionRestoreCommand | None:
        """Return the outstanding exact inverse when one already exists.

        A RESTORING transaction re-sends its active command; a
        RESTORE_FAILED one retries the *same* command (receipts carry an
        ``attempt`` counter) instead of appending a fresh command to
        ``restore_history`` on every retry tick (audit D9).
        """
        state = _lifecycle_value(transaction.state)
        command = transaction.active_restore
        if command is None or not command.actions:
            return None
        if state == InterventionLifecycleState.RESTORING.value:
            return command
        if state == InterventionLifecycleState.RESTORE_FAILED.value:
            self._transition(
                transaction,
                InterventionLifecycleState.RESTORING,
                "restore_retry_reuses_active_command",
            )
            return command
        return None

    def _transition(
        self,
        transaction: InterventionTransaction,
        target: InterventionLifecycleState,
        reason: str,
    ) -> None:
        current = _lifecycle_value(transaction.state)
        target_value = target.value
        if current == target_value:
            return
        allowed = INTERVENTION_ALLOWED_TRANSITIONS.get(current, frozenset())
        if target_value not in allowed:
            raise ValueError(
                f"invalid intervention transition {current!r} -> {target_value!r}"
            )
        transition_wall = max(
            self._clock.unix_ms(),
            transaction.updated_at_unix_ms,
            transaction.transitions[-1].at_unix_ms
            if transaction.transitions
            else 0,
        )
        transaction.transitions.append(
            LifecycleTransition(
                from_state=InterventionLifecycleState(current),
                to_state=target,
                reason=reason,
                at_unix_ms=transition_wall,
                at_mono_ns=self._clock.monotonic_ns(),
                boot_id=self._clock.boot_id,
            )
        )
        transaction.state = target
        transaction.revision += 1
        transaction.updated_at_unix_ms = transition_wall

    def _touch(self, transaction: InterventionTransaction) -> None:
        """Advance persisted wall metadata without permitting regression."""

        transaction.updated_at_unix_ms = max(
            transaction.updated_at_unix_ms,
            self._clock.unix_ms(),
        )

    async def register_proposal(self, manifest: ActionManifest) -> InterventionTransaction:
        """Persist a proposal before it is delivered to any surface."""

        async with self._lock:
            await self._ensure_loaded_unlocked()
            _validate_manifest_semantics(manifest)
            existing = self._journal.transactions.get(manifest.intervention_id)
            if existing is not None:
                if existing.manifest.manifest_sha256 != manifest.manifest_sha256:
                    raise ValueError(
                        "intervention_id already registered with a different manifest"
                    )
                return existing.model_copy(deep=True)
            now = self._clock.unix_ms()
            transaction = InterventionTransaction(
                intervention_id=manifest.intervention_id,
                manifest=manifest,
                created_at_unix_ms=now,
                updated_at_unix_ms=now,
                transitions=[
                    LifecycleTransition(
                        from_state=None,
                        to_state=InterventionLifecycleState.PROPOSED,
                        reason="manifest_registered",
                        at_unix_ms=now,
                        at_mono_ns=self._clock.monotonic_ns(),
                        boot_id=self._clock.boot_id,
                    )
                ],
            )
            self._journal.transactions[manifest.intervention_id] = transaction
            self._archive_terminal_unlocked()
            await self._store.save(self._journal)
            return transaction.model_copy(deep=True)

    async def mark_delivered(self, intervention_id: str) -> InterventionTransaction:
        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = self._journal.transactions[intervention_id]
            self._transition(
                transaction,
                InterventionLifecycleState.DELIVERED,
                "proposal_delivered",
            )
            await self._store.save(self._journal)
            return transaction.model_copy(deep=True)

    def _denied(
        self,
        request: InterventionAuthorizationRequest,
        reason_code: Literal[
            "unknown_intervention",
            "manifest_mismatch",
            "action_mismatch",
            "consent_denied",
            "consent_changed",
            "execution_mode_denied",
            "transaction_closed",
            "invalid_request",
            "no_executor",
        ],
        detail: str,
    ) -> AuthorizationDenied:
        return AuthorizationDenied(
            authorization_request_id=request.authorization_request_id,
            intervention_id=request.intervention_id,
            manifest_sha256=request.manifest_sha256,
            reason_code=reason_code,
            detail=detail,
        )

    async def authorize_and_consume(
        self,
        request: InterventionAuthorizationRequest,
        *,
        source_client_id: str | None,
        autonomous: bool = False,
    ) -> InterventionApplyCommand | AuthorizationDenied:
        """Validate and durably consume exact authority before dispatch.

        No adapter call may occur before this method returns an apply command.
        Consent's lock stays held through the journal write, closing the
        reset-vs-apply time-of-check/time-of-use race.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = self._journal.transactions.get(request.intervention_id)
            if transaction is None:
                return self._denied(request, "unknown_intervention", "proposal not found")
            if _lifecycle_value(transaction.state) in _TERMINAL_STATES:
                return self._denied(
                    request,
                    "transaction_closed",
                    _lifecycle_value(transaction.state),
                )
            if transaction.manifest.manifest_sha256 != request.manifest_sha256:
                return self._denied(request, "manifest_mismatch", "proposal digest changed")
            manifest_expired = (
                self._clock.unix_ms() >= transaction.manifest.expires_at_unix_ms
            )
            if transaction.manifest.boot_id == self._clock.boot_id:
                manifest_expired = manifest_expired or (
                    self._clock.monotonic_ns()
                    - transaction.manifest.created_at_mono_ns
                ) // 1_000_000 >= transaction.manifest.ttl_ms
            if manifest_expired:
                return self._denied(
                    request,
                    "transaction_closed",
                    "proposal manifest expired",
                )
            request_age_ms = (
                self._clock.unix_ms() - request.requested_at_unix_ms
            )
            if (
                request_age_ms > _AUTHORIZATION_REQUEST_MAX_AGE_MS
                or request_age_ms
                < -_AUTHORIZATION_REQUEST_MAX_FUTURE_SKEW_MS
            ):
                return self._denied(
                    request,
                    "invalid_request",
                    "authorization gesture timestamp is stale or in the future",
                )
            if _lifecycle_value(transaction.state) not in {
                InterventionLifecycleState.DELIVERED.value,
                InterventionLifecycleState.APPLIED.value,
                InterventionLifecycleState.PARTIAL.value,
                InterventionLifecycleState.FAILED.value,
            }:
                return self._denied(
                    request,
                    "transaction_closed",
                    f"transaction is {_lifecycle_value(transaction.state)}",
                )
            if self._execution_mode == "suggest_only":
                return self._denied(
                    request,
                    "execution_mode_denied",
                    "workspace execution is disabled",
                )
            if autonomous and self._execution_mode != "research_autonomous":
                return self._denied(
                    request,
                    "execution_mode_denied",
                    "research autonomy was not explicitly enabled",
                )
            if not autonomous and request.source_surface == "http":
                # HTTP is permitted only when the caller supplies the exact
                # manifest request; local capability-token middleware remains
                # the transport authentication boundary.
                pass

            for entry in transaction.authorizations:
                if (
                    entry.authorization.authorization_request_id
                    == request.authorization_request_id
                ):
                    return self._denied(
                        request,
                        "transaction_closed",
                        "authorization request was already consumed",
                    )

            manifest_by_id = {
                action.action_id: action for action in transaction.manifest.body.actions
            }
            requested_ids = tuple(request.approved_action_ids)
            if any(action_id not in manifest_by_id for action_id in requested_ids):
                return self._denied(
                    request,
                    "action_mismatch",
                    "approved action set is not a manifest subset",
                )
            selected = tuple(
                action
                for action in transaction.manifest.body.actions
                if action.action_id in set(requested_ids)
            )
            # Adapter operation keys intentionally bind one manifest action
            # to one lifetime operation. A new authorization for an action
            # that was already authorized (even if it failed or was restored)
            # would collide with that durable client record. Retrying requires
            # a freshly materialized proposal/action id; other untouched
            # actions in this manifest may still be authorized separately.
            previously_authorized = {
                action_id
                for entry in transaction.authorizations
                for action_id in entry.authorization.authorized_action_ids
            }
            repeated = set(requested_ids) & previously_authorized
            if repeated:
                return self._denied(
                    request,
                    "action_mismatch",
                    "action already had one-time authority in this proposal: "
                    + ",".join(sorted(repeated)),
                )
            consent_requests = tuple(
                (
                    canonical_action_type(action.capability),
                    action.required_consent_level,
                )
                for action in selected
            )

            async with self._consent_ladder.exact_decision_scope(
                consent_requests
            ) as (decisions, consent_revision):
                denied = [decision for decision in decisions if not decision.allowed]
                if denied:
                    return self._denied(
                        request,
                        "consent_denied",
                        "; ".join(decision.reason for decision in denied)[:500],
                    )
                now_wall = self._clock.unix_ms()
                remaining_manifest_ms = (
                    transaction.manifest.expires_at_unix_ms - now_wall
                )
                if transaction.manifest.boot_id == self._clock.boot_id:
                    elapsed_manifest_ms = max(
                        0,
                        self._clock.monotonic_ns()
                        - transaction.manifest.created_at_mono_ns,
                    ) // 1_000_000
                    remaining_manifest_ms = min(
                        remaining_manifest_ms,
                        transaction.manifest.ttl_ms - elapsed_manifest_ms,
                    )
                effective_ttl_ms = min(
                    self._authorization_ttl_ms,
                    remaining_manifest_ms,
                )
                if effective_ttl_ms < 1:
                    return self._denied(
                        request,
                        "transaction_closed",
                        "proposal manifest expired before authorization commit",
                    )
                authorization = ActionAuthorization(
                    authorization_request_id=request.authorization_request_id,
                    intervention_id=request.intervention_id,
                    manifest_sha256=request.manifest_sha256,
                    authorized_action_ids=requested_ids,
                    consent_revision=consent_revision,
                    authorization_kind=(
                        "research_autonomous" if autonomous else "user_confirmed"
                    ),
                    source_surface=("daemon" if autonomous else request.source_surface),
                    source_client_id=source_client_id,
                    requester_boot_id=request.boot_id,
                    issued_at_unix_ms=now_wall,
                    issued_at_mono_ns=self._clock.monotonic_ns(),
                    expires_at_unix_ms=now_wall + effective_ttl_ms,
                    ttl_ms=effective_ttl_ms,
                    boot_id=self._clock.boot_id,
                )
                entry = AuthorizationLedgerEntry(
                    authorization=authorization,
                    state=AuthorizationState.CONSUMED,
                    consumed_at_unix_ms=now_wall,
                    consumed_at_mono_ns=self._clock.monotonic_ns(),
                    state_reason="consumed_before_dispatch",
                )
                transaction.authorizations.append(entry)
                self._transition(
                    transaction,
                    InterventionLifecycleState.AUTHORIZED,
                    "exact_manifest_authorized",
                )
                self._transition(
                    transaction,
                    InterventionLifecycleState.APPLYING,
                    "authorization_consumed_before_dispatch",
                )
                await self._store.save(self._journal)

            return InterventionApplyCommand(
                manifest=transaction.manifest,
                authorization=authorization,
                actions=selected,
            )

    async def bind_dispatch_targets(
        self,
        command_id: str,
        action_client_instance_ids: dict[str, str],
    ) -> None:
        """Persist exact executor ownership before the first wire send.

        ``command_id`` is either an apply authorization id or a restore id.
        A restore may be routed to the currently reachable action-owner subset so
        an already-mutated surface can be compensated even while a peer is
        offline. Rebinding merges that subset into the latest durable record;
        receipt validation always uses the newest exact client per action.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction: InterventionTransaction | None = None
            expected_action_ids: set[str] = set()
            expected_restore_owners: dict[str, str] = {}
            is_apply_command = False
            apply_ledger: AuthorizationLedgerEntry | None = None
            for candidate in self._journal.transactions.values():
                ledger = next(
                    (
                        entry
                        for entry in candidate.authorizations
                        if entry.authorization.authorization_id == command_id
                    ),
                    None,
                )
                if ledger is not None:
                    transaction = candidate
                    is_apply_command = True
                    apply_ledger = ledger
                    expected_action_ids = set(
                        ledger.authorization.authorized_action_ids
                    )
                    break
                restore = next(
                    (
                        item
                        for item in reversed(candidate.restore_history)
                        if item.restore_id == command_id
                    ),
                    None,
                )
                if restore is not None:
                    transaction = candidate
                    expected_action_ids = {
                        action.action_id for action in restore.actions
                    }
                    expected_restore_owners = {
                        action.action_id: action.owner_client_instance_id
                        for action in restore.actions
                    }
                    break
            if transaction is None:
                raise ValueError("dispatch binding references unknown command")
            if is_apply_command:
                if (
                    apply_ledger is None
                    or apply_ledger.state != AuthorizationState.CONSUMED.value
                    or _lifecycle_value(transaction.state)
                    != InterventionLifecycleState.APPLYING.value
                ):
                    raise ValueError(
                        "apply dispatch binding references inactive authority"
                    )
                current_consent_revision = await self._consent_ladder.revision()
                if (
                    current_consent_revision
                    != apply_ledger.authorization.consent_revision
                ):
                    raise ValueError(
                        "consent revision changed before executor binding"
                    )
                now_wall = self._clock.unix_ms()
                expired = (
                    now_wall
                    >= apply_ledger.authorization.expires_at_unix_ms
                )
                if apply_ledger.authorization.boot_id == self._clock.boot_id:
                    elapsed_ms = max(
                        0,
                        self._clock.monotonic_ns()
                        - apply_ledger.authorization.issued_at_mono_ns,
                    ) // 1_000_000
                    expired = expired or (
                        elapsed_ms >= apply_ledger.authorization.ttl_ms
                    )
                if expired:
                    raise ValueError(
                        "authorization expired before executor binding"
                    )
            normalized = {
                str(key): str(value)
                for key, value in action_client_instance_ids.items()
            }
            normalized_action_ids = set(normalized)
            if (
                not normalized
                or not normalized_action_ids.issubset(expected_action_ids)
                or (
                    is_apply_command
                    and normalized_action_ids != expected_action_ids
                )
                or (
                    not is_apply_command
                    and any(
                        expected_restore_owners.get(action_id) != client_id
                        for action_id, client_id in normalized.items()
                    )
                )
            ):
                raise ValueError(
                    "dispatch binding contains an invalid action set"
                )
            latest_index = next(
                (
                    index
                    for index in range(
                        len(transaction.dispatch_history) - 1,
                        -1,
                        -1,
                    )
                    if transaction.dispatch_history[index].command_id
                    == command_id
                ),
                None,
            )
            merged: dict[str, str] = (
                dict(
                    transaction.dispatch_history[
                        latest_index
                    ].action_client_instance_ids
                )
                if latest_index is not None
                else {}
            )
            if is_apply_command and latest_index is not None:
                if merged != normalized:
                    raise ValueError(
                        "apply dispatch ownership is immutable after binding"
                    )
                return
            merged.update(normalized)
            if (
                latest_index is not None
                and merged
                == transaction.dispatch_history[
                    latest_index
                ].action_client_instance_ids
            ):
                return
            binding = ExecutorDispatchBinding(
                command_id=command_id,
                action_client_instance_ids=merged,
                bound_at_unix_ms=self._clock.unix_ms(),
                bound_at_mono_ns=self._clock.monotonic_ns(),
                boot_id=self._clock.boot_id,
            )
            if latest_index is None:
                transaction.dispatch_history.append(binding)
            else:
                # One current binding per command is sufficient evidence and
                # prevents reconnect retries from growing the journal without
                # bound. Lifecycle transitions still retain the state audit.
                transaction.dispatch_history[latest_index] = binding
            transaction.revision += 1
            self._touch(transaction)
            await self._store.save(self._journal)

    @staticmethod
    def _receipt_success(
        receipt: ActionReceipt,
        action: ManifestAction,
    ) -> bool:
        if receipt.status not in {
            ReceiptStatus.SUCCEEDED.value,
            ReceiptStatus.ALREADY_COMPLETE.value,
        }:
            return False
        if action.workspace_mutation:
            return receipt.verification == VerificationStatus.VERIFIED.value
        return receipt.verification in {
            VerificationStatus.VERIFIED.value,
            VerificationStatus.NOT_APPLICABLE.value,
        }

    @staticmethod
    def _receipt_effect_may_exist(receipt: ActionReceipt) -> bool:
        """Return whether a failed receipt requires ownership-safe recovery."""

        if receipt.inverse_payload_json is None:
            return False
        try:
            payload = json.loads(receipt.inverse_payload_json)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get(
            "cortexEffectMayExist"
        ) is True

    @staticmethod
    def _receipt_declares_no_effect(receipt: ActionReceipt) -> bool:
        """Return whether verified adapter evidence proves a semantic no-op."""

        if receipt.inverse_payload_json is None:
            return False
        try:
            payload = json.loads(receipt.inverse_payload_json)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("noEffect") is True

    @classmethod
    def _active_effects(
        cls,
        transaction: InterventionTransaction,
    ) -> tuple[set[str], set[str], dict[str, ActionReceipt]]:
        """Reduce the full receipt history into exact current ownership.

        A verified inverse is authoritative even when an older apply receipt
        arrives late over another socket. Manifest actions are one-shot, so
        the same action id can never acquire a later authorization that would
        legitimately supersede that inverse. ``possible`` contains failed
        applies whose adapter proved that an effect may nevertheless exist.
        """

        manifest_by_id = {
            action.action_id: action
            for action in transaction.manifest.body.actions
        }
        reversed_ids = {
            receipt.action_id
            for receipt in transaction.receipts
            if receipt.phase
            in {ReceiptPhase.COMPENSATE.value, ReceiptPhase.RESTORE.value}
            and receipt.status
            in {
                ReceiptStatus.SUCCEEDED.value,
                ReceiptStatus.ALREADY_COMPLETE.value,
            }
            and receipt.verification
            in {
                VerificationStatus.VERIFIED.value,
                VerificationStatus.NOT_APPLICABLE.value,
            }
        }
        definite: set[str] = set()
        possible: set[str] = set()
        latest_effect: dict[str, ActionReceipt] = {}
        for receipt in transaction.receipts:
            action = manifest_by_id.get(receipt.action_id)
            if (
                action is None
                or receipt.phase != ReceiptPhase.APPLY.value
                or receipt.action_id in reversed_ids
            ):
                continue
            prior = latest_effect.get(receipt.action_id)
            if prior is not None and prior.attempt > receipt.attempt:
                # Network arrival order is not attempt order. A delayed
                # receipt from an older attempt cannot replace newer effect
                # ownership evidence.
                continue
            if (
                cls._receipt_success(receipt, action)
                and not cls._receipt_declares_no_effect(receipt)
            ):
                definite.add(receipt.action_id)
                possible.discard(receipt.action_id)
                latest_effect[receipt.action_id] = receipt
            elif cls._receipt_effect_may_exist(receipt):
                possible.add(receipt.action_id)
                definite.discard(receipt.action_id)
                latest_effect[receipt.action_id] = receipt
        return definite, possible, latest_effect

    async def record_receipts(
        self,
        batch: InterventionReceiptBatch,
        *,
        source_client_type: str,
        source_client_id: str,
    ) -> tuple[InterventionLifecycleState, InterventionRestoreCommand | None]:
        """Validate, deduplicate, persist, and reduce a receipt batch."""

        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = self._journal.transactions.get(batch.intervention_id)
            if transaction is None:
                raise ValueError("receipt references unknown intervention")
            if transaction.manifest.manifest_sha256 != batch.manifest_sha256:
                raise ValueError("receipt manifest digest mismatch")
            manifest_by_id = {
                action.action_id: action
                for action in transaction.manifest.body.actions
            }

            receipt_phase = str(batch.receipts[0].phase)
            inverse_batch = receipt_phase in {
                ReceiptPhase.RESTORE.value,
                ReceiptPhase.COMPENSATE.value,
            }
            if inverse_batch:
                restore = next(
                    (
                        command
                        for command in reversed(transaction.restore_history)
                        if command.restore_id == batch.authorization_id
                    ),
                    None,
                )
                if restore is None:
                    raise ValueError("restore receipt does not match active restore")
                expected_phase = (
                    ReceiptPhase.COMPENSATE.value
                    if restore.reason == "partial_compensation"
                    else ReceiptPhase.RESTORE.value
                )
                if receipt_phase != expected_phase:
                    raise ValueError(
                        "inverse receipt phase does not match restore reason"
                    )
                allowed_ids = {action.action_id for action in restore.actions}
            else:
                ledger = next(
                    (
                        entry
                        for entry in transaction.authorizations
                        if entry.authorization.authorization_id == batch.authorization_id
                    ),
                    None,
                )
                if ledger is None or ledger.state != AuthorizationState.CONSUMED.value:
                    raise ValueError("receipt authorization was not consumed")
                allowed_ids = set(ledger.authorization.authorized_action_ids)

            executor_by_action = {
                action.action_id: action.executor
                for action in transaction.manifest.body.actions
            }
            expected_executor = {
                "chrome": "browser",
                "edge": "browser",
                "browser": "browser",
                "vscode": "editor",
                "editor": "editor",
                "desktop": "desktop",
                "terminal": "terminal",
                "daemon": "daemon",
            }.get(source_client_type, source_client_type)
            dispatch_binding = next(
                (
                    item
                    for item in reversed(transaction.dispatch_history)
                    if item.command_id == batch.authorization_id
                ),
                None,
            )
            if dispatch_binding is None:
                raise ValueError("receipt command has no durable dispatch binding")
            known_receipts = {receipt.receipt_id: receipt for receipt in transaction.receipts}
            known_idempotency = {
                receipt.idempotency_key: receipt
                for receipt in transaction.receipts
            }
            receipts_added = False
            for incoming in batch.receipts:
                if incoming.action_id not in allowed_ids:
                    raise ValueError("receipt action was not authorized")
                manifest_action = manifest_by_id.get(incoming.action_id)
                routed_executor = executor_by_action.get(incoming.action_id)
                if manifest_action is None or routed_executor is None:
                    raise ValueError("receipt action is absent from the manifest")
                if routed_executor != expected_executor:
                    raise ValueError("receipt source does not own action executor")
                bound_client_id = (
                    dispatch_binding.action_client_instance_ids.get(
                        incoming.action_id
                    )
                )
                if bound_client_id != source_client_id:
                    raise ValueError(
                        "receipt source is not the bound executor client"
                    )
                if (
                    incoming.source_client_type is not None
                    or incoming.source_client_id is not None
                ):
                    raise ValueError("client receipt must not self-assert source identity")
                if manifest_action.workspace_mutation:
                    if incoming.verification == VerificationStatus.NOT_APPLICABLE.value:
                        raise ValueError(
                            "workspace receipt cannot skip postcondition verification"
                        )
                    if incoming.status in {
                        ReceiptStatus.SUCCEEDED.value,
                        ReceiptStatus.ALREADY_COMPLETE.value,
                    }:
                        if incoming.verification != VerificationStatus.VERIFIED.value:
                            raise ValueError(
                                "successful workspace receipt must be verified"
                            )
                        if incoming.inverse_payload_json is None:
                            raise ValueError(
                                "successful workspace receipt lacks exact inverse"
                            )
                        if not incoming.after_fingerprint:
                            raise ValueError(
                                "successful workspace receipt lacks observed fingerprint"
                            )
                    elif incoming.verification != VerificationStatus.FAILED.value:
                        raise ValueError(
                            "non-success workspace receipt must report failed verification"
                        )
                stamped = incoming.model_copy(
                    update={
                        "source_client_type": source_client_type,
                        "source_client_id": source_client_id,
                    }
                )
                prior = known_receipts.get(stamped.receipt_id)
                if prior is not None:
                    if prior.model_dump(mode="json") != stamped.model_dump(mode="json"):
                        raise ValueError("receipt_id replayed with different content")
                    continue
                idempotent_prior = known_idempotency.get(
                    stamped.idempotency_key
                )
                if idempotent_prior is not None:
                    if (
                        idempotent_prior.model_dump(mode="json")
                        != stamped.model_dump(mode="json")
                    ):
                        raise ValueError(
                            "idempotency_key replayed with different content"
                        )
                    continue
                transaction.receipts.append(stamped)
                receipts_added = True
                known_receipts[stamped.receipt_id] = stamped
                known_idempotency[stamped.idempotency_key] = stamped

            if receipts_added:
                transaction.revision += 1
                self._touch(transaction)

            current_state = _lifecycle_value(transaction.state)
            if (
                not inverse_batch
                and current_state != InterventionLifecycleState.APPLYING.value
            ):
                # A transport race may trigger compensation before the apply
                # receipt arrives. Persist the late receipt for audit and
                # inverse provenance, but never move a restoring transaction
                # backwards into APPLYING/APPLIED.
                await self._store.save(self._journal)
                return InterventionLifecycleState(current_state), None

            if inverse_batch:
                active_restore = transaction.active_restore
                if (
                    active_restore is None
                    or active_restore.restore_id != batch.authorization_id
                ):
                    # A duplicate or delayed receipt from a completed restore
                    # remains audit evidence but cannot reduce a newer active
                    # restore or rewind an APPLIED aggregate.
                    await self._store.save(self._journal)
                    return InterventionLifecycleState(current_state), None
                latest_restore: dict[str, ActionReceipt] = {}
                for receipt in transaction.receipts:
                    if (
                        receipt.authorization_id == batch.authorization_id
                        and receipt.phase == receipt_phase
                    ):
                        current = latest_restore.get(receipt.action_id)
                        if current is None or receipt.attempt >= current.attempt:
                            latest_restore[receipt.action_id] = receipt
                if len(latest_restore) < len(allowed_ids):
                    # Exact restore commands are also split by executor. Wait
                    # for every owner before declaring a retryable failure.
                    await self._store.save(self._journal)
                    return InterventionLifecycleState.RESTORING, None
                success = all(
                    receipt.status
                    in {ReceiptStatus.SUCCEEDED.value, ReceiptStatus.ALREADY_COMPLETE.value}
                    and receipt.verification
                    in {VerificationStatus.VERIFIED.value, VerificationStatus.NOT_APPLICABLE.value}
                    for receipt in latest_restore.values()
                )
                definite, possible, _latest = self._active_effects(transaction)
                if success and receipt_phase == ReceiptPhase.COMPENSATE.value:
                    target_state = (
                        InterventionLifecycleState.APPLIED
                        if definite and not possible
                        else InterventionLifecycleState.RESTORED
                        if not definite and not possible
                        else InterventionLifecycleState.RESTORE_FAILED
                    )
                else:
                    target_state = (
                        InterventionLifecycleState.RESTORED
                        if success and not definite and not possible
                        else InterventionLifecycleState.RESTORE_FAILED
                    )
                self._transition(
                    transaction,
                    target_state,
                    (
                        "compensation_restored_prior_active_effects"
                        if success
                        and receipt_phase == ReceiptPhase.COMPENSATE.value
                        and target_state == InterventionLifecycleState.APPLIED
                        else "compensation_receipts_verified"
                        if success
                        and receipt_phase == ReceiptPhase.COMPENSATE.value
                        and target_state == InterventionLifecycleState.RESTORED
                        else "restore_receipts_verified"
                        if success
                        and target_state == InterventionLifecycleState.RESTORED
                        else "inverse_receipts_failed"
                    ),
                )
                if target_state in {
                    InterventionLifecycleState.APPLIED,
                    InterventionLifecycleState.RESTORED,
                }:
                    transaction.active_restore = None
                if target_state == InterventionLifecycleState.RESTORED:
                    self._archive_terminal_unlocked()
                await self._store.save(self._journal)
                return InterventionLifecycleState(
                    _lifecycle_value(transaction.state)
                ), None

            latest_apply: dict[str, ActionReceipt] = {}
            for receipt in transaction.receipts:
                if receipt.authorization_id != batch.authorization_id:
                    continue
                if receipt.phase == ReceiptPhase.APPLY.value:
                    current = latest_apply.get(receipt.action_id)
                    if current is None or receipt.attempt >= current.attempt:
                        latest_apply[receipt.action_id] = receipt

            successful_ids = {
                action_id
                for action_id, receipt in latest_apply.items()
                if self._receipt_success(receipt, manifest_by_id[action_id])
            }
            verified_effect_ids = {
                action_id
                for action_id in successful_ids
                if not self._receipt_declares_no_effect(latest_apply[action_id])
            }
            uncertain_ids = {
                action_id
                for action_id, receipt in latest_apply.items()
                if self._receipt_effect_may_exist(receipt)
            }
            received_ids = set(latest_apply)
            if received_ids != allowed_ids:
                # Each owning client sends its own batch. The first batch is
                # not a partial failure while another valid executor receipt
                # remains in flight.
                await self._store.save(self._journal)
                return InterventionLifecycleState.APPLYING, None
            failed_or_missing = allowed_ids - successful_ids - uncertain_ids
            definite_active, _possible_active, _latest = self._active_effects(
                transaction
            )
            if uncertain_ids or (verified_effect_ids and failed_or_missing):
                target = InterventionLifecycleState.PARTIAL
                reason = (
                    "unverified_apply_requires_compensation"
                    if uncertain_ids
                    else "partial_apply_requires_compensation"
                )
            elif successful_ids and not failed_or_missing:
                target = InterventionLifecycleState.APPLIED
                reason = "all_authorized_actions_verified"
            elif definite_active:
                target = InterventionLifecycleState.APPLIED
                reason = "authorization_failed_prior_effects_remain_active"
            else:
                target = InterventionLifecycleState.FAILED
                reason = "no_authorized_action_remains_applied"
            self._transition(transaction, target, reason)

            compensation: InterventionRestoreCommand | None = None
            if target == InterventionLifecycleState.PARTIAL:
                compensation = self._build_restore_unlocked(
                    transaction,
                    reason="partial_compensation",
                    only_action_ids=verified_effect_ids | uncertain_ids,
                )
            # Persist the reduction and required inverse command as one
            # aggregate snapshot. Cancellation must never strand PARTIAL
            # without the recovery command that makes it safe.
            await self._store.save(self._journal)
            return target, compensation

    async def validate_consumed(
        self,
        command: InterventionApplyCommand,
    ) -> bool:
        """Verify an apply command still names a consumed, unexpired grant.

        Local adapters call this at their final boundary. Remote clients
        receive only commands created by :meth:`authorize_and_consume`, but
        in-process adapters need the same defence against a fabricated model.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = self._journal.transactions.get(
                command.authorization.intervention_id
            )
            if transaction is None:
                return False
            if transaction.manifest != command.manifest:
                return False
            entry = next(
                (
                    item
                    for item in transaction.authorizations
                    if item.authorization.authorization_id
                    == command.authorization.authorization_id
                ),
                None,
            )
            if entry is None or entry.state != AuthorizationState.CONSUMED.value:
                return False
            if entry.authorization != command.authorization:
                return False
            now_wall = self._clock.unix_ms()
            if now_wall >= command.authorization.expires_at_unix_ms:
                return False
            if command.authorization.boot_id == self._clock.boot_id:
                elapsed_ms = (
                    self._clock.monotonic_ns()
                    - command.authorization.issued_at_mono_ns
                ) // 1_000_000
                if elapsed_ms >= command.authorization.ttl_ms:
                    return False
            return _lifecycle_value(transaction.state) in {
                InterventionLifecycleState.APPLYING.value,
                InterventionLifecycleState.PARTIAL.value,
                InterventionLifecycleState.APPLIED.value,
            }

    async def claim_consent_evidence(self, receipt_id: str) -> bool:
        """Claim one verified apply receipt exactly once across restarts.

        Consent escalation is intentionally downstream of verified adapter
        evidence. Persisting the claim *before* updating the ladder chooses a
        safe under-count if the process crashes between stores; replay can
        never count one approval twice and accidentally increase autonomy.
        WP7 replaces the two-store sequence with one SQLite transaction.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            match = next(
                (
                    (transaction, receipt)
                    for transaction in self._journal.transactions.values()
                    for receipt in transaction.receipts
                    if receipt.receipt_id == receipt_id
                ),
                None,
            )
            if match is None:
                return False
            transaction, receipt = match
            if (
                receipt.phase != ReceiptPhase.APPLY.value
                or receipt.status
                not in {
                    ReceiptStatus.SUCCEEDED.value,
                    ReceiptStatus.ALREADY_COMPLETE.value,
                }
                or receipt.verification != VerificationStatus.VERIFIED.value
                or self._receipt_declares_no_effect(receipt)
            ):
                return False
            if receipt_id in transaction.consent_evidence_receipt_ids:
                return False
            transaction.consent_evidence_receipt_ids.append(receipt_id)
            transaction.revision += 1
            self._touch(transaction)
            await self._store.save(self._journal)
            return True

    def _build_restore_unlocked(
        self,
        transaction: InterventionTransaction,
        *,
        reason: Literal[
            "user_undo",
            "dismissed",
            "snoozed",
            "timed_out",
            "natural_recovery",
            "system_cancelled",
            "partial_compensation",
            "startup_recovery",
            "emergency_restore",
        ],
        only_action_ids: set[str] | None = None,
    ) -> InterventionRestoreCommand:
        definite, possible, latest_effect_receipt = self._active_effects(
            transaction
        )
        candidate_ids = definite | possible
        if only_action_ids is not None:
            candidate_ids &= only_action_ids
        # Crash in APPLYING may have occurred after a client effect but before
        # its receipt. Include every reversible authorized action with an empty
        # inverse; clients consult their own Cortex-owned operation journal and
        # respond already_complete if no effect exists.
        if _lifecycle_value(transaction.state) == InterventionLifecycleState.APPLYING.value:
            latest_authorization = next(
                (
                    entry
                    for entry in reversed(transaction.authorizations)
                    if entry.state == AuthorizationState.CONSUMED.value
                ),
                None,
            )
            if latest_authorization is not None:
                candidate_ids.update(
                    latest_authorization.authorization.authorized_action_ids
                )

        restore_actions: list[RestoreAction] = []
        for action in reversed(transaction.manifest.body.actions):
            if action.action_id not in candidate_ids or not action.workspace_mutation:
                continue
            if action.reverse_capability is None:
                continue
            inverse_receipt = latest_effect_receipt.get(action.action_id)
            auth_id = (
                inverse_receipt.authorization_id
                if inverse_receipt is not None
                else next(
                    (
                        entry.authorization.authorization_id
                        for entry in reversed(transaction.authorizations)
                        if action.action_id
                        in entry.authorization.authorized_action_ids
                    ),
                    "unknown",
                )
            )
            original_binding = next(
                (
                    item
                    for item in reversed(transaction.dispatch_history)
                    if item.command_id == auth_id
                ),
                None,
            )
            owner_client_instance_id = (
                inverse_receipt.source_client_id
                if inverse_receipt is not None
                and inverse_receipt.source_client_id is not None
                else original_binding.action_client_instance_ids.get(
                    action.action_id
                )
                if original_binding is not None
                else None
            )
            # A forward effect cannot be sent before its action-level owner
            # binding is durable. If neither a stamped receipt nor that
            # binding exists, there is no client instance to which an exact
            # inverse could safely be addressed and no evidence an effect was
            # dispatched.
            if owner_client_instance_id is None:
                continue
            restore_actions.append(
                RestoreAction(
                    action_id=action.action_id,
                    executor=action.executor,
                    reverse_capability=action.reverse_capability,
                    inverse_payload_json=(
                        inverse_receipt.inverse_payload_json
                        if inverse_receipt is not None
                        and inverse_receipt.inverse_payload_json is not None
                        else "{}"
                    ),
                    original_authorization_id=auth_id,
                    inverse_receipt_id=(
                        inverse_receipt.receipt_id
                        if inverse_receipt is not None
                        else None
                    ),
                    owner_client_instance_id=owner_client_instance_id,
                )
            )
        command = InterventionRestoreCommand(
            intervention_id=transaction.intervention_id,
            manifest_sha256=transaction.manifest.manifest_sha256,
            reason=reason,
            requested_at_unix_ms=self._clock.unix_ms(),
            requested_at_mono_ns=self._clock.monotonic_ns(),
            boot_id=self._clock.boot_id,
            actions=tuple(restore_actions),
        )
        if _lifecycle_value(transaction.state) != InterventionLifecycleState.RESTORING.value:
            self._transition(
                transaction,
                InterventionLifecycleState.RESTORING,
                reason,
            )
        transaction.active_restore = command
        transaction.restore_history.append(command)
        return command

    async def record_dispatch_failure(
        self,
        authorization_id: str,
        *,
        reason: str,
    ) -> InterventionLifecycleState:
        """Close a consumed grant that reached no adapter.

        The transport preflights every required executor before its first
        send, so this transition cannot hide a partially dispatched effect.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = next(
                (
                    candidate
                    for candidate in self._journal.transactions.values()
                    if any(
                        entry.authorization.authorization_id == authorization_id
                        for entry in candidate.authorizations
                    )
                ),
                None,
            )
            if transaction is None:
                raise ValueError("dispatch failure references unknown authorization")
            if (
                _lifecycle_value(transaction.state)
                == InterventionLifecycleState.APPLYING.value
            ):
                definite, _possible, _latest = self._active_effects(
                    transaction
                )
                # A failed dispatch applies only to this one-time grant. An
                # earlier, independently authorized effect can still be
                # active on another surface, so the aggregate transaction
                # must remain APPLIED rather than falsely reporting that no
                # Cortex effect exists.
                target = (
                    InterventionLifecycleState.APPLIED
                    if definite
                    else InterventionLifecycleState.FAILED
                )
                self._transition(
                    transaction,
                    target,
                    (
                        "dispatch_failed_prior_effects_remain_active"
                        if definite
                        else reason[:200] or "executor_dispatch_failed"
                    ),
                )
                await self._store.save(self._journal)
            return InterventionLifecycleState(_lifecycle_value(transaction.state))

    async def compensate_partial_dispatch(
        self,
        authorization_id: str,
        *,
        reason: str,
    ) -> InterventionRestoreCommand | None:
        """Start recovery when a multi-executor send only partly landed.

        WebSocket delivery cannot be atomic across processes. Once at least
        one target accepted a command, the safe response is an idempotent
        inverse for every reversible authorized action; adapters with no
        local Cortex-owned operation answer ``already_complete``.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            match = next(
                (
                    (candidate, entry)
                    for candidate in self._journal.transactions.values()
                    for entry in candidate.authorizations
                    if (
                        entry.authorization.authorization_id
                        == authorization_id
                    )
                ),
                None,
            )
            if match is None:
                raise ValueError(
                    "partial dispatch references unknown authorization"
                )
            transaction, ledger = match
            state = _lifecycle_value(transaction.state)
            if (
                state == InterventionLifecycleState.RESTORING.value
                and transaction.active_restore is not None
            ):
                return transaction.active_restore.model_copy(deep=True)
            if state != InterventionLifecycleState.APPLYING.value:
                return None
            command = self._build_restore_unlocked(
                transaction,
                reason="partial_compensation",
                only_action_ids=set(
                    ledger.authorization.authorized_action_ids
                ),
            )
            if not command.actions:
                # The executable WP-6 catalog is reversible-only. Retain a
                # defensive terminal fallback if a future schema violates
                # that invariant instead of stranding RESTORING forever.
                self._transition(
                    transaction,
                    InterventionLifecycleState.RESTORED,
                    "partial_dispatch_had_no_reversible_effect",
                )
                transaction.active_restore = None
                await self._store.save(self._journal)
                return None
            logger.warning(
                "Compensating partial dispatch %s: %s",
                authorization_id,
                reason[:200] or "partial_executor_dispatch",
            )
            await self._store.save(self._journal)
            return command.model_copy(deep=True)

    async def request_restore(
        self,
        intervention_id: str,
        *,
        reason: Literal[
            "user_undo",
            "dismissed",
            "snoozed",
            "timed_out",
            "natural_recovery",
            "system_cancelled",
            "partial_compensation",
            "startup_recovery",
            "emergency_restore",
        ],
    ) -> InterventionRestoreCommand | None:
        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = self._journal.transactions.get(intervention_id)
            if transaction is None:
                return None
            state = _lifecycle_value(transaction.state)
            if state in _TERMINAL_STATES:
                return None
            if state in {
                InterventionLifecycleState.PROPOSED.value,
                InterventionLifecycleState.DELIVERED.value,
                InterventionLifecycleState.AUTHORIZED.value,
            }:
                self._transition(
                    transaction,
                    InterventionLifecycleState.ABANDONED,
                    "closed_before_workspace_effect",
                )
                self._archive_terminal_unlocked()
                await self._store.save(self._journal)
                return None
            reusable = self._reusable_restore_unlocked(transaction)
            if reusable is not None:
                if state != InterventionLifecycleState.RESTORING.value:
                    await self._store.save(self._journal)
                return reusable.model_copy(deep=True)
            command = self._build_restore_unlocked(transaction, reason=reason)
            if not command.actions:
                self._transition(
                    transaction,
                    InterventionLifecycleState.RESTORED,
                    "no_workspace_effect_to_restore",
                )
                transaction.active_restore = None
                self._archive_terminal_unlocked()
                await self._store.save(self._journal)
                return None
            await self._store.save(self._journal)
            return command.model_copy(deep=True)

    async def request_restore_all(
        self,
        *,
        reason: RestoreReason = "emergency_restore",
    ) -> list[InterventionRestoreCommand]:
        """Close every proposal and build exact inverses for every effect.

        This is the deterministic authority behind shutdown, consent reset,
        and the user-facing emergency restore. Commands retain their stable
        action owners and remain retryable when an owner is offline.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            commands: list[InterventionRestoreCommand] = []
            for transaction in self._journal.transactions.values():
                state = _lifecycle_value(transaction.state)
                if state in _TERMINAL_STATES:
                    continue
                if state in {
                    InterventionLifecycleState.PROPOSED.value,
                    InterventionLifecycleState.DELIVERED.value,
                    InterventionLifecycleState.AUTHORIZED.value,
                }:
                    self._transition(
                        transaction,
                        InterventionLifecycleState.ABANDONED,
                        "global_restore_closed_before_workspace_effect",
                    )
                    for entry in transaction.authorizations:
                        if entry.state == AuthorizationState.ISSUED.value:
                            entry.state = AuthorizationState.REVOKED
                            entry.state_reason = str(reason)
                    continue
                reusable = self._reusable_restore_unlocked(transaction)
                if reusable is not None:
                    commands.append(reusable.model_copy(deep=True))
                    continue
                command = self._build_restore_unlocked(
                    transaction,
                    reason=reason,
                )
                if command.actions:
                    commands.append(command.model_copy(deep=True))
                else:
                    self._transition(
                        transaction,
                        InterventionLifecycleState.RESTORED,
                        "global_restore_found_no_workspace_effect",
                    )
                    transaction.active_restore = None
            self._archive_terminal_unlocked()
            await self._store.save(self._journal)
            return commands

    async def recover_unfinished(self) -> list[InterventionRestoreCommand]:
        """Revoke stale authority and build startup restores after a crash."""

        async with self._lock:
            await self._ensure_loaded_unlocked()
            restores: list[InterventionRestoreCommand] = []
            for transaction in self._journal.transactions.values():
                state = _lifecycle_value(transaction.state)
                if state in {
                    InterventionLifecycleState.PROPOSED.value,
                    InterventionLifecycleState.DELIVERED.value,
                    InterventionLifecycleState.AUTHORIZED.value,
                }:
                    self._transition(
                        transaction,
                        InterventionLifecycleState.ABANDONED,
                        "startup_revoked_unfinished_authority",
                    )
                elif state in {
                    InterventionLifecycleState.APPLYING.value,
                    InterventionLifecycleState.APPLIED.value,
                    InterventionLifecycleState.PARTIAL.value,
                    InterventionLifecycleState.FAILED.value,
                    InterventionLifecycleState.RESTORING.value,
                    InterventionLifecycleState.RESTORE_FAILED.value,
                }:
                    reusable = self._reusable_restore_unlocked(transaction)
                    if reusable is not None:
                        command = reusable
                    else:
                        command = self._build_restore_unlocked(
                            transaction,
                            reason="startup_recovery",
                        )
                    if command.actions:
                        restores.append(command.model_copy(deep=True))
                    else:
                        self._transition(
                            transaction,
                            InterventionLifecycleState.RESTORED,
                            "startup_found_no_workspace_effect",
                        )
                        transaction.active_restore = None
            self._archive_terminal_unlocked()
            await self._store.save(self._journal)
            return restores

    async def get_transaction(
        self,
        intervention_id: str,
    ) -> InterventionTransaction | None:
        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = self._journal.transactions.get(intervention_id)
            return transaction.model_copy(deep=True) if transaction else None

    async def abandon(self, intervention_id: str, reason: str) -> bool:
        """Close a proposal only while no workspace effect can exist.

        Once an authorization has been consumed, transport delivery is
        inherently ambiguous. Such a transaction must go through
        :meth:`request_restore`; relabelling it ABANDONED would erase the
        obligation to compensate a possible client-side write.
        """

        async with self._lock:
            await self._ensure_loaded_unlocked()
            transaction = self._journal.transactions.get(intervention_id)
            if transaction is None:
                return False
            state = _lifecycle_value(transaction.state)
            if state in _TERMINAL_STATES or state not in {
                InterventionLifecycleState.PROPOSED.value,
                InterventionLifecycleState.DELIVERED.value,
                InterventionLifecycleState.AUTHORIZED.value,
            }:
                return False
            self._transition(
                transaction,
                InterventionLifecycleState.ABANDONED,
                reason[:200] or "abandoned",
            )
            transaction.active_restore = None
            for entry in transaction.authorizations:
                if entry.state == AuthorizationState.ISSUED.value:
                    entry.state = AuthorizationState.REVOKED
                    entry.state_reason = reason[:500]
            self._archive_terminal_unlocked()
            await self._store.save(self._journal)
            return True


__all__ = [
    "ExecutionMode",
    "InterventionTransactionCoordinator",
    "RestoreReason",
    "build_action_manifest",
]
