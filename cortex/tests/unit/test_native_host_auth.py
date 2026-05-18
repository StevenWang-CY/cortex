"""Audit F07b — native host returns the daemon's auth token.

The browser extension cannot read the mode-0600 token file directly.
F08 added a ``get_auth_token`` command to the native messaging host
that loads (or creates) the token and returns it to the extension.

This test invokes the response builder directly — the stdin/stdout
binary protocol is exercised by the broader integration suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.libs.auth.local_token import load_or_create_token
from cortex.scripts import native_host


def test_get_auth_token_returns_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "auth.token"
    expected = load_or_create_token(token_file)
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )

    response = native_host._get_auth_token_response()

    assert response["status"] == "ok"
    assert response["token"] == expected


def test_get_auth_token_provisions_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "auth.token"
    assert not token_file.exists()
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file
    )

    response = native_host._get_auth_token_response()

    assert response["status"] == "ok"
    assert len(response["token"]) >= 32
    assert token_file.exists()
