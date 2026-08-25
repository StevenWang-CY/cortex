"""WP8 production-policy and research-evaluation correctness gates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from importlib import resources
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest

from cortex.application.clock import FakeClock
from cortex.libs.config.settings import EvalConfig
from cortex.libs.schemas.policy import (
    MRTStudySpecification,
    PolicyContextSnapshot,
    PolicyDiagnosticsRequest,
    PolicyObservation,
    canonical_policy_json,
    policy_payload_sha256,
)
from cortex.services.eval.bandit_trainer import (
    RetiredPolicyTrainingError,
    evaluate_bandit,
    train_bandit,
)
from cortex.services.eval.off_policy import (
    OffPolicyEvaluationError,
    OPEObservation,
    TargetPolicyDefinition,
    evaluate_target_policy,
)
from cortex.services.eval.policy_diagnostics import (
    generate_daily_policy_diagnostics,
    migrate_legacy_causal_report_names,
)
from cortex.services.eval.policy_lifecycle import PolicyLifecycleService
from cortex.services.eval.policy_repository import PolicyRepository
from cortex.services.eval.production_policy import (
    DeterministicProductionPolicy,
    PolicySelectionInput,
)
from cortex.services.eval.research_analysis import (
    ResearchExportError,
    analyze_mrt_export,
    export_mrt_dataset,
)
from cortex.services.eval.research_policy import (
    ResearchPolicySettings,
    ResearchRandomizedPolicy,
)
from cortex.storage.database import (
    CORTEX_APPLICATION_ID,
    CURRENT_SCHEMA_VERSION,
    SQLiteDatabase,
)

_BOOT_ID = UUID("00000000-0000-0000-0000-000000000808")


def _clock() -> FakeClock:
    return FakeClock(
        wall_unix_ms=1_900_000_000_000,
        mono_ns=8_000_000_000,
        _boot_id=_BOOT_ID,
    )


def _context(*, complexity: float = 0.8, errors: int = 3) -> PolicyContextSnapshot:
    return PolicyContextSnapshot(
        support_state="HYPER",
        support_status="estimated",
        support_confidence=0.9,
        evidence_coverage=0.8,
        complexity_score=complexity,
        tab_count=12,
        error_count=errors,
        thrashing_score=0.7,
        hour_utc=17,
    )


def _request(
    *,
    point: UUID | None = None,
    session: str = "session-1",
    repeated_dismissal: bool = False,
    feasible: tuple[str, ...] = ("no_action", "suggest_only"),
) -> PolicySelectionInput:
    return PolicySelectionInput(  # type: ignore[arg-type]
        decision_point_id=point or uuid4(),
        session_id=session,
        context=_context(),
        eligible=True,
        available=True,
        availability_reason="eligible_and_receptive",
        feasible_arms=feasible,
        recent_repeated_dismissal=repeated_dismissal,
        reward_version="helpfulness-v2",
    )


def _database(root: Path, clock: FakeClock) -> SQLiteDatabase:
    return SQLiteDatabase(root / "data" / "cortex.sqlite3", clock=clock)


def test_default_and_legacy_configuration_contain_online_learning() -> None:
    assert EvalConfig().policy == "deterministic"
    migrated = EvalConfig.model_validate(
        {
            "policy": "amip",
            "amip": {"reward_window_seconds": 420, "epsilon_explore": 0.5},
            "causal_report": {"enabled": False, "bootstrap_samples": 10},
        }
    )
    assert migrated.policy == "deterministic"
    assert migrated.outcome.reward_window_seconds == 420
    assert migrated.policy_diagnostics.enabled is False

    with pytest.raises(ValueError, match="enabled"):
        EvalConfig(policy="research_randomized")

    configured = EvalConfig.model_validate(
        {
            "policy": "research_randomized",
            "outcome": {"reward_window_seconds": 90},
            "research": {
                "enabled": True,
                "study_id": "configured-study",
                "study_epoch": "epoch-1",
                "consent_version": "consent-v1",
                "seed_hex": "aa" * 32,
                "bootstrap_samples": 200,
                "analysis_seed": 17,
            },
        }
    )
    specification = configured.research.mrt_specification(
        policy_name="cortex-research-mrt",
        policy_version="2.0.0",
        reward_version=configured.outcome.reward_version,
        proximal_window_seconds=configured.outcome.reward_window_seconds,
    )
    assert specification.proximal_window_seconds == 90
    assert specification.bootstrap_samples == 200
    assert specification.analysis_seed == 17
    with pytest.raises(ValueError, match="real ISO"):
        PolicyDiagnosticsRequest(day_utc="2026-02-30")


@pytest.mark.asyncio
async def test_schema_v1_migrates_transactionally_with_verified_backup(
    tmp_path: Path,
) -> None:
    """Exercise the real v1→v2 path, not only a fresh-schema bootstrap."""

    clock = _clock()
    database_path = tmp_path / "data" / "cortex.sqlite3"
    database_path.parent.mkdir(parents=True)
    source = (
        resources.files("cortex.storage.migrations")
        .joinpath("0001_initial.sql")
        .read_text(encoding="utf-8")
    )
    source_sha = hashlib.sha256(source.encode()).hexdigest()
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(source)
        connection.execute(
            "INSERT INTO schema_migrations"
            "(version, name, source_sha256, applied_at_unix_ms) VALUES (?, ?, ?, ?)",
            (1, "0001_initial.sql", source_sha, clock.unix_ms()),
        )
        connection.execute(f"PRAGMA application_id={CORTEX_APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()

    database = SQLiteDatabase(database_path, clock=clock)
    await database.start()
    health = await database.health(full_integrity_check=True)
    assert health["healthy"] is True
    assert health["schema_version"] == CURRENT_SCHEMA_VERSION
    tables = await database.read(
        lambda conn: {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    )
    assert {"policy_outcome_windows", "policy_observations", "research_exports"} <= tables
    backups = list(database.backup_dir.glob("cortex.pre-schema-1.*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    await database.close()


def test_deterministic_replay_and_no_propensity_claim() -> None:
    clock = _clock()
    policy = DeterministicProductionPolicy(clock=clock)
    point = uuid4()
    first = policy.choose(_request(point=point))
    second = policy.choose(_request(point=point))
    assert first.selected_arm == second.selected_arm == "suggest_only"
    assert first.propensities is None
    assert first.supports_ope is False
    assert first.selected_probability == 1.0
    assert first.policy_state_sha256 == second.policy_state_sha256

    no_action = policy.choose(_request(repeated_dismissal=True))
    assert no_action.selected_arm == "no_action"


def test_legacy_bandit_training_and_evaluation_fail_closed() -> None:
    with pytest.raises(RetiredPolicyTrainingError, match="legacy helpfulness logs"):
        train_bandit([])
    with pytest.raises(RetiredPolicyTrainingError, match="legacy helpfulness logs"):
        evaluate_bandit(object(), [])


@pytest.mark.asyncio
async def test_no_action_has_identical_window_and_one_final_reward(tmp_path: Path) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(
        repository,
        clock=clock,
        reward_window_seconds=30,
    )
    await lifecycle.start()
    decision = await lifecycle.decide(_request(repeated_dismissal=True))
    assert decision.selected_arm == "no_action"

    rows = await repository.export_rows()
    assert rows[0]["delivery"]["status"] == "not_applicable"
    assert rows[0]["outcome"] is None
    clock.advance(wall_ms=30_000, monotonic_ns=30_000_000_000)
    results = await lifecycle.finalize_due(lambda: {"complexity_score": 0.8, "error_count": 3})
    assert len(results) == 1
    assert results[0].reward.value == pytest.approx(0.0)
    assert await lifecycle.finalize_due(lambda: None) == ()
    repeated = await repository.finalize(
        decision.decision_id,
        final_snapshot={"complexity_score": 0.8, "error_count": 3},
    )
    assert repeated.already_finalized is True
    reward_count = await database.read(
        lambda connection: connection.execute(
            "SELECT COUNT(*) FROM policy_rewards WHERE decision_id=?",
            (str(decision.decision_id),),
        ).fetchone()[0]
    )
    assert reward_count == 1
    await database.close()


@pytest.mark.asyncio
async def test_repeated_feedback_is_idempotent_and_reward_waits_for_window(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(
        repository,
        clock=clock,
        reward_window_seconds=30,
    )
    await lifecycle.start()
    decision = await lifecycle.decide(_request())
    await lifecycle.mark_delivered(decision.decision_id, "intervention-1")
    first = await lifecycle.observe_intervention(
        "intervention-1",
        kind="user_rating",
        idempotency_key="rating-up",
        payload={"rating": "thumbs_up"},
    )
    duplicate = await lifecycle.observe_intervention(
        "intervention-1",
        kind="user_rating",
        idempotency_key="rating-up",
        payload={"rating": "thumbs_up"},
    )
    assert first is True and duplicate is False
    assert (await repository.export_rows())[0]["reward"] is None

    clock.advance(wall_ms=30_000, monotonic_ns=30_000_000_000)
    await lifecycle.finalize_due(lambda: {"complexity_score": 0.5, "error_count": 1})
    accepted_after_close = await lifecycle.observe_intervention(
        "intervention-1",
        kind="user_rating",
        idempotency_key="rating-down-late",
        payload={"rating": "thumbs_down"},
    )
    assert accepted_after_close is False
    rows = await repository.export_rows()
    assert rows[0]["reward"]["components"]["user_rating"] == "thumbs_up"
    await database.close()


@pytest.mark.asyncio
async def test_observation_payload_has_a_hard_storage_bound(tmp_path: Path) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(repository, clock=clock, reward_window_seconds=30)
    decision = await lifecycle.decide(_request())

    with pytest.raises(ValueError, match="4096"):
        await repository.record_observation(
            PolicyObservation(
                decision_id=decision.decision_id,
                reward_version=decision.reward_version,
                kind="user_rating",
                idempotency_key="oversized-feedback",
                observed_at_unix_ms=clock.unix_ms(),
                payload={"text": "x" * 4_096},
            )
        )
    await database.close()


@pytest.mark.asyncio
async def test_missing_delivery_and_contamination_are_finalized_explicitly(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(
        repository,
        clock=clock,
        reward_window_seconds=30,
    )
    await lifecycle.start()
    decision = await lifecycle.decide(_request())
    assert decision.selected_arm == "suggest_only"
    clock.advance(wall_ms=30_000, monotonic_ns=30_000_000_000)
    results = await lifecycle.finalize_due(
        lambda: {"complexity_score": 0.8, "error_count": 3},
        contamination_provider=lambda: ("collector_overlap",),
    )
    assert len(results) == 1
    rows = await repository.export_rows()
    assert rows[0]["delivery"]["status"] == "not_delivered"
    assert rows[0]["outcome"]["contamination"] == ["collector_overlap"]
    assert rows[0]["reward"]["components"]["delivery_failure"] is True
    assert rows[0]["reward"]["version"] == "helpfulness-v2"
    await database.close()


@pytest.mark.asyncio
async def test_contamination_uses_other_delivery_time_not_own_exposure(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(repository, clock=clock, reward_window_seconds=30)
    first = await lifecycle.decide(_request(session="overlap-session"))
    await lifecycle.mark_delivered(first.decision_id, "intervention-first")
    clock.advance(wall_ms=1_000, monotonic_ns=1_000_000_000)
    second = await lifecycle.decide(_request(session="overlap-session"))
    await lifecycle.mark_delivered(second.decision_id, "intervention-second")

    clock.advance(wall_ms=29_000, monotonic_ns=29_000_000_000)
    first_result = await lifecycle.finalize_due(lambda: {"complexity_score": 0.7, "error_count": 2})
    assert [item.reward.decision_id for item in first_result] == [first.decision_id]
    rows = await repository.export_rows()
    first_row = next(
        row for row in rows if row["decision"]["decision_id"] == str(first.decision_id)
    )
    assert first_row["outcome"]["contamination"] == ["overlapping_policy_delivery"]

    clock.advance(wall_ms=1_000, monotonic_ns=1_000_000_000)
    await lifecycle.finalize_due(lambda: {"complexity_score": 0.7, "error_count": 2})
    rows = await repository.export_rows()
    second_row = next(
        row for row in rows if row["decision"]["decision_id"] == str(second.decision_id)
    )
    assert second_row["outcome"]["contamination"] == []
    await database.close()


@pytest.mark.asyncio
async def test_pending_no_action_window_survives_restart(tmp_path: Path) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(
        repository,
        clock=clock,
        reward_window_seconds=30,
    )
    await lifecycle.start()
    decision = await lifecycle.decide(_request(repeated_dismissal=True))
    await database.close()

    clock.advance(wall_ms=31_000, monotonic_ns=31_000_000_000)
    reopened = _database(tmp_path, clock)
    reopened_repository = PolicyRepository(reopened, clock=clock)
    reopened_lifecycle = PolicyLifecycleService(
        reopened_repository,
        clock=clock,
        reward_window_seconds=30,
    )
    await reopened_lifecycle.start()
    finalized = await reopened_lifecycle.finalize_due(
        lambda: {"complexity_score": 0.8, "error_count": 3}
    )
    assert finalized[0].reward.decision_id == decision.decision_id
    await reopened.close()


def test_research_propensity_mask_replay_and_reload_equivalence() -> None:
    settings = ResearchPolicySettings(
        study_id="study-alpha",
        study_epoch="epoch-1",
        consent_version="consent-2026-01",
        seed_hex="ab" * 32,
        specification_sha256="ef" * 32,
    )
    clock = _clock()
    first_policy = ResearchRandomizedPolicy(settings, clock=clock)
    first_decision, persisted = first_policy.choose(_request(point=uuid4()))
    assert first_decision.supports_ope is True
    assert first_decision.propensities is not None
    assert set(first_decision.propensities) == {"no_action", "suggest_only"}
    assert sum(first_decision.propensities.values()) == pytest.approx(1.0)
    assert min(first_decision.propensities.values()) >= settings.minimum_probability

    next_point = uuid4()
    uninterrupted, _ = first_policy.choose(_request(point=next_point))
    restored_policy = ResearchRandomizedPolicy(settings, clock=clock, state=persisted)
    restored, _ = restored_policy.choose(_request(point=next_point))
    assert restored.random_counter == uninterrupted.random_counter == 1
    assert restored.randomization_id == uninterrupted.randomization_id
    assert restored.selected_arm == uninterrupted.selected_arm
    assert restored.propensities == uninterrupted.propensities

    corrupted = dict(persisted)
    corrupted["counter"] = 999
    with pytest.raises(ValueError, match="checksum"):
        ResearchRandomizedPolicy(settings, clock=clock, state=corrupted)

    with pytest.raises(ValueError, match="frozen action catalog"):
        first_policy.choose(_request(feasible=("no_action",)))


@pytest.mark.asyncio
async def test_research_hard_exclusion_is_deterministic_and_not_exportable(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(
        repository,
        clock=clock,
        mode="research_randomized",
        reward_window_seconds=30,
        research_settings=ResearchPolicySettings(
            study_id="study-alpha",
            study_epoch="epoch-1",
            consent_version="consent-2026-01",
            seed_hex="ab" * 32,
            specification_sha256="ef" * 32,
        ),
    )
    await lifecycle.start()
    decision = await lifecycle.decide(_request(repeated_dismissal=True))
    assert decision.policy_mode == "deterministic"
    assert decision.selected_arm == "no_action"
    assert decision.supports_ope is False
    assert decision.propensities is None
    assert await repository.export_rows(policy_mode="research_randomized") == []
    rows = await repository.export_rows(policy_mode="deterministic")
    assert rows[0]["decision"]["availability_reason"].startswith("research_excluded")
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_research_decisions_serialize_unique_draw_counters(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    lifecycle = PolicyLifecycleService(
        repository,
        clock=clock,
        mode="research_randomized",
        reward_window_seconds=30,
        research_settings=ResearchPolicySettings(
            study_id="concurrency-study",
            study_epoch="epoch-1",
            consent_version="consent-v1",
            seed_hex="bc" * 32,
            specification_sha256="de" * 32,
        ),
    )
    decisions = await asyncio.gather(
        *(lifecycle.decide(_request(session=f"session-{index // 5}")) for index in range(40))
    )
    counters = sorted(int(item.random_counter or 0) for item in decisions)
    assert counters == list(range(40))
    assert len({item.randomization_id for item in decisions}) == 40
    rows = await repository.export_rows(policy_mode="research_randomized")
    assert len(rows) == 40
    assert sorted(row["decision"]["random_counter"] for row in rows) == list(range(40))
    await database.close()


def _write_synthetic_mrt_export(
    path: Path,
    *,
    treatment_effect: float,
    sessions: int = 30,
    points_per_session: int = 12,
) -> None:
    specification = MRTStudySpecification(
        study_id="synthetic-study",
        study_epoch="epoch-1",
        consent_version="consent-v1",
        policy_name="cortex-research-mrt",
        policy_version="2.0.0",
        action_catalog=("no_action", "suggest_only"),
        reward_version="helpfulness-v2",
        proximal_window_seconds=300,
        eligibility_rule="Synthetic eligible decision point.",
        availability_rule="Synthetic availability is always true.",
        missing_outcome_rule="Exclude missing outcomes from complete-case sensitivity analysis.",
        contamination_rule="Exclude any contaminated proximal window.",
        bootstrap_samples=200,
        analysis_seed=73,
    )
    rng = np.random.default_rng(41)
    rows: list[dict[str, object]] = []
    for session in range(sessions):
        cluster_effect = float(rng.normal(0.0, 0.03))
        for point in range(points_per_session):
            treated = bool(rng.integers(0, 2))
            outcome = (
                0.1
                + cluster_effect
                + treatment_effect * float(treated)
                + float(rng.normal(0.0, 0.03))
            )
            rows.append(
                {
                    "decision_id": f"d-{session}-{point}",
                    "session_id": f"session-{session}",
                    "selected_arm": "suggest_only" if treated else "no_action",
                    "behavior_propensities": {
                        "no_action": 0.5,
                        "suggest_only": 0.5,
                    },
                    "reward": max(-1.0, min(1.0, outcome)),
                    "analysis_included": True,
                }
            )
    spec_payload = specification.model_dump(mode="json")
    document = {
        "schema_version": "mrt-export/1.0",
        "export_id": str(uuid4()),
        "generated_at_unix_ms": 1_900_000_000_000,
        "specification": spec_payload,
        "specification_sha256": hashlib.sha256(
            canonical_policy_json(spec_payload).encode()
        ).hexdigest(),
        "row_count": len(rows),
        "rows": rows,
    }
    encoded = canonical_policy_json(document)
    path.write_text(encoded, encoding="utf-8")
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n",
        encoding="utf-8",
    )


def test_wcls_and_cluster_bootstrap_recover_known_effect(tmp_path: Path) -> None:
    export = tmp_path / "synthetic-mrt.json"
    _write_synthetic_mrt_export(export, treatment_effect=0.32)
    result = analyze_mrt_export(export)
    document = json.loads(export.read_text(encoding="utf-8"))
    treated = [row["reward"] for row in document["rows"] if row["selected_arm"] == "suggest_only"]
    control = [row["reward"] for row in document["rows"] if row["selected_arm"] == "no_action"]
    reference_difference = float(np.mean(treated) - np.mean(control))
    assert result.effect == pytest.approx(reference_difference, abs=1e-12)
    assert result.effect == pytest.approx(0.32, abs=0.025)
    assert result.cluster_count == 30
    assert result.bootstrap_samples >= 100
    assert result.confidence_interval_95 is not None
    assert result.confidence_interval_95[0] < 0.32 < result.confidence_interval_95[1]
    assert result.treatment_probability_min == result.treatment_probability_max == 0.5


def test_ope_known_value_and_extreme_weight_diagnostics() -> None:
    target = TargetPolicyDefinition(
        name="target-80pct-suggest",
        version="1.0",
        action_catalog=("no_action", "suggest_only"),
        probability_rule="For every exported row: P(suggest_only)=0.8.",
    )
    rng = np.random.default_rng(12)
    observations: list[OPEObservation] = []
    for index in range(20_000):
        treated = bool(rng.integers(0, 2))
        probability = 0.7 if treated else 0.2
        reward = float(rng.binomial(1, probability))
        observations.append(
            OPEObservation(
                decision_id=str(index),
                cluster_id=f"cluster-{index // 100}",
                selected_arm="suggest_only" if treated else "no_action",
                reward=reward,
                behavior_probabilities={"no_action": 0.5, "suggest_only": 0.5},
                target_probabilities={"no_action": 0.2, "suggest_only": 0.8},
                direct_outcome_estimates={"no_action": 0.2, "suggest_only": 0.7},
                supports_ope=True,
            )
        )
    result = evaluate_target_policy(target, observations, bootstrap_samples=0)
    true_value = 0.2 * 0.2 + 0.8 * 0.7
    assert result.ips.value == pytest.approx(true_value, abs=0.025)
    assert result.doubly_robust.value == pytest.approx(true_value, abs=0.015)
    assert result.direct_method.value == pytest.approx(true_value, abs=1e-12)
    assert len(result.target_assignments_sha256) == 64
    assert len(result.evaluation_rows_sha256) == 64

    extreme = OPEObservation(
        decision_id="extreme",
        cluster_id="one",
        selected_arm="suggest_only",
        reward=1.0,
        behavior_probabilities={"no_action": 0.999, "suggest_only": 0.001},
        target_probabilities={"no_action": 0.5, "suggest_only": 0.5},
        direct_outcome_estimates={"no_action": 0.0, "suggest_only": 0.0},
        supports_ope=True,
    )
    extreme_result = evaluate_target_policy(target, [extreme], bootstrap_samples=0)
    assert extreme_result.diagnostics["importance_weight_max"] == pytest.approx(500.0)
    assert extreme_result.diagnostics["weights_above_switch"] == 1
    assert extreme_result.clipped_doubly_robust.value < extreme_result.doubly_robust.value

    with pytest.raises(OffPolicyEvaluationError, match="deterministic"):
        evaluate_target_policy(
            target,
            [
                OPEObservation(
                    decision_id="product",
                    cluster_id="one",
                    selected_arm="suggest_only",
                    reward=0.1,
                    behavior_probabilities={"no_action": 0.0, "suggest_only": 1.0},
                    target_probabilities={"no_action": 0.0, "suggest_only": 1.0},
                    direct_outcome_estimates={"no_action": 0.0, "suggest_only": 0.1},
                    supports_ope=False,
                )
            ],
            bootstrap_samples=0,
        )


@pytest.mark.asyncio
async def test_immutable_research_export_and_diagnostic_naming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock()
    database = _database(tmp_path, clock)
    repository = PolicyRepository(database, clock=clock)
    specification = MRTStudySpecification(
        study_id="export-study",
        study_epoch="epoch-a",
        consent_version="consent-v1",
        policy_name="cortex-research-mrt",
        policy_version="2.0.0",
        action_catalog=("no_action", "suggest_only"),
        reward_version="helpfulness-v2",
        proximal_window_seconds=30,
        eligibility_rule="Eligible when the reviewed trigger gate passes.",
        availability_rule="Available only when the receptivity gate passes.",
        missing_outcome_rule="Retain row and exclude missing/censored outcomes.",
        contamination_rule="Retain row and exclude contaminated outcomes.",
        bootstrap_samples=200,
        analysis_seed=9,
    )
    settings = ResearchPolicySettings(
        study_id="export-study",
        study_epoch="epoch-a",
        consent_version="consent-v1",
        seed_hex="cd" * 32,
        specification_sha256=policy_payload_sha256(specification.model_dump(mode="json")),
    )
    lifecycle = PolicyLifecycleService(
        repository,
        clock=clock,
        mode="research_randomized",
        reward_window_seconds=30,
        research_settings=settings,
    )
    await lifecycle.start()
    decision = await lifecycle.decide(_request(session="export-session"))
    if decision.selected_arm != "no_action":
        await lifecycle.mark_delivered(decision.decision_id, "export-intervention")
    clock.advance(wall_ms=30_000, monotonic_ns=30_000_000_000)
    await lifecycle.finalize_due(lambda: {"complexity_score": 0.7, "error_count": 2})
    fixed_export_id = UUID("00000000-0000-0000-0000-000000000e08")
    monkeypatch.setattr(
        "cortex.services.eval.research_analysis.uuid4",
        lambda: fixed_export_id,
    )
    exported = await export_mrt_dataset(
        repository,
        specification,
        tmp_path / "research-exports",
        clock=clock,
    )
    assert exported.exists()
    assert exported.with_suffix(".json.sha256").exists()
    document = json.loads(exported.read_text(encoding="utf-8"))
    assert document["row_count"] == 1
    assert document["specification"]["estimand"].startswith("marginal_proximal")
    original_bytes = exported.read_bytes()
    with pytest.raises(ResearchExportError, match="already exists"):
        await export_mrt_dataset(
            repository,
            specification,
            tmp_path / "research-exports",
            clock=clock,
        )
    assert exported.read_bytes() == original_bytes
    mismatched = specification.model_copy(update={"analysis_seed": 10})
    with pytest.raises(ResearchExportError, match="not bound"):
        await export_mrt_dataset(
            repository,
            mismatched,
            tmp_path / "research-exports",
            clock=clock,
        )

    diagnostic = await generate_daily_policy_diagnostics(
        repository,
        tmp_path,
        day=clock.today_utc().isoformat(),
    )
    text = diagnostic.read_text(encoding="utf-8")
    assert diagnostic.name.startswith("policy_diagnostics_")
    assert "does not estimate a causal effect" in text
    with pytest.raises(ValueError, match="ISO calendar date"):
        await generate_daily_policy_diagnostics(
            repository,
            tmp_path,
            day="../../research-export",
        )

    reports = tmp_path / "reports"
    legacy = reports / "causal_2026-01-01.md"
    legacy.write_text("# old misleading title\n", encoding="utf-8")
    migrated = migrate_legacy_causal_report_names(tmp_path)
    assert migrated == [reports / "legacy_policy_diagnostics_2026-01-01.md"]
    assert not legacy.exists()
    await database.close()
