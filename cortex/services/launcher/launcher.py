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
import subprocess
import sys
from pathlib import Path

from cortex.services.launcher.project_config import ProjectConfig

logger = logging.getLogger(__name__)


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

    def __init__(self, storage_path: str = "./storage") -> None:
        self._storage_path = Path(storage_path)
        self._is_macos = sys.platform == "darwin"

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

        # Step 3: Run terminal commands
        for cmd in config.terminal_commands:
            ok = await self._run_terminal_command(cmd)
            results["steps"].append({"action": "run_command", "command": cmd, "success": ok})

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
        """Open VS Code workspace/directory."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "code", workspace,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            return proc.returncode == 0
        except Exception:
            logger.debug("Failed to open VS Code: %s", workspace)
            return False

    async def _open_chrome_url(self, url: str) -> bool:
        """Open a URL in Chrome."""
        if not self._is_macos:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", "Google Chrome", url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
            return proc.returncode == 0
        except Exception:
            logger.debug("Failed to open URL: %s", url)
            return False

    async def _run_terminal_command(self, command: str) -> bool:
        """Run a terminal command in the background."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Don't wait for completion — some commands are long-running (docker)
            await asyncio.sleep(1.0)
            return True
        except Exception:
            logger.debug("Failed to run command: %s", command)
            return False

    async def _hide_app(self, app_name: str) -> bool:
        """Hide an application using macOS osascript."""
        if not self._is_macos:
            return False
        script = f'tell application "System Events" to set visible of process "{app_name}" to false'
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            return proc.returncode == 0
        except Exception:
            logger.debug("Failed to hide app: %s", app_name)
            return False
