"""
SessionReader unit tests (P0 §3.1).

Stages fake ``session_<id>.json`` files in a ``tmp_path/sessions`` dir and
exercises the public :class:`SessionReader` API:

* ordering (newest first, deterministic tie-break by session_id)
* forward pagination via ``since`` cursor
* limit clamping ([1, 100])
* malformed JSON resilience (skip + log)
* missing directory resilience (empty response, no crash)
* ``read_session`` happy path / not-found / path traversal rejection
* mtime-keyed cache + ``invalidate(session_id)``
* ``intervention_count`` projection from HYPER state-transitions
* ``top_distraction_domain`` projection from the first domain entry
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cortex.services.session_report.models import SessionReport, StateTransition
from cortex.services.session_report.reader import SessionReader


def _write_session(
    sessions_dir: Path,
    session_id: str,
    *,
    start_time: datetime,
    duration_seconds: float = 600.0,
    flow_percentage: float = 70.0,
    peak_stress_integral: float = 100.0,
    top_distraction_domains: list[str] | None = None,
    hyper_transition_count: int = 0,
) -> Path:
    """Materialise one ``session_<id>.json`` on disk with the supplied fields."""
    end_time = start_time + timedelta(seconds=duration_seconds)
    transitions = [
        StateTransition(
            from_state="FLOW",
            to_state="HYPER",
            timestamp=start_time + timedelta(seconds=i * 10),
        )
        for i in range(hyper_transition_count)
    ]
    report = SessionReport(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration_seconds,
        flow_percentage=flow_percentage,
        peak_stress_integral=peak_stress_integral,
        top_distraction_domains=top_distraction_domains or [],
        state_transitions=transitions,
    )
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"session_{session_id}.json"
    path.write_text(report.model_dump_json(), encoding="utf-8")
    return path


@pytest.fixture()
def sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── ordering / pagination ────────────────────────────────────────────


def test_list_sessions_returns_newest_first(sessions_dir: Path) -> None:
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(sessions_dir, "old", start_time=base)
    _write_session(sessions_dir, "mid", start_time=base + timedelta(hours=1))
    _write_session(sessions_dir, "new", start_time=base + timedelta(hours=2))

    reader = SessionReader(sessions_dir)
    resp = reader.list_sessions(since=None, limit=30)

    assert [item.session_id for item in resp.items] == ["new", "mid", "old"]
    assert resp.total_known == 3
    assert resp.next_cursor is None


def test_list_sessions_pagination_via_cursor(sessions_dir: Path) -> None:
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    # 10 sessions, 1 minute apart for stable ordering.
    for i in range(10):
        _write_session(
            sessions_dir,
            f"s{i:02d}",
            start_time=base + timedelta(minutes=i),
        )

    reader = SessionReader(sessions_dir)
    page1 = reader.list_sessions(since=None, limit=3)
    assert [s.session_id for s in page1.items] == ["s09", "s08", "s07"]
    assert page1.next_cursor is not None

    page2 = reader.list_sessions(since=page1.next_cursor, limit=3)
    assert [s.session_id for s in page2.items] == ["s06", "s05", "s04"]
    assert page2.next_cursor is not None

    page3 = reader.list_sessions(since=page2.next_cursor, limit=3)
    assert [s.session_id for s in page3.items] == ["s03", "s02", "s01"]
    assert page3.next_cursor is not None

    page4 = reader.list_sessions(since=page3.next_cursor, limit=3)
    # Only 1 row left (s00); fewer than limit means no further cursor.
    assert [s.session_id for s in page4.items] == ["s00"]
    assert page4.next_cursor is None


def test_list_sessions_clamps_limit_to_max(sessions_dir: Path) -> None:
    """The reader clamps limit to ``_MAX_LIMIT`` (100). Asking for 9999
    must still cap at 100 rows."""
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    for i in range(110):
        _write_session(
            sessions_dir,
            f"s{i:03d}",
            start_time=base + timedelta(minutes=i),
        )

    reader = SessionReader(sessions_dir)
    resp = reader.list_sessions(since=None, limit=9999)
    assert len(resp.items) == 100  # clamped
    assert resp.total_known == 110


def test_list_sessions_clamps_zero_to_default(sessions_dir: Path) -> None:
    """A ``0`` or falsy limit falls back to the in-process default (30)."""
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    for i in range(35):
        _write_session(sessions_dir, f"s{i:03d}", start_time=base + timedelta(minutes=i))
    reader = SessionReader(sessions_dir)
    resp = reader.list_sessions(since=None, limit=0)
    assert len(resp.items) == 30


# ─── resilience ───────────────────────────────────────────────────────


def test_list_sessions_skips_malformed_json(sessions_dir: Path) -> None:
    """A literal ``not json`` file must not crash listing of valid siblings."""
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(sessions_dir, "good1", start_time=base)
    _write_session(sessions_dir, "good2", start_time=base + timedelta(hours=1))
    (sessions_dir / "session_xyz.json").write_text("not json", encoding="utf-8")

    reader = SessionReader(sessions_dir)
    resp = reader.list_sessions(since=None, limit=30)

    assert sorted(item.session_id for item in resp.items) == ["good1", "good2"]


def test_list_sessions_with_missing_directory_returns_empty() -> None:
    """A reader bound to a non-existent dir must return an empty envelope."""
    reader = SessionReader(Path("/definitely/does/not/exist/here"))
    resp = reader.list_sessions(since=None, limit=30)
    assert resp.items == []
    assert resp.next_cursor is None
    assert resp.total_known == 0


def test_list_sessions_skips_non_session_files(sessions_dir: Path) -> None:
    """Stray ``.json`` files without the ``session_`` prefix are ignored."""
    _write_session(
        sessions_dir, "real", start_time=datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    )
    (sessions_dir / "unrelated.json").write_text(
        json.dumps({"foo": "bar"}), encoding="utf-8"
    )
    (sessions_dir / "session_bad..id.json").write_text(
        json.dumps({"foo": "bar"}), encoding="utf-8"
    )

    reader = SessionReader(sessions_dir)
    resp = reader.list_sessions(since=None, limit=30)
    assert [item.session_id for item in resp.items] == ["real"]


# ─── read_session ─────────────────────────────────────────────────────


def test_read_session_happy_path(sessions_dir: Path) -> None:
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(
        sessions_dir,
        "deadbeef",
        start_time=base,
        duration_seconds=300.0,
        flow_percentage=55.5,
        peak_stress_integral=180.0,
        top_distraction_domains=["reddit.com"],
    )
    reader = SessionReader(sessions_dir)
    resp = reader.read_session("deadbeef")
    assert resp.error is None
    assert resp.report is not None
    assert resp.report.session_id == "deadbeef"
    assert resp.report.duration_seconds == 300.0
    assert resp.report.flow_percentage == 55.5
    assert resp.report.top_distraction_domains == ["reddit.com"]


def test_read_session_not_found_returns_envelope(sessions_dir: Path) -> None:
    reader = SessionReader(sessions_dir)
    resp = reader.read_session("doesnotexist")
    assert resp.report is None
    assert resp.error == "not_found"


def test_read_session_path_traversal_rejected(sessions_dir: Path) -> None:
    """``read_session('../../../etc/passwd')`` must NOT touch the filesystem."""
    reader = SessionReader(sessions_dir)
    resp = reader.read_session("../../../etc/passwd")
    assert resp.report is None
    assert resp.error == "not_found"


def test_read_session_with_slashes_rejected(sessions_dir: Path) -> None:
    reader = SessionReader(sessions_dir)
    for hostile in ("a/b", "foo/bar", "..", "with space"):
        resp = reader.read_session(hostile)
        assert resp.report is None
        assert resp.error == "not_found", f"hostile id {hostile!r} should be rejected"


def test_read_session_corrupt_file_returns_unreadable(sessions_dir: Path) -> None:
    """A file whose JSON is unparsable returns ``error='unreadable'``."""
    (sessions_dir / "session_corrupt.json").write_text("{not valid", encoding="utf-8")
    reader = SessionReader(sessions_dir)
    resp = reader.read_session("corrupt")
    assert resp.report is None
    assert resp.error == "unreadable"


# ─── cache + invalidate ───────────────────────────────────────────────


def test_cache_picks_up_file_mutation_via_mtime(sessions_dir: Path) -> None:
    """Mutate a file with a newer mtime and the listing reflects the new value."""
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    path = _write_session(
        sessions_dir, "mut", start_time=base, flow_percentage=20.0
    )
    reader = SessionReader(sessions_dir)

    page = reader.list_sessions(since=None, limit=30)
    assert page.items[0].flow_percentage == 20.0

    # Rewrite the file with a newer flow_percentage and a bumped mtime.
    _write_session(sessions_dir, "mut", start_time=base, flow_percentage=90.0)
    import os
    future_mtime = path.stat().st_mtime + 5.0
    os.utime(path, (future_mtime, future_mtime))

    # Cache invalidation isn't strictly necessary because mtime changed,
    # but exercise the public hook anyway.
    reader.invalidate("mut")
    page2 = reader.list_sessions(since=None, limit=30)
    assert page2.items[0].flow_percentage == 90.0


def test_invalidate_drops_only_target_id(sessions_dir: Path) -> None:
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(sessions_dir, "a", start_time=base)
    _write_session(sessions_dir, "b", start_time=base + timedelta(hours=1))
    reader = SessionReader(sessions_dir)
    reader.list_sessions(since=None, limit=30)  # populate cache

    reader.invalidate("a")
    # Both still appear after invalidate (file still exists; cache repopulates).
    page = reader.list_sessions(since=None, limit=30)
    ids = {item.session_id for item in page.items}
    assert ids == {"a", "b"}


def test_invalidate_all_clears_cache(sessions_dir: Path) -> None:
    _write_session(
        sessions_dir, "alpha", start_time=datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    )
    reader = SessionReader(sessions_dir)
    reader.list_sessions(since=None, limit=30)  # populate
    reader.invalidate(None)  # full clear
    # Next call re-walks the directory; entry should reappear.
    page = reader.list_sessions(since=None, limit=30)
    assert page.items[0].session_id == "alpha"


# ─── projections ──────────────────────────────────────────────────────


def test_intervention_count_derived_from_hyper_transitions(
    sessions_dir: Path,
) -> None:
    """3 HYPER transitions → intervention_count = 3 in the summary."""
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(
        sessions_dir, "ivc", start_time=base, hyper_transition_count=3
    )
    reader = SessionReader(sessions_dir)
    page = reader.list_sessions(since=None, limit=30)
    assert page.items[0].intervention_count == 3


def test_top_distraction_domain_picks_first(sessions_dir: Path) -> None:
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(
        sessions_dir,
        "td",
        start_time=base,
        top_distraction_domains=["reddit.com", "twitter.com"],
    )
    reader = SessionReader(sessions_dir)
    page = reader.list_sessions(since=None, limit=30)
    assert page.items[0].top_distraction_domain == "reddit.com"


def test_top_distraction_domain_is_none_when_empty(sessions_dir: Path) -> None:
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(sessions_dir, "td0", start_time=base, top_distraction_domains=[])
    reader = SessionReader(sessions_dir)
    page = reader.list_sessions(since=None, limit=30)
    assert page.items[0].top_distraction_domain is None


# ─── Wave-2 P1: strict skip on missing required fields ───────────────


def test_list_sessions_skips_missing_duration_seconds(
    sessions_dir: Path, caplog: Any
) -> None:
    """A session JSON missing ``duration_seconds`` is treated as malformed:
    the row is skipped (NOT silently zeroed) and a WARNING naming the file
    path is emitted. Adjacent valid sessions still list normally.
    """
    import logging

    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    _write_session(sessions_dir, "good", start_time=base)

    # Materialise a session JSON missing ``duration_seconds`` entirely.
    bad_path = sessions_dir / "session_bad.json"
    bad_path.write_text(
        json.dumps({
            "session_id": "bad",
            "start_time": base.isoformat(),
            "end_time": (base + timedelta(seconds=600)).isoformat(),
            # duration_seconds intentionally omitted.
            "flow_percentage": 50.0,
        }),
        encoding="utf-8",
    )

    reader = SessionReader(sessions_dir)
    with caplog.at_level(logging.WARNING, logger="cortex.services.session_report.reader"):
        resp = reader.list_sessions(since=None, limit=30)

    ids = [item.session_id for item in resp.items]
    assert ids == ["good"], f"expected only 'good'; got {ids}"
    # Warning must name the path so the operator can find the bad file.
    warned = [r for r in caplog.records if "session_bad.json" in r.getMessage()]
    assert warned, "expected a WARNING naming the malformed session file"
    assert any("duration_seconds" in r.getMessage() for r in warned)


def test_list_sessions_skips_missing_flow_percentage(
    sessions_dir: Path, caplog: Any
) -> None:
    """``flow_percentage`` is required (its 0.0 default would mask corruption)."""
    import logging

    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    bad_path = sessions_dir / "session_noflow.json"
    bad_path.write_text(
        json.dumps({
            "session_id": "noflow",
            "start_time": base.isoformat(),
            "end_time": (base + timedelta(seconds=600)).isoformat(),
            "duration_seconds": 600.0,
            # flow_percentage intentionally omitted.
        }),
        encoding="utf-8",
    )
    reader = SessionReader(sessions_dir)
    with caplog.at_level(logging.WARNING, logger="cortex.services.session_report.reader"):
        resp = reader.list_sessions(since=None, limit=30)
    assert resp.items == []
    assert any(
        "session_noflow.json" in r.getMessage() and "flow_percentage" in r.getMessage()
        for r in caplog.records
    )


def test_list_sessions_allows_missing_peak_stress_integral(
    sessions_dir: Path,
) -> None:
    """``peak_stress_integral`` is genuinely optional (defaults to 0.0)."""
    base = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)
    path = sessions_dir / "session_nopeak.json"
    path.write_text(
        json.dumps({
            "session_id": "nopeak",
            "start_time": base.isoformat(),
            "end_time": (base + timedelta(seconds=600)).isoformat(),
            "duration_seconds": 600.0,
            "flow_percentage": 70.0,
            # peak_stress_integral intentionally omitted.
        }),
        encoding="utf-8",
    )
    reader = SessionReader(sessions_dir)
    resp = reader.list_sessions(since=None, limit=30)
    assert [item.session_id for item in resp.items] == ["nopeak"]
    assert resp.items[0].peak_stress_integral == 0.0
