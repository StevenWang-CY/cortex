"""
Launcher — Project Configuration

Defines the YAML-serializable configuration for a project launch profile.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    """Configuration for a project launch profile."""

    name: str = Field(..., description="Project name (e.g., 'OS Project 3')")
    vscode_workspace: str = Field(
        "", description="Path to VS Code workspace or directory to open"
    )
    chrome_urls: list[str] = Field(
        default_factory=list,
        description="URLs to open in Chrome (docs, Canvas, etc.)",
    )
    terminal_commands: list[str] = Field(
        default_factory=list,
        description="Terminal commands to run (e.g., 'docker compose up')",
    )
    hide_apps: list[str] = Field(
        default_factory=list,
        description="Apps to hide on launch (e.g., 'Slack', 'Discord')",
    )
    focus_goal: str = Field(
        "", description="Session focus goal to set in Cortex"
    )
    screen_layout: str = Field(
        "default", description="Screen layout preset (default, split, focus)"
    )

    def save(self, storage_dir: Path) -> Path:
        """Save project config to YAML file."""
        projects_dir = storage_dir / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        path = projects_dir / f"{self.name.lower().replace(' ', '_')}.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.model_dump(), f, default_flow_style=False)
        return path

    @classmethod
    def load(cls, path: Path) -> ProjectConfig:
        """Load project config from YAML file."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)

    @classmethod
    def list_projects(cls, storage_dir: Path) -> list[ProjectConfig]:
        """List all saved project configs."""
        projects_dir = storage_dir / "projects"
        if not projects_dir.exists():
            return []
        configs = []
        for path in sorted(projects_dir.glob("*.yaml")):
            try:
                configs.append(cls.load(path))
            except Exception:
                continue
        return configs
