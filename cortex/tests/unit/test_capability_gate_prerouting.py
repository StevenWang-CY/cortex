"""Unauthenticated requests must be refused before their body is read.

The capability token is enforced as a FastAPI route dependency, and FastAPI
runs dependencies only *after* the router has pumped and parsed the body:
``fastapi/routing.py`` does ``body_bytes = await request.body()`` and
``json_body = await request.json()`` before ``solve_dependencies``. So an
unauthenticated caller could make the daemon allocate and JSON-parse a body of
any size — and the rate limiter deliberately waives budget for unauthenticated
requests (so an anonymous page cannot starve real clients out of ``/shutdown``
or ``/api/launch``), which meant nothing bounded the repetition either.

The tell is the status code: parsing a malformed body yields 422
``json_invalid``, which can only happen if the body was read. After the fix the
same request is refused with 401 and the body is never received.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from cortex.libs.config.settings import APIConfig
from cortex.libs.logging.structured import EventType
from cortex.services.api_gateway.app import create_app


@pytest.fixture()
def anon() -> TestClient:
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


def test_malformed_body_without_a_token_is_not_parsed(anon: TestClient) -> None:
    response = anon.post(
        "/state/infer",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401, (
        "a 422 here means the body was read and JSON-parsed before auth"
    )
    assert "json_invalid" not in response.text


def test_large_body_without_a_token_is_refused(anon: TestClient) -> None:
    body = b'{"x":"' + b"a" * (3 * 1024 * 1024) + b'"}'
    response = anon.post(
        "/state/infer",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401


def test_oversized_body_is_refused_even_with_a_valid_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authenticated client must not be able to force unbounded allocation."""
    from cortex.libs.auth.local_token import load_or_create_token
    from cortex.services.api_gateway.middleware.capability_gate import (
        MAX_REQUEST_BODY_BYTES,
    )

    token_file = tmp_path / "auth.token"
    monkeypatch.setattr(
        "cortex.libs.auth.local_token.auth_token_path", lambda: token_file,
    )
    token = load_or_create_token(token_file)

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        client.headers.update({"Authorization": f"Bearer {token}"})
        body = b'{"x":"' + b"a" * (MAX_REQUEST_BODY_BYTES + 1024) + b'"}'
        response = client.post(
            "/state/infer",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413


def test_liveness_probe_stays_reachable_without_a_token(anon: TestClient) -> None:
    """The launchers poll /health before they own a token."""
    assert anon.get("/health").status_code == 200


def test_cors_preflight_is_not_gated(anon: TestClient) -> None:
    """Preflight carries no credentials by design."""
    response = anon.options(
        "/state/infer",
        headers={
            "Origin": "http://localhost",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200


def test_rejection_still_emits_the_auth_rejected_event(
    anon: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    """Moving the refusal earlier must not move the alarm signal.

    Aggregators alarm on ``AUTH_REJECTED`` from the ``auth`` logger — that is
    the hostile-localhost-scanner signal. The gate emits the same event through
    the same owner rather than logging under its own module name.
    """
    with caplog.at_level(logging.WARNING, logger="cortex.services.api_gateway.auth"):
        assert anon.post("/state/infer", json={}).status_code == 401
    assert [
        record for record in caplog.records
        if EventType.AUTH_REJECTED.value in record.getMessage()
    ], f"saw: {[r.getMessage() for r in caplog.records]}"


def test_exposed_docs_remain_reachable_when_explicitly_enabled() -> None:
    """The opt-in must actually serve them.

    Swagger UI is opened in a browser that cannot attach the capability header,
    so gating these paths would make ``expose_api_docs`` do nothing.
    """
    with TestClient(create_app(config=APIConfig(expose_api_docs=True))) as client:
        assert client.get("/openapi.json").status_code == 200
