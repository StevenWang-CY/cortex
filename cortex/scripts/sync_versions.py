"""Synchronize and verify every shipped version surface.

``cortex/pyproject.toml`` is the sole hand-edited version source. The Python
runtime constant and JavaScript manifests are generated projections of it.
Build and release entry points run ``--check`` so a stale projection cannot
ship, while ``--tag`` additionally proves a release tag names that version.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_ROOT = Path(__file__).resolve().parents[2]
_PROJECT = _ROOT / "cortex"
_PYPROJECT = _PROJECT / "pyproject.toml"
_PYTHON_VERSION = _PROJECT / "_version.py"
_BROWSER_PACKAGE = _PROJECT / "apps" / "browser_extension" / "package.json"
_VSCODE_PACKAGE = _PROJECT / "apps" / "vscode_extension" / "package.json"
_VSCODE_LOCK = _PROJECT / "apps" / "vscode_extension" / "package-lock.json"


def canonical_version() -> str:
    """Return the validated version from the canonical project metadata."""

    with _PYPROJECT.open("rb") as handle:
        value = tomllib.load(handle)["project"]["version"]
    version = str(value)
    if _SEMVER_RE.fullmatch(version) is None:
        raise ValueError(f"project.version is not semantic versioning: {version!r}")
    return version


def _python_source(version: str) -> str:
    return (
        '"""Generated project version. Run '
        '``python -m cortex.scripts.sync_versions --apply``."""\n\n'
        f'VERSION = "{version}"\n'
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _json_source(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def expected_surfaces(version: str) -> dict[Path, str]:
    """Return generated file contents without mutating the repository."""

    browser = _load_json(_BROWSER_PACKAGE)
    browser["version"] = version

    vscode = _load_json(_VSCODE_PACKAGE)
    vscode["version"] = version

    vscode_lock = _load_json(_VSCODE_LOCK)
    vscode_lock["version"] = version
    packages = vscode_lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise ValueError("VS Code package-lock.json lacks packages[''] metadata")
    packages[""]["version"] = version

    return {
        _PYTHON_VERSION: _python_source(version),
        _BROWSER_PACKAGE: _json_source(browser),
        _VSCODE_PACKAGE: _json_source(vscode),
        _VSCODE_LOCK: _json_source(vscode_lock),
    }


def _relative(path: Path) -> str:
    return str(path.relative_to(_ROOT))


def check(version: str) -> list[str]:
    """Return actionable drift messages for every stale surface."""

    problems: list[str] = []
    for path, expected in expected_surfaces(version).items():
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            problems.append(f"{_relative(path)} is not synchronized to {version}")
    return problems


def apply(version: str) -> None:
    """Regenerate each version projection from the canonical value."""

    for path, expected in expected_surfaces(version).items():
        path.write_text(expected, encoding="utf-8")
        print(f"updated {_relative(path)}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--print", dest="print_version", action="store_true")
    parser.add_argument(
        "--tag",
        help="optional release tag to verify (must equal v<project.version>)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    version = canonical_version()
    if args.print_version:
        print(version)
        return 0
    if args.apply:
        apply(version)

    problems = check(version)
    if args.tag is not None and args.tag != f"v{version}":
        problems.append(
            f"release tag {args.tag!r} does not match canonical version v{version}"
        )
    if problems:
        print("version consistency FAILED:", file=sys.stderr)
        for problem in problems:
            print(f" - {problem}", file=sys.stderr)
        print(
            "run: python -m cortex.scripts.sync_versions --apply",
            file=sys.stderr,
        )
        return 1
    print(f"all version surfaces are synchronized at {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
