"""Validate real-device evidence before promoting a draft macOS release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _ROOT / "docs/release/manual-release-evidence.schema.json"
REQUIRED_CASE_IDS = frozenset(
    {
        "artifact.identity",
        "install.launch",
        "onboarding.keyboard_voiceover",
        "permissions.deny_grant_revoke",
        "credentials.keychain_containment",
        "browser.chrome_native",
        "browser.edge_native",
        "editor.vscode",
        "runtime.lifecycle_camera_tcc",
        "authority.transaction_restore",
        "fault.recovery",
        "update.migration",
        "export.delete",
        "uninstall.cleanup",
    }
)


class ReleaseRecordError(RuntimeError):
    """A release record is incomplete, inconsistent, or not independent."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseRecordError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseRecordError(f"release record must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_errors(record: dict[str, Any]) -> list[str]:
    schema = _load_object(_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(record), key=lambda item: list(item.path))
    ]


def validate_release_records(
    records_dir: Path,
    *,
    asset_dir: Path,
    expected_version: str,
    expected_commit: str,
) -> dict[str, Any]:
    """Require one complete, independently reviewed record per architecture."""

    if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
        raise ReleaseRecordError("expected_commit must be a full lowercase Git SHA")
    record_paths = tuple(sorted(records_dir.glob("manual-release-evidence-*.json")))
    if len(record_paths) != 2:
        raise ReleaseRecordError("expected exactly two manual-release-evidence-<arch>.json records")

    seen_architectures: set[str] = set()
    builder_ids: set[str] = set()
    independent_ids: set[str] = set()
    evidence_assets: set[str] = set()
    record_summaries: list[dict[str, Any]] = []

    for path in record_paths:
        record = _load_object(path)
        errors = _schema_errors(record)
        if errors:
            raise ReleaseRecordError(f"{path.name} violates schema: {'; '.join(errors)}")
        if record["decision"] != "release":
            raise ReleaseRecordError(f"{path.name} is not approved for release")

        architecture = str(record["host"]["architecture"])
        if architecture in seen_architectures:
            raise ReleaseRecordError(f"duplicate architecture record: {architecture}")
        seen_architectures.add(architecture)
        expected_record_name = f"manual-release-evidence-{architecture}.json"
        if path.name != expected_record_name:
            raise ReleaseRecordError(
                f"record for {architecture} must be named {expected_record_name!r}"
            )

        artifact = record["artifact"]
        expected_filename = f"Cortex-{expected_version}-macos-{architecture}.dmg"
        if artifact["version"] != expected_version:
            raise ReleaseRecordError(
                f"{path.name} version {artifact['version']!r} != {expected_version!r}"
            )
        if artifact["filename"] != expected_filename:
            raise ReleaseRecordError(f"{path.name} artifact must be {expected_filename!r}")
        if artifact["git_commit"] != expected_commit:
            raise ReleaseRecordError(f"{path.name} is bound to a different commit")
        artifact_path = asset_dir / expected_filename
        if not artifact_path.is_file():
            raise ReleaseRecordError(f"release artifact is missing: {artifact_path}")
        if _sha256(artifact_path) != artifact["sha256"]:
            raise ReleaseRecordError(f"artifact hash mismatch in {path.name}")
        evidence_bundle = asset_dir / f"Cortex-{expected_version}-macos-{architecture}-evidence.zip"
        checksum_path = asset_dir / f"SHA256SUMS-{architecture}"
        for required_asset in (evidence_bundle, checksum_path):
            if not required_asset.is_file() or required_asset.stat().st_size == 0:
                raise ReleaseRecordError(
                    f"required automated evidence asset is missing: {required_asset}"
                )
        expected_checksum_line = f"{artifact['sha256']}  {expected_filename}"
        checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
        if expected_checksum_line not in checksum_lines:
            raise ReleaseRecordError(
                f"{checksum_path.name} does not bind the reviewed artifact hash"
            )

        cases = record["cases"]
        case_ids = [str(case["id"]) for case in cases]
        if len(case_ids) != len(set(case_ids)):
            raise ReleaseRecordError(f"{path.name} contains duplicate case IDs")
        actual_cases = set(case_ids)
        if actual_cases != REQUIRED_CASE_IDS:
            missing = sorted(REQUIRED_CASE_IDS - actual_cases)
            unexpected = sorted(actual_cases - REQUIRED_CASE_IDS)
            raise ReleaseRecordError(
                f"{path.name} case catalog mismatch; missing={missing}, unexpected={unexpected}"
            )
        for case in cases:
            for evidence_name in case["evidence_files"]:
                evidence_name = str(evidence_name)
                accepted_evidence_stems = (
                    f"manual-evidence-{architecture}.",
                    f"manual-evidence-{architecture}-",
                )
                if not evidence_name.startswith(accepted_evidence_stems):
                    raise ReleaseRecordError(
                        f"{path.name} references non-manual or cross-architecture "
                        f"evidence asset {evidence_name!r}"
                    )
                evidence_path = asset_dir / evidence_name
                if not evidence_path.is_file() or evidence_path.stat().st_size == 0:
                    raise ReleaseRecordError(
                        f"{path.name} references missing or empty evidence asset {evidence_name!r}"
                    )
                evidence_assets.add(evidence_name)

        for reviewer in record["reviewers"]:
            reviewer_id = str(reviewer["reviewer_id"])
            if reviewer["role"] == "builder":
                builder_ids.add(reviewer_id)
            elif reviewer["role"] == "independent_reviewer":
                independent_ids.add(reviewer_id)
        record_summaries.append(
            {
                "path": path.name,
                "sha256": _sha256(path),
                "architecture": architecture,
                "artifact": expected_filename,
            }
        )

    expected_architectures = {"arm64", "x86_64"}
    if seen_architectures != expected_architectures:
        raise ReleaseRecordError(f"release records must cover {sorted(expected_architectures)}")
    overlapping_reviewers = sorted(builder_ids & independent_ids)
    if overlapping_reviewers:
        raise ReleaseRecordError(
            "builder and independent-reviewer identities overlap: "
            + ", ".join(overlapping_reviewers)
        )
    return {
        "schema_version": "1.0",
        "status": "passed",
        "version": expected_version,
        "git_commit": expected_commit,
        "architectures": sorted(seen_architectures),
        "records": record_summaries,
        "evidence_assets": sorted(evidence_assets),
        "builder_ids": sorted(builder_ids),
        "independent_reviewer_ids": sorted(independent_ids),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = validate_release_records(
            args.records_dir,
            asset_dir=args.asset_dir,
            expected_version=args.expected_version,
            expected_commit=args.expected_commit,
        )
    except ReleaseRecordError as exc:
        print(f"release record validation FAILED: {exc}", file=sys.stderr)
        return 1
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
