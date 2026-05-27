"""P2-11: CORTEX_STORAGE__BASE_DIR must not appear in the build allowlist."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BUILD_SCRIPT = _ROOT / "cortex" / "scripts" / "build_macos_app.sh"


def _read_allowed_keys() -> list[str]:
    """Extract keys from the ALLOWED_KEYS= regex line in build_macos_app.sh."""
    text = _BUILD_SCRIPT.read_text(encoding="utf-8")
    # Match the ALLOWED_KEYS assignment (may span one line).
    # The value looks like: '^(KEY1|KEY2|...)='
    match = re.search(r"ALLOWED_KEYS='\^\(([^)]+)\)", text)
    if not match:
        return []
    return [k.strip() for k in match.group(1).split("|")]


def test_storage_base_dir_not_in_allowlist() -> None:
    keys = _read_allowed_keys()
    assert "CORTEX_STORAGE__BASE_DIR" not in keys, (
        "CORTEX_STORAGE__BASE_DIR must NOT be in the build ALLOWED_KEYS — "
        "the real key is CORTEX_STORAGE__PATH and bundled apps should not "
        "pin to a developer's local path."
    )


def test_no_storage_path_keys_in_allowlist() -> None:
    """No CORTEX_STORAGE__ keys should be in the allowlist at all."""
    keys = _read_allowed_keys()
    storage_keys = [k for k in keys if k.startswith("CORTEX_STORAGE__")]
    assert storage_keys == [], (
        f"CORTEX_STORAGE__ keys should not be in the build allowlist: {storage_keys}"
    )
