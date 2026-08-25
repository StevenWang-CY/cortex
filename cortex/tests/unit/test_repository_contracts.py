"""Repository documentation and generated-surface drift gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.scripts import verify_repository_contracts
from cortex.scripts.verify_repository_contracts import all_problems


def test_repository_contracts_are_synchronized() -> None:
    problems = all_problems()
    assert problems == [], "\n".join(problems)


def test_dependency_contract_rejects_overlapping_opencv_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
dependencies = ["opencv-contrib-python>=4.9.0,<5"]
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "opencv-contrib-python"
version = "4.11.0.86"

[[package]]
name = "opencv-python"
version = "4.11.0.86"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repository_contracts, "_PROJECT", tmp_path)

    problems = verify_repository_contracts.check_dependency_graph_contract()

    assert any("must not co-install opencv-python" in problem for problem in problems)
