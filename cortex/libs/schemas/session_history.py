"""
Cortex Session-History Wire Envelopes (P0 §3.1 / §3.2 / §3.3)

These models live in :mod:`cortex.libs.schemas` so they participate in
the Debt-1 codegen pipeline (``python -m cortex.scripts.generate_ts_schemas``).
The on-disk ``SessionReport`` (see :mod:`cortex.libs.schemas.session_report`)
remains the canonical record; this module only defines the cheap projections
and pagination envelopes that travel over the WebSocket and REST APIs.

Three envelopes, one per use-case:

* :class:`SessionSummary` — one row in the History listing. Cheap to
  materialise from the report header; safe to render in a list view at
  60 Hz without re-parsing transitions/activities for every row.
* :class:`SessionListResponse` — paginated listing reply for
  ``REQUEST_SESSION_LIST``.
* :class:`SessionDetailResponse` — single-report reply for
  ``REQUEST_SESSION_DETAIL`` (also used by the recap sheet when a stale
  popup re-asks via ``REQUEST_SESSION_RECAP``).
* :class:`TrendsResponse` — week / month / quarter rollup reply for ``REQUEST_TRENDS``.
* :class:`TrendsRequest` — documented request envelope for ``REQUEST_TRENDS``.

Privacy invariants (P0 §3.1 "Privacy / safety invariants"):

* Reports stay on ``127.0.0.1``; the new REST endpoints are gated by the
  Debt-2 capability token, identical to every other mutating route.
* Only aggregate fields cross the wire. Domains surface as bare hostnames
  (``urlparse(...).hostname``); raw URLs and screen content never leave
  the daemon process.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from cortex.libs.schemas.longitudinal import ChronotypeModel, DailyBaseline
from cortex.libs.schemas.session_report import SessionReport

# ─── Path-safe session_id (single source of truth) ───────────────────────
#
# Defense vs path traversal: a session_id must look like our generator
# outputs (UUID / hex / readable token), never contain ``/`` or ``..``.
# Cap at 128 chars so the constructed path stays well under PATH_MAX.
# The reader (`cortex/services/session_report/reader.py`) and the REST
# route (`cortex/services/api_gateway/routes.py`) both reuse this same
# constant so the validation contract is documented in one place.
SESSION_ID_PATTERN: str = r"^[A-Za-z0-9_\-]{1,128}$"

SessionIdStr = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=SESSION_ID_PATTERN,
        description="Safe session_id token (regex: " + SESSION_ID_PATTERN + ").",
    ),
]


class SessionSummary(BaseModel):
    """One row in the History listing.

    Projected from the persisted :class:`SessionReport` header at list
    time (``cortex/services/session_report/reader.py::list_sessions``)
    so the dashboard can render hundreds of rows without re-parsing
    every nested ``StateTransition`` / ``ActivitySummary`` list.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: SessionIdStr = Field(
        ..., description="UUID matching the on-disk session_<id>.json filename"
    )
    start_time: datetime = Field(..., description="Session start (timezone-aware where present)")
    end_time: datetime = Field(..., description="Session end (timezone-aware where present)")
    duration_seconds: float = Field(..., ge=0.0, description="end_time - start_time in seconds")
    flow_percentage: float = Field(0.0, ge=0.0, le=100.0, description="Biometrically-verified focus / wall-clock")
    peak_stress_integral: float = Field(0.0, ge=0.0, description="Max cumulative HRV-suppression integral observed")
    top_distraction_domain: str | None = Field(
        None,
        max_length=253,
        description="Most-visited distraction hostname during the session (RFC 1035 FQDN ≤253).",
    )
    intervention_count: int = Field(0, ge=0, description="Count of state_transitions whose to_state is HYPER (proxy for interventions fired)")


class SessionListResponse(BaseModel):
    """Reply envelope for ``REQUEST_SESSION_LIST``.

    Pagination is forward-only via ``next_cursor`` (epoch-seconds of the
    last row in this page). Clients send the cursor back as ``since`` on
    the next request. ``None`` means "no more rows after this page."

    ``cursor_session_id`` is the canonical tie-breaker for entries that
    share the same ``start_time``: a client passes it back alongside
    ``since`` so the daemon can resume after the exact row the previous
    page ended on (see ``reader.list_sessions``).
    """

    model_config = ConfigDict(extra="ignore")

    items: list[SessionSummary] = Field(default_factory=list, max_length=100)
    next_cursor: float | None = Field(
        None,
        description="Epoch seconds of the oldest row in this page; pass as 'since' to request the next page. None when there is no more history.",
    )
    cursor_session_id: str | None = Field(
        None,
        max_length=128,
        description=(
            "Session id of the oldest row in this page. When two rows share "
            "the same ``start_time`` the daemon resumes after this id on the "
            "next page so no row is skipped on ties (P0 §3.1 fix #10)."
        ),
    )
    total_known: int = Field(
        0,
        ge=0,
        description="Total number of session files the daemon currently knows about (for the 'Sessions · last N' header).",
    )
    error: Literal[
        "internal",
        "daemon_unavailable",
        "no_cache",
        "invalid_request",
        "handler_not_registered",
    ] | None = Field(
        None,
        description=(
            "Set when the daemon cannot fulfil the request. Known values: "
            "'internal' | 'daemon_unavailable' | 'no_cache' | "
            "'invalid_request' | 'handler_not_registered' | None (success)."
        ),
    )


class SessionDetailResponse(BaseModel):
    """Reply envelope for ``REQUEST_SESSION_DETAIL`` / ``REQUEST_SESSION_RECAP``."""

    model_config = ConfigDict(extra="ignore")

    report: SessionReport | None = Field(
        None,
        description="Full report when found; None when the file is missing or unparsable (see 'error').",
    )
    error: Literal[
        "not_found",
        "unreadable",
        "invalid_id",
        "internal",
        "daemon_unavailable",
        "handler_not_registered",
    ] | None = Field(
        None,
        description=(
            "Error code when report is None. 'not_found' = no such "
            "session_id on disk; 'unreadable' = file present but JSON "
            "malformed; 'invalid_id' = id failed the safe-char regex; "
            "'internal' = callback raised; 'daemon_unavailable' = no "
            "daemon registered; 'handler_not_registered' = the daemon "
            "has not wired a session-detail callback yet (cold-start race)."
        ),
    )


class TrendsRequest(BaseModel):
    """Documented envelope for ``REQUEST_TRENDS`` payloads.

    The WS dispatch arm (``_handle_request_trends``) currently parses
    raw payload dicts; this schema documents the contract so clients
    and the REST route (``GET /api/trends``) share one definition.
    Adding it here is required for the Debt-1 codegen pipeline to
    emit a typed TypeScript surface for the extension.
    """

    model_config = ConfigDict(extra="ignore")

    window: Literal["week", "month", "quarter"] = Field(
        "week",
        description=(
            "Aggregation window: 'week' = last 7 days, 'month' = last 30, "
            "'quarter' = last 90."
        ),
    )
    refresh: bool = Field(
        False,
        description=(
            "When True, force the daemon to recompute the chronotype model "
            "from disk before replying. Defaults to False so the cached "
            "model.json is served."
        ),
    )


class TrendsResponse(BaseModel):
    """Reply envelope for ``REQUEST_TRENDS``.

    Combines the per-day ``DailyBaseline`` rows (the bars) with the
    aggregated :class:`ChronotypeModel` (golden hour stripe, trend pill,
    top task / hour patterns).
    """

    model_config = ConfigDict(extra="ignore")

    window: Literal["week", "month", "quarter"] = Field(
        "week",
        description=(
            "The window the daily rows cover. Week=last 7 days, "
            "month=last 30, quarter=last 90."
        ),
    )
    daily: list[DailyBaseline] = Field(
        default_factory=list,
        description="One DailyBaseline row per day in the window, chronologically ascending. UI must read this — never iterate ``chronotype.baselines`` directly.",
    )
    chronotype: ChronotypeModel = Field(
        default_factory=ChronotypeModel,
        description="Aggregated model — trend_direction, sensitivity_multiplier, hourly_patterns, task_patterns.",
    )
    last_aggregated: datetime | None = Field(
        None,
        description="When the cached aggregator last ran (mirrors chronotype.last_updated for convenience).",
    )
    error: Literal[
        "internal",
        "daemon_unavailable",
        "no_cache",
        "invalid_window",
        "handler_not_registered",
    ] | None = Field(
        None,
        description=(
            "Set when the daemon cannot fulfil the request. Known values: "
            "'internal' | 'daemon_unavailable' | 'no_cache' | "
            "'invalid_window' | 'handler_not_registered' | None (success)."
        ),
    )


__all__ = [
    "SESSION_ID_PATTERN",
    "SessionIdStr",
    "SessionSummary",
    "SessionListResponse",
    "SessionDetailResponse",
    "TrendsRequest",
    "TrendsResponse",
]
