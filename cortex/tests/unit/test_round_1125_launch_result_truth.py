"""``POST /api/launch/{name}`` must report the launcher's own verdict.

``ProjectLauncher.launch`` signals an unknown project by *returning*
``{"success": False, "error": ...}``; it does not raise
``FileNotFoundError``. The route mapped only exceptions to failure, so a
launch of a project that does not exist answered ``launched: true``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cortex.services.api_gateway.app import create_app, registry


class _Launcher:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.names: list[str] = []

    async def launch(self, project_name: str) -> dict[str, Any]:
        self.names.append(project_name)
        return self.result


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from cortex.libs.auth.local_token import load_or_create_token

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr("cortex.libs.auth.local_token.auth_token_path", lambda: token_file)
    token = load_or_create_token(token_file)
    registry.reset()
    try:
        with TestClient(create_app()) as test_client:
            test_client.headers.update({"Authorization": f"Bearer {token}"})
            yield test_client
    finally:
        registry.reset()


def test_unknown_project_is_not_reported_as_launched(client: TestClient) -> None:
    launcher = _Launcher({"success": False, "error": "Project 'ghost' not found"})
    registry.register("project_launcher", launcher)

    response = client.post("/api/launch/ghost")

    assert response.status_code == 200
    body = response.json()
    assert launcher.names == ["ghost"]
    assert body["launched"] is False
    assert body["errors"] == ["project_not_found"]


def test_known_project_is_still_reported_as_launched(client: TestClient) -> None:
    registry.register("project_launcher", _Launcher({"success": True, "steps": []}))

    body = client.post("/api/launch/real").json()

    assert body["launched"] is True
    assert body["errors"] == []
