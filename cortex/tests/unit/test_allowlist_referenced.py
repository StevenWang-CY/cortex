"""P2-13: Every key in the build ALLOWED_KEYS must be referenced in Python source."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BUILD_SCRIPT = _ROOT / "cortex" / "scripts" / "build_macos_app.sh"
_CORTEX_SRC = _ROOT / "cortex"


def _read_allowed_keys() -> list[str]:
    """Extract keys from the ALLOWED_KEYS= regex in build_macos_app.sh."""
    text = _BUILD_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"ALLOWED_KEYS='\^\(([^)]+)\)", text)
    if not match:
        return []
    return [k.strip() for k in match.group(1).split("|")]


def _all_python_source() -> str:
    parts = []
    for py_file in _CORTEX_SRC.rglob("*.py"):
        try:
            parts.append(py_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)


def test_each_allowed_key_referenced_in_python() -> None:
    """No dead keys in the build allowlist."""
    keys = _read_allowed_keys()
    assert keys, "No ALLOWED_KEYS found in build script — check the regex"

    source = _all_python_source()
    dead_keys = []
    for key in keys:
        if key not in source:
            dead_keys.append(key)

    assert dead_keys == [], (
        f"The following ALLOWED_KEYS have no references in cortex/ Python source "
        f"and are candidates for removal: {dead_keys}"
    )
