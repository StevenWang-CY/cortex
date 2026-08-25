"""Dependency-audit exceptions are narrow, expiring, and fail closed."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cortex.scripts.verify_dependency_audit import main


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _run(
    monkeypatch: object,
    tmp_path: Path,
    *,
    report: dict[str, object],
    exceptions: list[dict[str, object]],
) -> tuple[int, dict[str, object]]:
    report_path = tmp_path / "audit.json"
    exception_path = tmp_path / "exceptions.json"
    summary_path = tmp_path / "summary.json"
    _write(report_path, report)
    _write(exception_path, {"schema_version": 1, "exceptions": exceptions})
    argv = [
        "verify_dependency_audit",
        "--ecosystem",
        "pnpm",
        "--report",
        str(report_path),
        "--exceptions",
        str(exception_path),
        "--summary-out",
        str(summary_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)  # type: ignore[attr-defined]
    result = main()
    return result, json.loads(summary_path.read_text(encoding="utf-8"))


def _report(*, severity: str = "moderate", path: str = ".>root>unsafe") -> dict[str, object]:
    return {
        "advisories": {
            "1": {
                "github_advisory_id": "GHSA-test",
                "module_name": "unsafe",
                "severity": severity,
                "findings": [{"paths": [path]}],
            }
        }
    }


def _exception(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ecosystem": "pnpm",
        "advisory_id": "GHSA-test",
        "package": "unsafe",
        "max_severity": "moderate",
        "path_prefixes": [".>root>unsafe"],
        "expires_on": (datetime.now(UTC).date() + timedelta(days=30)).isoformat(),
        "reason": "The vulnerable parser is unreachable from shipped runtime code.",
        "mitigation": "The release excludes that build-only path and tracks upstream.",
    }
    value.update(updates)
    return value


def test_exact_reviewed_path_is_permitted(monkeypatch: object, tmp_path: Path) -> None:
    code, summary = _run(
        monkeypatch,
        tmp_path,
        report=_report(),
        exceptions=[_exception()],
    )
    assert code == 0
    assert summary["status"] == "pass"


def test_pnpm_path_spacing_is_not_dependency_drift(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    code, summary = _run(
        monkeypatch,
        tmp_path,
        report=_report(path=". > root > unsafe"),
        exceptions=[_exception()],
    )
    assert code == 0
    assert summary["status"] == "pass"


def test_pnpm_resolved_versions_are_not_dependency_drift(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    code, summary = _run(
        monkeypatch,
        tmp_path,
        report=_report(path=". > root@1.2.3 > unsafe@4.5.6"),
        exceptions=[_exception()],
    )
    assert code == 0
    assert summary["status"] == "pass"


def test_dependency_path_drift_fails(monkeypatch: object, tmp_path: Path) -> None:
    code, summary = _run(
        monkeypatch,
        tmp_path,
        report=_report(path=".>new-parent>unsafe"),
        exceptions=[_exception()],
    )
    assert code == 1
    assert any("dependency-path drift" in item for item in summary["problems"])


def test_severity_escalation_fails(monkeypatch: object, tmp_path: Path) -> None:
    code, summary = _run(
        monkeypatch,
        tmp_path,
        report=_report(severity="high"),
        exceptions=[_exception()],
    )
    assert code == 1
    assert any("severity escalated" in item for item in summary["problems"])


def test_unreviewed_and_stale_exceptions_both_fail(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    stale = _exception(advisory_id="GHSA-stale")
    code, summary = _run(
        monkeypatch,
        tmp_path,
        report=_report(),
        exceptions=[stale],
    )
    assert code == 1
    assert any("unreviewed" in item for item in summary["problems"])
    assert any("stale exceptions" in item for item in summary["problems"])
