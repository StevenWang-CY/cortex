"""Tests for ProjectConfig serialization and ProjectLauncher."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.services.launcher.launcher import ProjectLauncher
from cortex.services.launcher.project_config import ProjectConfig


class TestProjectConfigFields:
    """ProjectConfig should expose all expected fields with correct defaults."""

    def test_required_name_field(self):
        config = ProjectConfig(name="My Project")
        assert config.name == "My Project"

    def test_default_field_values(self):
        config = ProjectConfig(name="Test")
        assert config.vscode_workspace == ""
        assert config.chrome_urls == []
        assert config.terminal_commands == []
        assert config.hide_apps == []
        assert config.focus_goal == ""
        assert config.screen_layout == "default"

    def test_all_fields_populated(self):
        config = ProjectConfig(
            name="OS Project 3",
            vscode_workspace="/home/user/os3",
            chrome_urls=["https://canvas.com/os3", "https://docs.kernel.org"],
            terminal_commands=["docker compose up", "make build"],
            hide_apps=["Slack", "Discord"],
            focus_goal="Finish syscall implementation",
            screen_layout="split",
        )
        assert config.name == "OS Project 3"
        assert config.vscode_workspace == "/home/user/os3"
        assert len(config.chrome_urls) == 2
        assert len(config.terminal_commands) == 2
        assert "Slack" in config.hide_apps
        assert config.focus_goal == "Finish syscall implementation"
        assert config.screen_layout == "split"


class TestProjectConfigSerialization:
    """ProjectConfig should round-trip through YAML save/load."""

    def test_save_creates_yaml_file(self, tmp_path: Path):
        config = ProjectConfig(name="Test Project", vscode_workspace="/tmp/test")
        path = config.save(tmp_path)
        assert path.exists()
        assert path.suffix == ".yaml"
        assert "test_project" in path.name

    def test_round_trip_yaml(self, tmp_path: Path):
        original = ProjectConfig(
            name="OS Project 3",
            vscode_workspace="/home/user/os3",
            chrome_urls=["https://example.com"],
            terminal_commands=["make build"],
            hide_apps=["Slack"],
            focus_goal="Focus on the kernel",
            screen_layout="split",
        )
        path = original.save(tmp_path)
        loaded = ProjectConfig.load(path)

        assert loaded.name == original.name
        assert loaded.vscode_workspace == original.vscode_workspace
        assert loaded.chrome_urls == original.chrome_urls
        assert loaded.terminal_commands == original.terminal_commands
        assert loaded.hide_apps == original.hide_apps
        assert loaded.focus_goal == original.focus_goal
        assert loaded.screen_layout == original.screen_layout

    def test_list_projects_empty_dir(self, tmp_path: Path):
        configs = ProjectConfig.list_projects(tmp_path)
        assert configs == []

    def test_list_projects_returns_saved_configs(self, tmp_path: Path):
        ProjectConfig(name="Alpha").save(tmp_path)
        ProjectConfig(name="Beta").save(tmp_path)
        ProjectConfig(name="Gamma").save(tmp_path)

        configs = ProjectConfig.list_projects(tmp_path)
        assert len(configs) == 3
        names = {c.name for c in configs}
        assert names == {"Alpha", "Beta", "Gamma"}


class TestProjectLauncher:
    """ProjectLauncher.list_projects should return a list of dicts."""

    def test_list_projects_returns_list(self, tmp_path: Path):
        # Save some configs
        ProjectConfig(name="P1", vscode_workspace="/p1").save(tmp_path)
        ProjectConfig(name="P2", chrome_urls=["https://p2.com"]).save(tmp_path)

        launcher = ProjectLauncher(storage_path=str(tmp_path))
        projects = launcher.list_projects()

        assert isinstance(projects, list)
        assert len(projects) == 2
        # Each item is a dict (from model_dump)
        assert all(isinstance(p, dict) for p in projects)
        names = {p["name"] for p in projects}
        assert names == {"P1", "P2"}

    def test_list_projects_empty(self, tmp_path: Path):
        launcher = ProjectLauncher(storage_path=str(tmp_path))
        projects = launcher.list_projects()
        assert isinstance(projects, list)
        assert len(projects) == 0
