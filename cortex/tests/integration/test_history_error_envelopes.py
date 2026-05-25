"""Contract tests for the P0 §3.1/§3.2/§3.3 error-envelope guarantee.

When the daemon's session-list / session-detail / trends callback
raises, the WS dispatch arm MUST send back a structured error envelope
(``{"error": "internal", ...}``) rather than silently dropping the
reply. Without that, the requesting UI hangs on a pending promise.

These tests pin that contract at the schema layer: every error envelope
the WS dispatch arms emit must round-trip cleanly through the Pydantic
model, with the new ``error`` field carrying one of the documented
literal codes.

Adjacent guarantees this file also pins:

* ``SessionListResponse.cursor_session_id`` round-trips so tied-timestamp
  pagination keeps a stable tie-break key on the wire (P0 §3.1 fix #10).
* ``SessionDetailResponse.error`` is the narrowed Literal vocabulary
  (``"not_found" | "unreadable" | "invalid_id" | "internal" | "daemon_unavailable"``);
  unknown values are rejected at validation time.
* ``TrendsResponse.window`` is narrowed to ``Literal["week", "month"]``
  (the legacy ``"quarter"`` value was dropped per Phase 4.A — the UI never
  rendered it correctly).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.longitudinal import ChronotypeModel
from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    TrendsRequest,
    TrendsResponse,
)


# ─── SessionListResponse.error envelope ─────────────────────────────


def test_session_list_response_error_field_round_trips() -> None:
    """An error envelope from the daemon round-trips with the new field."""
    envelope = SessionListResponse(
        items=[],
        next_cursor=None,
        cursor_session_id=None,
        total_known=0,
        error="internal",
    )
    payload = envelope.model_dump(mode="json")
    assert payload["error"] == "internal"
    restored = SessionListResponse.model_validate(payload)
    assert restored.error == "internal"
    assert restored.items == []
    assert restored.next_cursor is None
    assert restored.total_known == 0


def test_session_list_response_success_omits_error() -> None:
    """A happy-path reply leaves ``error`` as None (additive field)."""
    envelope = SessionListResponse(
        items=[],
        next_cursor=12345.6,
        cursor_session_id="sid_abc",
        total_known=3,
    )
    payload = envelope.model_dump(mode="json")
    assert payload["error"] is None


def test_session_list_response_cursor_session_id_round_trips() -> None:
    """The tied-timestamp tie-break key round-trips on the wire."""
    envelope = SessionListResponse(
        items=[],
        next_cursor=1_700_000_000.0,
        cursor_session_id="sid_xyz_0123",
        total_known=1,
    )
    payload = envelope.model_dump(mode="json")
    assert payload["cursor_session_id"] == "sid_xyz_0123"
    restored = SessionListResponse.model_validate(payload)
    assert restored.cursor_session_id == "sid_xyz_0123"


# ─── SessionDetailResponse.error narrowed Literal ───────────────────


@pytest.mark.parametrize(
    "code",
    ["not_found", "unreadable", "invalid_id", "internal", "daemon_unavailable"],
)
def test_session_detail_response_accepts_every_documented_error_code(code: str) -> None:
    """The full Literal vocabulary is accepted at construction time."""
    envelope = SessionDetailResponse(report=None, error=code)
    payload = envelope.model_dump(mode="json")
    assert payload["error"] == code
    restored = SessionDetailResponse.model_validate(payload)
    assert restored.error == code
    assert restored.report is None


def test_session_detail_response_rejects_unknown_error_code() -> None:
    """An undocumented error code fails validation (catches typos)."""
    with pytest.raises(ValidationError):
        SessionDetailResponse(report=None, error="banana")  # type: ignore[arg-type]


def test_session_detail_response_success_omits_error() -> None:
    """A populated ``report`` carries ``error=None`` on the wire."""
    envelope = SessionDetailResponse(report=None, error=None)
    payload = envelope.model_dump(mode="json")
    assert payload["error"] is None


# ─── TrendsResponse.error envelope + narrowed window Literal ────────


def test_trends_response_error_field_round_trips() -> None:
    """The trends envelope carries the new ``error`` field through the wire."""
    envelope = TrendsResponse(
        window="week",
        daily=[],
        chronotype=ChronotypeModel(),
        error="internal",
    )
    payload = envelope.model_dump(mode="json")
    assert payload["error"] == "internal"
    restored = TrendsResponse.model_validate(payload)
    assert restored.error == "internal"


@pytest.mark.parametrize("window", ["week", "month"])
def test_trends_response_window_literal_accepts_documented_values(window: str) -> None:
    """The narrowed Literal accepts both ``week`` and ``month``."""
    envelope = TrendsResponse(
        window=window,  # type: ignore[arg-type]
        daily=[],
        chronotype=ChronotypeModel(),
    )
    assert envelope.window == window


def test_trends_response_window_literal_rejects_quarter() -> None:
    """``quarter`` was dropped from the wire (no UI panel for it)."""
    with pytest.raises(ValidationError):
        TrendsResponse(
            window="quarter",  # type: ignore[arg-type]
            daily=[],
            chronotype=ChronotypeModel(),
        )


# ─── TrendsRequest envelope (NEW in Phase 4.A) ──────────────────────


def test_trends_request_defaults_are_safe() -> None:
    """Default ``window="week"`` and ``refresh=False`` so a bare payload works."""
    req = TrendsRequest()
    assert req.window == "week"
    assert req.refresh is False


@pytest.mark.parametrize(
    ("window", "refresh"),
    [("week", False), ("week", True), ("month", False), ("month", True)],
)
def test_trends_request_round_trips_all_combinations(window: str, refresh: bool) -> None:
    """Every documented combination round-trips through model_dump."""
    req = TrendsRequest(window=window, refresh=refresh)  # type: ignore[arg-type]
    payload = req.model_dump(mode="json")
    restored = TrendsRequest.model_validate(payload)
    assert restored.window == window
    assert restored.refresh is refresh


def test_trends_request_rejects_unknown_window() -> None:
    """An unknown ``window`` fails before reaching the daemon."""
    with pytest.raises(ValidationError):
        TrendsRequest(window="year", refresh=False)  # type: ignore[arg-type]


# ─── Empty-payload contract for REQUEST_SESSION_RECAP (P0 fix #1) ──


def test_session_detail_response_with_empty_dict_payload_validates() -> None:
    """An empty-dict reply (Phase 4.A: ``recap or {}``) is parseable.

    The daemon's REQUEST_SESSION_RECAP handler now returns ``{}`` when
    no recap is cached (rather than silently dropping). The browser
    extension's SESSION_RECAP handler MUST gate on ``session_id`` presence
    before caching/badging — the schema layer here just confirms an
    empty payload doesn't crash construction.
    """
    # Mirrors the daemon's empty reply.
    payload: dict = {}
    # Pydantic accepts the empty dict because all fields are optional.
    restored = SessionDetailResponse.model_validate(payload)
    assert restored.report is None
    assert restored.error is None
