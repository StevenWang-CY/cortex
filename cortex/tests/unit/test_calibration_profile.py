"""Immutable calibration provenance and activation invariants."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from cortex.application.clock import FakeClock
from cortex.libs.schemas.calibration import (
    CalibrationBaselineValues,
    CalibrationCameraIdentity,
    CalibrationDistribution,
    CalibrationMetricMaturity,
    CalibrationMetricName,
    CalibrationMetricSummary,
    CalibrationProfile,
    CalibrationProvenance,
    CalibrationReferenceTask,
)
from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.services.capture_service import (
    algorithm_identity as identity_module,
)
from cortex.services.capture_service import (
    calibration_store as calibration_store_module,
)
from cortex.services.capture_service.algorithm_identity import source_digest
from cortex.services.capture_service.calibration_store import CalibrationProfileStore


def _algorithm(name: str = "kinematics") -> SignalAlgorithmIdentity:
    return SignalAlgorithmIdentity(
        name=name,
        version="2.0.0",
        implementation_sha256="a" * 64,
        configuration_sha256="b" * 64,
        selection_mode="fixed",
    )


def _summary(
    metric: CalibrationMetricName,
    value: float,
    *,
    maturity: CalibrationMetricMaturity = CalibrationMetricMaturity.OBSERVED,
    task: CalibrationReferenceTask = CalibrationReferenceTask.REPRESENTATIVE_WORK,
) -> CalibrationMetricSummary:
    return CalibrationMetricSummary(
        metric=metric,
        unit="bpm" if metric == CalibrationMetricName.HEART_RATE_BPM else "ratio",
        reference_task=task,
        maturity=maturity,
        value=value,
        distribution=CalibrationDistribution(
            mean=value,
            std=1.0,
            p10=value - 1.0,
            median=value,
            p90=value + 1.0,
        ),
        sample_count=30,
        effective_sample_count=12.0,
        valid_duration_seconds=30.0,
        missing_fraction=0.05,
        quality_p10=0.6,
        quality_median=0.8,
        quality_p90=0.95,
        algorithm=_algorithm(),
    )


def _profile(
    *,
    provenance: CalibrationProvenance = CalibrationProvenance.MEASURED,
    approved: bool = True,
    profile_id: UUID = UUID("00000000-0000-0000-0000-000000000123"),
) -> CalibrationProfile:
    camera = None
    if provenance == CalibrationProvenance.MEASURED:
        camera = CalibrationCameraIdentity(
            identity_key="built-in:facetime-hd",
            device_name="FaceTime HD Camera",
            source="builtin",
            width=1280,
            height=720,
        )
    return CalibrationProfile(
        profile_id=profile_id,
        provenance=provenance,
        created_at_unix_ms=1_700_000_000_000,
        approved_at_unix_ms=(
            1_700_000_001_000
            if approved and provenance == CalibrationProvenance.MEASURED
            else None
        ),
        feature_schema_version="features/2.0",
        protocol_version="calibration/2.0",
        camera=camera,
        metrics=(
            _summary(
                CalibrationMetricName.HEART_RATE_BPM,
                88.0,
                maturity=CalibrationMetricMaturity.EXPERIMENTAL,
                task=CalibrationReferenceTask.PHYSIOLOGICAL_REST,
            ),
            _summary(CalibrationMetricName.BLINK_RATE_PER_MIN, 14.0),
        ),
        baselines=CalibrationBaselineValues(
            heart_rate_bpm=88.0,
            blink_rate_per_min=14.0,
        ),
    )


def test_profile_is_deeply_immutable_and_unique_by_metric() -> None:
    profile = _profile()
    with pytest.raises(Exception):
        profile.baselines.blink_rate_per_min = 20.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="metrics must be unique"):
        _profile().model_copy(
            update={"metrics": (_profile().metrics[1], _profile().metrics[1])}
        ).model_validate(_profile().model_copy(
            update={"metrics": (_profile().metrics[1], _profile().metrics[1])}
        ).model_dump())


def test_experimental_physiology_is_not_promoted_into_legacy_scorer() -> None:
    baselines = _profile().to_user_baselines()
    assert baselines.hr_baseline == 72.0
    assert "hr" not in baselines.metric_distributions
    assert baselines.blink_rate_baseline == 14.0
    assert baselines.is_calibrated is True


def test_baseline_value_requires_matching_metric_evidence() -> None:
    profile = _profile()
    with pytest.raises(ValueError, match="does not match metric evidence"):
        CalibrationProfile.model_validate(
            profile.model_copy(
                update={
                    "baselines": profile.baselines.model_copy(
                        update={"blink_rate_per_min": 22.0}
                    )
                }
            ).model_dump()
        )


def test_demo_profile_cannot_carry_approval() -> None:
    payload = _profile(
        provenance=CalibrationProvenance.DEMO,
        approved=False,
    ).model_dump()
    payload["approved_at_unix_ms"] = 1_700_000_001_000
    with pytest.raises(ValueError, match="demo calibration profiles cannot be approved"):
        CalibrationProfile.model_validate(payload)


def test_activate_commits_pointer_and_compatibility_projection(tmp_path: Path) -> None:
    clock = FakeClock(wall_unix_ms=1_700_000_009_000)
    store = CalibrationProfileStore(tmp_path, clock=clock)
    profile = _profile()
    pointer = store.activate(profile)

    assert store.profile_path(profile.profile_id).exists()
    assert (tmp_path / "baselines" / "default.json").exists()
    assert store.active_pointer_path.exists()
    assert pointer.activated_at_unix_ms == clock.wall_unix_ms
    assert store.load_active() == profile


def test_pointer_failure_preserves_prior_authority_and_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CalibrationProfileStore(tmp_path)
    prior = _profile(
        profile_id=UUID("00000000-0000-0000-0000-000000000124")
    )
    candidate = _profile(
        profile_id=UUID("00000000-0000-0000-0000-000000000125")
    )
    store.activate(prior)
    projection_before = (tmp_path / "baselines" / "default.json").read_bytes()
    real_atomic_write = calibration_store_module.atomic_write_json

    def fail_pointer(path: Path, payload: object) -> None:
        if Path(path) == store.active_pointer_path:
            raise OSError("injected pointer failure")
        real_atomic_write(path, payload)

    monkeypatch.setattr(calibration_store_module, "atomic_write_json", fail_pointer)
    with pytest.raises(OSError, match="pointer failure"):
        store.activate(candidate)

    assert store.load_active() == prior
    assert (tmp_path / "baselines" / "default.json").read_bytes() == projection_before


def test_projection_failure_does_not_undo_committed_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CalibrationProfileStore(tmp_path)
    profile = _profile()
    real_atomic_write = calibration_store_module.atomic_write_json

    def fail_projection(path: Path, payload: object) -> None:
        if Path(path) == tmp_path / "baselines" / "default.json":
            raise OSError("injected projection failure")
        real_atomic_write(path, payload)

    monkeypatch.setattr(
        calibration_store_module,
        "atomic_write_json",
        fail_projection,
    )
    pointer = store.activate(profile)

    assert pointer.profile_id == profile.profile_id
    assert store.load_active() == profile
    assert not (tmp_path / "baselines" / "default.json").exists()


def test_profile_lookup_rejects_non_uuid_path_input(tmp_path: Path) -> None:
    store = CalibrationProfileStore(tmp_path)
    with pytest.raises(ValueError, match="must be a UUID"):
        store.load_profile("../../outside")


def test_frozen_bundle_identity_fallback_hashes_executable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def returns_one(_self: object) -> int:
        return 1

    def returns_two(_self: object) -> int:
        return 2

    first = type("BundledAlgorithm", (), {"run": returns_one})
    second = type("BundledAlgorithm", (), {"run": returns_two})
    monkeypatch.setattr(identity_module.inspect, "getsourcefile", lambda _value: None)

    assert source_digest(first) == source_digest(first)
    assert source_digest(first) != source_digest(second)


@pytest.mark.parametrize(
    "profile",
    [
        _profile(provenance=CalibrationProvenance.DEMO),
        _profile(approved=False),
    ],
)
def test_demo_or_unapproved_profile_can_never_become_active(
    tmp_path: Path,
    profile: CalibrationProfile,
) -> None:
    store = CalibrationProfileStore(tmp_path)
    with pytest.raises(ValueError):
        store.activate(profile)
    assert not store.active_pointer_path.exists()


def test_demo_profile_is_namespaced_away_from_active_profiles(tmp_path: Path) -> None:
    store = CalibrationProfileStore(tmp_path)
    profile = _profile(provenance=CalibrationProvenance.DEMO)
    path = store.save_demo(profile)
    assert path.parent == store.demo_profiles_dir
    assert store.load_active() is None


def test_profile_uuid_cannot_be_overwritten_with_different_content(tmp_path: Path) -> None:
    store = CalibrationProfileStore(tmp_path)
    profile = _profile()
    store.save_inactive(profile)
    changed = profile.model_copy(
        update={"notes": ("different immutable content",)}
    )
    with pytest.raises(ValueError, match="immutable"):
        store.save_inactive(changed)


def test_active_pointer_detects_profile_tampering(tmp_path: Path) -> None:
    store = CalibrationProfileStore(tmp_path)
    profile = _profile()
    store.activate(profile)
    profile_path = store.profile_path(profile.profile_id)
    payload = json.loads(profile_path.read_text())
    payload["notes"] = ["tampered"]
    profile_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.load_active()
