"""audit Phase-I: browser-extension bundle size regression guard.

The Cortex Chrome extension ships as a Plasmo MV3 bundle. The most
recent measurement on the parent repo's ``build/chrome-mv3-prod/``:

    Uncompressed total:  ~549 KB
    Gzipped total:       ~175 KB  (target: < 250 KB)
    Largest file:        popup.100f6462.js — 169 KB uncompressed

Because the build step is not available everywhere this test runs
(plasmo + pnpm + a network install), we cannot drive ``pnpm plasmo build``
from pytest. Instead the regression guard pins per-source-file size
budgets — every TypeScript entry point that contributes to the bundle
must stay below its budget. The bundler ratio (~2:1 ratio of source to
compressed) is stable enough that source budgets correlate with bundle
budgets.

Per-file budgets are deliberately set ~20% above current sizes so
ordinary feature work does not need to update them; a single TS file
ballooning beyond the budget is the kind of regression worth flagging.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_EXTENSION_ROOT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "cortex"
    / "apps"
    / "browser_extension"
)

# Per-source-file budgets, in bytes. Each entry maps a path relative to
# the extension root to its maximum allowed size. The budget is the
# current size plus ~20% headroom so a refactor that moves a few
# functions around does not force a constant churn of budget bumps;
# a feature change that doubles a file's size will fail this test.
_SOURCE_BUDGETS: dict[str, int] = {
    # Entry points (Plasmo discovers these by filename).
    "background.ts": 200_000,
    "popup.tsx": 80_000,
    "newtab.tsx": 80_000,
    "tab-manager.ts": 60_000,
    "activity-tracker.ts": 40_000,
    "design-tokens.ts": 30_000,
    # Content scripts under contents/ ship as separate bundles per Plasmo
    # convention. leetcode-observer was moved here in audit Phase-I so
    # its 25 KB never gets pulled into the background bundle.
    "contents/ambient.ts": 40_000,
    "contents/leetcode-observer.ts": 40_000,
    # Tabs are also separate bundles.
    "tabs/onboarding.tsx": 80_000,
}

# Combined entry-point source budget. The compressed-bundle target is
# 250 KB; sources compress ~2:1 → source budget < 500 KB keeps us
# inside that envelope. Set conservatively at 600 KB for resilience.
_TOTAL_SOURCE_BUDGET_BYTES = 600_000


@pytest.fixture(scope="module")
def extension_files() -> list[Path]:
    """All TypeScript/TSX entry-point sources discovered in the extension
    package. Excludes test/mock files (audit Phase-I focuses on the
    shipped bundle)."""
    files: list[Path] = []
    for pattern in ("*.ts", "*.tsx"):
        for candidate in _EXTENSION_ROOT.glob(pattern):
            if candidate.name.endswith(".d.ts"):
                continue
            files.append(candidate)
    for sub in ("contents", "tabs"):
        for candidate in (_EXTENSION_ROOT / sub).glob("*.ts*"):
            if candidate.name.endswith(".d.ts"):
                continue
            files.append(candidate)
    return files


def test_extension_root_exists() -> None:
    """The browser extension lives where the budgets expect it to. If
    this fails the audit-Phase-I budget table is referencing a stale
    layout — fix the table, not the test."""
    assert _EXTENSION_ROOT.is_dir(), (
        f"browser extension not at {_EXTENSION_ROOT}; audit Phase-I budgets "
        "are stale"
    )


def test_no_unexpected_top_level_sources(extension_files: list[Path]) -> None:
    """Catch new top-level entry points that ship code without a budget.

    Adding a new file requires (a) deciding whether it belongs at the
    top level (entry point — Plasmo will bundle it) or under
    ``contents/`` / ``tabs/`` (separate bundle) or ``lib/`` (utility,
    not an entry point), and (b) adding an explicit budget line in
    ``_SOURCE_BUDGETS`` if it ships as an entry. The test prevents the
    common regression of dropping a 200 KB file at the top level and
    not noticing it bloated the popup bundle."""
    known = set(_SOURCE_BUDGETS.keys())
    unknown: list[str] = []
    for f in extension_files:
        rel = f.relative_to(_EXTENSION_ROOT).as_posix()
        if rel not in known:
            # Permit utility / lib files that are not entry points and
            # show up only when imported. Plasmo entry-point files are
            # all at the top level, under contents/, or under tabs/.
            top = rel.split("/", 1)[0]
            if top in {"contents", "tabs"} or "/" not in rel:
                unknown.append(rel)
    assert not unknown, (
        f"new extension entry points without a size budget: {unknown}. "
        "Either add a budget in _SOURCE_BUDGETS or move the file under "
        "lib/ if it is a non-entry utility."
    )


@pytest.mark.parametrize("relpath,budget", list(_SOURCE_BUDGETS.items()))
def test_per_file_source_budget(relpath: str, budget: int) -> None:
    """Each shipping source file stays inside its per-file budget."""
    path = _EXTENSION_ROOT / relpath
    if not path.exists():
        pytest.skip(f"{relpath} not present in this worktree")
    size = path.stat().st_size
    assert size <= budget, (
        f"{relpath} grew to {size:,} bytes — budget is {budget:,} "
        f"({size / budget:.0%}). Either refactor the file or, if the "
        "growth is justified, bump the budget with measurement notes."
    )


def test_total_entry_point_source_budget() -> None:
    """Total source across all shipping entry points stays inside the
    aggregate budget. A surprise dependency dragging multiple files
    above their individual budgets at once is the kind of regression
    this catch-all guards against."""
    total = 0
    for relpath in _SOURCE_BUDGETS:
        path = _EXTENSION_ROOT / relpath
        if path.exists():
            total += path.stat().st_size
    assert total <= _TOTAL_SOURCE_BUDGET_BYTES, (
        f"extension source totals {total:,} bytes — budget is "
        f"{_TOTAL_SOURCE_BUDGET_BYTES:,}. Compressed bundle target "
        "(250 KB) may have been blown."
    )
