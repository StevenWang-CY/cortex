"""P2-12: Build script must NOT hard-code the VSIX version string."""

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


def test_build_script_reads_vsix_version_from_package_json() -> None:
    """Confirm VSIX_VERSION is derived from package.json via jq."""
    text = _BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "VSIX_VERSION=$(jq -r .version" in text, (
        "Build script must set VSIX_VERSION via jq from package.json"
    )


def test_vsix_path_uses_vsix_version_variable() -> None:
    """Confirm the VSIX path references ${VSIX_VERSION}."""
    text = _BUILD_SCRIPT.read_text(encoding="utf-8")
    # Find the VSIX= assignment (possibly after VSIX_VERSION definition).
    vsix_assign = re.search(r"^VSIX=.*$", text, re.MULTILINE)
    assert vsix_assign is not None
    assert "${VSIX_VERSION}" in vsix_assign.group(0), (
        "VSIX= assignment must use ${VSIX_VERSION} variable"
    )
