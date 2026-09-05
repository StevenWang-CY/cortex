"""Validate real-device evidence before promoting a draft macOS release.

Two assurance tiers exist. Both require the complete automated chain (signed,
notarized, stapled, mounted-smoke-tested DMGs with GitHub attestations); the
tiers differ only in how much human, on-device evidence backs the candidate:

``self-attested``
    At least one architecture was exercised on a clean profile by the
    maintainer. The core cases that map to past shipped regressions must pass;
    every other catalogued case is recorded as ``passed`` or ``not_run`` with a
    stated reason. Architectures without a record are labelled CI-verified only.

``independently-reviewed``
    Both architectures carry a record, every case passed, and the builder and
    independent-reviewer identities are globally disjoint.

The tier is written into ``release-promotion-validation.json`` and rendered
into the public release notes so nobody can mistake one tier for the other.
"""

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
SUPPORTED_ARCHITECTURES = ("arm64", "x86_64")
ASSURANCE_TIERS = ("self-attested", "independently-reviewed")
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
# Cases that map directly to regressions that reached a staged or shipped
# artifact (v0.3.6 startup crash, v0.3.10 installer identity, v0.3.14/15 browser
# bridge, v0.3.15 background-only bundle). A self-attested release may leave
# other cases ``not_run`` with a reason, but never these.
CORE_CASE_IDS = frozenset(
    {
        "artifact.identity",
        "install.launch",
        "browser.chrome_native",
        "runtime.lifecycle_camera_tcc",
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


def _validate_artifact_binding(
    path: Path,
    record: dict[str, Any],
    *,
    asset_dir: Path,
    expected_version: str,
    expected_commit: str,
) -> str:
    architecture = str(record["host"]["architecture"])
    expected_record_name = f"manual-release-evidence-{architecture}.json"
    if path.name != expected_record_name:
        raise ReleaseRecordError(f"record for {architecture} must be named {expected_record_name!r}")
    artifact = record["artifact"]
    expected_filename = f"Cortex-{expected_version}-macos-{architecture}.dmg"
    if artifact["version"] != expected_version:
        raise ReleaseRecordError(f"{path.name} version {artifact['version']!r} != {expected_version!r}")
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
            raise ReleaseRecordError(f"required automated evidence asset is missing: {required_asset}")
    expected_checksum_line = f"{artifact['sha256']}  {expected_filename}"
    checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
    if expected_checksum_line not in checksum_lines:
        raise ReleaseRecordError(f"{checksum_path.name} does not bind the reviewed artifact hash")
    return expected_filename


def _validate_cases(
    path: Path,
    record: dict[str, Any],
    *,
    tier: str,
    asset_dir: Path,
    evidence_assets: set[str],
) -> list[str]:
    architecture = str(record["host"]["architecture"])
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
    not_run: list[str] = []
    for case in cases:
        case_id = str(case["id"])
        result = str(case["result"])
        if result != "passed":
            if tier == "independently-reviewed":
                raise ReleaseRecordError(
                    f"{path.name} case {case_id} is {result!r}; an independently reviewed "
                    "release requires every case to pass"
                )
            if case_id in CORE_CASE_IDS:
                raise ReleaseRecordError(
                    f"{path.name} core case {case_id} is {result!r}; self-attested releases "
                    "must pass every core case on hardware"
                )
            if result != "not_run":
                raise ReleaseRecordError(f"{path.name} case {case_id} is {result!r}")
            not_run.append(case_id)
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
    return sorted(not_run)


def validate_release_records(
    records_dir: Path,
    *,
    asset_dir: Path,
    expected_version: str,
    expected_commit: str,
    tier: str | None = None,
) -> dict[str, Any]:
    """Require complete, truthfully tiered records for the staged candidate.

    ``tier`` pins the tier the operator intends to publish. Every record must
    declare that same tier; when ``tier`` is ``None`` the (single, consistent)
    tier declared by the records is used.
    """

    if re.fullmatch(r"[0-9a-f]{40}", expected_commit) is None:
        raise ReleaseRecordError("expected_commit must be a full lowercase Git SHA")
    if tier is not None and tier not in ASSURANCE_TIERS:
        raise ReleaseRecordError(f"unknown assurance tier {tier!r}")
    record_paths = tuple(sorted(records_dir.glob("manual-release-evidence-*.json")))
    if not 1 <= len(record_paths) <= len(SUPPORTED_ARCHITECTURES):
        raise ReleaseRecordError(
            "expected one manual-release-evidence-<arch>.json record per exercised "
            f"architecture (1-{len(SUPPORTED_ARCHITECTURES)}), found {len(record_paths)}"
        )

    declared_tiers: set[str] = set()
    seen_architectures: set[str] = set()
    builder_ids: set[str] = set()
    independent_ids: set[str] = set()
    evidence_assets: set[str] = set()
    record_summaries: list[dict[str, Any]] = []
    cases_not_run: dict[str, list[str]] = {}
    hosts: dict[str, dict[str, Any]] = {}

    for path in record_paths:
        record = _load_object(path)
        errors = _schema_errors(record)
        if errors:
            raise ReleaseRecordError(f"{path.name} violates schema: {'; '.join(errors)}")
        if record["decision"] != "release":
            raise ReleaseRecordError(f"{path.name} is not approved for release")
        declared_tiers.add(str(record["assurance_tier"]))
        architecture = str(record["host"]["architecture"])
        if architecture in seen_architectures:
            raise ReleaseRecordError(f"duplicate architecture record: {architecture}")
        seen_architectures.add(architecture)
        hosts[architecture] = {
            "macos_version": str(record["host"]["macos_version"]),
            "device_class": str(record["host"]["device_class"]),
        }
        expected_filename = _validate_artifact_binding(
            path,
            record,
            asset_dir=asset_dir,
            expected_version=expected_version,
            expected_commit=expected_commit,
        )
        for reviewer in record["reviewers"]:
            reviewer_id = str(reviewer["reviewer_id"])
            if reviewer["role"] in {"builder", "maintainer"}:
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
        # Case validation depends on the tier, which must be consistent across
        # records; defer it until every declared tier is known.
        record_summaries[-1]["_record"] = record
        record_summaries[-1]["_path"] = path

    if len(declared_tiers) != 1:
        raise ReleaseRecordError(f"records declare conflicting assurance tiers: {sorted(declared_tiers)}")
    declared_tier = next(iter(declared_tiers))
    if tier is not None and tier != declared_tier:
        raise ReleaseRecordError(
            f"records declare {declared_tier!r} but the operator requested {tier!r}"
        )
    effective_tier = declared_tier

    for summary in record_summaries:
        record = summary.pop("_record")
        path = summary.pop("_path")
        cases_not_run[summary["architecture"]] = _validate_cases(
            path,
            record,
            tier=effective_tier,
            asset_dir=asset_dir,
            evidence_assets=evidence_assets,
        )

    expected_architectures = set(SUPPORTED_ARCHITECTURES)
    if effective_tier == "independently-reviewed":
        if seen_architectures != expected_architectures:
            raise ReleaseRecordError(
                "an independently reviewed release must cover "
                f"{sorted(expected_architectures)}"
            )
        overlapping_reviewers = sorted(builder_ids & independent_ids)
        if overlapping_reviewers:
            raise ReleaseRecordError(
                "builder and independent-reviewer identities overlap: "
                + ", ".join(overlapping_reviewers)
            )
        if not independent_ids:
            raise ReleaseRecordError("an independently reviewed release names no independent reviewer")
    return {
        "schema_version": "1.1",
        "status": "passed",
        "assurance_tier": effective_tier,
        "version": expected_version,
        "git_commit": expected_commit,
        "architectures": sorted(seen_architectures),
        "hardware_verified_architectures": sorted(seen_architectures),
        "ci_only_architectures": sorted(expected_architectures - seen_architectures),
        "hosts": hosts,
        "core_cases": sorted(CORE_CASE_IDS),
        "cases_not_run": cases_not_run,
        "records": record_summaries,
        "evidence_assets": sorted(evidence_assets),
        "builder_ids": sorted(builder_ids),
        "independent_reviewer_ids": sorted(independent_ids),
    }


def render_assurance_notes(report: dict[str, Any], *, workflow_run_url: str | None = None) -> str:
    """Render the public release-notes section that states the assurance tier."""

    tier = str(report["assurance_tier"])
    lines = ["## Assurance", ""]
    if tier == "independently-reviewed":
        lines.append(
            "**Tier: independently reviewed.** Both architectures were exercised on clean "
            "hardware, every catalogued case passed, and the builder and independent reviewer "
            "were different people."
        )
    else:
        lines.append(
            "**Tier: self-attested.** The maintainer who built this release also exercised it. "
            "No independent reviewer has validated this artifact."
        )
    lines.append("")
    lines.append(
        "Automated evidence (all tiers): CI built, Developer ID signed, notarized, and stapled "
        "both DMGs from the tagged commit, mounted each image, verified bundle identity and "
        "signatures, ran the hardware-free startup and native-host probes, and published SBOMs "
        "plus GitHub provenance attestations."
    )
    if workflow_run_url:
        lines.append(f"Release workflow: {workflow_run_url}")
    lines.append("")
    for architecture in report["hardware_verified_architectures"]:
        host = report["hosts"].get(architecture, {})
        not_run = report["cases_not_run"].get(architecture, [])
        passed = len(REQUIRED_CASE_IDS) - len(not_run)
        lines.append(
            f"- `{architecture}`: exercised on {host.get('device_class', 'a clean Mac')} "
            f"(macOS {host.get('macos_version', 'unknown')}); {passed} of "
            f"{len(REQUIRED_CASE_IDS)} catalogued cases passed"
            + (f"; not run: {', '.join(not_run)}" if not_run else "")
            + "."
        )
    for architecture in report["ci_only_architectures"]:
        lines.append(
            f"- `{architecture}`: **CI-verified only** (notarized, mounted, headless startup and "
            "native-host probes); not exercised on hardware for this release."
        )
    lines.append("")
    lines.append(
        "Verify a download with `shasum -a 256 -c SHA256SUMS-<arch>`, "
        "`gh attestation verify <dmg> --repo StevenWang-CY/cortex`, and "
        "`xcrun stapler validate <dmg>`."
    )
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", type=Path, required=True)
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--tier", choices=ASSURANCE_TIERS)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--notes-output",
        type=Path,
        help="write the Markdown assurance section appended to the release notes",
    )
    parser.add_argument("--workflow-run-url")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = validate_release_records(
            args.records_dir,
            asset_dir=args.asset_dir,
            expected_version=args.expected_version,
            expected_commit=args.expected_commit,
            tier=args.tier,
        )
    except ReleaseRecordError as exc:
        print(f"release record validation FAILED: {exc}", file=sys.stderr)
        return 1
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    if args.notes_output is not None:
        args.notes_output.parent.mkdir(parents=True, exist_ok=True)
        args.notes_output.write_text(
            render_assurance_notes(report, workflow_run_url=args.workflow_run_url),
            encoding="utf-8",
        )
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
