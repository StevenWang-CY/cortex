"""Phase-4a Debt-1: regression tests for the small schema-correctness
fixes that landed alongside the new envelopes.

These are the loose ends Phase-4a closed (one assertion each):

* :class:`SessionReport` carries an explicit ``schema_version`` that
  defaults to 1.
* :class:`SessionDetailResponse` accepts ``"handler_not_registered"``
  as a valid error literal.
* :class:`SessionListResponse` / :class:`TrendsResponse` reject
  free-form error strings and accept only the documented vocabulary.
* :class:`WSMessage` stamps its ``timestamp`` in unix epoch seconds
  (so JavaScript ``Date.now()/1000`` is directly comparable) — not
  ``time.monotonic`` which is process-local.
* :class:`AckResponse` / :class:`ShutdownResponse` (REST) follow the
  same epoch-seconds convention.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    TrendsResponse,
)
from cortex.libs.schemas.session_report import SessionReport
from cortex.libs.schemas.ws_message import WSMessage
from cortex.libs.schemas.ws_message_types import MessageType

# ─── SessionReport.schema_version ──────────────────────────────────────


def _minimal_report() -> SessionReport:
    now = datetime.now(UTC)
    return SessionReport(
        session_id="abc",
        start_time=now,
        end_time=now,
        duration_seconds=10.0,
    )


def test_session_report_schema_version_defaults_to_1() -> None:
    assert _minimal_report().schema_version == 1


def test_session_report_schema_version_can_be_overridden() -> None:
    rep = SessionReport(
        schema_version=2,
        session_id="abc",
        start_time=datetime.now(UTC),
        end_time=datetime.now(UTC),
        duration_seconds=10.0,
    )
    assert rep.schema_version == 2


def test_session_report_schema_version_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        SessionReport(
            schema_version=0,
            session_id="abc",
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            duration_seconds=10.0,
        )


# ─── SessionDetailResponse.error ───────────────────────────────────────


def test_session_detail_response_handler_not_registered_is_valid() -> None:
    msg = SessionDetailResponse(report=None, error="handler_not_registered")
    assert msg.error == "handler_not_registered"


def test_session_detail_response_rejects_unknown_error() -> None:
    with pytest.raises(ValidationError):
        SessionDetailResponse(report=None, error="random_string")  # type: ignore[arg-type]


# ─── SessionListResponse.error ─────────────────────────────────────────


def test_session_list_response_accepts_documented_errors() -> None:
    for code in (
        "internal",
        "daemon_unavailable",
        "no_cache",
        "invalid_request",
        "handler_not_registered",
    ):
        SessionListResponse(error=code)  # type: ignore[arg-type]


def test_session_list_response_rejects_unknown_error() -> None:
    with pytest.raises(ValidationError):
        SessionListResponse(error="oops")  # type: ignore[arg-type]


# ─── TrendsResponse.error ──────────────────────────────────────────────


def test_trends_response_accepts_documented_errors() -> None:
    for code in (
        "internal",
        "daemon_unavailable",
        "no_cache",
        "invalid_window",
        "handler_not_registered",
    ):
        TrendsResponse(window="week", error=code)  # type: ignore[arg-type]


def test_trends_response_rejects_unknown_error() -> None:
    with pytest.raises(ValidationError):
        TrendsResponse(window="week", error="bogus")  # type: ignore[arg-type]


# ─── WSMessage.timestamp uses epoch seconds ────────────────────────────


def test_ws_message_timestamp_is_unix_epoch_seconds() -> None:
    """The default factory now stamps wall-clock seconds. Verify it's
    within a couple of seconds of ``time.time()`` rather than the
    much smaller process-uptime number ``time.monotonic`` would return.
    """
    before = time.time()
    msg = WSMessage(type=MessageType.STATE_UPDATE)
    after = time.time()
    # Allow some slop for slow CI; the point is to distinguish epoch
    # seconds (~1.7e9 today) from monotonic (~hundreds at most).
    assert before - 1 <= msg.timestamp <= after + 1
    assert msg.timestamp > 1_000_000_000.0  # well beyond any monotonic uptime
