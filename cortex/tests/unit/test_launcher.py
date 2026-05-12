"""Tests for ProjectLauncher and ProjectConfig."""
import tempfile
from pathlib import Path

import pytest

from cortex.services.launcher.launcher import ProjectLauncher
from cortex.services.launcher.project_config import ProjectConfig


class TestProjectConfig:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(
                name="Test Project",
                vscode_workspace="/path/to/workspace",
                chrome_urls=["https://example.com"],
                terminal_commands=["echo hello"],
                hide_apps=["Slack"],
                focus_goal="Build feature X",
            )
            path = config.save(Path(tmp))
            assert path.exists()
            loaded = ProjectConfig.load(path)
            assert loaded.name == "Test Project"
            assert loaded.vscode_workspace == "/path/to/workspace"
            assert len(loaded.chrome_urls) == 1
            assert loaded.focus_goal == "Build feature X"

    def test_list_projects_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            configs = ProjectConfig.list_projects(Path(tmp))
            assert configs == []

    def test_list_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            c1 = ProjectConfig(name="Project A", focus_goal="Goal A")
            c2 = ProjectConfig(name="Project B", focus_goal="Goal B")
            c1.save(Path(tmp))
            c2.save(Path(tmp))
            configs = ProjectConfig.list_projects(Path(tmp))
            assert len(configs) == 2

    def test_filename_sanitization(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = ProjectConfig(name="OS Project 3")
            path = config.save(Path(tmp))
            assert "os_project_3" in path.name


class TestProjectLauncher:
    def test_list_projects_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = ProjectLauncher(storage_path=tmp)
            projects = launcher.list_projects()
            assert projects == []

    @pytest.mark.asyncio
    async def test_launch_nonexistent_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = ProjectLauncher(storage_path=tmp)
            result = await launcher.launch("nonexistent")
            assert result["success"] is False
            assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_save_current_as_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = ProjectLauncher(storage_path=tmp)
            config = await launcher.save_current_as_project(
                name="My Project", focus_goal="Build stuff",
            )
            assert config.name == "My Project"
            # Should now be listable
            projects = launcher.list_projects()
            assert len(projects) == 1
