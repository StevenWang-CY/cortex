"""Every gate this repository declares must actually run.

A threshold that is configured but never executed is worse than no
threshold: it reads, to anyone auditing the repository, as an enforced
guarantee. Two of them were exactly that before v0.5.0, and the tests here
exist so the next one is caught by CI rather than by an audit.

* ``tests-gates:280`` — ``[tool.coverage.report] fail_under = 85`` sat in
  ``cortex/pyproject.toml`` while nothing in the repository ever passed
  ``--cov``: not the CI workflows, not the release workflow, not
  ``run_python_gate.sh``, not a Makefile target, not ``addopts``. The real
  figure was 68.79 %. Compounding it, coverage looks for its configuration in
  the working directory, and the canonical gate runs from the repository
  root — so even a run that did pass ``--cov`` would have read none of that
  block, ``omit`` included, and counted every test module as covered source.

* ``tests-gates:144`` — the extension bundle-size guard budgeted a
  hand-maintained file list and waved through every other subdirectory, so a
  refactor moved a third of the shipping source outside it. See
  ``cortex/tests/performance/test_bundle_size.py``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_GATE_SCRIPT = _ROOT / "cortex" / "scripts" / "run_python_gate.sh"
_PYPROJECT = _ROOT / "cortex" / "pyproject.toml"


def _gate_source() -> str:
    assert _GATE_SCRIPT.is_file(), f"canonical gate missing at {_GATE_SCRIPT}"
    return _GATE_SCRIPT.read_text()


def test_the_canonical_gate_measures_coverage() -> None:
    """``fail_under`` only means something if a run computes coverage."""
    source = _gate_source()
    assert "--cov" in source, (
        "run_python_gate.sh does not pass --cov, so [tool.coverage.report] "
        "fail_under is dead configuration that reads as an enforced floor"
    )


def test_the_canonical_gate_points_coverage_at_the_config() -> None:
    """Coverage searches the working directory, which is the repo root.

    The settings live in ``cortex/pyproject.toml``. Without an explicit
    ``--cov-config`` the entire ``[tool.coverage]`` block is skipped in
    silence — no error, no warning, just a run with default settings and no
    floor.
    """
    source = _gate_source()
    assert "--cov-config=cortex/pyproject.toml" in source, (
        "the gate runs from the repository root, where coverage finds no "
        "config; pass --cov-config=cortex/pyproject.toml explicitly"
    )


def test_the_coverage_floor_is_declared_and_plausible() -> None:
    """The floor exists, and is a number the suite can actually meet.

    Measured 68.79 % on 2026-09-19. The floor is deliberately below that: the
    gate runs on two runners with different Python versions and skip sets, and
    a floor pinned a fraction under the measurement would flap on ordinary
    variance and be deleted again within a release.

    The upper bound here is not a coverage target; it is a guard against
    someone restoring an aspirational figure the suite has never reached,
    which is how this became dead configuration in the first place. Raising
    the floor means raising coverage first.
    """
    config = tomllib.loads(_PYPROJECT.read_text())
    report = config["tool"]["coverage"]["report"]
    floor = report["fail_under"]
    assert isinstance(floor, int | float)
    assert 60 <= floor <= 75, (
        f"fail_under is {floor}; the measured coverage is 68.79%. A floor "
        "above the measurement does not enforce anything — it just fails "
        "every build, or gets quietly removed again."
    )


def test_coverage_omits_the_test_suite_from_its_own_denominator() -> None:
    """Test modules are ~98 % covered by construction and flatter the total."""
    config = tomllib.loads(_PYPROJECT.read_text())
    omit = config["tool"]["coverage"]["run"]["omit"]
    assert any("tests" in pattern for pattern in omit), (
        f"coverage omit is {omit}; without a tests pattern the suite measures "
        "itself and reports a number roughly 14 points above the truth"
    )
