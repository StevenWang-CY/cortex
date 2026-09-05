"""D8 — ``hide_apps`` names can never become AppleScript.

``ProjectLauncher._hide_app`` interpolated the project-YAML app name into
``tell application "System Events" to set visible of process "<name>" to
false``; a quote broke out into arbitrary AppleScript (``do shell script``
included). The name is now validated against a plain-application-name
allowlist and handed to ``osascript`` as an ``argv`` parameter of a fixed
script.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cortex.services.launcher.launcher import (
    _HIDE_APP_SCRIPT,
    HIDE_APP_NAME_RE,
    ProjectLauncher,
)


class _Proc:
    pid = 4242
    returncode: int | None = 0

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:  # pragma: no cover - not reached
        return None


@pytest.fixture()
def launcher(tmp_path: Path) -> ProjectLauncher:
    instance = ProjectLauncher(storage_path=str(tmp_path))
    instance._is_macos = True  # noqa: SLF001 — exercise the osascript path everywhere
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile",
    [
        'Slack"; do shell script "id',
        "Slack' & (do shell script \"id\") & '",
        "Slack\nrm -rf ~",
        "-Slack",
        "",
        "x" * 65,
        "Slack; rm -rf ~",
        "Slack\\",
    ],
)
async def test_hostile_names_never_reach_osascript(
    launcher: ProjectLauncher, monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    spawned: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **_kwargs: object) -> _Proc:
        spawned.append(argv)
        raise AssertionError("osascript must not be spawned for a rejected name")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await launcher._hide_app(hostile) is False  # noqa: SLF001
    assert spawned == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["Slack", "Google Chrome", "1Password 7", "Visual Studio Code", "Foo.Bar_Baz+Qux-1"],
)
async def test_plain_names_are_passed_as_argv_not_script_text(
    launcher: ProjectLauncher, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    spawned: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **_kwargs: object) -> _Proc:
        spawned.append(argv)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await launcher._hide_app(name) is True  # noqa: SLF001
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[:2] == ("osascript", "-e")
    assert argv[2] == _HIDE_APP_SCRIPT
    assert name not in argv[2], "the name must never be part of the script source"
    assert argv[3] == name
    assert "item 1 of argv" in _HIDE_APP_SCRIPT


@pytest.mark.asyncio
async def test_launch_reports_rejected_hide_app_as_failed_step(
    launcher: ProjectLauncher, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cortex.services.launcher.project_config import ProjectConfig

    ProjectConfig(name="Injected", hide_apps=['Slack"; do shell script "id']).save(tmp_path)
    spawned: list[tuple[str, ...]] = []

    async def fake_exec(*argv: str, **_kwargs: object) -> _Proc:
        spawned.append(argv)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await launcher.launch("Injected")
    hide_steps = [step for step in result["steps"] if step["action"] == "hide_app"]
    assert hide_steps == [
        {"action": "hide_app", "app": 'Slack"; do shell script "id', "success": False}
    ]
    assert spawned == []


def test_allowlist_regex_shape() -> None:
    assert HIDE_APP_NAME_RE.fullmatch("Slack")
    assert HIDE_APP_NAME_RE.fullmatch("A" + "b" * 63)
    assert not HIDE_APP_NAME_RE.fullmatch("A" + "b" * 64)
    assert not HIDE_APP_NAME_RE.fullmatch('"')
    assert not HIDE_APP_NAME_RE.fullmatch(" Slack")
