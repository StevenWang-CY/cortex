"""Audit defect D12 — policy reward, finalization, delivery bookkeeping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.schemas.policy import PolicyContextSnapshot
from cortex.services.eval.policy_diagnostics import generate_daily_policy_diagnostics
from cortex.services.eval.policy_lifecycle import PolicyLifecycleService
from cortex.services.eval.policy_repository import (
    IGNORED_INTERRUPTION_WINDOW_MS,
    PolicyLifecycleConflict,
    PolicyRepository,
    _reward_components,
)
from cortex.services.eval.production_policy import PolicySelectionInput
from cortex.storage.database import SQLiteDatabase

_BOOT_ID = UUID("00000000-0000-0000-0000-000000000d12")


def _clock() -> FakeClock:
    return FakeClock(wall_unix_ms=1_900_000_000_000, mono_ns=8_000_000_000, _boot_id=_BOOT_ID)


def _context() -> PolicyContextSnapshot:
    return PolicyContextSnapshot(
        support_state="HYPER",
        support_status="estimated",
        support_confidence=0.9,
        evidence_coverage=0.8,
        complexity_score=0.8,
        tab_count=12,
        error_count=3,
        thrashing_score=0.7,
        hour_utc=17,
    )


def _request(*, repeated_dismissal: bool = False) -> PolicySelectionInput:
    return PolicySelectionInput(
        decision_point_id=uuid4(),
        session_id="session-d12",
        context=_context(),
        eligible=True,
        available=True,
        availability_reason="eligible_and_receptive",
        feasible_arms=("no_action", "suggest_only"),
        recent_repeated_dismissal=repeated_dismissal,
        reward_version="helpfulness-v2",
    )


def _lifecycle(tmp_path: Path, clock: FakeClock) -> tuple[SQLiteDatabase, PolicyRepository, PolicyLifecycleService]:
    database = SQLiteDatabase(tmp_path / "data" / "cortex.sqlite3", clock=clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(repository, clock=clock, reward_window_seconds=30)
    return database, repository, lifecycle


# ---------------------------------------------------------------------------
# Reward: a delivery by itself carries no cost
# ---------------------------------------------------------------------------


def _decision_payload() -> dict[str, object]:
    return {"context": {"complexity_score": 0.8, "error_count": 3}, "boot_id": str(_BOOT_ID)}


def test_null_effect_gives_treatment_and_no_action_the_same_zero_reward() -> None:
    snapshot = {"complexity_score": 0.8, "error_count": 3}
    treated, treated_components = _reward_components(
        decision=_decision_payload(),
        delivery={"status": "delivered", "delivered_at_unix_ms": 1_000},
        observations=[],
        final_snapshot=snapshot,
    )
    control, control_components = _reward_components(
        decision=_decision_payload(),
        delivery={"status": "not_applicable", "delivered_at_unix_ms": None},
        observations=[],
        final_snapshot=snapshot,
    )
    assert treated == pytest.approx(0.0)
    assert control == pytest.approx(0.0)
    assert treated_components["interruption_signal"] == 0.0
    assert treated_components["user_feedback_observable"] is True
    assert control_components["user_feedback_observable"] is False


def test_interruption_cost_requires_an_immediate_rejection() -> None:
    quick = _reward_components(
        decision=_decision_payload(),
        delivery={"status": "delivered", "delivered_at_unix_ms": 1_000},
        observations=[
            {
                "kind": "user_action",
                "payload": {"action": "dismissed"},
                "observed_at_unix_ms": 1_000 + IGNORED_INTERRUPTION_WINDOW_MS,
            }
        ],
        final_snapshot=None,
    )
    late = _reward_components(
        decision=_decision_payload(),
        delivery={"status": "delivered", "delivered_at_unix_ms": 1_000},
        observations=[
            {
                "kind": "user_action",
                "payload": {"action": "dismissed"},
                "observed_at_unix_ms": 1_000 + IGNORED_INTERRUPTION_WINDOW_MS + 1,
            }
        ],
        final_snapshot=None,
    )
    assert quick[1]["interruption_signal"] == -1.0
    assert late[1]["interruption_signal"] == 0.0
    assert quick[0] < late[0]


# ---------------------------------------------------------------------------
# Finalization: late closes and cold-pipeline snapshots are censored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_finalize_is_censored_and_discards_the_stale_snapshot(tmp_path: Path) -> None:
    clock = _clock()
    database, repository, lifecycle = _lifecycle(tmp_path, clock)
    await lifecycle.start()
    decision = await lifecycle.decide(_request())
    # Window is 30 s; anything more than one window past the close is late.
    clock.advance(wall_ms=30_000 + 31_000, monotonic_ns=61_000_000_000)
    results = await lifecycle.finalize_due(lambda: {"complexity_score": 0.0, "error_count": 0})
    assert len(results) == 1
    assert results[0].outcome_status == "censored"
    rows = await repository.export_rows()
    assert rows[0]["outcome"]["missingness_reason"] == "finalized_late"
    assert rows[0]["outcome"]["final_snapshot"] is None
    assert rows[0]["reward"]["components"]["post_snapshot_missing"] is True
    assert rows[0]["reward"]["components"]["task_measure_signal"] == 0.0
    assert decision.decision_id == results[0].reward.decision_id
    await database.close()


@pytest.mark.asyncio
async def test_on_time_finalize_with_same_boot_snapshot_is_finalized(tmp_path: Path) -> None:
    clock = _clock()
    database, repository, lifecycle = _lifecycle(tmp_path, clock)
    await lifecycle.start()
    await lifecycle.decide(_request())
    clock.advance(wall_ms=31_000, monotonic_ns=31_000_000_000)
    results = await lifecycle.finalize_due(
        lambda: {"complexity_score": 0.5, "error_count": 1, "boot_id": str(_BOOT_ID)}
    )
    assert results[0].outcome_status == "finalized"
    await database.close()


@pytest.mark.asyncio
async def test_snapshot_from_a_different_boot_is_censored(tmp_path: Path) -> None:
    clock = _clock()
    database, repository, lifecycle = _lifecycle(tmp_path, clock)
    await lifecycle.start()
    await lifecycle.decide(_request())
    clock.advance(wall_ms=31_000, monotonic_ns=31_000_000_000)
    results = await lifecycle.finalize_due(
        lambda: {"complexity_score": 0.5, "error_count": 1, "boot_id": str(uuid4())}
    )
    assert results[0].outcome_status == "censored"
    rows = await repository.export_rows()
    assert rows[0]["outcome"]["missingness_reason"] == "post_snapshot_from_different_boot"
    await database.close()


# ---------------------------------------------------------------------------
# Delivery bookkeeping: pre-send delivered row may only be corrected downward
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivered_row_can_be_superseded_by_a_delivery_race_only(tmp_path: Path) -> None:
    clock = _clock()
    database, repository, lifecycle = _lifecycle(tmp_path, clock)
    await lifecycle.start()
    decision = await lifecycle.decide(_request())
    await lifecycle.mark_delivered(decision.decision_id, "iv-race")
    with pytest.raises(PolicyLifecycleConflict):
        await lifecycle.mark_not_delivered(decision.decision_id, "presentation_delivery_race")
    await lifecycle.mark_not_delivered(
        decision.decision_id,
        "presentation_delivery_race",
        supersede_delivered=True,
    )
    rows = await repository.export_rows()
    assert rows[0]["delivery"]["status"] == "not_delivered"
    assert rows[0]["delivery"]["reason"] == "presentation_delivery_race"
    # The dead intervention id is unbound so no observation can attach to it.
    assert (
        await lifecycle.observe_intervention(
            "iv-race", kind="user_rating", idempotency_key="r", payload={"rating": "thumbs_up"},
        )
        is False
    )
    # Upward rewrites remain impossible.
    with pytest.raises(PolicyLifecycleConflict):
        await lifecycle.mark_delivered(decision.decision_id, "iv-race")
    await database.close()


# ---------------------------------------------------------------------------
# Diagnostics: the day is interpreted in the caller's zone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_diagnostics_bucket_by_the_requested_zone(tmp_path: Path) -> None:
    plus_two = timezone(timedelta(hours=2))
    # 2026-09-03 23:30 UTC == 2026-09-04 01:30 in UTC+2.
    wall = datetime(2026, 9, 3, 23, 30, tzinfo=UTC)
    clock = FakeClock(wall_unix_ms=int(wall.timestamp() * 1000), mono_ns=1, _boot_id=_BOOT_ID)
    database, repository, lifecycle = _lifecycle(tmp_path, clock)
    await lifecycle.start()
    await lifecycle.decide(_request())

    local_report = await generate_daily_policy_diagnostics(
        repository, tmp_path, day="2026-09-04", tz=plus_two,
    )
    assert "Decision points: 1" in local_report.read_text(encoding="utf-8")
    utc_report = await generate_daily_policy_diagnostics(repository, tmp_path, day="2026-09-04")
    assert "Decision points: 0" in utc_report.read_text(encoding="utf-8")
    await database.close()
