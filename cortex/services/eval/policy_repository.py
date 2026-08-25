"""Transactional repository for the complete policy decision lifecycle."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cortex.application.clock import Clock
from cortex.libs.schemas.policy import (
    PolicyDecisionRecord,
    PolicyDeliveryRecord,
    PolicyObservation,
    PolicyObservationKind,
    PolicyRewardRecord,
    canonical_policy_json,
    policy_payload_sha256,
)
from cortex.storage.database import SQLiteDatabase, StorageCorruptionError


class PolicyLifecycleConflict(RuntimeError):
    """A supposedly idempotent lifecycle mutation conflicts with authority."""


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    reward: PolicyRewardRecord
    outcome_status: str
    already_finalized: bool


def _decode_object(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise StorageCorruptionError("policy payload is not a JSON object")
    return {str(key): item for key, item in value.items()}


def _reward_components(
    *,
    decision: dict[str, Any],
    delivery: dict[str, Any] | None,
    observations: list[dict[str, Any]],
    final_snapshot: dict[str, Any] | None,
) -> tuple[float, dict[str, float | str | bool | None]]:
    """Compute the prespecified ``helpfulness-v2`` reward exactly once.

    Direct user feedback dominates.  The small task-measure term uses only
    browser/editor behavior (complexity and error count), not the same support
    score that triggered treatment, avoiding circular self-validation.
    """

    latest_rating: str | None = None
    latest_action: str | None = None
    undo = False
    restore_failure = False
    delivery_failure = delivery is not None and delivery.get("status") == "not_delivered"
    for observation in observations:
        kind = str(observation.get("kind") or "")
        payload = observation.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if kind == "user_rating":
            rating = str(payload.get("rating") or "")
            if rating in {"thumbs_up", "thumbs_down"}:
                latest_rating = rating
        elif kind == "user_action":
            latest_action = str(payload.get("action") or "") or None
        elif kind == "undo":
            undo = True
        elif kind == "restore_failure":
            restore_failure = True

    rating_signal = (
        {"thumbs_up": 1.0, "thumbs_down": -1.0}.get(latest_rating, 0.0)
        if latest_rating is not None
        else 0.0
    )
    action_signal = (
        {
            "engaged": 0.5,
            "natural_recovery": 0.5,
            "dismissed": -0.75,
            "snoozed": -0.4,
            "restore": 0.0,
            "system_cancelled": 0.0,
        }.get(latest_action, 0.0)
        if latest_action is not None
        else 0.0
    )
    if undo:
        action_signal = min(action_signal, -0.75)

    initial = decision.get("context")
    initial = initial if isinstance(initial, dict) else {}
    task_signal = 0.0
    if final_snapshot is not None:
        try:
            complexity_improvement = float(initial.get("complexity_score", 0.0)) - float(
                final_snapshot.get("complexity_score", 0.0)
            )
            initial_errors = float(initial.get("error_count", 0.0))
            final_errors = float(final_snapshot.get("error_count", 0.0))
            error_improvement = (initial_errors - final_errors) / max(1.0, initial_errors + 1.0)
            task_signal = float(
                max(-1.0, min(1.0, 0.6 * complexity_improvement + 0.4 * error_improvement))
            )
        except (TypeError, ValueError):
            task_signal = 0.0

    delivered = delivery is not None and delivery.get("status") == "delivered"
    interruption_signal = -1.0 if delivered else 0.0
    safety_signal = -1.0 if restore_failure else 0.0
    transport_signal = -1.0 if delivery_failure else 0.0
    reward = (
        0.45 * rating_signal
        + 0.25 * action_signal
        + 0.15 * task_signal
        + 0.10 * safety_signal
        + 0.03 * transport_signal
        + 0.02 * interruption_signal
    )
    reward = float(max(-1.0, min(1.0, reward)))
    components: dict[str, float | str | bool | None] = {
        "reward_definition": "helpfulness-v2",
        "user_rating": latest_rating,
        "user_rating_signal": rating_signal,
        "terminal_user_action": latest_action,
        "user_action_signal": action_signal,
        "task_measure_signal": task_signal,
        "restore_failure": restore_failure,
        "delivery_failure": delivery_failure,
        "interruption_signal": interruption_signal,
        "post_snapshot_missing": final_snapshot is None,
        "weight_user_rating": 0.45,
        "weight_user_action": 0.25,
        "weight_task_measure": 0.15,
        "weight_restore_failure": 0.10,
        "weight_delivery_failure": 0.03,
        "weight_interruption": 0.02,
    }
    return reward, components


class PolicyRepository:
    """Serialize decision→delivery→observations→outcome/reward in SQLite."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def record_decision(
        self,
        decision: PolicyDecisionRecord,
        *,
        reward_window_seconds: int,
        policy_state: dict[str, Any],
    ) -> None:
        if reward_window_seconds < 1:
            raise ValueError("reward_window_seconds must be positive")
        state_document = dict(policy_state)
        state_digest = str(state_document.pop("state_sha256", "")) or policy_payload_sha256(
            state_document
        )
        if state_digest != decision.policy_state_sha256:
            raise ValueError("policy state and decision checksum diverge")
        state_json = canonical_policy_json(state_document)
        payload = decision.model_dump(mode="json")
        payload_json = canonical_policy_json(payload)
        payload_digest = hashlib.sha256(payload_json.encode()).hexdigest()
        context_json = canonical_policy_json(decision.context.model_dump(mode="json"))
        context_digest = hashlib.sha256(context_json.encode()).hexdigest()
        feasible_json = canonical_policy_json(list(decision.feasible_arms))
        propensities_json = (
            canonical_policy_json(decision.propensities)
            if decision.propensities is not None
            else None
        )
        closes_at = decision.occurred_at_unix_ms + reward_window_seconds * 1_000

        def write(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT payload_sha256 FROM policy_decisions WHERE decision_id=?",
                (str(decision.decision_id),),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != payload_digest:
                    raise PolicyLifecycleConflict("decision id already has different content")
                return
            point = connection.execute(
                "SELECT decision_id FROM policy_decisions WHERE decision_point_id=?",
                (str(decision.decision_point_id),),
            ).fetchone()
            if point is not None:
                raise PolicyLifecycleConflict("decision point was already consumed")
            connection.execute(
                "INSERT INTO policy_decisions("
                "decision_id, decision_point_id, policy_name, policy_version, "
                "policy_state_sha256, selected_arm, selected_probability, eligible, "
                "available, occurred_at_unix_ms, occurred_at_mono_ns, boot_id, "
                "intervention_id, payload_json, payload_sha256, policy_mode, session_id, "
                "context_schema_version, context_json, context_sha256, availability_reason, "
                "feasible_arms_json, propensities_json, supports_ope, randomization_id, "
                "random_seed_hex, random_counter, research_study_id, research_study_epoch, "
                "research_consent_version, research_specification_sha256, reward_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(decision.decision_id),
                    str(decision.decision_point_id),
                    decision.policy_name,
                    decision.policy_version,
                    decision.policy_state_sha256,
                    decision.selected_arm,
                    decision.selected_probability,
                    int(decision.eligible),
                    int(decision.available),
                    decision.occurred_at_unix_ms,
                    decision.occurred_at_mono_ns,
                    str(decision.boot_id),
                    payload_json,
                    payload_digest,
                    decision.policy_mode,
                    decision.session_id,
                    decision.context.schema_version,
                    context_json,
                    context_digest,
                    decision.availability_reason,
                    feasible_json,
                    propensities_json,
                    int(decision.supports_ope),
                    str(decision.randomization_id) if decision.randomization_id else None,
                    decision.random_seed_hex,
                    decision.random_counter,
                    decision.research_study_id,
                    decision.research_study_epoch,
                    decision.research_consent_version,
                    decision.research_specification_sha256,
                    decision.reward_version,
                ),
            )
            connection.execute(
                "INSERT INTO policy_outcome_windows("
                "decision_id, reward_version, status, opened_at_unix_ms, "
                "scheduled_close_at_unix_ms, initial_snapshot_json, "
                "initial_snapshot_sha256, contamination_json"
                ") VALUES (?, ?, 'pending', ?, ?, ?, ?, '[]')",
                (
                    str(decision.decision_id),
                    decision.reward_version,
                    decision.occurred_at_unix_ms,
                    closes_at,
                    context_json,
                    context_digest,
                ),
            )
            connection.execute(
                "INSERT INTO policy_states(policy_name, policy_version, state_json, "
                "state_sha256, updated_at_unix_ms) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(policy_name, policy_version) DO UPDATE SET "
                "state_json=excluded.state_json, state_sha256=excluded.state_sha256, "
                "updated_at_unix_ms=excluded.updated_at_unix_ms",
                (
                    decision.policy_name,
                    decision.policy_version,
                    state_json,
                    state_digest,
                    decision.occurred_at_unix_ms,
                ),
            )

        await self._database.transaction(write)

    async def load_policy_state(
        self,
        policy_name: str,
        policy_version: str,
    ) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute(
                "SELECT state_json, state_sha256 FROM policy_states "
                "WHERE policy_name=? AND policy_version=?",
                (policy_name, policy_version),
            ).fetchone()
            if row is None:
                return None
            value = _decode_object(str(row[0]))
            if policy_payload_sha256(value) != str(row[1]):
                raise StorageCorruptionError("policy state checksum mismatch")
            value["state_sha256"] = str(row[1])
            return value

        return await self._database.read(read)

    async def mark_delivery(self, delivery: PolicyDeliveryRecord) -> None:
        payload = delivery.model_dump(mode="json")
        payload_json = canonical_policy_json(payload)
        digest = hashlib.sha256(payload_json.encode()).hexdigest()

        def write(connection: sqlite3.Connection) -> None:
            decision = connection.execute(
                "SELECT selected_arm FROM policy_decisions WHERE decision_id=?",
                (str(delivery.decision_id),),
            ).fetchone()
            if decision is None:
                raise PolicyLifecycleConflict("delivery references an unknown decision")
            selected = str(decision[0])
            if selected == "no_action" and delivery.status != "not_applicable":
                raise PolicyLifecycleConflict("no_action must use not_applicable delivery")
            if selected != "no_action" and delivery.status == "not_applicable":
                raise PolicyLifecycleConflict("active arms require a delivery result")
            existing = connection.execute(
                "SELECT payload_sha256 FROM policy_deliveries WHERE decision_id=?",
                (str(delivery.decision_id),),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != digest:
                    raise PolicyLifecycleConflict("delivery was already finalized differently")
                return
            connection.execute(
                "INSERT INTO policy_deliveries(decision_id, delivery_status, "
                "delivered_at_unix_ms, intervention_id, payload_json, payload_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(delivery.decision_id),
                    delivery.status,
                    delivery.delivered_at_unix_ms,
                    delivery.intervention_id,
                    payload_json,
                    digest,
                ),
            )
            if delivery.intervention_id:
                collision = connection.execute(
                    "SELECT decision_id FROM policy_decisions "
                    "WHERE intervention_id=? AND decision_id<>?",
                    (delivery.intervention_id, str(delivery.decision_id)),
                ).fetchone()
                if collision is not None:
                    raise PolicyLifecycleConflict("intervention is bound to another decision")
                connection.execute(
                    "UPDATE policy_decisions SET intervention_id=? WHERE decision_id=?",
                    (delivery.intervention_id, str(delivery.decision_id)),
                )

        await self._database.transaction(write)

    async def record_observation_for_intervention(
        self,
        intervention_id: str,
        *,
        kind: PolicyObservationKind,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> bool:
        now = self._clock.unix_ms()

        def resolve(connection: sqlite3.Connection) -> tuple[str, str] | None:
            row = connection.execute(
                "SELECT d.decision_id, d.reward_version "
                "FROM policy_decisions d JOIN policy_outcome_windows w "
                "ON w.decision_id=d.decision_id AND w.reward_version=d.reward_version "
                "WHERE d.intervention_id=? AND w.status='pending' "
                "AND w.scheduled_close_at_unix_ms>=? "
                "ORDER BY d.occurred_at_unix_ms DESC LIMIT 1",
                (intervention_id, now),
            ).fetchone()
            return None if row is None else (str(row[0]), str(row[1]))

        resolved = await self._database.read(resolve)
        if resolved is None:
            return False
        decision_id, reward_version = resolved
        return await self.record_observation(
            PolicyObservation(
                decision_id=UUID(decision_id),
                reward_version=reward_version,
                kind=kind,
                idempotency_key=idempotency_key,
                observed_at_unix_ms=now,
                payload=payload,
            )
        )

    async def record_observation(self, observation: PolicyObservation) -> bool:
        payload_json = canonical_policy_json(observation.payload)
        if len(payload_json.encode("utf-8")) > 4_096:
            raise ValueError("policy observation payload exceeds 4096 bytes")
        digest = hashlib.sha256(payload_json.encode()).hexdigest()

        def write(connection: sqlite3.Connection) -> bool:
            window = connection.execute(
                "SELECT status, scheduled_close_at_unix_ms FROM policy_outcome_windows "
                "WHERE decision_id=? AND reward_version=?",
                (str(observation.decision_id), observation.reward_version),
            ).fetchone()
            if window is None:
                raise PolicyLifecycleConflict("observation references no outcome window")
            if str(window[0]) != "pending" or observation.observed_at_unix_ms > int(window[1]):
                return False
            existing = connection.execute(
                "SELECT payload_sha256, observation_kind FROM policy_observations "
                "WHERE decision_id=? AND reward_version=? AND idempotency_key=?",
                (
                    str(observation.decision_id),
                    observation.reward_version,
                    observation.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != digest or str(existing[1]) != observation.kind:
                    raise PolicyLifecycleConflict("observation idempotency key conflicts")
                return False
            connection.execute(
                "INSERT INTO policy_observations(observation_id, decision_id, reward_version, "
                "observation_kind, idempotency_key, observed_at_unix_ms, payload_json, "
                "payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(observation.observation_id),
                    str(observation.decision_id),
                    observation.reward_version,
                    observation.kind,
                    observation.idempotency_key,
                    observation.observed_at_unix_ms,
                    payload_json,
                    digest,
                ),
            )
            return True

        return await self._database.transaction(write)

    async def due_decision_ids(self, *, limit: int = 100) -> tuple[UUID, ...]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be in 1..10000")
        now = self._clock.unix_ms()
        return await self._database.read(
            lambda connection: tuple(
                UUID(str(row[0]))
                for row in connection.execute(
                    "SELECT decision_id FROM policy_outcome_windows "
                    "WHERE status='pending' AND scheduled_close_at_unix_ms<=? "
                    "ORDER BY scheduled_close_at_unix_ms, decision_id LIMIT ?",
                    (now, limit),
                )
            )
        )

    async def finalize(
        self,
        decision_id: UUID,
        *,
        final_snapshot: dict[str, Any] | None,
        contamination: tuple[str, ...] = (),
    ) -> FinalizationResult:
        final_json = canonical_policy_json(final_snapshot) if final_snapshot is not None else None
        final_digest = hashlib.sha256(final_json.encode()).hexdigest() if final_json else None
        now = self._clock.unix_ms()

        def write(connection: sqlite3.Connection) -> FinalizationResult:
            decision_row = connection.execute(
                "SELECT payload_json, reward_version, occurred_at_unix_ms "
                "FROM policy_decisions WHERE decision_id=?",
                (str(decision_id),),
            ).fetchone()
            if decision_row is None:
                raise PolicyLifecycleConflict("cannot finalize an unknown decision")
            decision = _decode_object(str(decision_row[0]))
            reward_version = str(decision_row[1])
            window = connection.execute(
                "SELECT status, opened_at_unix_ms, scheduled_close_at_unix_ms "
                "FROM policy_outcome_windows WHERE decision_id=? AND reward_version=?",
                (str(decision_id), reward_version),
            ).fetchone()
            if window is None:
                raise StorageCorruptionError("decision has no outcome window")
            if str(window[0]) != "pending":
                reward_row = connection.execute(
                    "SELECT reward_value, finalized_at_unix_ms, components_json "
                    "FROM policy_rewards WHERE decision_id=? AND reward_version=?",
                    (str(decision_id), reward_version),
                ).fetchone()
                if reward_row is None:
                    raise StorageCorruptionError("finalized window has no finalized reward")
                return FinalizationResult(
                    reward=PolicyRewardRecord(
                        decision_id=decision_id,
                        reward_version=reward_version,
                        value=float(reward_row[0]),
                        finalized_at_unix_ms=int(reward_row[1]),
                        components=_decode_object(str(reward_row[2])),
                    ),
                    outcome_status=str(window[0]),
                    already_finalized=True,
                )
            scheduled_close = int(window[2])
            if now < scheduled_close:
                raise PolicyLifecycleConflict("outcome window has not reached its scheduled close")

            delivery_row = connection.execute(
                "SELECT payload_json FROM policy_deliveries WHERE decision_id=?",
                (str(decision_id),),
            ).fetchone()
            if delivery_row is None:
                selected_arm = str(decision.get("selected_arm") or "")
                synthesized = PolicyDeliveryRecord(
                    decision_id=decision_id,
                    status=("not_applicable" if selected_arm == "no_action" else "not_delivered"),
                    delivered_at_unix_ms=None,
                    intervention_id=None,
                    reason="delivery_missing_at_window_close",
                )
                delivery = synthesized.model_dump(mode="json")
                delivery_json = canonical_policy_json(delivery)
                connection.execute(
                    "INSERT INTO policy_deliveries(decision_id, delivery_status, "
                    "delivered_at_unix_ms, intervention_id, payload_json, payload_sha256) "
                    "VALUES (?, ?, NULL, NULL, ?, ?)",
                    (
                        str(decision_id),
                        synthesized.status,
                        delivery_json,
                        hashlib.sha256(delivery_json.encode()).hexdigest(),
                    ),
                )
            else:
                delivery = _decode_object(str(delivery_row[0]))
            observations: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT observation_kind, payload_json, observed_at_unix_ms "
                "FROM policy_observations WHERE decision_id=? AND reward_version=? "
                "ORDER BY observed_at_unix_ms, rowid",
                (str(decision_id), reward_version),
            ):
                observations.append(
                    {
                        "kind": str(row[0]),
                        "payload": _decode_object(str(row[1])),
                        "observed_at_unix_ms": int(row[2]),
                    }
                )
            reward_value, components = _reward_components(
                decision=decision,
                delivery=delivery,
                observations=observations,
                final_snapshot=final_snapshot,
            )
            overlapping = connection.execute(
                "SELECT COUNT(*) FROM policy_deliveries l "
                "WHERE l.delivery_status='delivered' AND l.decision_id<>? "
                "AND l.delivered_at_unix_ms BETWEEN ? AND ?",
                (str(decision_id), int(window[1]), scheduled_close),
            ).fetchone()
            contamination_values = list(dict.fromkeys(contamination))
            if overlapping is not None and int(overlapping[0]) > 0:
                contamination_values.append("overlapping_policy_delivery")
            outcome_status = "finalized" if final_snapshot is not None else "censored"
            outcome_payload = {
                "schema_version": "policy-outcome/2.0",
                "decision_id": str(decision_id),
                "reward_version": reward_version,
                "status": outcome_status,
                "window_opened_at_unix_ms": int(window[1]),
                "scheduled_close_at_unix_ms": scheduled_close,
                "finalized_at_unix_ms": now,
                "final_snapshot": final_snapshot,
                "contamination": contamination_values,
                "missingness_reason": (
                    None if final_snapshot is not None else "post_snapshot_unavailable"
                ),
                "observations": observations,
            }
            outcome_json = canonical_policy_json(outcome_payload)
            outcome_digest = hashlib.sha256(outcome_json.encode()).hexdigest()
            components_json = canonical_policy_json(components)
            reward_payload = {
                "decision_id": str(decision_id),
                "reward_version": reward_version,
                "reward_value": reward_value,
                "finalized_at_unix_ms": now,
                "components": components,
            }
            reward_digest = policy_payload_sha256(reward_payload)
            connection.execute(
                "INSERT INTO policy_outcomes(decision_id, outcome_status, "
                "window_opened_at_unix_ms, window_closed_at_unix_ms, contamination_json, "
                "payload_json, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(decision_id),
                    outcome_status,
                    int(window[1]),
                    now,
                    canonical_policy_json(contamination_values),
                    outcome_json,
                    outcome_digest,
                ),
            )
            connection.execute(
                "INSERT INTO policy_rewards(decision_id, reward_version, reward_value, "
                "finalized_at_unix_ms, components_json, payload_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(decision_id),
                    reward_version,
                    reward_value,
                    now,
                    components_json,
                    reward_digest,
                ),
            )
            connection.execute(
                "UPDATE policy_outcome_windows SET status=?, finalized_at_unix_ms=?, "
                "final_snapshot_json=?, final_snapshot_sha256=?, contamination_json=?, "
                "missingness_reason=? WHERE decision_id=? AND reward_version=?",
                (
                    outcome_status,
                    now,
                    final_json,
                    final_digest,
                    canonical_policy_json(contamination_values),
                    None if final_snapshot is not None else "post_snapshot_unavailable",
                    str(decision_id),
                    reward_version,
                ),
            )
            reward = PolicyRewardRecord(
                decision_id=decision_id,
                reward_version=reward_version,
                value=reward_value,
                finalized_at_unix_ms=now,
                components=components,
            )
            return FinalizationResult(
                reward=reward,
                outcome_status=outcome_status,
                already_finalized=False,
            )

        return await self._database.transaction(write)

    async def export_rows(
        self,
        *,
        policy_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            where = "WHERE d.policy_mode=?" if policy_mode is not None else ""
            parameters: tuple[Any, ...] = (policy_mode,) if policy_mode is not None else ()
            rows: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT d.payload_json, l.payload_json, o.payload_json, "
                "r.reward_value, r.reward_version, r.finalized_at_unix_ms, "
                "r.components_json FROM policy_decisions d "
                "LEFT JOIN policy_deliveries l ON l.decision_id=d.decision_id "
                "LEFT JOIN policy_outcomes o ON o.decision_id=d.decision_id "
                "LEFT JOIN policy_rewards r ON r.decision_id=d.decision_id "
                "AND r.reward_version=d.reward_version "
                f"{where} ORDER BY d.occurred_at_unix_ms, d.decision_id",
                parameters,
            ):
                rows.append(
                    {
                        "decision": _decode_object(str(row[0])),
                        "delivery": _decode_object(str(row[1])) if row[1] else None,
                        "outcome": _decode_object(str(row[2])) if row[2] else None,
                        "reward": (
                            {
                                "value": float(row[3]),
                                "version": str(row[4]),
                                "finalized_at_unix_ms": int(row[5]),
                                "components": _decode_object(str(row[6])),
                            }
                            if row[3] is not None
                            else None
                        ),
                    }
                )
            return rows

        return await self._database.read(read)

    async def assert_integrity(self) -> None:
        """Check lifecycle cardinality and digest invariants at startup/tests."""

        def check(connection: sqlite3.Connection) -> None:
            invalid = connection.execute(
                "SELECT d.decision_id FROM policy_decisions d "
                "LEFT JOIN policy_outcome_windows w ON w.decision_id=d.decision_id "
                "AND w.reward_version=d.reward_version "
                "WHERE d.policy_mode<>'legacy_diagnostic' AND w.decision_id IS NULL LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise StorageCorruptionError(
                    f"policy decision {invalid[0]} has no versioned outcome window"
                )
            invalid = connection.execute(
                "SELECT w.decision_id FROM policy_outcome_windows w "
                "LEFT JOIN policy_rewards r ON r.decision_id=w.decision_id "
                "AND r.reward_version=w.reward_version "
                "WHERE w.status IN ('finalized','censored') AND r.decision_id IS NULL LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise StorageCorruptionError(
                    f"final policy outcome {invalid[0]} has no unique reward"
                )
            invalid = connection.execute(
                "SELECT w.decision_id FROM policy_outcome_windows w "
                "LEFT JOIN policy_deliveries l ON l.decision_id=w.decision_id "
                "WHERE w.status IN ('finalized','censored') AND l.decision_id IS NULL LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise StorageCorruptionError(
                    f"final policy outcome {invalid[0]} has no delivery status"
                )
            for row in connection.execute(
                "SELECT payload_json, payload_sha256, context_json, context_sha256, "
                "policy_mode, supports_ope, propensities_json FROM policy_decisions"
            ):
                if hashlib.sha256(str(row[0]).encode()).hexdigest() != str(row[1]):
                    raise StorageCorruptionError("policy decision payload checksum mismatch")
                if hashlib.sha256(str(row[2]).encode()).hexdigest() != str(row[3]):
                    raise StorageCorruptionError("policy context payload checksum mismatch")
                if str(row[4]) == "deterministic" and (int(row[5]) != 0 or row[6] is not None):
                    raise StorageCorruptionError(
                        "deterministic decision falsely declares randomized support"
                    )
                if str(row[4]) == "research_randomized" and (int(row[5]) != 1 or row[6] is None):
                    raise StorageCorruptionError(
                        "research decision lacks randomized support metadata"
                    )
            for row in connection.execute("SELECT state_json, state_sha256 FROM policy_states"):
                if hashlib.sha256(str(row[0]).encode()).hexdigest() != str(row[1]):
                    raise StorageCorruptionError("policy state payload checksum mismatch")
            for row in connection.execute(
                "SELECT payload_json, payload_sha256 FROM policy_deliveries"
            ):
                if hashlib.sha256(str(row[0]).encode()).hexdigest() != str(row[1]):
                    raise StorageCorruptionError("policy delivery payload checksum mismatch")
            for row in connection.execute(
                "SELECT payload_json, payload_sha256 FROM policy_outcomes"
            ):
                if hashlib.sha256(str(row[0]).encode()).hexdigest() != str(row[1]):
                    raise StorageCorruptionError("policy outcome payload checksum mismatch")
            for row in connection.execute(
                "SELECT decision_id, reward_version, reward_value, finalized_at_unix_ms, "
                "components_json, payload_sha256 FROM policy_rewards"
            ):
                payload = {
                    "decision_id": str(row[0]),
                    "reward_version": str(row[1]),
                    "reward_value": float(row[2]),
                    "finalized_at_unix_ms": int(row[3]),
                    "components": _decode_object(str(row[4])),
                }
                if policy_payload_sha256(payload) != str(row[5]):
                    raise StorageCorruptionError("policy reward payload checksum mismatch")

        await self._database.read(check)

    async def record_research_export(
        self,
        *,
        export_id: UUID,
        study_id: str,
        study_epoch: str,
        specification_sha256: str,
        dataset_sha256: str,
        filename: str,
        row_count: int,
    ) -> None:
        """Ledger an immutable research dataset after its file is durable."""

        if row_count < 0:
            raise ValueError("row_count cannot be negative")

        def write(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT dataset_sha256, filename FROM research_exports WHERE export_id=?",
                (str(export_id),),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != dataset_sha256 or str(existing[1]) != filename:
                    raise PolicyLifecycleConflict("research export id already has other content")
                return
            connection.execute(
                "INSERT INTO research_exports(export_id, study_id, study_epoch, "
                "specification_sha256, dataset_sha256, filename, row_count, "
                "created_at_unix_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(export_id),
                    study_id,
                    study_epoch,
                    specification_sha256,
                    dataset_sha256,
                    filename,
                    row_count,
                    self._clock.unix_ms(),
                ),
            )

        await self._database.transaction(write)


__all__ = [
    "FinalizationResult",
    "PolicyLifecycleConflict",
    "PolicyRepository",
]
