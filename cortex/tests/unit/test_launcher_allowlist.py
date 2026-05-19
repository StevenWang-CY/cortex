"""Audit F12 — ProjectLauncher shell-injection containment.

The pre-fix launcher passed ``terminal_commands`` straight to
``asyncio.create_subprocess_shell``. A hostile project YAML could
therefore execute arbitrary shell. These tests pin the allowlist
behaviour both at the helper level
(:func:`cortex.libs.utils.shell_allowlist.validate_command`) and at the
launcher integration boundary.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cortex.libs.utils.shell_allowlist import DEFAULT_ALLOWLIST, validate_command
from cortex.services.launcher.launcher import ProjectLauncher
from cortex.services.launcher.project_config import ProjectConfig

# ---------------------------------------------------------------------------
# Helper-level cases (the six the audit prompt asks for)
# ---------------------------------------------------------------------------


class TestValidateCommand:
    def test_vscode_dot_allowed(self) -> None:
        """``vscode .`` is the canonical 'open this workspace' command."""
        argv, error = validate_command("vscode .")
        assert error is None
        assert argv == ["vscode", "."]

    def test_rm_rf_home_rejected(self) -> None:
        """The headline shell-injection attack must be refused."""
        argv, error = validate_command("rm -rf ~")
        assert argv == []
        assert error == "binary_not_in_allowlist"

    def test_code_diff_allowed(self) -> None:
        """``code --diff a b`` is a legitimate multi-arg invocation."""
        argv, error = validate_command("code --diff a b")
        assert error is None
        assert argv == ["code", "--diff", "a", "b"]

    def test_bash_dash_c_rejected(self) -> None:
        """``bash -c '...'`` would re-introduce shell semantics; refuse."""
        argv, error = validate_command("bash -c 'evil'")
        assert argv == []
        assert error == "binary_not_in_allowlist"
        # Sanity check: ``bash`` is intentionally NOT on the default
        # allowlist even though shells are common — the entire point is
        # to keep arbitrary-arg dispatch out of reach.
        assert "bash" not in DEFAULT_ALLOWLIST

    def test_extra_allowlist_via_config(self) -> None:
        """Power users extend the allowlist via LauncherConfig."""
        # ``custom_tool`` is not on the default allowlist...
        argv, error = validate_command("custom_tool --flag")
        assert error == "binary_not_in_allowlist"
        # ...but adding it via the user allowlist makes it pass.
        argv, error = validate_command(
            "custom_tool --flag",
            allowlist=["custom_tool"],
        )
        assert error is None
        assert argv == ["custom_tool", "--flag"]

    def test_error_envelope_via_launcher_response(self) -> None:
        """Rejected commands surface a typed error envelope."""
        # The launcher's response shape: a step record with
        # ``error="unsupported_command"`` and the original command
        # quoted back to the caller.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ProjectConfig(
                name="Attack",
                terminal_commands=["rm -rf ~"],
            )
            cfg.save(Path(tmp))
            launcher = ProjectLauncher(storage_path=tmp)

            import asyncio
            result = asyncio.run(launcher.launch("Attack"))

        steps = [s for s in result["steps"] if s.get("action") == "run_command"]
        assert len(steps) == 1
        step = steps[0]
        assert step["success"] is False
        assert step["error"] == "unsupported_command"
        assert step["command"] == "rm -rf ~"


# ---------------------------------------------------------------------------
# Edge-case coverage
# ---------------------------------------------------------------------------


class TestValidateCommandEdges:
    def test_empty_command_rejected(self) -> None:
        argv, error = validate_command("")
        assert argv == []
        assert error == "empty_command"

    def test_whitespace_only_rejected(self) -> None:
        argv, error = validate_command("   ")
        assert argv == []
        assert error == "empty_command"

    def test_unbalanced_quotes_rejected(self) -> None:
        argv, error = validate_command("code --diff 'a b")
        assert argv == []
        assert error is not None
        assert error.startswith("unparseable_command")

    def test_path_prefixed_binary_allowed(self) -> None:
        """Tools resolved via $PATH still match on basename."""
        argv, error = validate_command("/usr/local/bin/code .")
        assert error is None
        assert argv == ["/usr/local/bin/code", "."]


# ---------------------------------------------------------------------------
# Integration: launcher with extra allowlist
# ---------------------------------------------------------------------------


class TestLauncherWithUserAllowlist:
    @pytest.mark.asyncio
    async def test_user_allowlist_threading(self) -> None:
        """``user_command_allowlist`` reaches the validator."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ProjectConfig(
                name="Custom",
                # The command itself is harmless — we only assert the
                # rejection envelope changes shape based on whether the
                # binary is on the user allowlist. We don't actually want
                # to spawn ``custom_tool`` in CI, so use a binary that
                # will fail at exec time but pass the allowlist gate.
                terminal_commands=["definitely-not-installed-tool --foo"],
            )
            cfg.save(Path(tmp))

            # Without the extra allowlist the rejection is at the gate.
            launcher = ProjectLauncher(storage_path=tmp)
            result = await launcher.launch("Custom")
            step = [
                s for s in result["steps"] if s.get("action") == "run_command"
            ][0]
            assert step["error"] == "unsupported_command"

            # With the extra allowlist the gate lets the command through;
            # we then expect ``spawn_failed`` because the binary is not
            # on disk in the test environment.
            launcher = ProjectLauncher(
                storage_path=tmp,
                user_command_allowlist=["definitely-not-installed-tool"],
            )
            result = await launcher.launch("Custom")
            step = [
                s for s in result["steps"] if s.get("action") == "run_command"
            ][0]
            assert step["success"] is False
            assert step["error"] == "spawn_failed"
