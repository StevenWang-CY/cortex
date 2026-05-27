"""
Schema round-trip tests for P0 §3.1 / §3.2 / §3.3 wire envelopes
(``cortex.libs.schemas.session_history``).

These cover the four envelopes that cross the daemon ↔ desktop / extension
boundary: ``SessionSummary``, ``SessionListResponse``, ``SessionDetailResponse``,
``TrendsResponse``. The goal is to guarantee:

* ``model_dump(mode="json")`` → ``model_validate`` round-trips identically.
* Default factories work (empty lists, ``None`` cursors, etc.).
* Validators reject negative durations / out-of-range percentages.
* ``extra="ignore"`` forwards-compat: unknown JSON fields don't raise.
* Detail envelope with ``report=None, error="not_found"`` round-trips.
* Datetimes serialise to ISO strings and parse back to tz-aware datetimes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.longitudinal import (
    ChronotypeModel,
    DailyBaseline,
    HourlyOverloadRate,
    TaskOverloadPattern,
)
from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    SessionSummary,
    TrendsRequest,
    TrendsResponse,
)
from cortex.services.session_report.models import SessionReport, StateTransition


def _make_summary(**overrides):
    """Build a realistic :class:`SessionSummary` dict."""
    base = {
        "session_id": "abcd1234",
        "start_time": datetime(2026, 5, 24, 9, 0, tzinfo=UTC),
        "end_time": datetime(2026, 5, 24, 10, 30, tzinfo=UTC),
        "duration_seconds": 5400.0,
        "flow_percentage": 72.5,
        "peak_stress_integral": 312.0,
        "top_distraction_domain": "reddit.com",
        "intervention_count": 3,
    }
    base.update(overrides)
    return base


def test_session_summary_roundtrip_identical() -> None:
    """``model_dump(mode='json')`` then ``model_validate`` must round-trip."""
    s = SessionSummary(**_make_summary())
    dumped = s.model_dump(mode="json")
    restored = SessionSummary.model_validate(dumped)
    assert restored.model_dump(mode="json") == dumped


def test_session_summary_datetime_is_iso_string_on_wire() -> None:
    """JSON-mode dump must serialise datetimes as ISO strings."""
    s = SessionSummary(**_make_summary())
    dumped = s.model_dump(mode="json")
    assert isinstance(dumped["start_time"], str)
    parsed = datetime.fromisoformat(dumped["start_time"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    # Round-trips back to a tz-aware datetime equal to the original.
    restored = SessionSummary.model_validate(dumped)
    assert restored.start_time.tzinfo is not None
    assert restored.start_time == s.start_time


def test_session_summary_defaults_sane() -> None:
    """Only required fields specified; optional ones use sensible defaults."""
    s = SessionSummary(
        session_id="x",
        start_time=datetime(2026, 5, 1, tzinfo=UTC),
        end_time=datetime(2026, 5, 1, 0, 1, tzinfo=UTC),
        duration_seconds=60.0,
    )
    assert s.flow_percentage == 0.0
    assert s.peak_stress_integral == 0.0
    assert s.top_distraction_domain is None
    assert s.intervention_count == 0


def test_session_summary_rejects_negative_duration() -> None:
    """``ge=0`` on duration must reject negative inputs."""
    with pytest.raises(ValidationError):
        SessionSummary(**_make_summary(duration_seconds=-1.0))


def test_session_summary_rejects_flow_pct_over_100() -> None:
    """``le=100`` on flow_percentage must reject > 100."""
    with pytest.raises(ValidationError):
        SessionSummary(**_make_summary(flow_percentage=101.0))


def test_session_summary_rejects_negative_intervention_count() -> None:
    """``ge=0`` on intervention_count must reject negatives."""
    with pytest.raises(ValidationError):
        SessionSummary(**_make_summary(intervention_count=-1))


def test_session_summary_ignores_unknown_fields() -> None:
    """Forward-compat: extra JSON keys must not raise (``extra='ignore'``)."""
    payload = _make_summary()
    payload["future_field"] = {"nested": [1, 2, 3]}
    s = SessionSummary.model_validate(payload)
    # Unknown field stripped during validation.
    assert "future_field" not in s.model_dump(mode="json")


def test_session_list_response_defaults() -> None:
    """Empty ``SessionListResponse`` has sane defaults."""
    r = SessionListResponse()
    assert r.items == []
    assert r.next_cursor is None
    assert r.total_known == 0


def test_session_list_response_roundtrip_with_items() -> None:
    r = SessionListResponse(
        items=[SessionSummary(**_make_summary())],
        next_cursor=1716540000.0,
        total_known=42,
    )
    dumped = r.model_dump(mode="json")
    restored = SessionListResponse.model_validate(dumped)
    assert restored.model_dump(mode="json") == dumped
    assert len(restored.items) == 1
    assert restored.next_cursor == 1716540000.0
    assert restored.total_known == 42


def test_session_list_response_rejects_negative_total_known() -> None:
    with pytest.raises(ValidationError):
        SessionListResponse(total_known=-1)


def test_session_list_response_ignores_unknown_fields() -> None:
    payload = {
        "items": [],
        "next_cursor": None,
        "total_known": 0,
        "future_pagination_token": "v2-cursor",
    }
    r = SessionListResponse.model_validate(payload)
    assert "future_pagination_token" not in r.model_dump(mode="json")


def _make_full_session_report() -> SessionReport:
    """A SessionReport with non-trivial sub-models so we exercise nesting."""
    now = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    return SessionReport(
        session_id="full-1",
        start_time=now,
        end_time=now + timedelta(seconds=600),
        duration_seconds=600.0,
        time_in_flow_seconds=400.0,
        time_in_hyper_seconds=120.0,
        time_in_hypo_seconds=0.0,
        time_in_recovery_seconds=80.0,
        flow_percentage=66.67,
        peak_stress_integral=250.0,
        top_distraction_domains=["reddit.com", "twitter.com"],
        state_transitions=[
            StateTransition(
                from_state="FLOW",
                to_state="HYPER",
                timestamp=now + timedelta(seconds=300),
            ),
        ],
        avg_hr_bpm=72.0,
        avg_hrv_rmssd=45.0,
    )


def test_session_detail_response_happy_path_roundtrip() -> None:
    report = _make_full_session_report()
    envelope = SessionDetailResponse(report=report, error=None)
    dumped = envelope.model_dump(mode="json")
    restored = SessionDetailResponse.model_validate(dumped)
    assert restored.error is None
    assert restored.report is not None
    assert restored.report.session_id == "full-1"
    # Inner datetimes are tz-aware after round-trip.
    assert restored.report.start_time.tzinfo is not None
    assert restored.model_dump(mode="json") == dumped


def test_session_detail_response_not_found_roundtrip() -> None:
    """``report=None, error='not_found'`` must round-trip."""
    envelope = SessionDetailResponse(report=None, error="not_found")
    dumped = envelope.model_dump(mode="json")
    assert dumped["report"] is None
    assert dumped["error"] == "not_found"
    restored = SessionDetailResponse.model_validate(dumped)
    assert restored.report is None
    assert restored.error == "not_found"


def test_session_detail_response_unreadable_roundtrip() -> None:
    """Alternate error code also round-trips."""
    envelope = SessionDetailResponse(report=None, error="unreadable")
    restored = SessionDetailResponse.model_validate(envelope.model_dump(mode="json"))
    assert restored.error == "unreadable"


def test_session_detail_response_ignores_unknown_fields() -> None:
    payload = {"report": None, "error": "not_found", "hint": "try later"}
    envelope = SessionDetailResponse.model_validate(payload)
    assert "hint" not in envelope.model_dump(mode="json")


def test_trends_response_defaults() -> None:
    """Default construction must produce a usable empty envelope."""
    r = TrendsResponse()
    assert r.window == "week"
    assert r.daily == []
    assert isinstance(r.chronotype, ChronotypeModel)
    assert r.last_aggregated is None


def test_trends_response_roundtrip_full() -> None:
    today = date(2026, 5, 24)
    chrono = ChronotypeModel(
        baselines=[
            DailyBaseline(record_date=today - timedelta(days=1)),
            DailyBaseline(record_date=today),
        ],
        trend_direction="improving",
        sensitivity_multiplier=1.1,
        hourly_patterns=[HourlyOverloadRate(hour=14, overload_rate=0.4, sample_count=3)],
        task_patterns=[
            TaskOverloadPattern(
                pattern_key="reddit.com",
                overload_rate=0.7,
                avg_stress_integral=120.0,
                correlation="trigger",
            )
        ],
        last_updated=datetime(2026, 5, 24, 0, 5, tzinfo=UTC),
        window_days=30,
    )
    envelope = TrendsResponse(
        window="month",
        daily=chrono.baselines,
        chronotype=chrono,
        last_aggregated=chrono.last_updated,
    )
    dumped = envelope.model_dump(mode="json")
    restored = TrendsResponse.model_validate(dumped)
    assert restored.model_dump(mode="json") == dumped
    assert restored.window == "month"
    assert len(restored.daily) == 2
    assert restored.chronotype.trend_direction == "improving"
    assert restored.last_aggregated is not None
    assert restored.last_aggregated.tzinfo is not None


def test_trends_response_rejects_invalid_window() -> None:
    """Phase 4.4 contracts: literal is ``week``/``month`` only."""
    with pytest.raises(ValidationError):
        TrendsResponse(window="year")  # type: ignore[arg-type]


def test_trends_response_rejects_quarter_window() -> None:
    """Phase 4.4 contracts: ``quarter`` was dropped from the wire literal."""
    with pytest.raises(ValidationError):
        TrendsResponse(window="quarter")  # type: ignore[arg-type]


def test_trends_request_accepts_documented_windows() -> None:
    """Phase 4.4 contracts: TrendsRequest accepts only week / month."""
    for window in ("week", "month"):
        req = TrendsRequest(window=window)  # type: ignore[arg-type]
        assert req.window == window
        dumped = req.model_dump(mode="json")
        assert dumped["window"] == window
        assert TrendsRequest.model_validate(dumped).window == window
    with pytest.raises(ValidationError):
        TrendsRequest(window="quarter")  # type: ignore[arg-type]


def test_trends_request_rejects_invalid_window() -> None:
    """TrendsRequest must reject windows outside the literal union."""
    with pytest.raises(ValidationError):
        TrendsRequest(window="year")  # type: ignore[arg-type]


def test_trends_request_defaults() -> None:
    """Default TrendsRequest is week, refresh=False."""
    req = TrendsRequest()
    assert req.window == "week"
    assert req.refresh is False


def test_trends_response_ignores_unknown_fields() -> None:
    payload = {
        "window": "week",
        "daily": [],
        "chronotype": {},
        "last_aggregated": None,
        "future_field": True,
    }
    r = TrendsResponse.model_validate(payload)
    assert "future_field" not in r.model_dump(mode="json")


def test_session_summary_accepts_alternate_timezone() -> None:
    """A non-UTC tz-aware datetime survives the dump → validate cycle."""
    pacific = timezone(timedelta(hours=-7))
    s = SessionSummary(
        **_make_summary(start_time=datetime(2026, 5, 24, 2, 0, tzinfo=pacific))
    )
    dumped = s.model_dump(mode="json")
    restored = SessionSummary.model_validate(dumped)
    assert restored.start_time.tzinfo is not None
    # Same instant in time regardless of tz representation.
    assert restored.start_time == s.start_time
