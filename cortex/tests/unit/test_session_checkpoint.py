"""Session-checkpoint coverage (Task C).

Root cause of "History shows 0 sessions while the dashboard shows live
activity": sessions were only written on daemon stop(), so the History tab —
which reads the on-disk session store — saw nothing for an actively-running
session. The fix is a non-mutating ``SessionReportGenerator.snapshot`` plus a
periodic checkpoint that writes it to the session file.

These tests pin the two guarantees the fix depends on:

* ``snapshot`` builds a report for an in-progress session WITHOUT mutating
  the accumulators (safe to call repeatedly; ``finish`` after it is still
  correct);
* a checkpointed session file is immediately discoverable by
  :class:`SessionReader` (the exact produce→persist→query path the History
  tab walks), so the in-progress session appears in History.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.services.session_report.generator import SessionReportGenerator
from cortex.services.session_report.reader import SessionReader

# Fully deterministic synthetic timeline. ``start()`` stamps ``_start_time``
# at real now(); we pin it to ``_FLOW_AT`` so the report's
# ``duration_seconds`` (= end_time − start_time) is a fixed +90 s regardless
# of when the test runs in the suite — the earlier version anchored on real
# wall-clock and went negative once the full suite took >90 s to reach here.
_FLOW_AT = 1_900_000_000.0  # fixed epoch (well within datetime range)
_HYPER_AT = _FLOW_AT + 30.0  # 30 s of FLOW closed
_SNAP_AT = _FLOW_AT + 90.0  # +60 s of open HYPER


def _seeded_generator() -> SessionReportGenerator:
    g = SessionReportGenerator()
    g.start()
    # Pin the session start to the synthetic FLOW timestamp so duration is
    # deterministic (white-box: the daemon stamps this from real time).
    g._start_time = datetime.fromtimestamp(_FLOW_AT, tz=UTC)
    g.record_state("FLOW", _FLOW_AT)
    g.record_state("HYPER", _HYPER_AT)
    return g


def test_snapshot_is_non_mutating() -> None:
    g = _seeded_generator()
    s1 = g.snapshot(end_timestamp=_SNAP_AT)
    s2 = g.snapshot(end_timestamp=_SNAP_AT)
    # Repeated snapshots are identical — no double counting of the open segment.
    assert s1.time_in_flow_seconds == s2.time_in_flow_seconds == 30.0
    assert s1.time_in_hyper_seconds == s2.time_in_hyper_seconds == 60.0


def test_finish_after_snapshot_is_not_double_counted() -> None:
    g = _seeded_generator()
    g.snapshot(end_timestamp=_SNAP_AT)
    g.snapshot(end_timestamp=_SNAP_AT)
    final = g.finish(end_timestamp=_SNAP_AT)
    assert final.time_in_flow_seconds == 30.0
    assert final.time_in_hyper_seconds == 60.0


def test_snapshot_shares_session_id_with_finish() -> None:
    """Checkpoint + final write target the same ``session_<id>.json``."""
    g = _seeded_generator()
    snap = g.snapshot(end_timestamp=_SNAP_AT)
    final = g.finish(end_timestamp=_SNAP_AT)
    assert snap.session_id == final.session_id


def test_checkpointed_session_is_visible_to_reader(tmp_path: Path) -> None:
    """A written in-progress session shows up in the History listing path."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    reader = SessionReader(sessions_dir)

    # Before any checkpoint: history is empty (the reported symptom).
    assert reader.list_sessions(since=None, limit=30).total_known == 0

    g = _seeded_generator()
    snap = g.snapshot(end_timestamp=_SNAP_AT)
    atomic_write_json(
        sessions_dir / f"session_{snap.session_id}.json",
        snap.model_dump(mode="json"),
    )
    reader.invalidate(snap.session_id)

    resp = reader.list_sessions(since=None, limit=30)
    assert resp.total_known == 1
    assert any(s.session_id == snap.session_id for s in resp.items)


def test_final_write_overwrites_checkpoint(tmp_path: Path) -> None:
    """Re-writing the same session id updates, never duplicates, the row."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    reader = SessionReader(sessions_dir)
    g = _seeded_generator()

    snap = g.snapshot(end_timestamp=_SNAP_AT)
    path = sessions_dir / f"session_{snap.session_id}.json"
    atomic_write_json(path, snap.model_dump(mode="json"))
    reader.invalidate(snap.session_id)
    assert reader.list_sessions(since=None, limit=30).total_known == 1

    final = g.finish(end_timestamp=_FLOW_AT + 200.0)  # session ran longer
    atomic_write_json(path, final.model_dump(mode="json"))
    reader.invalidate(final.session_id)
    resp = reader.list_sessions(since=None, limit=30)
    assert resp.total_known == 1  # same id → overwritten, not duplicated
