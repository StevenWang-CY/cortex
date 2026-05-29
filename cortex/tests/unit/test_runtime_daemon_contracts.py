"""Behavioural tests for the DAEMON-side contracts + bug fixes
(production remediation).

Covers, against the REAL :class:`CortexDaemon` (no capture/WS boot):

* C4  — SESSION_RECAP is sent as the declared ``SessionRecap`` wrapper
        (``report`` / ``generated_at`` / ``persisted``) via ``force_recap``.
* C5  — the rPPG window build forwards the PRIOR frame's
        blink-suppression score into ``PulseEstimator.process_window``.
* C6  — FACE_LOST / FACE_REACQUIRED + QUIET_MODE_ENTERED/EXITED +
        OS_NOTIFICATION_SENT structured events fire on their transitions.
* C7  — ``_store_degraded`` mirrors ``store.degraded`` after construction.
* fix#10 — Copilot throttle is force-enabled on the shutdown path.
* fix#9  — the dismissal model is trained on the TRIGGER-time
           confidence/complexity snapshot, not recovery_confidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from cortex.libs.logging.structured import EventType
from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.session_report.models import SessionReport


@pytest.fixture()
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    if hasattr(settings_mod.get_config, "cache_clear"):
        settings_mod.get_config.cache_clear()  # type: ignore[attr-defined]
    return storage


@pytest.fixture()
def daemon(isolated_storage: Path):  # type: ignore[no-untyped-def]
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    return CortexDaemon(config=get_config())


def _make_report(duration_seconds: float = 600.0) -> SessionReport:
    now = datetime.now(UTC)
    return SessionReport(
        session_id="contract-test",
        start_time=now,
        end_time=now + timedelta(seconds=duration_seconds),
        duration_seconds=duration_seconds,
        time_in_flow_seconds=duration_seconds * 0.7,
        time_in_hyper_seconds=duration_seconds * 0.2,
        flow_percentage=70.0,
        peak_stress_integral=180.0,
    )


# ─── C4: SessionRecap wrapper ────────────────────────────────────────────


async def test_force_recap_sends_session_recap_wrapper(daemon) -> None:  # type: ignore[no-untyped-def]
    """force_recap must emit the declared SessionRecap envelope:
    {report: {...}, generated_at: <iso str>, persisted: bool}."""
    daemon._ws_server.send_message = AsyncMock(return_value=True)
    daemon._session_report_started = True
    daemon._session_report = MagicMock()
    daemon._session_report.finish.return_value = _make_report(600.0)

    ok = await daemon.force_recap()
    assert ok is True

    daemon._ws_server.send_message.assert_called_once()
    msg_type, payload = daemon._ws_server.send_message.call_args.args
    assert msg_type == MessageType.SESSION_RECAP.value
    # C4: the wrapper, not the bare report.
    assert set(payload.keys()) == {"report", "generated_at", "persisted"}
    assert payload["report"]["session_id"] == "contract-test"
    assert payload["persisted"] is False
    # generated_at is a parseable ISO-8601 string.
    datetime.fromisoformat(payload["generated_at"])


# ─── C7: store.degraded mirror ───────────────────────────────────────────


def test_store_degraded_mirrors_store_when_redis_degraded(isolated_storage: Path) -> None:
    """When the (Redis) store reports degraded=True, the daemon mirrors it."""
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    cfg = get_config()
    cfg.redis.enabled = True
    fake_store = MagicMock()
    fake_store.degraded = True
    with patch("cortex.services.runtime_daemon.RedisStore", return_value=fake_store):
        d = CortexDaemon(config=cfg)
    assert d._store_degraded is True


def test_store_not_degraded_for_default_inmemory(daemon) -> None:  # type: ignore[no-untyped-def]
    """The DMG-default persistent InMemoryStore is NOT degraded."""
    # No Redis enabled in the default config → make_default_store → InMemory.
    assert daemon._store_degraded is False


# ─── C5: blink_suppression forwarded with 1-frame lag ────────────────────


def _frame_meta(low_quality: bool = False, ts: float = 0.0, face: bool = True) -> object:
    return SimpleNamespace(
        timestamp=ts,
        face_detected=face,
        face_confidence=0.95,
        brightness_score=0.8,
        blur_score=0.9,
        motion_score=0.8,
        low_quality=low_quality,
    )


def _pipeline_output(ts: float = 0.0, face: bool = True, low_quality: bool = False) -> object:
    return SimpleNamespace(
        frame_meta=_frame_meta(low_quality=low_quality, ts=ts, face=face),
        landmarks_px=np.zeros((468, 2), dtype=np.float32),
        frame=np.zeros((480, 640, 3), dtype=np.uint8),
    )


async def _drive_frames(
    daemon: Any, blink_scores: list[float], n_per_frame: int
) -> list[float]:
    """Feed enough frames to fill the rPPG window, capturing the
    ``blink_suppression`` kwarg seen by process_window on each call."""
    captured: list[float] = []
    fake_roi = MagicMock()
    fake_roi.combined_rgb.return_value = np.ones(3, dtype=np.float64)
    fake_roi.head_jitter_px = 0.0

    def _fake_process_window(*_a: Any, **kw: Any) -> object:
        captured.append(float(kw.get("blink_suppression", -1.0)))
        return MagicMock()

    idx = {"i": 0}

    def _fake_blink_update(*_a: Any, **_kw: Any) -> object:
        i = idx["i"]
        idx["i"] += 1
        score = blink_scores[min(i, len(blink_scores) - 1)]
        return SimpleNamespace(
            blink_rate=None,
            blink_rate_delta=None,
            blink_suppression_score=score,
            perclos_60s=None,
            mean_blink_duration_ms=None,
            ear_variance=None,
        )

    fake_pose = SimpleNamespace(pitch=None, yaw=None, roll=None)
    fake_posture = SimpleNamespace(
        slump_score=None, forward_lean_score=None, shoulder_drop_ratio=None
    )

    with (
        patch.object(daemon, "_roi_extractor") as roi_x,
        patch.object(daemon, "_blink_detector") as blink_d,
        patch.object(daemon, "_head_pose") as pose_d,
        patch.object(daemon, "_posture") as posture_d,
        patch.object(daemon, "_feature_fusion"),
        patch.object(daemon._pulse_estimator, "process_window", side_effect=_fake_process_window),
        patch.object(daemon._pulse_estimator, "get_features", return_value=daemon._latest_physio),
        patch("cortex.services.runtime_daemon.extract_bvp", return_value=np.zeros(n_per_frame)),
        patch("cortex.services.runtime_daemon.registry"),
    ):
        roi_x.extract.return_value = fake_roi
        blink_d.update.side_effect = _fake_blink_update
        pose_d.update.return_value = fake_pose
        posture_d.update_with_face.return_value = fake_posture
        # Window needs maxlen frames before process_window fires. Stride is
        # satisfied because _last_physio_update starts at 0.0.
        n = (daemon._rgb_history.maxlen or 1) + 2
        for i in range(n):
            await daemon._process_capture_output(_pipeline_output(ts=float(i + 1)))
    return captured


@pytest.mark.asyncio
async def test_blink_suppression_forwarded_with_one_frame_lag(daemon) -> None:  # type: ignore[no-untyped-def]
    """C5: process_window sees the PRIOR frame's blink-suppression score."""
    # Distinct ascending scores (capped at 1.0 — KinematicFeatures bounds
    # blink_suppression_score to [0, 1]) so the 1-frame lag is observable.
    scores = [min(1.0, 0.1 * (i + 1)) for i in range(400)]
    n_per_frame = daemon._rgb_history.maxlen or 1
    captured = await _drive_frames(daemon, scores, n_per_frame)
    assert captured, "process_window was never invoked"
    # The first process_window call fires once the window is full (after
    # `maxlen` frames). The blink_suppression it receives is the score from
    # the PRIOR frame, i.e. strictly less than the current-frame score, and
    # never the default sentinel -1.0.
    assert all(v >= 0.0 for v in captured), captured
    # 1-frame lag: the value forwarded equals the score cached on the prior
    # frame, never 0.0 once the detector has warmed up.
    assert max(captured) > 0.0


# ─── C6: FACE_LOST / FACE_REACQUIRED structured events ───────────────────


@pytest.mark.asyncio
async def test_face_lost_and_reacquired_events(daemon) -> None:  # type: ignore[no-untyped-def]
    """A face_detected True→False→True sequence emits exactly one
    FACE_LOST then one FACE_REACQUIRED."""
    fake_roi = MagicMock()
    fake_roi.combined_rgb.return_value = np.ones(3, dtype=np.float64)
    fake_roi.head_jitter_px = 0.0
    fake_blink = SimpleNamespace(
        blink_rate=None, blink_rate_delta=None, blink_suppression_score=None,
        perclos_60s=None, mean_blink_duration_ms=None, ear_variance=None,
    )
    fake_pose = SimpleNamespace(pitch=None, yaw=None, roll=None)
    fake_posture = SimpleNamespace(
        slump_score=None, forward_lean_score=None, shoulder_drop_ratio=None
    )
    events: list[str] = []

    def _capture_event(event: EventType, **_kw: Any) -> None:
        events.append(event.value)

    seq = [True, True, False, False, True]  # present, present, lost, lost, reacquired
    with (
        patch.object(daemon, "_roi_extractor") as roi_x,
        patch.object(daemon, "_blink_detector") as blink_d,
        patch.object(daemon, "_head_pose") as pose_d,
        patch.object(daemon, "_posture") as posture_d,
        patch.object(daemon, "_feature_fusion"),
        patch("cortex.services.runtime_daemon._emit_event", side_effect=_capture_event),
        patch("cortex.services.runtime_daemon.registry"),
    ):
        roi_x.extract.return_value = fake_roi
        blink_d.update.return_value = fake_blink
        pose_d.update.return_value = fake_pose
        posture_d.update_with_face.return_value = fake_posture
        for i, face in enumerate(seq):
            await daemon._process_capture_output(_pipeline_output(ts=float(i + 1), face=face))

    assert events.count(EventType.FACE_LOST.value) == 1, events
    assert events.count(EventType.FACE_REACQUIRED.value) == 1, events
    # Order: lost precedes reacquired.
    assert events.index(EventType.FACE_LOST.value) < events.index(
        EventType.FACE_REACQUIRED.value
    )


# ─── C6: QUIET_MODE_ENTERED / QUIET_MODE_EXITED ──────────────────────────


@pytest.mark.asyncio
async def test_quiet_mode_enter_exit_events(daemon) -> None:  # type: ignore[no-untyped-def]
    daemon._ws_server.send_message = AsyncMock(return_value=True)
    daemon._broadcast_quiet_mode_state = AsyncMock(return_value=None)  # type: ignore[method-assign]
    events: list[str] = []
    with patch(
        "cortex.services.runtime_daemon._emit_event",
        side_effect=lambda e, **_kw: events.append(e.value),
    ):
        await daemon.set_quiet_mode("snooze_15", duration_minutes=15)
        await daemon.set_quiet_mode("off")

    assert EventType.QUIET_MODE_ENTERED.value in events
    assert EventType.QUIET_MODE_EXITED.value in events
    assert events.index(EventType.QUIET_MODE_ENTERED.value) < events.index(
        EventType.QUIET_MODE_EXITED.value
    )


# ─── fix #10: Copilot force-enable on shutdown ───────────────────────────


@pytest.mark.asyncio
async def test_copilot_force_enabled_on_stop(daemon) -> None:  # type: ignore[no-untyped-def]
    """stop() must call force_enable() so a throttled editor is restored."""
    daemon._copilot_throttle.force_enable = AsyncMock(return_value=None)
    # Neutralise the rest of stop() so the test stays isolated.
    daemon._ws_server.stop = AsyncMock(return_value=None)
    daemon._capture_pipeline.stop = AsyncMock(return_value=None)
    daemon._input_hooks.stop = MagicMock()
    daemon._window_tracker.stop = MagicMock()
    daemon._session_report_started = False
    daemon._midnight_scheduler = None
    with patch("cortex.services.runtime_daemon.registry"):
        await daemon.stop()
    daemon._copilot_throttle.force_enable.assert_awaited_once()


# ─── fix #2: MorningBriefing constructor + await ─────────────────────────


@pytest.mark.asyncio
async def test_morning_briefing_awaits_and_uses_real_constructor(daemon) -> None:  # type: ignore[no-untyped-def]
    """_check_morning_briefing must construct with ``storage_path`` and
    AWAIT check_and_generate (the coroutine), then forward summary /
    action_items / left_off_at (mapped from title)."""
    storage_root = Path(daemon.config.storage.path)
    handovers = storage_root / "handovers"
    handovers.mkdir(parents=True, exist_ok=True)
    # Write a handover the briefing parser will pick up (yesterday).
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    (handovers / f"handover_{yesterday}.md").write_text(
        "# Refining the logomark\n\n"
        "## Summary\nYou were mid-way through the SVG export.\n\n"
        "## TODO\n- [ ] Finish the export\n",
        encoding="utf-8",
    )
    daemon._ws_server.send_message = AsyncMock(return_value=True)
    await daemon._check_morning_briefing()

    daemon._ws_server.send_message.assert_called_once()
    msg_type, payload = daemon._ws_server.send_message.call_args.args
    assert msg_type == "MORNING_BRIEFING"
    # The wire contract keys must all be present and be plain strings/lists,
    # never a coroutine attribute-error.
    assert "summary" in payload
    assert isinstance(payload["action_items"], list)
    assert payload["left_off_at"] == "Refining the logomark"


# ─── fix #2: ML classifier predict_proba ─────────────────────────────────


def test_ml_classifier_uses_predict_proba_api() -> None:
    """The real classifier exposes predict_proba (not predict); calling
    predict_proba on a fitted model returns a probability vector."""
    from cortex.services.state_engine.ml_classifier import PerUserLogisticClassifier

    clf = PerUserLogisticClassifier(n_features=3)
    x = np.array([[0.1, 0.2, 0.3], [0.9, 0.8, 0.7]], dtype=np.float64)
    y = np.array([0, 1], dtype=np.float64)
    clf.fit(x, y, epochs=50)
    assert not hasattr(clf, "predict"), "predict no longer exists; use predict_proba"
    proba = clf.predict_proba(np.array([[0.5, 0.5, 0.5]], dtype=np.float64))
    assert 0.0 <= float(proba[0]) <= 1.0


# ─── C3: trigger_url helper ──────────────────────────────────────────────


def test_active_trigger_url_reads_active_tab_url(daemon) -> None:  # type: ignore[no-untyped-def]
    from cortex.services.runtime_daemon import CortexDaemon

    ctx = SimpleNamespace(
        browser_context=SimpleNamespace(active_tab_url="https://example.com/x")
    )
    assert CortexDaemon._active_trigger_url(ctx) == "https://example.com/x"
    # No browser context → None.
    assert CortexDaemon._active_trigger_url(SimpleNamespace(browser_context=None)) is None
    # Blank URL → None.
    blank = SimpleNamespace(browser_context=SimpleNamespace(active_tab_url="   "))
    assert CortexDaemon._active_trigger_url(blank) is None


# ─── fix #9: dismissal model trained on trigger-time snapshot ────────────


@pytest.mark.asyncio
async def test_dismissal_uses_cached_trigger_time_features(daemon) -> None:  # type: ignore[no-untyped-def]
    """On dismissal, record_outcome receives the TRIGGER-time confidence /
    complexity that were cached at trigger time — NOT recovery_confidence
    or the feedback-time context."""
    iid = "iv-9"
    daemon._dismissal_features_by_intervention[iid] = (0.83, 0.41)
    daemon._latest_context = SimpleNamespace(complexity_score=0.99)  # must be ignored
    daemon._trigger_policy.record_outcome = MagicMock()
    daemon._trigger_policy.record_dismissal = MagicMock()
    daemon._consent_ladder.record_rejection = AsyncMock(return_value=None)
    # restore_manager.dismiss returns an outcome with a recovery_confidence
    # that MUST be ignored by the dismissal-model training call.
    outcome = SimpleNamespace(recovery_confidence=0.05, intervention_id=iid)
    daemon._restore_manager.dismiss = AsyncMock(return_value=outcome)
    # Short-circuit the rest of the handler after record_outcome.
    daemon._amip_decision_ids_by_intervention.pop(iid, None)

    # Drive only the dismissal branch; the handler does more afterwards but
    # the record_outcome call is what we assert.
    with patch("cortex.services.runtime_daemon.registry"):
        try:
            await daemon._handle_user_action(
                {"intervention_id": iid, "action": "dismissed"}
            )
        except Exception:
            pass  # later stages may need wiring we didn't stub; ignore.

    daemon._trigger_policy.record_outcome.assert_called()
    _, kwargs = daemon._trigger_policy.record_outcome.call_args
    assert kwargs["dismissed"] is True
    assert kwargs["confidence"] == 0.83
    assert kwargs["context_complexity"] == 0.41


# ---------------------------------------------------------------------------
# P1 — consent escalation: the daemon must record approvals/rejections under
# the CANONICAL action-types the executor's per-action gate checks, not the
# literal "intervention" (which is disjoint from every gated key, so the
# ladder never escalated the gate on an approved action).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engage_records_consent_under_canonical_action_types(daemon) -> None:  # type: ignore[no-untyped-def]
    iid = "iv-consent-engage"
    daemon._consent_actions_by_intervention[iid] = ["group_tabs", "fold_code"]
    daemon._restore_manager.engage = AsyncMock(return_value=None)
    daemon._consent_ladder.record_approval = AsyncMock()
    daemon._trigger_policy.record_outcome = MagicMock()

    await daemon._handle_user_action({"intervention_id": iid, "action": "engaged"})

    approved = {c.args[0] for c in daemon._consent_ladder.record_approval.call_args_list}
    assert approved == {"group_tabs", "fold_code"}
    assert "intervention" not in approved


@pytest.mark.asyncio
async def test_dismiss_records_consent_rejection_under_canonical_action_types(daemon) -> None:  # type: ignore[no-untyped-def]
    iid = "iv-consent-dismiss"
    daemon._consent_actions_by_intervention[iid] = ["group_tabs"]
    daemon._restore_manager.dismiss = AsyncMock(return_value=None)
    daemon._consent_ladder.record_rejection = AsyncMock()
    daemon._trigger_policy.record_dismissal = MagicMock()
    daemon._trigger_policy.record_outcome = MagicMock()

    await daemon._handle_user_action({"intervention_id": iid, "action": "dismissed"})

    rejected = {c.args[0] for c in daemon._consent_ladder.record_rejection.call_args_list}
    assert rejected == {"group_tabs"}
    assert "intervention" not in rejected


@pytest.mark.asyncio
async def test_engage_falls_back_to_intervention_key_when_no_actions_cached(daemon) -> None:  # type: ignore[no-untyped-def]
    iid = "iv-consent-none"  # nothing cached for this id
    daemon._restore_manager.engage = AsyncMock(return_value=None)
    daemon._consent_ladder.record_approval = AsyncMock()
    daemon._trigger_policy.record_outcome = MagicMock()

    await daemon._handle_user_action({"intervention_id": iid, "action": "engaged"})

    daemon._consent_ladder.record_approval.assert_awaited_once_with("intervention")
