"""
Intervention Engine — Executor

Applies an intervention plan to the workspace by dispatching adapter
commands to VS Code, Chrome, terminal, and desktop overlay. Tracks
all mutations for later restoration.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from cortex.libs.adapters.registry import AdapterRegistry
from cortex.libs.schemas.intervention import AdapterCommand, InterventionPlan

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-action consent decision callable type (Phase-4b TASK 1)
# ---------------------------------------------------------------------------
#
# Signature: ``async (action_type: str, requested_level: int) -> bool``.
# Return True when the requested action is permitted; False when the
# consent ladder denies the action. The ``apply()`` loop short-circuits a
# denied command into a Mutation with ``success=False`` and the reason
# ``"consent_denied"`` so callers can surface it in observability without
# crashing the workspace.

ConsentDecisionFn = Callable[[str, int], Awaitable[bool]]


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


class WorkspaceAdapter(Protocol):
    """Protocol for workspace adapters that can execute commands."""

    async def execute(self, action: str, params: dict[str, Any]) -> bool:
        """Execute an adapter command. Returns True on success."""
        ...


# ---------------------------------------------------------------------------
# Mutation tracking
# ---------------------------------------------------------------------------


@dataclass
class Mutation:
    """Record of a single workspace mutation."""

    adapter: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0
    success: bool = False
    reverse_action: str | None = None
    # Phase-4b TASK 1: optional structured reason for failures so callers
    # (runtime_daemon → WS broadcast) can surface
    # ``"consent_denied"`` / ``"no_active_editor"`` / ``"adapter_missing"``
    # without parsing free-form log lines.
    reason: str | None = None

    @property
    def is_reversible(self) -> bool:
        """Check if this mutation can be reversed."""
        return self.reverse_action is not None


# Action → reverse action mapping
#
# Membership policy: only actions whose effect can be sensibly undone by
# another single adapter command belong in this map. Actions that are
# either inherently one-shot (e.g. a copy_to_clipboard, a notification,
# a guided breathing overlay) or whose "undo" would itself be
# user-driven (e.g. the user reopens the tab they just closed) are
# omitted on purpose. The downstream "Restore" pill in the desktop
# overlay gates its visibility on membership here.
_REVERSE_ACTIONS: dict[str, str] = {
    # Legacy adapter-level mutations.
    "hide_tabs_except_active": "show_all_tabs",
    "collapse_before_error": "expand_terminal",
    "fold_except_current": "unfold_all",
    "dim_background": "remove_dim",
    "show_overlay": "hide_overlay",
    # Phase-4a Debt-1: round out the reversibility map for the suggested-
    # action vocabulary (``intervention.py::SuggestedAction.action_type``).
    # Tab actions are reversible via the standard browser undo paths; the
    # extension owns the actual reopen via captured tab metadata.
    "close_tab": "reopen_tab",
    "group_tabs": "ungroup_tabs",
    "bookmark_and_close": "reopen_from_bookmark",
    # NOTE: ``take_biology_break``, ``resume_last_active_file``,
    # ``prompt_micro_commit``, ``suggest_movement_break``, ``open_url``,
    # ``search_error``, ``highlight_tab``, ``save_session``,
    # ``copy_to_clipboard`` and ``start_timer`` are intentionally
    # omitted — they have no sensible single-step reverse mutation.
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class InterventionExecutor:
    """
    Executes an intervention plan by dispatching commands to adapters.

    Tracks all mutations for restoration. Ensures no destructive operations.
    """

    def __init__(self, adapter_registry: AdapterRegistry | None = None) -> None:
        self._adapters: dict[str, WorkspaceAdapter] = {}
        self._registry: AdapterRegistry | None = adapter_registry
        self._active_mutations: dict[str, list[Mutation]] = {}
        # B17 (Phase 4.1): cumulative count of permanently-missing
        # adapter dispatches. Increments only after the one-shot retry
        # below also fails, so a transient adapter registration race
        # (extension reconnecting) doesn't inflate the counter.
        self._adapter_missing_total: int = 0
        # intervention_id → list of mutations
        # Phase-4b TASK 1: per-action consent gate. The daemon binds this
        # at startup so the executor can refuse an action whose live
        # consent level is below the action's policy minimum. When unset
        # (legacy callers / test rigs) the gate is permissive.
        self._consent_check: ConsentDecisionFn | None = None
        # P1-7: when the consent gate is not wired, default-deny any plan
        # that requires workspace mutation (i.e. not overlay-only). Set
        # this flag to True ONLY in unit tests that want to exercise the
        # executor logic without binding a real consent handler.
        self._allow_unwired_consent: bool = False
        # Phase-4b TASK 1: optional hook(s) for the three special actions
        # (resume_last_active_file, prompt_micro_commit, suggest_movement_break).
        # The daemon injects these so the executor can deliver the action
        # without taking a direct dependency on the editor adapter or WS
        # server. Each must be ``async (params: dict) -> tuple[bool, str | None]``
        # — the bool is success, the str is an optional ``reason`` returned
        # in the Mutation when success=False.
        self._editor_focus_hook: Callable[
            [dict[str, Any]], Awaitable[tuple[bool, str | None]]
        ] | None = None
        self._prompt_broadcast_hook: Callable[
            [str, dict[str, Any]], Awaitable[tuple[bool, str | None]]
        ] | None = None

    def set_consent_check(self, fn: ConsentDecisionFn | None) -> None:
        """Bind the per-action consent gate (Phase-4b TASK 1).

        The gate runs BEFORE adapter dispatch inside :meth:`apply`. A
        denial short-circuits the command into a Mutation with
        ``success=False, reason="consent_denied"`` so the daemon can
        record the outcome and inform AMIP that this plan should have
        been gated at LLM time.
        """
        self._consent_check = fn

    def set_editor_focus_hook(
        self,
        hook: Callable[[dict[str, Any]], Awaitable[tuple[bool, str | None]]] | None,
    ) -> None:
        """Bind the ``resume_last_active_file`` adapter hook.

        Signature: ``async (params) -> (success, reason)``. ``params``
        is the planner-emitted action params (currently empty for this
        action). ``reason`` is None on success or one of
        ``"no_active_editor"`` / ``"editor_send_failed"`` on failure.
        """
        self._editor_focus_hook = hook

    def set_prompt_broadcast_hook(
        self,
        hook: Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str | None]]]
        | None,
    ) -> None:
        """Bind the WS broadcast hook for ``prompt_micro_commit`` /
        ``suggest_movement_break``.

        Signature: ``async (action_type, params) -> (success, reason)``.
        The daemon implements this by sending a typed broadcast with the
        appropriate payload; failure returns ``"broadcast_failed"`` so
        the executor can mark the mutation accordingly.
        """
        self._prompt_broadcast_hook = hook

    def register_adapter(self, name: str, adapter: WorkspaceAdapter) -> None:
        """Register a workspace adapter (editor, browser, terminal, overlay)."""
        self._adapters[name] = adapter

    def has_adapter(self, name: str) -> bool:
        """Check if an adapter is registered."""
        if self._registry is not None and self._registry.has(name):
            return True
        return name in self._adapters

    def set_registry(self, registry: AdapterRegistry) -> None:
        """Set the adapter registry for new-style adapters."""
        self._registry = registry

    def _get_adapter(self, name: str) -> WorkspaceAdapter | None:
        """Get adapter by name, checking registry first, then legacy dict."""
        if self._registry is not None:
            adapter = self._registry.get(name)
            if adapter is not None:
                # AdapterRegistry.get returns Any; we rely on the runtime
                # protocol structural-check (WorkspaceAdapter.execute) rather
                # than a static cast — adapters are registered by name and
                # mypy cannot prove the type without a runtime isinstance.
                return adapter  # type: ignore[return-value]
        return self._adapters.get(name)

    async def apply(
        self,
        plan: InterventionPlan,
        commands: list[AdapterCommand],
        *,
        timestamp: float | None = None,
    ) -> list[Mutation]:
        """
        Apply intervention commands to workspace adapters.

        Args:
            plan: The intervention plan being applied.
            commands: Mapped adapter commands from the planner.
            timestamp: Override for testing.

        Returns:
            List of mutations that were applied (both successful and failed).
        """
        now = timestamp if timestamp is not None else time.monotonic()
        mutations: list[Mutation] = []

        # Phase-4b TASK M: map planner action_type → consent level int.
        # Mirrors ``runtime_daemon.consent_level_map`` so the gate runs
        # at the same policy resolution as the upstream check.
        _CONSENT_LEVELS = {
            "observe": 0, "suggest": 1, "preview": 2,
            "reversible_act": 3, "autonomous_act": 4,
        }
        plan_level_int = _CONSENT_LEVELS.get(plan.consent_level, 2)

        # P1-7: default-deny when consent handler is not wired and the
        # plan requires workspace mutation (i.e. anything beyond a pure
        # overlay that only shows information without touching tabs/files).
        # overlay_only plans are safe to execute without a consent gate.
        _OVERLAY_ONLY = "overlay_only"
        if (
            self._consent_check is None
            and not self._allow_unwired_consent
            and plan.level != _OVERLAY_ONLY
        ):
            logger.warning(
                "executor: consent_check not wired and plan.level=%r requires mutation"
                " — refusing plan %s",
                plan.level,
                plan.intervention_id,
            )
            for cmd in commands:
                mutations.append(Mutation(
                    adapter=cmd.adapter,
                    action=cmd.action,
                    params=dict(cmd.params),
                    timestamp=now,
                    success=False,
                    reason="consent_handler_not_wired",
                ))
            self._active_mutations[plan.intervention_id] = mutations
            return mutations

        # Phase-4b TASK M: action_type → handler hook dispatch. The
        # special actions are not adapter-bound; the daemon wires
        # hooks via ``set_editor_focus_hook`` (resume_last_active_file)
        # and ``set_prompt_broadcast_hook`` (prompt_micro_commit,
        # suggest_movement_break).
        _PROMPT_BROADCAST_ACTIONS = frozenset({
            "prompt_micro_commit",
            "suggest_movement_break",
        })

        for cmd in commands:
            mutation = Mutation(
                adapter=cmd.adapter,
                action=cmd.action,
                params=dict(cmd.params),
                timestamp=now,
                reverse_action=_REVERSE_ACTIONS.get(cmd.action),
            )

            # Phase-4b TASK M: per-action consent gate. Runs BEFORE
            # adapter dispatch so a denied command never touches the
            # workspace. Permissive when the daemon hasn't wired the
            # gate (legacy callers / test rigs).
            if self._consent_check is not None:
                try:
                    permitted = await self._consent_check(
                        cmd.action, plan_level_int,
                    )
                except Exception:
                    logger.exception(
                        "consent_check raised for action=%s; treating as denied",
                        cmd.action,
                    )
                    permitted = False
                if not permitted:
                    mutation.success = False
                    mutation.reason = "consent_denied"
                    mutations.append(mutation)
                    continue

            # Phase-4b TASK M: special action hooks. These don't run
            # through the adapter registry because their effect is a
            # daemon-side WS broadcast or an editor-focus message.
            if cmd.action == "resume_last_active_file":
                if self._editor_focus_hook is None:
                    mutation.success = False
                    mutation.reason = "hook_not_registered"
                    mutations.append(mutation)
                    continue
                try:
                    ok, reason = await self._editor_focus_hook(dict(cmd.params))
                    mutation.success = bool(ok)
                    if not ok:
                        mutation.reason = reason or "editor_focus_failed"
                except Exception:
                    logger.exception(
                        "editor_focus_hook raised for action=%s", cmd.action,
                    )
                    mutation.success = False
                    mutation.reason = "editor_focus_hook_raised"
                mutations.append(mutation)
                continue
            if cmd.action in _PROMPT_BROADCAST_ACTIONS:
                if self._prompt_broadcast_hook is None:
                    mutation.success = False
                    mutation.reason = "hook_not_registered"
                    mutations.append(mutation)
                    continue
                try:
                    ok, reason = await self._prompt_broadcast_hook(
                        cmd.action, dict(cmd.params),
                    )
                    mutation.success = bool(ok)
                    if not ok:
                        mutation.reason = reason or "broadcast_failed"
                except Exception:
                    logger.exception(
                        "prompt_broadcast_hook raised for action=%s",
                        cmd.action,
                    )
                    mutation.success = False
                    mutation.reason = "prompt_broadcast_hook_raised"
                mutations.append(mutation)
                continue

            adapter = self._get_adapter(cmd.adapter)
            if adapter is None:
                # B17 (Phase 4.1): one retry after 500ms to absorb a
                # transient adapter-registration race (e.g. the browser
                # extension just reconnected and the daemon hasn't yet
                # finished re-binding ``register_adapter``). Permanent
                # misses still set ``success=False`` and increment the
                # counter for /health visibility.
                import asyncio as _asyncio
                await _asyncio.sleep(0.5)
                adapter = self._get_adapter(cmd.adapter)
            if adapter is None:
                self._adapter_missing_total += 1
                logger.warning(
                    "No adapter registered for '%s', skipping %s (adapter_missing_total=%d)",
                    cmd.adapter,
                    cmd.action,
                    self._adapter_missing_total,
                )
                mutation.success = False
                mutation.reason = "adapter_missing"
                mutations.append(mutation)
                continue

            try:
                success = await adapter.execute(cmd.action, cmd.params)
                mutation.success = success
                if not success:
                    mutation.reason = "adapter_returned_false"
                    logger.warning(
                        "Adapter '%s' failed to execute '%s'",
                        cmd.adapter,
                        cmd.action,
                    )
            except Exception:
                logger.exception(
                    "Error executing '%s' on adapter '%s'",
                    cmd.action,
                    cmd.adapter,
                )
                mutation.success = False
                mutation.reason = "adapter_raised"

            mutations.append(mutation)

        # Store mutations by intervention_id
        self._active_mutations[plan.intervention_id] = mutations
        return mutations

    async def reverse(self, intervention_id: str) -> list[Mutation]:
        """
        Reverse all mutations for a given intervention.

        Returns list of reversal mutations (success/fail status set).
        """
        mutations = self._active_mutations.pop(intervention_id, [])
        reversals: list[Mutation] = []

        # Reverse in opposite order
        for m in reversed(mutations):
            if not m.success or not m.is_reversible:
                continue

            adapter = self._get_adapter(m.adapter)
            if adapter is None:
                continue

            reversal = Mutation(
                adapter=m.adapter,
                # m.reverse_action is typed as str | None; the guard
                # `if not m.is_reversible: continue` above (line ~183)
                # ensures it is non-None here. mypy cannot follow the
                # property-based narrowing across the attribute access.
                action=m.reverse_action,  # type: ignore[arg-type]
                timestamp=time.monotonic(),
            )

            try:
                success = await adapter.execute(reversal.action, {})
                reversal.success = success
            except Exception:
                logger.exception(
                    "Error reversing '%s' on adapter '%s'",
                    reversal.action,
                    m.adapter,
                )
                reversal.success = False

            reversals.append(reversal)

        return reversals

    def get_active_mutations(
        self, intervention_id: str
    ) -> list[Mutation]:
        """Get mutations for an active intervention."""
        return self._active_mutations.get(intervention_id, [])

    @property
    def active_intervention_ids(self) -> list[str]:
        """List of intervention IDs with active mutations."""
        return list(self._active_mutations.keys())

    def clear(self) -> None:
        """Clear all tracked mutations."""
        self._active_mutations.clear()
