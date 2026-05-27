"""I13: architectural boundary test — libs/ ⊥ services/ ⊥ apps/.

Cortex enforces a layered architecture:

* ``cortex.libs``    — pure utilities, schemas, ports. No service or
  app dependencies. Imports allowed: stdlib, third-party,
  ``cortex.libs.*``.
* ``cortex.services`` — daemon-side runtime engines (capture, LLM,
  intervention, telemetry, …). May depend on ``cortex.libs`` and other
  ``cortex.services``. MUST NOT import ``cortex.apps`` (desktop shell
  / browser extension / VS Code extension).
* ``cortex.apps``    — surfaces (desktop shell, extensions). May
  depend on either, but the reverse direction is forbidden.

The boundary used to be enforced ad-hoc (``rg`` greps in code review);
when ``cortex/libs/ports/intervention_port.py`` started importing
``cortex.services.intervention_engine.planner`` (audit finding I1) the
violation went undetected for weeks. This test makes the contract
machine-checked:

* AST-walks every ``.py`` file under the offending tree (not just
  textual grep) so commented-out imports / strings don't trip a false
  positive.
* Reports EVERY violation in a single AssertionError so cleanup PRs
  see the full scope at once.
* Skips the test tree itself (tests may legitimately reach across).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# Resolve from this test file: cortex/tests/unit/<this> → cortex/ root
_CORTEX_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _walk_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return every .py under ``root`` excluding caches and bundled
    third-party trees (.venv, __pycache__, .pytest_cache)."""
    out: list[pathlib.Path] = []
    for path in root.rglob("*.py"):
        # Skip caches and bundled .venv copies.
        parts = set(path.parts)
        if "__pycache__" in parts or ".venv" in parts or ".pytest_cache" in parts:
            continue
        # Skip auto-generated PyInstaller / Plasmo trees.
        if "build" in parts or ".plasmo" in parts:
            continue
        out.append(path)
    return out


def _imports(path: pathlib.Path) -> list[str]:
    """Return every dotted import name referenced by ``path``.

    Uses ``ast`` so commented-out imports, docstring mentions, and
    f-strings do not produce false positives. Yields the *fully
    qualified* module name (``cortex.services.foo`` for ``from
    cortex.services.foo import bar``).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        # Files that don't parse cleanly are surfaced by ruff/pytest
        # elsewhere; the boundary check is content-blind so we skip.
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative import — never crosses the libs/services
                # boundary (relative imports stay within the package).
                continue
            if node.module:
                names.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
    return names


def _violations(
    *,
    tree_root: pathlib.Path,
    forbidden_prefixes: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Return ``[(file, forbidden_import), …]`` for every offending
    import found by walking ``tree_root``."""
    out: list[tuple[str, str]] = []
    for path in _walk_python_files(tree_root):
        for imp in _imports(path):
            for prefix in forbidden_prefixes:
                if imp == prefix or imp.startswith(prefix + "."):
                    out.append((str(path.relative_to(_CORTEX_ROOT.parent)), imp))
                    break
    return out


def test_libs_does_not_import_services_or_apps() -> None:
    """``cortex.libs`` must not import ``cortex.services`` or
    ``cortex.apps``. Violations are listed in a single AssertionError
    so the reader sees the full cleanup scope at once."""
    libs_root = _CORTEX_ROOT / "libs"
    assert libs_root.is_dir(), f"libs root missing at {libs_root}"
    found = _violations(
        tree_root=libs_root,
        forbidden_prefixes=("cortex.services", "cortex.apps"),
    )
    if found:
        formatted = "\n".join(f"  {path} → imports {imp}" for path, imp in found)
        pytest.fail(
            "cortex.libs must not depend on cortex.services or cortex.apps. "
            f"Found {len(found)} violation(s):\n{formatted}\n\n"
            "Fix by moving the shared type into cortex/libs/schemas/ or "
            "cortex/libs/ports/ and re-exporting from the services side."
        )


def test_services_does_not_import_apps() -> None:
    """``cortex.services`` is the daemon — it must not import any
    desktop-shell / extension code under ``cortex.apps``."""
    services_root = _CORTEX_ROOT / "services"
    assert services_root.is_dir(), f"services root missing at {services_root}"
    found = _violations(
        tree_root=services_root,
        forbidden_prefixes=("cortex.apps",),
    )
    if found:
        formatted = "\n".join(f"  {path} → imports {imp}" for path, imp in found)
        pytest.fail(
            "cortex.services must not depend on cortex.apps. "
            f"Found {len(found)} violation(s):\n{formatted}"
        )
