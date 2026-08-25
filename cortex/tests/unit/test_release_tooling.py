"""Release evidence and frozen-resource contract tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from cortex import __version__
from cortex.scripts.generate_release_evidence import (
    ReleaseEvidenceError,
    generate,
    sha256_file,
)
from cortex.scripts.release_smoke import inspect_release_resources
from cortex.scripts.validate_release_records import (
    REQUIRED_CASE_IDS,
    ReleaseRecordError,
    validate_release_records,
)
from cortex.scripts.verify_macos_release import _scan_forbidden

_ROOT = Path(__file__).resolve().parents[3]


def _write_approved_release_record(
    root: Path,
    *,
    architecture: str,
    commit: str,
    builder_id: str = "builder-one",
    reviewer_id: str = "reviewer-one",
) -> Path:
    artifact_name = f"Cortex-{__version__}-macos-{architecture}.dmg"
    artifact = root / artifact_name
    artifact.write_bytes(f"artifact-{architecture}".encode())
    artifact_sha256 = sha256_file(artifact)
    (root / f"Cortex-{__version__}-macos-{architecture}-evidence.zip").write_bytes(
        b"automated evidence"
    )
    (root / f"SHA256SUMS-{architecture}").write_text(
        f"{artifact_sha256}  {artifact_name}\n",
        encoding="utf-8",
    )
    evidence_name = f"manual-evidence-{architecture}.zip"
    (root / evidence_name).write_bytes(b"evidence")
    record = {
        "schema_version": "1.0",
        "artifact": {
            "version": __version__,
            "filename": artifact_name,
            "sha256": artifact_sha256,
            "git_commit": commit,
            "notarized": True,
        },
        "host": {
            "architecture": architecture,
            "macos_version": "15.0",
            "device_class": "clean test fixture",
            "clean_profile": True,
        },
        "cases": [
            {
                "id": case_id,
                "result": "passed",
                "observed": f"Executed {case_id} against the exact candidate artifact.",
                "evidence_files": [evidence_name],
            }
            for case_id in sorted(REQUIRED_CASE_IDS)
        ],
        "reviewers": [
            {
                "role": "builder",
                "reviewer_id": builder_id,
                "signed_at_utc": "2026-08-25T12:00:00Z",
                "attestation": "I built and executed this candidate record.",
            },
            {
                "role": "independent_reviewer",
                "reviewer_id": reviewer_id,
                "signed_at_utc": "2026-08-25T13:00:00Z",
                "attestation": "I independently reviewed this candidate evidence.",
            },
        ],
        "decision": "release",
    }
    path = root / f"manual-release-evidence-{architecture}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_source_release_smoke_covers_critical_resources() -> None:
    report = inspect_release_resources(_ROOT, frozen=False)
    assert report.cortex_version == __version__
    assert report.checks["face_landmarker"] != ""
    assert report.checks["migration_0002"] != ""
    assert report.checks["browser_source"] == "present"


def test_frozen_release_smoke_rejects_secret_bearing_env(tmp_path: Path) -> None:
    required = (
        "cortex/libs/config/defaults.yaml",
        "cortex/storage/migrations/0001_initial.sql",
        "cortex/storage/migrations/0002.sql",
        "cortex/scripts/native_host.py",
        "cortex/scripts/install_native_host.py",
        "cortex/models/face_landmarker.task",
        "cortex/assets/audio/box_4s.wav",
    )
    for relative in required:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    for relative in ("browser_extension_chrome", "browser_extension_edge"):
        directory = tmp_path / relative
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"cortex-somatic-{__version__}.vsix").write_bytes(b"fixture")
    (tmp_path / ".env").write_text("AWS_BEARER_TOKEN_BEDROCK=must-not-ship\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="forbidden"):
        inspect_release_resources(tmp_path, frozen=True)


def test_release_evidence_hashes_every_input(tmp_path: Path) -> None:
    artifact = tmp_path / f"Cortex-{__version__}-macos-arm64.dmg"
    sbom = tmp_path / "cortex-app.spdx.json"
    verification = tmp_path / "release-verification.json"
    artifact.write_bytes(b"immutable artifact")
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    verification.write_text('{"status":"passed"}\n', encoding="utf-8")
    output = tmp_path / "evidence"

    metadata_path, sums_path = generate(
        artifact,
        sboms=(sbom,),
        verification=verification,
        output_dir=output,
        expected_tag=None,
        require_clean=False,
        checksum_name="SHA256SUMS-arm64",
    )

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    records = {item["path"]: item for item in metadata["inputs"]}
    assert records[artifact.name]["sha256"] == sha256_file(artifact)
    sums = sums_path.read_text(encoding="utf-8")
    assert artifact.name in sums
    assert sbom.name in sums
    assert verification.name in sums
    assert metadata_path.name in sums
    assert sums_path.name == "SHA256SUMS-arm64"


def test_release_evidence_rejects_checksum_paths(tmp_path: Path) -> None:
    artifact = tmp_path / f"Cortex-{__version__}-macos-arm64.dmg"
    artifact.write_bytes(b"artifact")

    with pytest.raises(ReleaseEvidenceError, match="checksum_name"):
        generate(
            artifact,
            sboms=(),
            verification=None,
            output_dir=tmp_path / "evidence",
            expected_tag=None,
            require_clean=False,
            checksum_name="../SHA256SUMS",
        )


def test_release_evidence_rejects_ambiguous_checksum_basenames(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / f"Cortex-{__version__}-macos-arm64.dmg"
    artifact.write_bytes(b"artifact")
    first = tmp_path / "one" / "sbom.json"
    second = tmp_path / "two" / "sbom.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    with pytest.raises(ReleaseEvidenceError, match="unique basenames"):
        generate(
            artifact,
            sboms=(first, second),
            verification=None,
            output_dir=tmp_path / "evidence",
            expected_tag=None,
            require_clean=False,
        )


def test_release_bundle_scan_finds_embedded_credentials(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("nothing sensitive", encoding="utf-8")
    (tmp_path / "unsafe.bin").write_bytes(b"prefix sk-ant-not-a-real-key suffix")
    findings = _scan_forbidden(tmp_path)
    assert findings == ["unsafe.bin contains b'sk-ant-'"]


def test_release_bundle_scan_detects_pattern_across_read_boundary(tmp_path: Path) -> None:
    prefix = b"x" * (1024 * 1024 - 3)
    (tmp_path / "boundary.bin").write_bytes(prefix + b"sk-ant-placeholder")

    assert _scan_forbidden(tmp_path) == ["boundary.bin contains b'sk-ant-'"]


def test_manual_release_template_is_valid_and_cannot_be_released() -> None:
    schema = json.loads(
        (_ROOT / "docs/release/manual-release-evidence.schema.json").read_text(encoding="utf-8")
    )
    template = json.loads(
        (_ROOT / "docs/release/manual-release-evidence.template.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(template)) == []
    assert template["decision"] == "block"
    assert {item["role"] for item in template["reviewers"]} == {
        "builder",
        "independent_reviewer",
    }

    false_release = deepcopy(template)
    false_release["decision"] = "release"
    assert list(validator.iter_errors(false_release)) != []


def test_dataset_manifest_example_matches_published_schema() -> None:
    schema = json.loads(
        (_ROOT / "cortex/tests/physio/manifest.schema.json").read_text(encoding="utf-8")
    )
    example = json.loads(
        (_ROOT / "cortex/tests/physio/manifest.example.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(example)) == []
    assert example["participant_data_committed"] is False


def test_release_record_validator_requires_complete_two_architecture_evidence(
    tmp_path: Path,
) -> None:
    commit = "a" * 40
    _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)

    report = validate_release_records(
        tmp_path,
        asset_dir=tmp_path,
        expected_version=__version__,
        expected_commit=commit,
    )

    assert report["status"] == "passed"
    assert report["architectures"] == ["arm64", "x86_64"]


def test_release_record_validator_rejects_reviewer_role_overlap(
    tmp_path: Path,
) -> None:
    commit = "b" * 40
    _write_approved_release_record(
        tmp_path,
        architecture="arm64",
        commit=commit,
        builder_id="same-person",
        reviewer_id="same-person",
    )
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)

    with pytest.raises(ReleaseRecordError, match="identities overlap"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )


def test_release_record_validator_rejects_incomplete_case_catalog(
    tmp_path: Path,
) -> None:
    commit = "c" * 40
    arm_record = _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)
    payload = json.loads(arm_record.read_text(encoding="utf-8"))
    payload["cases"].pop()
    arm_record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseRecordError, match="violates schema"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )


def test_release_record_validator_rejects_artifact_hash_mismatch(
    tmp_path: Path,
) -> None:
    commit = "d" * 40
    _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)
    (tmp_path / f"Cortex-{__version__}-macos-arm64.dmg").write_bytes(b"tampered")

    with pytest.raises(ReleaseRecordError, match="hash mismatch"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )


def test_release_record_validator_rejects_automated_asset_as_manual_evidence(
    tmp_path: Path,
) -> None:
    commit = "e" * 40
    arm_record = _write_approved_release_record(tmp_path, architecture="arm64", commit=commit)
    _write_approved_release_record(tmp_path, architecture="x86_64", commit=commit)
    payload = json.loads(arm_record.read_text(encoding="utf-8"))
    payload["cases"][0]["evidence_files"] = [f"Cortex-{__version__}-macos-arm64.dmg"]
    arm_record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReleaseRecordError, match="non-manual"):
        validate_release_records(
            tmp_path,
            asset_dir=tmp_path,
            expected_version=__version__,
            expected_commit=commit,
        )
