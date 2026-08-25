"""Regression gates for the installable Python wheel surface."""

from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path

import pytest

from cortex.scripts.verify_python_artifact import REQUIRED_MEMBERS, verify_wheel

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_wheel(path: Path, members: set[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for member in sorted(members):
            archive.writestr(member, b"fixture")


def test_metadata_only_wheel_is_rejected(tmp_path: Path) -> None:
    wheel = tmp_path / "cortex-0-py3-none-any.whl"
    _write_wheel(wheel, {"cortex-0.dist-info/METADATA"})
    with pytest.raises(ValueError, match="missing required runtime members"):
        verify_wheel(wheel)


def test_required_runtime_wheel_is_accepted(tmp_path: Path) -> None:
    wheel = tmp_path / "cortex-0-py3-none-any.whl"
    _write_wheel(wheel, set(REQUIRED_MEMBERS) | {"cortex-0.dist-info/METADATA"})
    member_count, unpacked_bytes = verify_wheel(wheel)
    assert member_count == len(REQUIRED_MEMBERS) + 1
    assert unpacked_bytes > 0


@pytest.mark.parametrize(
    "forbidden",
    [
        "./__init__.py",
        "cortex/tests/test_runtime.py",
        "cortex/apps/browser_extension/background.ts",
        "cortex/services/__pycache__/runtime.pyc",
        "cortex/.env",
        "cortex/.env.local",
        "cortex/scripts/native_host_debug.log",
        "cortex/scripts/notarization.key",
        "cortex/scripts/APP_STORE_CONNECT.P8",
        "cortex/storage/runtime.sqlite3",
    ],
)
def test_forbidden_wheel_members_are_rejected(
    tmp_path: Path,
    forbidden: str,
) -> None:
    wheel = tmp_path / "cortex-0-py3-none-any.whl"
    _write_wheel(
        wheel,
        set(REQUIRED_MEMBERS)
        | {"cortex-0.dist-info/METADATA", forbidden},
    )
    with pytest.raises(ValueError, match="forbidden source/artifacts"):
        verify_wheel(wheel)


def test_wheel_build_excludes_machine_local_artifact_patterns() -> None:
    config = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "force-include" not in wheel
    assert wheel["sources"] == {"": "cortex"}
    excludes = set(wheel["exclude"])
    assert {
        "**/.env",
        "**/.env.*",
        "**/__pycache__/**",
        "**/*.log",
        "**/*.pem",
        "**/*.key",
        "**/*.p8",
        "**/*.jks",
        "**/*.keystore",
        "**/*.db",
        "**/*.sqlite",
        "**/*.sqlite3",
    } <= excludes
