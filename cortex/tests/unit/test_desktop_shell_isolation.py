"""Run the legacy process-global Qt stub suite behind a process boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_CORTEX_ROOT = Path(__file__).resolve().parents[2]
_LEGACY_SUITE = _CORTEX_ROOT / "tests" / "unit" / "test_desktop_shell.py"


def test_legacy_desktop_shell_suite_is_process_isolated() -> None:
    """Fake PySide6 modules must never leak into the real-Qt test process."""

    environment = os.environ.copy()
    environment["CORTEX_LEGACY_QT_ISOLATED"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(_LEGACY_SUITE)],
        cwd=_CORTEX_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, (
        "isolated legacy desktop-shell suite failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
