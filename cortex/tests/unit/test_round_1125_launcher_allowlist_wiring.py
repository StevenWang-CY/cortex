"""``launcher.user_command_allowlist`` must reach the daemon's launcher.

The configuration reference documents
``CORTEX_LAUNCHER__USER_COMMAND_ALLOWLIST`` as "extra binary basenames the
project launcher will accept", and ``ProjectLauncher`` accepts the list, but
the daemon builds its launcher with ``ProjectLauncher(storage_path=...)``
only. Every user-added binary was therefore still rejected as
``unsupported_command``.

The binary below does not exist, so an accepted command fails at spawn
(``spawn_failed``) without running anything; a rejected one never reaches
spawn (``unsupported_command``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

_TOOL = "cortex-round-1125-not-installed-tool"


@pytest.fixture()
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    monkeypatch.setenv("CORTEX_LAUNCHER__USER_COMMAND_ALLOWLIST", f'["{_TOOL}"]')
    from cortex.libs.config import settings as settings_mod

    settings_mod.get_config.cache_clear()
    yield
    settings_mod.get_config.cache_clear()


def test_configured_allowlist_reaches_the_daemon_project_launcher(configured: None) -> None:
    from cortex.libs.config import settings as settings_mod
    from cortex.services.runtime_daemon import CortexDaemon

    config = settings_mod.get_config()
    assert config.launcher.user_command_allowlist == [_TOOL]
    daemon = CortexDaemon(config=config)

    step = asyncio.run(daemon._project_launcher._run_terminal_command(f"{_TOOL} --version"))

    assert step.get("error") != "unsupported_command", step
