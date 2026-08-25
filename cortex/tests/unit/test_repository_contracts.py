"""Repository documentation and generated-surface drift gates."""

from __future__ import annotations

from cortex.scripts.verify_repository_contracts import all_problems


def test_repository_contracts_are_synchronized() -> None:
    problems = all_problems()
    assert problems == [], "\n".join(problems)
