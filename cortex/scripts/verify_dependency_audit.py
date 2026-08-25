"""Fail CI on unreviewed, expired, escalated, or path-drifted advisories."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

_SEVERITY = {"unknown": 0, "info": 1, "low": 2, "moderate": 3, "high": 4, "critical": 5}
_MAX_REVIEW_DAYS = {"low": 120, "moderate": 90, "high": 45, "critical": 0}


def _normalize_pnpm_path(path: object) -> str:
    """Compare package chains, not pnpm formatting or resolved versions."""
    compact = re.sub(r"\s*>\s*", ">", str(path).strip())
    normalized: list[str] = []
    for segment in compact.split(">"):
        if segment.startswith("@"):
            package, separator, _version = segment.rpartition("@")
            if separator and "/" in package:
                segment = package
        elif "@" in segment:
            segment = segment.split("@", 1)[0]
        normalized.append(segment)
    return ">".join(normalized)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _pnpm_findings(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    findings: dict[str, dict[str, Any]] = {}
    for key, raw in report.get("advisories", {}).items():
        advisory = dict(raw)
        advisory_id = str(advisory.get("github_advisory_id") or key)
        paths = sorted({
            _normalize_pnpm_path(path)
            for item in advisory.get("findings", [])
            for path in item.get("paths", [])
        })
        findings[advisory_id] = {
            "advisory_id": advisory_id,
            "package": str(advisory.get("module_name") or ""),
            "severity": str(advisory.get("severity") or "unknown").lower(),
            "paths": paths,
        }
    return findings


def _npm_findings(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for package, vulnerability in report.get("vulnerabilities", {}).items():
        nodes = [str(node) for node in vulnerability.get("nodes", [])]
        for via in vulnerability.get("via", []):
            if not isinstance(via, dict):
                continue
            url = str(via.get("url") or "")
            advisory_id = url.rstrip("/").split("/")[-1] or str(via.get("source"))
            current = merged.setdefault(advisory_id, {
                "advisory_id": advisory_id,
                "package": str(via.get("name") or package),
                "severity": str(via.get("severity") or "unknown").lower(),
                "paths": [],
            })
            current["paths"] = sorted(set(current["paths"] + nodes))
            via_severity = str(via.get("severity") or "unknown").lower()
            if _SEVERITY.get(via_severity, 0) > _SEVERITY.get(
                str(current["severity"]), 0,
            ):
                current["severity"] = via_severity
    return merged


def _pip_findings(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    findings: dict[str, dict[str, Any]] = {}
    for dependency in report.get("dependencies", []):
        package = str(dependency.get("name") or "")
        for vulnerability in dependency.get("vulns", []):
            advisory_id = str(vulnerability.get("id") or "unknown")
            findings[advisory_id] = {
                "advisory_id": advisory_id,
                "package": package,
                # pip-audit does not expose a normalized severity. Treat every
                # finding as blocking unless it receives an explicit exception.
                "severity": "unknown",
                "paths": [package],
            }
    return findings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ecosystem", choices=("pnpm", "npm", "pip"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exceptions", type=Path)
    parser.add_argument("--summary-out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = _read_json(args.report)
    parsers = {"pnpm": _pnpm_findings, "npm": _npm_findings, "pip": _pip_findings}
    findings = parsers[args.ecosystem](report)

    problems: list[str] = []
    exception_rows: list[dict[str, Any]] = []
    if args.exceptions is not None:
        exception_doc = _read_json(args.exceptions)
        if exception_doc.get("schema_version") != 1:
            problems.append("exception file schema_version must equal 1")
        exception_rows = [
            row for row in exception_doc.get("exceptions", [])
            if row.get("ecosystem") == args.ecosystem
        ]
    exceptions: dict[str, dict[str, Any]] = {}
    for row in exception_rows:
        advisory_id = str(row.get("advisory_id") or "")
        if not advisory_id:
            problems.append("exception lacks advisory_id")
            continue
        if advisory_id in exceptions:
            problems.append(f"duplicate exception {advisory_id}")
            continue
        exceptions[advisory_id] = row

    allowed: list[str] = []
    today = datetime.now(UTC).date()
    seen: set[str] = set()
    by_severity: defaultdict[str, int] = defaultdict(int)

    for advisory_id, finding in sorted(findings.items()):
        severity = str(finding["severity"])
        by_severity[severity] += 1
        exception = exceptions.get(advisory_id)
        if exception is None:
            problems.append(
                f"unreviewed {severity} advisory {advisory_id} "
                f"for {finding['package']}"
            )
            continue
        seen.add(advisory_id)
        problem_count_before = len(problems)
        max_severity = str(exception.get("max_severity") or "").lower()
        if max_severity not in _MAX_REVIEW_DAYS:
            problems.append(
                f"exception {advisory_id} has invalid max_severity {max_severity!r}"
            )
            continue
        if max_severity == "critical":
            problems.append(f"critical advisory {advisory_id} cannot be excepted")
        try:
            expires_on = date.fromisoformat(str(exception["expires_on"]))
        except (KeyError, ValueError):
            problems.append(f"exception {advisory_id} has invalid expires_on")
            continue
        max_days = _MAX_REVIEW_DAYS[max_severity]
        if expires_on < today:
            problems.append(f"expired exception {advisory_id} ({expires_on})")
        if max_days and (expires_on - today).days > max_days:
            problems.append(
                f"exception {advisory_id} exceeds {max_days}-day review window"
            )
        if _SEVERITY.get(severity, 0) > _SEVERITY.get(
            max_severity, 0,
        ):
            problems.append(
                f"severity escalated for {advisory_id}: {severity} > "
                f"{exception.get('max_severity')}"
            )
        if finding["package"] != exception.get("package"):
            problems.append(
                f"package drift for {advisory_id}: {finding['package']} != "
                f"{exception.get('package')}"
            )
        prefixes = [
            _normalize_pnpm_path(prefix)
            for prefix in exception.get("path_prefixes", [])
        ]
        if not prefixes:
            problems.append(f"exception {advisory_id} lacks dependency path prefixes")
        unmatched = [
            path for path in finding["paths"]
            if not any(path.startswith(prefix) for prefix in prefixes)
        ]
        if unmatched:
            problems.append(
                f"dependency-path drift for {advisory_id}: {unmatched}"
            )
        if len(str(exception.get("reason") or "")) < 30:
            problems.append(f"exception {advisory_id} lacks a substantive reason")
        if len(str(exception.get("mitigation") or "")) < 30:
            problems.append(f"exception {advisory_id} lacks a substantive mitigation")
        if len(problems) == problem_count_before:
            allowed.append(advisory_id)

    stale = sorted(set(exceptions) - seen)
    if stale:
        problems.append(f"stale exceptions must be removed: {stale}")

    summary = {
        "schema_version": 1,
        "ecosystem": args.ecosystem,
        "generated_at_unix": int(datetime.now(UTC).timestamp()),
        "finding_count": len(findings),
        "by_severity": dict(sorted(by_severity.items())),
        "reviewed_exceptions": sorted(allowed),
        "status": "pass" if not problems else "fail",
        "problems": problems,
    }
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
