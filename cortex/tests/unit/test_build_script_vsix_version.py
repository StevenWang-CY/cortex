"""Build artifacts must use the canonical pyproject version."""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BUILD_SCRIPT = _ROOT / "cortex" / "scripts" / "build_macos_app.sh"
_PKG_JSON = _ROOT / "cortex" / "apps" / "vscode_extension" / "package.json"


def _read_version() -> str:
    return json.loads(_PKG_JSON.read_text(encoding="utf-8"))["version"]


def test_build_script_does_not_hardcode_vsix_version() -> None:
    """The VSIX path must not contain a hard-coded version string like 0.2.1."""
    text = _BUILD_SCRIPT.read_text(encoding="utf-8")
    version = _read_version()

    # Look specifically for the VSIX= assignment line; it should use a variable.
    vsix_assign = re.search(r"^VSIX=.*$", text, re.MULTILINE)
    assert vsix_assign is not None, "VSIX= assignment line not found in build script"

    line = vsix_assign.group(0)
    # The line must NOT contain the literal version string.
    assert version not in line, (
        f"Build script VSIX path hard-codes version '{version}'. "
        "Use VSIX_VERSION=$(jq -r .version .../package.json) instead."
    )


def test_build_script_reads_canonical_project_version() -> None:
    """The locked build interpreter must read the drift-checked source."""
    text = _BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'PYTHON_BIN="${CORTEX_DIR}/.venv/bin/python"' in text
    assert '"${PYTHON_BIN}" -m cortex.scripts.sync_versions --check' in text
    assert (
        'CORTEX_VERSION=$("${PYTHON_BIN}" -m '
        "cortex.scripts.sync_versions --print)" in text
    )


def test_vsix_and_dmg_paths_use_canonical_version_variable() -> None:
    """Both shipped artifact names expose the same canonical version."""
    text = _BUILD_SCRIPT.read_text(encoding="utf-8")
    vsix_assign = re.search(r"^VSIX=.*$", text, re.MULTILINE)
    assert vsix_assign is not None
    assert "${CORTEX_VERSION}" in vsix_assign.group(0)
    dmg_assign = re.search(r"^DMG_PATH=.*$", text, re.MULTILINE)
    assert dmg_assign is not None
    assert "Cortex-${CORTEX_VERSION}-macos-${ARTIFACT_ARCH}.dmg" in dmg_assign.group(0)
