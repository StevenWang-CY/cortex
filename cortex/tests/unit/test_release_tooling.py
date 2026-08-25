"""Release evidence and frozen-resource contract tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from cortex import __version__
from cortex.scripts.generate_release_evidence import (
    ReleaseEvidenceError,
    _command,
    generate,
    sha256_file,
)
from cortex.scripts.release_smoke import inspect_release_resources
from cortex.scripts.select_notary_auth import (
    API_KEY_VARIABLES,
    APPLE_ID_VARIABLES,
    NotaryAuthConfigurationError,
    select_notary_auth_mode,
)
from cortex.scripts.validate_release_records import (
    REQUIRED_CASE_IDS,
    ReleaseRecordError,
    validate_release_records,
)
from cortex.scripts.verify_macos_release import _default_personal_roots, _scan_forbidden

_ROOT = Path(__file__).resolve().parents[3]


def _credential_environment(names: tuple[str, ...]) -> dict[str, str]:
    return {name: f"fixture-for-{name.lower()}" for name in names}


def test_notary_auth_selector_accepts_each_complete_mode() -> None:
    assert select_notary_auth_mode(_credential_environment(API_KEY_VARIABLES)) == "api-key"
    assert select_notary_auth_mode(_credential_environment(APPLE_ID_VARIABLES)) == "apple-id"


@pytest.mark.parametrize("group", [API_KEY_VARIABLES, APPLE_ID_VARIABLES])
def test_notary_auth_selector_rejects_every_partial_mode(group: tuple[str, ...]) -> None:
    for omitted in group:
        environment = _credential_environment(group)
        environment.pop(omitted)
        with pytest.raises(NotaryAuthConfigurationError, match=omitted):
            select_notary_auth_mode(environment)


def test_notary_auth_selector_rejects_missing_or_mixed_modes() -> None:
    with pytest.raises(NotaryAuthConfigurationError, match="missing notarization"):
        select_notary_auth_mode({})

    both = _credential_environment((*API_KEY_VARIABLES, *APPLE_ID_VARIABLES))
    with pytest.raises(NotaryAuthConfigurationError, match="exactly one"):
        select_notary_auth_mode(both)


def test_release_workflow_wires_both_notary_modes_through_selector() -> None:
    workflow = (_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for variable in (*API_KEY_VARIABLES, *APPLE_ID_VARIABLES):
        assert f"{variable}: ${{{{ secrets.{variable} }}}}" in workflow
    assert "-m cortex.scripts.select_notary_auth" in workflow
    assert '--key "${NOTARY_KEY_PATH}"' in workflow
    assert '--apple-id "${APPLE_ID_USERNAME}"' in workflow
    assert '--password "${APPLE_ID_APP_PASSWORD}"' in workflow
    assert '--team-id "${APPLE_TEAM_ID}"' in workflow


def test_release_workflow_stages_versioned_changelog_notes() -> None:
    workflow = (_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'HEADING="## [${RELEASE_TAG}]"' in workflow
    assert "body_path: ${{ env.CORTEX_RELEASE_NOTES }}" in workflow
    assert "generate_release_notes: false" in workflow


def test_macos_builder_preserves_caller_selected_node_before_gui_fallbacks() -> None:
    build_script = (_ROOT / "cortex/scripts/build_macos_app.sh").read_text(encoding="utf-8")
    assert 'export PATH="${PATH}:/opt/homebrew/bin:/usr/local/bin"' in build_script
    assert 'export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"' not in build_script


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


def test_release_evidence_records_missing_optional_builder_tool() -> None:
    assert _command(["cortex-builder-tool-that-does-not-exist", "--version"]) == (
        "unavailable (not found)"
    )


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
    credential = b"".join((b"sk-", b"ant-", b"abcdefghijklmnop123456"))
    (tmp_path / "unsafe.bin").write_bytes(b"prefix " + credential + b" suffix")
    findings = _scan_forbidden(tmp_path, personal_roots=())
    assert findings == ["unsafe.bin matches credential rule anthropic-api-key"]


@pytest.mark.parametrize(
    ("payload", "expected_rule"),
    [
        (
            b"AWS_SECRET_ACCESS_KEY=" + b"abcdefghijklmnopqrstuvwxyz1234567890ABCD",
            "credential-assignment",
        ),
        (b"-----BEGIN " + b"PRIVATE KEY-----", "private-key"),
        (b"AKIA" + b"ABCDEFGHIJKLMNOP", "aws-access-key-id"),
        (b"ASIA" + b"ABCDEFGHIJKLMNOP", "aws-access-key-id"),
        (b"sk-" + b"proj-" + b"abcdefghijklmnopqrstuvwxyz123456", "openai-api-key"),
        (b"ghp_" + b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJ", "github-token"),
        (b"github_" + b"pat_" + b"abcdefghijklmnopqrstuv", "github-token"),
        (b"ghs_123456_" + b"header.payload.signature-material", "github-token"),
        (b"xoxb-" + b"1234567890-abcdefghijkl", "slack-token"),
        (b"xapp-" + b"1234567890-abcdefghijkl", "slack-token"),
        (b"xwfp-" + b"1234567890-abcdefghijkl", "slack-token"),
    ],
    ids=(
        "aws-secret-assignment",
        "private-key",
        "aws-long-term-access-id",
        "aws-temporary-access-id",
        "openai-project-key",
        "github-classic-pat",
        "github-fine-grained-pat",
        "github-stateless-installation-token",
        "slack-bot-token",
        "slack-app-token",
        "slack-workflow-token",
    ),
)
def test_release_bundle_scan_rejects_high_confidence_secret_forms(
    tmp_path: Path,
    payload: bytes,
    expected_rule: str,
) -> None:
    (tmp_path / "unsafe.bin").write_bytes(payload)

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        f"unsafe.bin matches credential rule {expected_rule}"
    ]


def test_release_bundle_scan_detects_pattern_across_read_boundary(tmp_path: Path) -> None:
    prefix = b"x" * (1024 * 1024 - 3)
    credential = b"".join((b"sk-", b"ant-", b"abcdefghijklmnop123456"))
    (tmp_path / "boundary.bin").write_bytes(prefix + b" " + credential)

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        "boundary.bin matches credential rule anthropic-api-key"
    ]


def test_release_bundle_scan_detects_long_token_across_read_boundary(tmp_path: Path) -> None:
    prefix = b"x" * (1024 * 1024 - 1001) + b" "
    credential = b"".join((b"ghs_123456_", b"a" * 1500))
    (tmp_path / "boundary.bin").write_bytes(prefix + credential + b" ")

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        "boundary.bin matches credential rule github-token"
    ]


def test_release_bundle_scan_ignores_detector_literals_and_generic_runner_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "detector.js").write_bytes(rb"/\bsk-ant-[A-Za-z0-9_-]{16,}\b/gu")
    (tmp_path / "dependency.bin").write_bytes(
        b"debug metadata /Users/runner/work/dependency/source.c"
    )

    assert _scan_forbidden(tmp_path, personal_roots=()) == []


def test_release_bundle_scan_rejects_explicit_personal_build_root(tmp_path: Path) -> None:
    (tmp_path / "compiled.bin").write_bytes(b"debug metadata /Users/alice/private-project/source.c")

    assert _scan_forbidden(tmp_path, personal_roots=("/Users/alice",)) == [
        "compiled.bin contains a non-generic local home path"
    ]


def test_release_bundle_scan_does_not_confuse_home_prefixes(tmp_path: Path) -> None:
    (tmp_path / "compiled.bin").write_bytes(b"debug metadata /Users/alice2/project/source.c")

    assert _scan_forbidden(tmp_path, personal_roots=("/Users/alice",)) == []


def test_default_personal_roots_captures_local_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/Users/alice")
    monkeypatch.delenv("USERPROFILE", raising=False)

    assert _default_personal_roots() == ("/Users/alice",)


def test_default_personal_roots_excludes_generic_build_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/Users/runner")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\runneradmin")

    assert _default_personal_roots() == ()


def test_release_bundle_scan_fails_closed_on_unreadable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = tmp_path / "unreadable.bin"
    unreadable.write_bytes(b"content")
    original_open = Path.open

    def deny_unreadable_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == unreadable:
            raise PermissionError("simulated unreadable bundle member")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_unreadable_open)

    assert _scan_forbidden(tmp_path, personal_roots=()) == [
        "unreadable.bin could not be scanned (PermissionError)"
    ]


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
