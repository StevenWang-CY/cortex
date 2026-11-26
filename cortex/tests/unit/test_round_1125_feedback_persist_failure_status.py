"""A bug report the daemon failed to save must not answer HTTP 200.

``POST /api/feedback`` catches the ``OSError`` from the atomic write and
returns ``FeedbackResponse(ok=False)`` with status 200. Its only client, the
browser popup, branches on ``resp.ok`` (the HTTP status): 200 shows "saved",
clears the text box and closes the dialog; any non-404 error status shows
"Server returned N" and keeps the text. A full disk or an unwritable
``<config_dir>/feedback`` therefore discarded the user's report while
telling them it was saved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_popup_decides_saved_from_the_http_status() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "browser_extension"
        / "popup.tsx"
    ).read_text(encoding="utf-8")
    start = source.index("/api/feedback")
    window = source[start : start + 1200]
    assert "if (resp.ok) {" in window
    assert 'setBugReportStatus("saved")' in window


def test_feedback_persist_failure_is_not_reported_as_http_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setattr("cortex.libs.utils.platform.get_config_dir", lambda: tmp_path)

    def _disk_full(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("cortex.libs.utils.atomic_write.atomic_write_json", _disk_full)

    from cortex.libs.auth import load_or_create_token
    from cortex.libs.config.settings import APIConfig
    from cortex.services.api_gateway.app import create_app

    token = load_or_create_token()
    client = TestClient(create_app(config=APIConfig()))
    resp = client.post(
        "/api/feedback",
        json={"description": "the popup froze twice", "include_logs": False},
        headers={"X-Cortex-Auth-Token": token},
    )

    assert not list((tmp_path / "feedback").glob("*.json"))  # nothing was saved
    assert resp.status_code >= 500, (resp.status_code, resp.json())
