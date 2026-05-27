"""P1-15: Repo hygiene — lock file tracking.

Asserts that neither browser_extension nor vscode_extension
package-lock.json files are tracked by git, and that both are
listed in .gitignore.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _git_ls_files(*paths: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", *paths],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _gitignore_contents() -> str:
    return (_ROOT / ".gitignore").read_text(encoding="utf-8")


class TestPackageLockNotTracked:
    def test_browser_extension_lock_not_tracked(self) -> None:
        tracked = _git_ls_files("cortex/apps/browser_extension/package-lock.json")
        assert tracked == [], (
            "cortex/apps/browser_extension/package-lock.json must NOT be tracked by git; "
            f"found: {tracked}"
        )

    def test_vscode_extension_lock_not_tracked(self) -> None:
        tracked = _git_ls_files("cortex/apps/vscode_extension/package-lock.json")
        assert tracked == [], (
            "cortex/apps/vscode_extension/package-lock.json must NOT be tracked by git; "
            f"found: {tracked}"
        )


class TestPackageLockInGitignore:
    def test_browser_extension_lock_in_gitignore(self) -> None:
        contents = _gitignore_contents()
        assert "cortex/apps/browser_extension/package-lock.json" in contents, (
            "cortex/apps/browser_extension/package-lock.json must appear in .gitignore"
        )

    def test_vscode_extension_lock_in_gitignore(self) -> None:
        contents = _gitignore_contents()
        assert "cortex/apps/vscode_extension/package-lock.json" in contents, (
            "cortex/apps/vscode_extension/package-lock.json must appear in .gitignore"
        )
