"""Regression gates for the installable Python wheel surface."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from cortex.scripts.verify_python_artifact import REQUIRED_MEMBERS, verify_wheel


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

