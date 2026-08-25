"""Architectural seams that keep WP10 decomposition from regressing."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "cortex/services/runtime_daemon.py"
BACKGROUND = ROOT / "cortex/apps/browser_extension/background.ts"
POPUP = ROOT / "cortex/apps/browser_extension/popup.tsx"
DASHBOARD = ROOT / "cortex/apps/desktop_shell/dashboard.py"


def test_runtime_uses_kernel_and_has_no_global_registry_or_bare_tasks() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("cortex.application.kernel", "ApplicationKernel") in imported
    assert ("cortex.services.api_gateway.app", "registry") not in imported
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "create_task"
        for node in ast.walk(tree)
    )


def test_browser_worker_imports_every_bounded_coordinator() -> None:
    source = BACKGROUND.read_text(encoding="utf-8")
    for module in (
        "./lib/daemon-connection",
        "./lib/persisted-session",
        "./lib/context-collector",
        "./lib/intervention-presentation",
        "./lib/capability-executor",
        "./lib/focus-session",
        "./lib/browser-telemetry",
    ):
        assert f'from "{module}"' in source
    assert "async function collectTabs(" not in source
    assert "function extractPageText(" not in source
    assert "interface TabData" not in source


def test_views_depend_on_presentation_models() -> None:
    assert './lib/popup-view-model"' in POPUP.read_text(encoding="utf-8")
    dashboard_tree = ast.parse(DASHBOARD.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "cortex.apps.desktop_shell.view_models"
        for node in ast.walk(dashboard_tree)
    )
    assert "DesktopMessageRouter" in (
        ROOT / "cortex/apps/desktop_shell/main.py"
    ).read_text(encoding="utf-8")
    assert "DesktopMessageRouter" in (
        ROOT / "cortex/apps/desktop_shell/controller.py"
    ).read_text(encoding="utf-8")
