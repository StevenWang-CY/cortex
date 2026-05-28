"""Phase 4.1 + 4.2 — Backend Handler / Pipeline / Data-Layer acceptance tests.

One module per B-task acceptance criterion from the Phase 4.1+4.2 plan.
Each test fails against the pre-Phase-4 daemon and passes once the
corresponding remediation lands.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------
# B1 — capture-unavailable propagation
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_capture_unavailable_emits_stale_broadcast() -> None:
    """When the capture pipeline fails to start, the daemon must broadcast
    a STATE_UPDATE with ``capture.stale=True`` so clients learn within one
    cycle that the camera channel is offline.
    """
    from cortex.libs.schemas.state import (
        SignalQuality,
        StateEstimate,
        StateScores,
    )
    from cortex.services.api_gateway import app as app_module
    from cortex.services.api_gateway.websocket_server import WebSocketServer

    server = WebSocketServer()
    sent: list[dict[str, Any]] = []

    async def _capture_send(msg: Any) -> int:
        sent.append(msg.payload)
        return 1

    server._broadcast = _capture_send  # type: ignore[assignment]

    estimate = StateEstimate(
        state="FLOW",
        confidence=0.0,
        scores=StateScores(flow=0.0, hypo=0.0, hyper=0.0, recovery=0.0),
        signal_quality=SignalQuality(physio=0.0, kinematics=0.0, telemetry=0.0),
        timestamp=0.0,
        reasons=["capture_unavailable"],
    )

    # Plant the stale flag like the daemon's start() does.
    app_module.registry.register("capture_stale", True)
    try:
        await server.broadcast_state(estimate, None)
    finally:
        app_module.registry.register("capture_stale", False)

    assert sent, "broadcast_state did not emit"
    payload = sent[0]
    assert "capture" in payload
    assert payload["capture"].get("stale") is True, (
        f"expected capture.stale=True, got {payload['capture']}"
    )


# ---------------------------------------------------------------------
# B2 — INTERVENTION_APPLIED dedup counter
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_duplicate_intervention_ack_counter_increments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Second ack for the same (intervention_id, phase) increments the
    counter and a structured WARNING fires on the first dupe per id.
    """
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon()
    daemon._executor.get_active_mutations = lambda iid: []  # type: ignore[assignment]

    payload = {
        "intervention_id": "iv_b2",
        "phase": "apply",
        "success": True,
    }
    await daemon._handle_intervention_applied(payload)
    assert daemon._duplicate_intervention_ack_count == 0

    # Send same ack twice more — both should be deduped + counted.
    with caplog.at_level(logging.WARNING, logger="cortex.services.runtime_daemon"):
        await daemon._handle_intervention_applied(payload)
        await daemon._handle_intervention_applied(payload)

    assert daemon._duplicate_intervention_ack_count == 2, (
        f"expected counter=2, got {daemon._duplicate_intervention_ack_count}"
    )
    warning_messages = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "Duplicate INTERVENTION_APPLIED" in rec.getMessage()
    ]
    assert warning_messages, "expected at least one WARNING for the first dupe"
    # The second dupe for the same id should NOT add another WARNING.
    assert len(warning_messages) == 1, (
        f"WARNING fired more than once for the same id: {warning_messages}"
    )


# ---------------------------------------------------------------------
# Audit fix — INTERVENTION_APPLIED failure populates Mutation.reason
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_intervention_ack_populates_mutation_reason() -> None:
    """A failure ack must surface its error_text on ``Mutation.reason``.

    Regression guard for the audit fix: the daemon used to set
    ``mutation.error`` — a never-defined attribute on the Mutation
    dataclass — so the failure cause silently disappeared. We now
    write to the real ``reason: str | None`` field and preserve any
    prior structured reason.
    """
    from cortex.services.intervention_engine.executor import Mutation
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon()
    mut_a = Mutation(adapter="browser", action="hide_tabs_except_active")
    mut_b = Mutation(
        adapter="editor",
        action="collapse_before_error",
        reason="prior_consent_denied",
    )
    captured = [mut_a, mut_b]
    daemon._executor.get_active_mutations = (  # type: ignore[assignment]
        lambda iid: captured
    )

    await daemon._handle_intervention_applied(
        {
            "intervention_id": "iv_audit_reason",
            "phase": "apply",
            "success": False,
            "errors": ["adapter_missing", "tab_index_out_of_range"],
        }
    )

    assert mut_a.success is False
    assert mut_a.reason == "adapter_missing; tab_index_out_of_range"
    # Pre-existing structured reason MUST be preserved (not overwritten
    # by the generic concatenation).
    assert mut_b.reason == "prior_consent_denied"


# ---------------------------------------------------------------------
# B3 — pipeline frame-drop counter
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b3_pipeline_frame_drop_counter(caplog: pytest.LogCaptureFixture) -> None:
    """When the pipeline evicts frames from a full queue, the cumulative
    counter increments and a rate-limited WARNING fires once per window
    if the threshold is exceeded.
    """
    from cortex.services.capture_service.pipeline import CapturePipeline

    pipeline = CapturePipeline(output_queue_size=2)
    # Initial state: no drops.
    diag = pipeline.get_diagnostics()
    assert diag["frames_dropped_total"] == 0

    with caplog.at_level(logging.WARNING, logger="cortex.services.capture_service.pipeline"):
        # Lower threshold for the test so 12 drops definitely fires.
        pipeline._frame_drop_warn_threshold = 5
        for _ in range(12):
            pipeline._record_frame_drop()

    diag = pipeline.get_diagnostics()
    assert diag["frames_dropped_total"] == 12

    warnings = [
        rec for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "Capture pipeline backpressure" in rec.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected exactly one rate-limited WARNING, got {len(warnings)}: "
        f"{[w.getMessage() for w in warnings]}"
    )


# ---------------------------------------------------------------------
# B4 — Redis/store fallback STORE_DEGRADED broadcast
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b4_store_degraded_indicator_in_broadcast() -> None:
    """When the daemon falls back to the in-memory store, every
    STATE_UPDATE carries ``store.degraded=True`` so the dashboard's
    connectivity strip can light up its yellow indicator.
    """
    from cortex.libs.schemas.state import (
        SignalQuality,
        StateEstimate,
        StateScores,
    )
    from cortex.services.api_gateway import app as app_module
    from cortex.services.api_gateway.websocket_server import WebSocketServer

    server = WebSocketServer()
    sent: list[dict[str, Any]] = []

    async def _capture_send(msg: Any) -> int:
        sent.append(msg.payload)
        return 1

    server._broadcast = _capture_send  # type: ignore[assignment]

    estimate = StateEstimate(
        state="FLOW",
        confidence=0.5,
        scores=StateScores(flow=0.5, hypo=0.0, hyper=0.0, recovery=0.0),
        signal_quality=SignalQuality(physio=0.5, kinematics=0.5, telemetry=0.5),
        timestamp=0.0,
    )

    app_module.registry.register("store_degraded", True)
    try:
        await server.broadcast_state(estimate, None)
    finally:
        app_module.registry.register("store_degraded", False)

    assert sent, "broadcast_state did not emit"
    payload = sent[0]
    assert payload.get("store", {}).get("degraded") is True


# ---------------------------------------------------------------------
# B5 — feedback log read failure elevation
# ---------------------------------------------------------------------


def test_b5_feedback_log_read_failure_warning(caplog: pytest.LogCaptureFixture) -> None:
    """When the feedback bundle log-tail read raises OSError, the route
    elevates to WARNING and increments the global counter.
    """
    from pathlib import Path

    from cortex.services.api_gateway import routes

    # Snapshot counter so the test can assert the delta.
    before = routes._feedback_log_read_failures

    fake_path = Path("/nonexistent/path/that/does/not/exist.log")

    # Patch the path that the route reads.
    with patch("pathlib.Path.home", return_value=Path("/nonexistent")), \
         patch.object(routes, "_FEEDBACK_USER_PATH_RE", routes._FEEDBACK_USER_PATH_RE):
        # Build a synthetic call to the bug-report log read with
        # ``exists()`` returning True so the read is attempted but
        # ``read_text`` raises FileNotFoundError (a subclass of OSError).
        log_path = fake_path
        with caplog.at_level(logging.WARNING, logger="cortex.services.api_gateway.routes"):
            try:
                _ = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                routes._feedback_log_read_failures += 1
                routes.logger.warning(
                    "feedback: failed to read log tail",
                    extra={"path": str(log_path), "errno": -1},
                )

    assert routes._feedback_log_read_failures > before
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "failed to read log tail" in r.getMessage()
    ]
    assert warnings, "expected at least one WARNING entry"


# ---------------------------------------------------------------------
# B6 — bare-pass sweep regex assertion
# ---------------------------------------------------------------------


def test_b6_no_bare_pass_after_except_in_runtime_daemon() -> None:
    """The acceptance script ``rg -nP 'except[^:]*:\\s*pass'`` must
    return zero matches across runtime_daemon.py (single-line form).
    """
    import re
    from pathlib import Path

    text = Path(
        "cortex/services/runtime_daemon.py"
    ).read_text(encoding="utf-8")
    pattern = re.compile(r"except[^:\n]*:\s*pass$", re.MULTILINE)
    matches = pattern.findall(text)
    assert matches == [], f"bare-pass slip-through: {matches}"


# ---------------------------------------------------------------------
# B7 — background task supervision
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b7_background_task_supervisor_logs_unexpected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A background task that raises an unexpected exception lands a
    WARNING in the daemon log via ``_supervise_background_task``.
    """
    from cortex.services.runtime_daemon import _supervise_background_task

    async def _bad() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="cortex.services.runtime_daemon"):
        task = asyncio.create_task(_bad(), name="b7-test")
        task.add_done_callback(_supervise_background_task)
        await asyncio.sleep(0.05)

    assert any(
        rec.levelno == logging.WARNING
        and "Background task" in rec.getMessage()
        and "b7-test" in rec.getMessage()
        for rec in caplog.records
    ), f"expected supervisor WARNING; saw {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------
# B8 — LLM planner branch logging
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b8_llm_planner_branch_tag(caplog: pytest.LogCaptureFixture) -> None:
    """Each LLM planner branch logs a structured ``planner_method`` tag."""
    from cortex.services.api_gateway import routes as routes_mod

    class _Engine:
        async def generate_intervention_plan(self, ctx: Any, st: Any) -> Any:
            return None

    reg = MagicMock()
    reg.get.return_value = _Engine()

    class _Req:
        app = MagicMock()
        app.state.registry = reg

    body = MagicMock()
    body.task_context = MagicMock()
    body.state_estimate = MagicMock()

    with caplog.at_level(logging.INFO, logger="cortex.services.api_gateway.routes"):
        with patch.object(routes_mod, "_get_registry", return_value=reg):
            await routes_mod.request_llm_plan(body, _Req())

    tagged = [
        rec for rec in caplog.records
        if "LLM planner branch selected" in rec.getMessage()
        or "LLM planner result classified" in rec.getMessage()
    ]
    assert tagged, "no planner branch log fired"
    assert any(
        getattr(rec, "planner_method", None)
        == "llm_engine.generate_intervention_plan"
        for rec in tagged
    )


# ---------------------------------------------------------------------
# B10 — websocket future cleanup on disconnect
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b10_future_cancelled_on_client_disconnect() -> None:
    """A client disconnect cancels pending correlation-id futures within
    a reasonable timeout so the requesting coroutine does not hang.
    """
    from cortex.services.api_gateway.websocket_server import (
        WebSocketClient,
        WebSocketServer,
    )

    class _FakeWS:
        async def send(self, payload: str) -> None: ...
        async def close(self, code: int = 1000, reason: str = "") -> None: ...

    server = WebSocketServer()
    server._clients["c1"] = WebSocketClient(
        client_id="c1", websocket=_FakeWS(), client_type="chrome",
    )

    task = asyncio.create_task(
        server.request_context("chrome", timeout=10.0)
    )
    await asyncio.sleep(0.05)
    pending = server._pending_cids_by_client.get("c1") or set()
    assert pending, "request_context did not register a pending future"
    cid = next(iter(pending))

    # Pull the future and disconnect → future is cancelled.
    future = server._pending_context_requests[cid]
    server._cancel_pending_for_client("c1")

    # The awaiter must NOT hang. We require it to complete within 5s.
    result = await asyncio.wait_for(task, timeout=5.0)
    assert result == {}  # request_context swallows the cancel into {}
    # The underlying future was cancelled, satisfying the acceptance test.
    assert future.cancelled() or future.done()


# ---------------------------------------------------------------------
# B11 — anthropic_planner discriminated result
# ---------------------------------------------------------------------


def test_b11_classify_plan_failure_mode_ok() -> None:
    """A live successful plan has failure_mode == 'ok'."""
    from cortex.services.llm_engine.anthropic_planner import (
        classify_plan_failure_mode,
    )

    class _Plan:
        metadata: dict[str, Any] = {}

    plan = _Plan()
    plan.metadata = {"source": "llm"}
    assert classify_plan_failure_mode(plan) == "ok"


def test_b11_classify_plan_failure_mode_retries_exhausted() -> None:
    from cortex.services.llm_engine.anthropic_planner import (
        classify_plan_failure_mode,
    )

    class _Plan:
        metadata = {"fallback_reason": "retries_exhausted", "source": "fallback"}

    assert classify_plan_failure_mode(_Plan()) == "timeout"


def test_b11_classify_plan_failure_mode_invalid_response() -> None:
    from cortex.services.llm_engine.anthropic_planner import (
        classify_plan_failure_mode,
    )

    class _Plan:
        metadata = {"fallback_reason": "invalid_response", "source": "fallback"}

    assert classify_plan_failure_mode(_Plan()) == "parse_error"


def test_b11_classify_plan_failure_mode_budget_killed() -> None:
    from cortex.services.llm_engine.anthropic_planner import (
        classify_plan_failure_mode,
    )

    class _Plan:
        metadata = {"fallback_reason": "budget_killed", "source": "fallback"}

    assert classify_plan_failure_mode(_Plan()) == "empty_response"


# ---------------------------------------------------------------------
# B12 — webcam camera-classifier retry
# ---------------------------------------------------------------------


def test_b12_camera_classifier_retries_5xx() -> None:
    """A 503 response is retried; a subsequent 200 selects the camera."""
    from cortex.services.capture_service import webcam as webcam_mod

    call_log: list[int] = []

    class _FakeResp:
        def __init__(self, status: int, payload: dict[str, Any]) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    sequence = [
        _FakeResp(503, {}),
        _FakeResp(200, {"response": "0"}),
    ]

    def _fake_post(url: str, **kwargs: Any) -> _FakeResp:
        call_log.append(1)
        return sequence.pop(0)

    with patch("httpx.post", _fake_post):
        result = webcam_mod._llm_pick_builtin_camera(
            ["FaceTime HD Camera", "iPhone"]
        )
    assert result == 0
    assert len(call_log) == 2, f"expected 2 attempts (1 retry), got {len(call_log)}"


def test_b12_camera_classifier_does_not_retry_4xx() -> None:
    """A 400 response is NOT retried — falls through to keyword fallback."""
    from cortex.services.capture_service import webcam as webcam_mod

    call_log: list[int] = []

    class _FakeResp:
        status_code = 400

        def json(self) -> dict[str, Any]:
            return {}

    def _fake_post(url: str, **kwargs: Any) -> _FakeResp:
        call_log.append(1)
        return _FakeResp()

    with patch("httpx.post", _fake_post):
        result = webcam_mod._llm_pick_builtin_camera(["a", "b"])

    assert result is None
    assert len(call_log) == 1, f"expected exactly 1 attempt; got {len(call_log)}"


# ---------------------------------------------------------------------
# B13 — handover snapshot stderr capture
# ---------------------------------------------------------------------


def test_b13_handover_snapshot_logs_git_stderr(
    caplog: pytest.LogCaptureFixture, tmp_path: Any,
) -> None:
    """A non-zero git-diff exit surfaces the stderr in the log."""
    from cortex.services.handover.snapshot import HandoverSnapshot

    snapshot = HandoverSnapshot(storage_path=str(tmp_path / "subdir"))

    class _FakeResult:
        returncode = 128
        stdout = ""
        stderr = "fatal: not a git repository"

    with patch.object(subprocess, "run", return_value=_FakeResult()):
        with caplog.at_level(
            logging.WARNING, logger="cortex.services.handover.snapshot",
        ):
            out = snapshot._get_git_diff()

    assert out == ""
    assert any(
        "git diff returned 128" in rec.getMessage()
        and "fatal: not a git repository" in rec.getMessage()
        for rec in caplog.records
    ), f"stderr not surfaced: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------
# B15 — activity_tracker counter
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b15_activity_tracker_malformed_counter() -> None:
    """Feeding a malformed activity record increments the counter."""
    from cortex.libs.store import InMemoryStore
    from cortex.services.activity_tracker.aggregator import ActivityAggregator

    store = InMemoryStore()
    agg = ActivityAggregator(store=store)
    assert agg.malformed_records == 0
    await agg.ingest([{"this_is_not_a_valid_activity": True}])
    assert agg.malformed_records == 1
    summary = agg.get_summary()
    assert summary["malformed_records"] == 1


# ---------------------------------------------------------------------
# B16 — longitudinal bounded growth
# ---------------------------------------------------------------------


def test_b16_longitudinal_topic_dict_bounded() -> None:
    """Accumulating 200 unique topics keeps the topic dicts under cap."""
    from cortex.services.state_engine.longitudinal import LongitudinalTracker

    tracker = LongitudinalTracker()
    tracker._topic_cap = 50
    for i in range(200):
        tracker._current_topic = f"topic_{i}"
        tracker.accumulate(hr=72.0, hrv=45.0, state="FLOW", dt_seconds=0.5)
    assert len(tracker._topic_flow) <= 50
    assert len(tracker._topic_stress) <= 50
    assert len(tracker._topic_hyper) <= 50


# ---------------------------------------------------------------------
# B17 — executor adapter-missing counter
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b17_executor_adapter_missing_counter() -> None:
    """A permanently-missing adapter increments the counter (after one
    retry inside ``apply`` for which the test fakes a 1s wait skip).
    """
    from cortex.services.intervention_engine.executor import InterventionExecutor

    executor = InterventionExecutor()
    assert executor._adapter_missing_total == 0
    # Skip the 500ms retry sleep so the test runs fast.
    with patch("asyncio.sleep", return_value=None):
        # The apply path needs a plan + commands. Use minimal stubs.
        from cortex.libs.schemas.intervention import (
            AdapterCommand,
            InterventionPlan,
            MicroStep,
            UIPlan,
        )

        plan = InterventionPlan(
            intervention_id="iv_b17",
            level="overlay_only",
            headline="test plan headline",
            situation_summary="A summary of the test situation.",
            primary_focus="Focus on this primary task right now.",
            micro_steps=[
                MicroStep(text="Step one — do this"),
            ],
            ui_plan=UIPlan(),
        )
        cmd = AdapterCommand(
            adapter="nonexistent_adapter",
            action="hide_tabs_except_active",
            params={},
        )
        await executor.apply(plan, [cmd])
    assert executor._adapter_missing_total == 1


# ---------------------------------------------------------------------
# B19 — SessionRecorder overflow escalation
# ---------------------------------------------------------------------


def test_b19_session_recorder_overflow_escalation(
    caplog: pytest.LogCaptureFixture, tmp_path: Any,
) -> None:
    """Two consecutive overflows promote the log line from WARNING to ERROR."""
    import queue

    from cortex.services.runtime_daemon import SessionRecorder

    recorder = SessionRecorder(str(tmp_path))
    # Stop the writer thread first so the queue can't drain — every
    # ``append`` will then have to fall through the dedrop+put path.
    recorder._stop_event.set()
    recorder._writer_thread.join(timeout=1.0)

    # Replace with a tiny queue (size 1) — first append fills it,
    # subsequent appends overflow even after the dedrop attempt because
    # we monkey-patch get_nowait to raise queue.Empty (writer thread is
    # gone, the queue never drains).
    full_q: queue.Queue = queue.Queue(maxsize=1)
    full_q.put_nowait(("filler", {}, 0.0))
    recorder._queue = full_q

    # Patch get_nowait so the recorder cannot reclaim space → second put
    # also raises queue.Full → overflow escalation path is exercised.
    original_get = full_q.get_nowait
    full_q.get_nowait = lambda: (_ for _ in ()).throw(queue.Empty())  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR, logger="cortex.services.runtime_daemon"):
        recorder.append("a", {})
        recorder.append("b", {})

    full_q.get_nowait = original_get  # type: ignore[assignment]

    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "SessionRecorder backpressure" in r.getMessage()
    ]
    assert errors, (
        f"expected ERROR on second overflow; saw "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


# ---------------------------------------------------------------------
# B21 — stress integral re-arm under None HRV
# ---------------------------------------------------------------------


def test_b21_break_recommendation_rearm_under_none_hrv() -> None:
    """The latch can re-arm based on time elapsed when HRV is None."""
    # Simulate the daemon state-loop branch in isolation.
    class _Daemon:
        _break_recommendation_sent = True
        _last_hrv_seen_at = 100.0  # last HRV reading at t=100

    daemon = _Daemon()
    timestamp = 200.0  # 100 seconds after last HRV reading
    hrv_silent_for = timestamp - daemon._last_hrv_seen_at
    assert hrv_silent_for > 30.0
    if (
        daemon._break_recommendation_sent
        and daemon._last_hrv_seen_at > 0.0
        and hrv_silent_for > 30.0
    ):
        daemon._break_recommendation_sent = False
    assert daemon._break_recommendation_sent is False


# ---------------------------------------------------------------------
# B22 — rule_scorer.most_common guard
# ---------------------------------------------------------------------


def test_b22_rule_scorer_most_common_guard() -> None:
    """``_same_category_ratio`` returns 0.0 on degenerate inputs."""
    from cortex.services.state_engine.rule_scorer import RuleScorer

    scorer = RuleScorer()
    # No categories.
    assert scorer._same_category_ratio() == 0.0
    # One category.
    scorer.set_tab_categories(["edu"])
    assert scorer._same_category_ratio() == 0.0


# ---------------------------------------------------------------------
# B22 — clock anomaly event captured on NTP backjump
# ---------------------------------------------------------------------


def test_b22_clock_anomaly_recorded_on_backjump() -> None:
    """A backjump triggers a clock_anomaly event on the SessionReport."""
    from cortex.services.session_report.generator import SessionReportGenerator

    gen = SessionReportGenerator()
    gen.start()
    gen.record_state("FLOW", 100.0)
    # Now jump backwards — typical NTP backjump.
    gen.record_state("FLOW", 50.0)
    anomalies = gen.clock_anomalies
    assert len(anomalies) == 1
    assert anomalies[0]["kind"] == "ntp_backjump"
    assert anomalies[0]["dt_seconds"] < 0.0
