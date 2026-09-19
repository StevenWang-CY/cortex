"""Browser-extension size regression guard.

Two measurements, in order of authority:

1. **The shipped bundle**, when a production build is present
   (``build/chrome-mv3-prod``). This is the real thing. CI builds the
   extension, so the check runs there; it skips in a worktree without a
   build rather than pretending a source proxy is the same measurement.

2. **The shipping source tree**, always. A proxy, but one that is
   *discovered* rather than listed, so it cannot erode.

Erosion is why this file was rewritten. The previous guard budgeted a
hand-maintained list of top-level files plus ``contents/`` and ``tabs/``,
and explicitly waved through every other subdirectory as a "non-entry
utility". v0.4.0 then split ``background.ts`` and ``popup.tsx`` into
``bg/surfaces/``, ``popup/components/``, ``popup/styles.ts`` and ``lib/``.
That code is imported by the entry points and therefore bundled, but the
guard stopped counting it: 243,158 bytes, 32 % of the shipping source,
invisible. The per-file budgets appeared to *improve* (background.ts
315 KB -> 265 KB) purely because code moved sideways, and the aggregate
read 78 % of its 680 KB ceiling while the real total was 771,920 bytes —
13 % over. The guard was green and its own stated budget was blown.

Measured on 2026-09-19, ``plasmo build`` (chrome-mv3, Plasmo 0.90.5):

    Shipping source:      771,920 bytes
    Bundle, uncompressed: 820,399 bytes
    Bundle, gzip sum:     263,803 bytes   (17 files)
    Source : gzip ratio   2.93 : 1

The 263,803 gzip figure exceeds the 250 KB aspiration this file used to
quote. That aspiration was calibrated in 2026-08 against a bundle that has
since grown, and the ceilings below are set against what is true today with
headroom, not against a number the build has not met for a release. Lowering
it is a size-reduction task, not a test edit; see ``docs/limitations.md``.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

_EXTENSION_ROOT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "cortex"
    / "apps"
    / "browser_extension"
)
_BUNDLE_ROOT = _EXTENSION_ROOT / "build" / "chrome-mv3-prod"

# Directories that never ship: dependencies, build output, tests, mocks.
_NON_SHIPPING_DIRS: frozenset[str] = frozenset({
    ".git",
    ".plasmo",
    "__tests__",
    "build",
    "dist",
    "node_modules",
    "test",
})

# Files that live in the package but are not compiled into the bundle.
_DEV_ONLY_FILES: frozenset[str] = frozenset({
    "plasmo.config.ts",
    "vitest.config.ts",
})

# ── Ceilings ────────────────────────────────────────────────────────────
#
# Each is the 2026-09-19 measurement plus headroom, so ordinary feature work
# does not force a bump but a step change does. Raising one requires a fresh
# production build and an updated measurement block in this docstring.

_BUNDLE_GZIP_BUDGET_BYTES = 280_000       # measured 263,803  (+6 %)
_BUNDLE_UNCOMPRESSED_BUDGET_BYTES = 900_000  # measured 820,399  (+10 %)
_TOTAL_SOURCE_BUDGET_BYTES = 850_000      # measured 771,920  (+10 %)

# Per-file ceilings for the largest sources. These exist to localise a
# regression to one file; the aggregate above is what bounds the bundle.
# A file not listed here is still counted in the aggregate — that is the
# property the old table lacked.
_SOURCE_BUDGETS: dict[str, int] = {
    "background.ts": 300_000,
    "popup.tsx": 176_000,
    "newtab.tsx": 80_000,
    "tab-manager.ts": 60_000,
    "design-tokens.ts": 30_000,
    "config.ts": 4_000,
    "contents/ambient.ts": 40_000,
    "contents/leetcode-observer.ts": 40_000,
    "contents/activity-tracker.ts": 40_000,
    "tabs/onboarding.tsx": 80_000,
}

# Per-directory ceilings. A refactor that moves code out of a budgeted
# entry point has to land somewhere, and this is what notices where.
_DIRECTORY_BUDGETS: dict[str, int] = {
    "(root)": 500_000,   # measured 436,443
    "lib": 170_000,      # measured 136,039
    "contents": 90_000,  # measured  74,314
    "bg": 70_000,        # measured  53,807
    "popup": 70_000,     # measured  53,312
    "tabs": 30_000,      # measured  18,005
}


def _shipping_sources() -> list[Path]:
    """Every TypeScript source compiled into the extension bundle.

    Discovered by walking the package, not read from a list: a list is
    what eroded. Anything that is a ``.ts``/``.tsx``, is not a type-only
    declaration, and does not sit under a non-shipping directory, ships.
    """
    found: list[Path] = []
    for candidate in _EXTENSION_ROOT.rglob("*.ts*"):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(_EXTENSION_ROOT)
        if any(part in _NON_SHIPPING_DIRS for part in relative.parts[:-1]):
            continue
        if candidate.name.endswith(".d.ts"):
            continue
        if candidate.name in _DEV_ONLY_FILES:
            continue
        found.append(candidate)
    return found


def _top_level(relative: str) -> str:
    return relative.split("/", 1)[0] if "/" in relative else "(root)"


@pytest.fixture(scope="module")
def shipping_sources() -> list[Path]:
    return _shipping_sources()


def test_extension_root_exists() -> None:
    """The budgets reference a layout that still exists."""
    assert _EXTENSION_ROOT.is_dir(), (
        f"browser extension not at {_EXTENSION_ROOT}; the budgets here are "
        "stale — fix the path, not the assertion"
    )


def test_every_shipping_directory_has_a_budget(
    shipping_sources: list[Path],
) -> None:
    """A new directory cannot ship code without a ceiling.

    This is the assertion the old guard did not have. It explicitly
    permitted any subdirectory other than ``contents/`` and ``tabs/`` as a
    "non-entry utility", so the v0.4.0 split into ``bg/``, ``popup/`` and
    ``lib/`` moved a third of the codebase outside every budget without
    failing anything.
    """
    directories = {
        _top_level(path.relative_to(_EXTENSION_ROOT).as_posix())
        for path in shipping_sources
    }
    unbudgeted = sorted(directories - set(_DIRECTORY_BUDGETS))
    assert not unbudgeted, (
        f"shipping source in directories with no size budget: {unbudgeted}. "
        "Add a line to _DIRECTORY_BUDGETS with a measurement, or move the "
        "code somewhere already budgeted. Do not add an exemption."
    )


@pytest.mark.parametrize("relpath,budget", sorted(_SOURCE_BUDGETS.items()))
def test_per_file_source_budget(relpath: str, budget: int) -> None:
    """Each named source file stays inside its ceiling."""
    path = _EXTENSION_ROOT / relpath
    if not path.exists():
        pytest.skip(f"{relpath} not present in this worktree")
    size = path.stat().st_size
    assert size <= budget, (
        f"{relpath} grew to {size:,} bytes — budget is {budget:,} "
        f"({size / budget:.0%}). Refactor, or bump the budget with a fresh "
        "production-build measurement."
    )


def test_per_directory_source_budget(shipping_sources: list[Path]) -> None:
    """Each shipping directory stays inside its ceiling."""
    totals: dict[str, int] = {}
    for path in shipping_sources:
        relative = path.relative_to(_EXTENSION_ROOT).as_posix()
        top = _top_level(relative)
        totals[top] = totals.get(top, 0) + path.stat().st_size

    over = {
        directory: (size, _DIRECTORY_BUDGETS[directory])
        for directory, size in totals.items()
        if directory in _DIRECTORY_BUDGETS
        and size > _DIRECTORY_BUDGETS[directory]
    }
    assert not over, (
        "extension directories over budget: "
        + ", ".join(
            f"{name} {size:,} > {budget:,}" for name, (size, budget) in over.items()
        )
    )


def test_total_shipping_source_budget(shipping_sources: list[Path]) -> None:
    """Every shipping source file counts toward one aggregate.

    Not a curated subset: the point of the rewrite is that moving code
    between files changes nothing here.
    """
    total = sum(path.stat().st_size for path in shipping_sources)
    assert total <= _TOTAL_SOURCE_BUDGET_BYTES, (
        f"extension source totals {total:,} bytes across "
        f"{len(shipping_sources)} files — budget is "
        f"{_TOTAL_SOURCE_BUDGET_BYTES:,}. At the measured 2.93:1 source-to-gzip "
        "ratio this puts the shipped bundle near or past its own ceiling; "
        "run `plasmo build` and check the bundle assertions directly."
    )


# ── The real measurement, when a build is available ─────────────────────


def _bundle_files() -> list[Path]:
    return [path for path in _BUNDLE_ROOT.rglob("*") if path.is_file()]


@pytest.mark.skipif(
    not _BUNDLE_ROOT.is_dir(),
    reason=(
        "no production build at build/chrome-mv3-prod — run `plasmo build`. "
        "CI builds the extension, so this runs there."
    ),
)
def test_shipped_bundle_gzip_budget() -> None:
    """The gzipped bundle, which is what a user actually downloads."""
    files = _bundle_files()
    assert files, f"{_BUNDLE_ROOT} exists but is empty"
    total = sum(len(gzip.compress(path.read_bytes(), 9)) for path in files)
    assert total <= _BUNDLE_GZIP_BUDGET_BYTES, (
        f"gzipped bundle is {total:,} bytes across {len(files)} files — "
        f"budget is {_BUNDLE_GZIP_BUDGET_BYTES:,}."
    )


@pytest.mark.skipif(
    not _BUNDLE_ROOT.is_dir(),
    reason="no production build at build/chrome-mv3-prod",
)
def test_shipped_bundle_uncompressed_budget() -> None:
    """Uncompressed size bounds install footprint and worker parse time."""
    files = _bundle_files()
    assert files, f"{_BUNDLE_ROOT} exists but is empty"
    total = sum(path.stat().st_size for path in files)
    assert total <= _BUNDLE_UNCOMPRESSED_BUDGET_BYTES, (
        f"bundle is {total:,} bytes uncompressed — budget is "
        f"{_BUNDLE_UNCOMPRESSED_BUDGET_BYTES:,}."
    )
