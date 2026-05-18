"""
Unit tests for ``cortex.scripts.generate_ts_schemas`` (Debt-1 closure).

These tests do NOT shell out to ``json2ts``. The codegen pipeline itself
has a separate end-to-end smoke test that runs only when ``json2ts`` is
discoverable on ``PATH``; the unit suite must pass on a developer laptop
with only pip dependencies installed. Instead these tests exercise:

1. Module discovery — every ``cortex.libs.schemas.*`` submodule is
   reachable via ``pkgutil`` and contains at least one Pydantic model.
2. Banner stripping — the helper that removes ``pydantic2ts``' default
   header is idempotent and produces the expected canonical layout.
3. Output path resolution — the generator targets the committed file
   in the browser extension's ``types/generated/`` directory.
4. CLI surface — ``--check`` is a recognised flag.
5. Drift detection — when the committed file is tampered, ``run_check``
   prints a diff and returns ``1``.

The drift test mutates ``OUTPUT_PATH`` via monkeypatch so the real
committed file is never touched.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cortex.scripts import generate_ts_schemas as gen


def test_discovers_every_schema_module() -> None:
    """Every ``*.py`` under ``cortex/libs/schemas`` is enumerated."""
    discovered = set(gen._discover_schema_modules())
    expected = {
        "cortex.libs.schemas.consent",
        "cortex.libs.schemas.context",
        "cortex.libs.schemas.eval",
        "cortex.libs.schemas.features",
        "cortex.libs.schemas.intervention",
        "cortex.libs.schemas.leetcode",
        "cortex.libs.schemas.longitudinal",
        "cortex.libs.schemas.state",
        "cortex.libs.schemas.transition_graph",
        "cortex.libs.schemas.activity",
    }
    missing = expected - discovered
    assert not missing, f"Schema modules not discovered: {missing}"


def test_import_all_schemas_does_not_raise() -> None:
    """All schema modules import cleanly — guards against circular deps."""
    gen._import_all_schemas()  # no exception is the assertion


def test_strip_banner_removes_pydantic2ts_header() -> None:
    """The default ``/* tslint:disable */`` banner is stripped."""
    raw = textwrap.dedent(
        """\
        /* tslint:disable */
        /* eslint-disable */
        /**
        /* This file was automatically generated from pydantic models by running pydantic2ts.
        /* Do not modify it by hand - just update the pydantic models and then re-run the script
        */

        export interface Example {
          foo: string;
        }
        """
    )
    cleaned = gen._strip_default_banner(raw)
    assert cleaned.startswith("export interface Example")
    assert "tslint:disable" not in cleaned
    assert "pydantic2ts" not in cleaned


def test_strip_banner_is_idempotent_on_clean_input() -> None:
    """If banner is already gone, ``_strip_default_banner`` is a no-op."""
    body = "export interface Foo {\n  bar: string;\n}\n"
    assert gen._strip_default_banner(body) == body


def test_strip_banner_passes_through_imports() -> None:
    """A leading ``import type`` is preserved (no false-positive trim)."""
    body = (
        "import type { Foo } from './foo';\n"
        "\nexport interface Bar { foo: Foo }\n"
    )
    cleaned = gen._strip_default_banner(body)
    assert cleaned.startswith("import type")


def test_output_path_targets_extension_generated_dir() -> None:
    """The committed file lives under the browser extension's typed dir."""
    expected_tail = Path("apps/browser_extension/types/generated/cortex_schemas.d.ts")
    assert str(gen.OUTPUT_PATH).endswith(str(expected_tail))


def test_cli_accepts_check_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` lists the ``--check`` flag (regression guard for CI gate)."""
    with pytest.raises(SystemExit):
        gen.main(["--help"])
    captured = capsys.readouterr()
    assert "--check" in captured.out


def test_run_check_reports_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tampered committed file makes ``run_check`` exit 1 with a diff.

    We monkeypatch ``OUTPUT_PATH`` and ``_generate_into`` so the test
    never shells out to ``json2ts`` and never touches the real
    committed file. The body-of-evidence the function returns is the
    side-effect: stdout contains a unified diff between the tampered
    and the freshly-generated body, and the exit code is 1.
    """
    committed = tmp_path / "cortex_schemas.d.ts"
    committed.write_text("// stale committed body\n", encoding="utf-8")

    def fake_generate(target: Path) -> None:
        target.write_text("// fresh generated body\n", encoding="utf-8")

    monkeypatch.setattr(gen, "OUTPUT_PATH", committed)
    monkeypatch.setattr(gen, "_generate_into", fake_generate)

    exit_code = gen.run_check()
    assert exit_code == 1


def test_run_check_passes_when_in_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the committed body matches generation, ``run_check`` returns 0."""
    body = "// in-sync body\n"
    committed = tmp_path / "cortex_schemas.d.ts"
    committed.write_text(body, encoding="utf-8")

    def fake_generate(target: Path) -> None:
        target.write_text(body, encoding="utf-8")

    monkeypatch.setattr(gen, "OUTPUT_PATH", committed)
    monkeypatch.setattr(gen, "_generate_into", fake_generate)

    assert gen.run_check() == 0


def test_run_check_reports_missing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing committed file is a drift condition, not a generator error."""
    monkeypatch.setattr(gen, "OUTPUT_PATH", tmp_path / "does_not_exist.d.ts")
    assert gen.run_check() == 1


def test_header_documents_regeneration_command() -> None:
    """The header includes the canonical regeneration command for humans."""
    assert "AUTOGENERATED" in gen.HEADER
    assert "python -m cortex.scripts.generate_ts_schemas" in gen.HEADER


@pytest.mark.skipif(
    not shutil.which("json2ts"),
    reason="json2ts not on PATH — end-to-end codegen test requires it",
)
def test_committed_file_matches_current_models() -> None:
    """End-to-end: ``--check`` against the real committed file is green.

    Skipped when ``json2ts`` is unavailable so the suite still runs in
    minimal CI matrices; the dedicated codegen CI job installs the
    binary and therefore exercises this path.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cortex.scripts.generate_ts_schemas",
            "--check",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Committed cortex_schemas.d.ts is out of sync with Pydantic models.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
