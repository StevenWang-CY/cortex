"""Application service joining policy selection to durable outcome windows."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal
from uuid import UUID

from cortex.application.clock import Clock
from cortex.libs.schemas.policy import (
    PolicyDecisionRecord,
    PolicyDeliveryRecord,
    PolicyObservationKind,
)
from cortex.services.eval.policy_repository import FinalizationResult, PolicyRepository
from cortex.services.eval.production_policy import (
    DeterministicProductionPolicy,
    PolicySelectionInput,
)
from cortex.services.eval.research_policy import (
    ResearchPolicySettings,
    ResearchRandomizedPolicy,
)


class PolicyLifecycleService:
    """Own policy mode, durable selection, delivery, and one reward window."""

    def __init__(
        self,
        repository: PolicyRepository,
        *,
        clock: Clock,
        mode: Literal["deterministic", "research_randomized"] = "deterministic",
        reward_window_seconds: int = 300,
        research_settings: ResearchPolicySettings | None = None,
    ) -> None:
        if reward_window_seconds < 30 or reward_window_seconds > 86_400:
            raise ValueError("reward_window_seconds must be in 30..86400")
        if mode == "research_randomized" and research_settings is None:
            raise ValueError("research mode requires reviewed settings and separate consent")
        self.repository = repository
        self._clock = clock
        self.mode = mode
        self.reward_window_seconds = reward_window_seconds
        self._production = DeterministicProductionPolicy(clock=clock)
        self._research_settings = research_settings
        self._research: ResearchRandomizedPolicy | None = None
        self._start_lock = asyncio.Lock()
        self._decision_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self.repository.assert_integrity()
            if self.mode == "research_randomized":
                settings = self._research_settings
                if settings is None:  # pragma: no cover - constructor guards it
                    raise RuntimeError("research settings disappeared")
                restored = await self.repository.load_policy_state(
                    ResearchRandomizedPolicy.policy_name,
                    ResearchRandomizedPolicy.policy_version,
                )
                if restored is not None and (
                    restored.get("study_id") != settings.study_id
                    or restored.get("study_epoch") != settings.study_epoch
                    or restored.get("consent_version") != settings.consent_version
                    or restored.get("specification_sha256") != settings.specification_sha256
                ):
                    # ``policy_states`` retains the latest fixed epoch only.
                    # Historic decisions carry their own state/specification
                    # checksum; a newly reviewed epoch must cold-start.
                    restored = None
                self._research = ResearchRandomizedPolicy(
                    settings,
                    clock=self._clock,
                    state=restored,
                )
            self._started = True

    async def decide(self, request: PolicySelectionInput) -> PolicyDecisionRecord:
        await self.start()
        async with self._decision_lock:
            return await self._decide_serialized(request)

    async def _decide_serialized(
        self,
        request: PolicySelectionInput,
    ) -> PolicyDecisionRecord:
        """Select and persist while holding the per-process decision lock."""

        if self.mode == "research_randomized":
            research_catalog_available = {
                "no_action",
                "suggest_only",
            }.issubset(request.feasible_arms)
            if (
                not request.eligible
                or not request.available
                or request.recent_repeated_dismissal
                or not research_catalog_available
            ):
                # Hard safety/availability exclusions are still decision
                # points, but they are not randomizations. Log a deterministic
                # no-action record so they cannot enter MRT/OPE exports.
                exclusion = replace(
                    request,
                    available=False,
                    availability_reason=(
                        "research_excluded_repeated_dismissal"
                        if request.recent_repeated_dismissal
                        else "research_excluded_unavailable_or_infeasible"
                    ),
                    preferred_low_friction_arm=None,
                )
                decision = self._production.choose(exclusion)
                state = self._production.state()
                await self.repository.record_decision(
                    decision,
                    reward_window_seconds=self.reward_window_seconds,
                    policy_state=state,
                )
                await self.mark_no_action(decision.decision_id)
                return decision
            research = self._research
            if research is None:  # pragma: no cover - start guarantees it
                raise RuntimeError("research policy unavailable")
            decision, state = research.choose(request)
        else:
            decision = self._production.choose(request)
            state = self._production.state()
        await self.repository.record_decision(
            decision,
            reward_window_seconds=self.reward_window_seconds,
            policy_state=state,
        )
        if decision.selected_arm == "no_action":
            await self.mark_no_action(decision.decision_id)
        return decision

    async def mark_no_action(self, decision_id: UUID) -> None:
        await self.repository.mark_delivery(
            PolicyDeliveryRecord(
                decision_id=decision_id,
                status="not_applicable",
                delivered_at_unix_ms=None,
                intervention_id=None,
                reason="selected_no_action",
            )
        )

    async def mark_delivered(self, decision_id: UUID, intervention_id: str) -> None:
        await self.repository.mark_delivery(
            PolicyDeliveryRecord(
                decision_id=decision_id,
                status="delivered",
                delivered_at_unix_ms=self._clock.unix_ms(),
                intervention_id=intervention_id,
                reason="presented_on_authenticated_surface",
            )
        )

    async def mark_not_delivered(self, decision_id: UUID, reason: str) -> None:
        await self.repository.mark_delivery(
            PolicyDeliveryRecord(
                decision_id=decision_id,
                status="not_delivered",
                delivered_at_unix_ms=None,
                intervention_id=None,
                reason=reason[:160] or "presentation_not_delivered",
            )
        )

    async def observe_intervention(
        self,
        intervention_id: str,
        *,
        kind: PolicyObservationKind,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> bool:
        return await self.repository.record_observation_for_intervention(
            intervention_id,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=payload,
        )

    async def finalize_due(
        self,
        snapshot_provider: Callable[[], dict[str, Any] | None],
        *,
        contamination_provider: Callable[[], tuple[str, ...]] | None = None,
        limit: int = 100,
    ) -> tuple[FinalizationResult, ...]:
        results: list[FinalizationResult] = []
        for decision_id in await self.repository.due_decision_ids(limit=limit):
            snapshot = snapshot_provider()
            contamination = contamination_provider() if contamination_provider is not None else ()
            results.append(
                await self.repository.finalize(
                    decision_id,
                    final_snapshot=snapshot,
                    contamination=contamination,
                )
            )
        return tuple(results)


__all__ = ["PolicyLifecycleService"]
