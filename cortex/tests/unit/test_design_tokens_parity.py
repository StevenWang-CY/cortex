"""P1-18: Design-token parity CI gate tests.

Drives sync_design_tokens in-process:
  - In-sync state exits 0.
  - Mutating a token file causes --check to exit non-zero.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPT = "cortex.scripts.sync_design_tokens"


def _run_check() -> int:
    """Run sync_design_tokens.main(["--check"]) and return exit code."""
    import importlib

    mod = importlib.import_module(_SCRIPT)
    return mod.main(["--check"])


def test_check_passes_when_in_sync() -> None:
    """The committed token files must already be in sync with tokens.yaml."""
    rc = _run_check()
    assert rc == 0, (
        "Design token files are out of sync with tokens.yaml. "
        "Run: python -m cortex.scripts.sync_design_tokens --apply"
    )


def test_check_fails_when_browser_token_mutated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutating the browser design-tokens.ts should cause --check to fail."""
    import importlib

    mod = importlib.import_module(_SCRIPT)

    browser_out = mod._BROWSER_OUT
    original = browser_out.read_text(encoding="utf-8")

    # Inject a recognisable mutation.
    mutated = original + "\n// MUTATION_MARKER_FOR_TEST\n"
    browser_out.write_text(mutated, encoding="utf-8")

    try:
        rc = mod.main(["--check"])
        assert rc != 0, "Expected non-zero exit when browser token file is mutated"
    finally:
        # Always restore so the test is a no-op on the working tree.
        browser_out.write_text(original, encoding="utf-8")


def test_apply_then_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running --apply then --check should return 0."""
    import importlib

    mod = importlib.import_module(_SCRIPT)

    rc_apply = mod.main(["--apply"])
    assert rc_apply == 0

    rc_check = mod.main(["--check"])
    assert rc_check == 0
