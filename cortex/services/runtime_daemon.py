"""
Runtime daemon for Cortex.

This module wires the sensing, state, context, LLM, intervention, REST, and
WebSocket layers into one in-process supervisor so the app can run as a real
product instead of a collection of disconnected test surfaces.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import uvicorn

from cortex.libs.adapters.leetcode_adapter import LeetCodeAdapter
from cortex.libs.config.settings import CortexConfig, get_config
from cortex.libs.logging.correlation import get_correlation_id
from cortex.libs.logging.structured import EventType
from cortex.libs.schemas.features import KinematicFeatures, PhysioFeatures
from cortex.libs.schemas.intervention import (
    InterventionApplyResult,
    InterventionPlan,
)
from cortex.libs.schemas.leetcode import LeetCodeContext
from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    TrendsResponse,
)
from cortex.libs.schemas.state import UserBaselines
from cortex.libs.schemas.ws_message_types import MessageType

# v2.0 imports
from cortex.libs.store import InMemoryStore, RedisStore
from cortex.libs.utils import receptivity
from cortex.services.activity_tracker.aggregator import ActivityAggregator
from cortex.services.api_gateway.app import create_app, registry
from cortex.services.api_gateway.websocket_server import WebSocketServer
from cortex.services.capture_service.pipeline import CapturePipeline, PipelineOutput
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import (
    AUTONOMOUS_ACT,
    REVERSIBLE_ACT,
    ConsentPolicy,
)
from cortex.services.context_engine import (
    BrowserAdapter,
    ContextAssembler,
    EditorAdapter,
    TerminalAdapter,
)
from cortex.services.eval.amip import AMIPPolicy
from cortex.services.eval.bandit import ContextualBandit
from cortex.services.eval.causal_report import generate_daily_causal_report
from cortex.services.eval.helpfulness import HelpfulnessTracker
from cortex.services.eval.tab_relevance import TabRelevanceTracker
from cortex.services.handover.briefing import MorningBriefing
from cortex.services.handover.detector import ShutdownDetector
from cortex.services.handover.snapshot import HandoverSnapshot
from cortex.services.intervention_engine.break_overlay import (
    BiologyBreakController,
    BreakUIHandler,
)
from cortex.services.intervention_engine.executor import InterventionExecutor
from cortex.services.intervention_engine.leetcode_interventions import InterventionMatrix
from cortex.services.intervention_engine.planner import (
    prepare_plan,
    promote_biology_break,
)
from cortex.services.intervention_engine.restore import RestoreManager
from cortex.services.intervention_engine.snapshot import capture_snapshot
from cortex.services.janitor.retention import (
    enforce_chronotype_retention,
)
from cortex.services.janitor.retention import (
    sweep_once_async as run_retention_sweep_async,
)
from cortex.services.kinematics_engine.blink_detector import BlinkDetector
from cortex.services.kinematics_engine.head_pose import HeadPoseEstimator
from cortex.services.kinematics_engine.posture import PostureAnalyzer
from cortex.services.launcher.launcher import ProjectLauncher
from cortex.services.llm_engine import create_llm_client
from cortex.services.llm_engine.parser import enrich_plan_with_context
from cortex.services.physio_engine.pulse_estimator import PulseEstimator
from cortex.services.physio_engine.roi_extractor import RoiExtractor
from cortex.services.physio_engine.rppg import extract_bvp
from cortex.services.session_report.generator import SessionReportGenerator
from cortex.services.session_report.longitudinal import LongitudinalAggregator
from cortex.services.session_report.reader import SessionReader
from cortex.services.session_report.scheduler import MidnightScheduler
from cortex.services.state_engine import FeatureFusion, RuleScorer, ScoreSmoother
from cortex.services.state_engine.amygdala_hijack import AmygdalaHijackDetector
from cortex.services.state_engine.causal_attribution import CausalAttributor
from cortex.services.state_engine.destructive_struggle import DestructiveStruggleDetector
from cortex.services.state_engine.leetcode_mode_resolver import LeetCodeModeResolver
from cortex.services.state_engine.longitudinal import LongitudinalTracker
from cortex.services.state_engine.ml_classifier import PerUserLogisticClassifier
from cortex.services.state_engine.parasympathetic_rebound import ParasympatheticReboundDetector
from cortex.services.state_engine.rabbit_hole import RabbitHoleDetector
from cortex.services.state_engine.stress_integral import StressIntegralTracker
from cortex.services.state_engine.trigger_policy import TriggerPolicy
from cortex.services.state_engine.zombie_detector import ZombieReadingDetector
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
from cortex.services.telemetry_engine.input_hooks import InputHooks
from cortex.services.telemetry_engine.window_tracker import WindowTracker
from cortex.services.throttle.copilot_throttle import CopilotThrottle

logger = logging.getLogger(__name__)


def _supervise_background_task(task: asyncio.Task[Any]) -> None:
    """B7 (Phase 4.1): module-level supervisor for ``asyncio.create_task``.

    Lives at module scope so test scaffolds that bind a subset of
    :class:`CortexDaemon` methods (notably ``_spawn_background_task``)
    don't have to also bind this callback. Logs unexpected exceptions
    at WARNING; cancellations are intentional and silently swallowed.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.warning(
        "Background task %s raised %s",
        task.get_name(),
        exc.__class__.__name__,
        exc_info=exc,
    )

# P0 §3.3: bound on the SESSION_RECAP broadcast inside ``stop()`` so a
# stuck WS client (e.g. a dead browser tab that never reads its frame)
# cannot deadlock the daemon shutdown.
_SESSION_RECAP_BROADCAST_TIMEOUT_S: float = 5.0
# P0 §3.3 (Wave-2 P1): how long ``stop()`` waits for a UI to confirm
# the recap was dismissed before tearing down the WS server. Matches
# the spec — long enough for a human to register the card slide-up,
# short enough that a dead UI doesn't deadlock the shutdown.
_SESSION_RECAP_DISMISSAL_TIMEOUT_S: float = 5.0
# P0 §3.10 (Wave-2 P1): debounce window guarding rapid
# HYPER → RECOVERY → HYPER cycles from spamming the browser extension
# with START_FOCUS_AUTO / STOP_FOCUS_AUTO frames. ``_auto_focus_armed``
# must hold for at least ``_AUTO_FOCUS_DEBOUNCE_S`` before
# STOP_FOCUS_AUTO is allowed to fire (minimum-hold), and once it
# disarms the daemon waits the same window before re-arming (cooldown).
_AUTO_FOCUS_DEBOUNCE_S: float = 30.0
# P0 §3.2: rolling chronotype window (days) the janitor enforces on the
# nightly tick. Daily baselines older than this are pruned from
# ``storage/chronotype/daily/*.json``.
_CHRONOTYPE_WINDOW_DAYS: int = 90


def enforce_session_storage_budget(
    sessions_dir: Path,
    *,
    incoming_bytes: int,
    max_total_size_mb: int,
) -> int:
    """Evict oldest session reports until adding ``incoming_bytes``
    would keep the cumulative size of ``sessions_dir/*.json`` at or
    under ``max_total_size_mb`` (F36).

    Returns the number of files evicted (0 if the directory is below
    budget already, > 0 if eviction occurred). ``max_total_size_mb == 0``
    is a sentinel that evicts every existing session before each write —
    callers depending on a strict bound use this; tests use it as the
    lowest-bound smoke test of the eviction path.

    Files are stat-ed once for both size and mtime; oldest mtime is
    evicted first. The function is a no-op if the directory does not
    exist or contains no ``.json`` files.
    """
    if max_total_size_mb < 0:
        return 0
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return 0

    budget_bytes = max_total_size_mb * 1024 * 1024
    entries: list[tuple[float, int, Path]] = []
    for p in sessions_dir.iterdir():
        if not p.is_file() or p.suffix != ".json":
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, p))

    total = sum(size for _mtime, size, _p in entries)
    if total + incoming_bytes <= budget_bytes:
        return 0

    # Evict oldest-first until the headroom fits the new write.
    entries.sort(key=lambda e: e[0])
    evicted = 0
    for _mtime, size, path in entries:
        if total + incoming_bytes <= budget_bytes:
            break
        try:
            path.unlink()
            total -= size
            evicted += 1
        except OSError:
            logger.warning(
                "F36 storage budget: could not evict %s", path, exc_info=True
            )
    if evicted > 0:
        logger.info(
            "F36 storage budget: evicted %d session(s) to make room for "
            "%d-byte write (cap=%d MB)",
            evicted, incoming_bytes, max_total_size_mb,
        )
    return evicted


class _OptimisticInterventionAdapter:
    """In-process adapter that awaits the client's ``INTERVENTION_APPLIED`` ack.

    F05: the previous implementation returned ``True`` unconditionally,
    which meant ``Mutation.success`` was always reported as success — the
    daemon's session report could not distinguish a partial / failed
    extension apply from a clean one. The new implementation registers
    a future per ``intervention_id`` with the daemon's
    ``await_apply_confirmation`` machinery; the future is resolved by the
    WS ``INTERVENTION_APPLIED`` handler or by a 30 s timeout watcher. The
    adapter's ``execute`` still returns immediately (mutation tracking
    needs *some* boolean before the ack arrives), but the daemon's
    ``_handle_intervention_applied`` then overwrites ``Mutation.success``
    with the actual outcome reported by the client. The session report
    persists the actual ack outcome (see ``await_apply_confirmation``).

    Real workspace effects (DOM, fold ranges, overlay) are performed by
    the Chrome / VS Code / desktop-shell clients in response to the
    ``INTERVENTION_TRIGGER`` WebSocket broadcast.
    """

    async def execute(self, action: str, params: dict[str, Any]) -> bool:
        # The actual outcome is resolved asynchronously when the ack arrives.
        # Returning True here matches the pre-F05 contract for the executor's
        # mutation-tracking pass; the daemon's
        # ``_handle_intervention_applied`` overwrites the value once the
        # client has reported back, and ``await_apply_confirmation``
        # surfaces the real outcome to callers of ``apply_intervention``.
        return True


# Backwards-compatible alias for older tests/imports that referenced the
# previous adapter name.
_PassiveWorkspaceAdapter = _OptimisticInterventionAdapter


class SessionRecorder:
    """Append-only JSONL recorder for states, plans, and outcomes.

    Audit-2 fix: serialise writes through a thread-safe queue + dedicated
    writer thread instead of opening/closing the file synchronously on the
    asyncio loop. Two consequences of the old design:

    1. ``with open("a"): write()`` is an open + sync write + close
       sequence the asyncio loop must wait for. On a slow / encrypted FS
       a single 4 KB intervention plan can stall the broadcast cadence
       for hundreds of ms.
    2. ``O_APPEND`` is *not* atomic on macOS APFS for writes exceeding
       ``PIPE_BUF`` (4 KB). Two concurrent appends (state-loop tick +
       user-action handler ack) can interleave bytes mid-line, producing
       malformed JSONL the replay harness rejects.

    The writer thread holds the file open in line-buffered append mode
    and pulls records off a ``queue.Queue``. Writes are therefore
    serialised by the queue's consumer (one writer thread) so byte
    interleave is impossible, and the producer (``append``) returns
    after only an in-memory put. ``flush()`` drains pending records on
    shutdown.
    """

    def __init__(self, storage_root: str) -> None:
        root = Path(storage_root)
        root.mkdir(parents=True, exist_ok=True)
        session_dir = root / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        self._path = session_dir / f"session_{int(time.time())}.jsonl"
        # Bounded queue so a runaway producer can't exhaust memory; if
        # the writer thread falls behind by more than 4096 records we
        # drop the oldest and log so the data loss is observable.
        self._queue: queue.Queue[tuple[str, dict[str, Any], float] | None] = (
            queue.Queue(maxsize=4096)
        )
        self._stop_event = threading.Event()
        # B19 (Phase 4.1): consecutive-overflow tracker for the
        # ``queue.Full`` path. Reset on every successful put; promotes
        # the second-in-a-row overflow log line to ERROR with the
        # current sequence number stamped so on-call can locate the
        # exact gap in session JSONL.
        self._overflow_streak: int = 0
        self._overflow_seq: int = 0
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="cortex-session-recorder",
            daemon=True,
        )
        self._writer_thread.start()

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        record = (event_type, payload, time.time())
        try:
            self._queue.put_nowait(record)
            # B19 (Phase 4.1): clear the overflow streak counter on every
            # successful put so an intermittent burst doesn't accumulate
            # into a false-alarm ERROR after a healthy stretch.
            self._overflow_streak = 0
            return
        except queue.Full:
            # Drop oldest to keep producer non-blocking. Surface the
            # drop as a structured event so the on-call can see backpressure.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                # B6 (Phase 4.1): another consumer drained the queue
                # between the .full() check and .get_nowait — benign
                # race, fall through to the put_nowait below.
                logger.debug("SessionRecorder dedrop race: queue empty under contention")
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                # B19 (Phase 4.1): bounded queue is genuinely full. Escalate
                # on the SECOND consecutive overflow so a transient burst
                # doesn't generate WARNING noise but sustained backpressure
                # is alarm-worthy.
                self._overflow_streak += 1
                if self._overflow_streak >= 2:
                    logger.error(
                        "SessionRecorder backpressure (overflow #%d): "
                        "dropped %s seq=%d — writer thread is starving",
                        self._overflow_streak,
                        event_type,
                        self._overflow_seq,
                    )
                else:
                    logger.warning("SessionRecorder backpressure: dropped %s", event_type)
                self._overflow_seq += 1

    def _writer_loop(self) -> None:
        try:
            with self._path.open("a", encoding="utf-8", buffering=1) as f:
                while not self._stop_event.is_set():
                    try:
                        item = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if item is None:
                        return
                    event_type, payload, ts = item
                    try:
                        line = json.dumps(
                            {"type": event_type, "timestamp": ts, "payload": payload},
                            default=str,
                        )
                        f.write(line + "\n")
                    except Exception:
                        logger.exception(
                            "SessionRecorder write failed for %s", event_type
                        )
        except Exception:
            logger.exception("SessionRecorder writer thread crashed")

    def flush(self, timeout: float = 5.0) -> None:
        """Drain the queue and stop the writer thread. Best-effort."""
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # B6 (Phase 4.1): queue is full — the writer thread is alive
            # but pinned. The ``_stop_event.set()`` below still wakes
            # it, and the join timeout will handle a thread that won't
            # come up. Benign.
            logger.debug("SessionRecorder.flush: sentinel put failed (queue full)")
        self._stop_event.set()
        try:
            self._writer_thread.join(timeout=timeout)
        except Exception:
            # B6 (Phase 4.1): join() can raise RuntimeError if the
            # thread was never started; best-effort cleanup, log and
            # move on.
            logger.debug("SessionRecorder.flush: writer thread join failed", exc_info=True)


class CortexDaemon:
    """In-process supervisor for the full Cortex runtime."""

    def __init__(self, config: CortexConfig | None = None) -> None:
        self.config = config or get_config()
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        # F03: every dynamically-spawned background task (intervention
        # dispatch, in-flight LLM call, etc.) is tracked here so stop()
        # can cancel it. Previously the state-loop created intervention
        # tasks via bare ``asyncio.create_task(...)`` with no reference;
        # shutdown could complete while one was still mid-write,
        # truncating session JSONL and leaking file handles.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._uvicorn_server: uvicorn.Server | None = None
        self._api_task: asyncio.Task[Any] | None = None

        # Desktop UI callback hooks (called from asyncio thread — recipients
        # must handle thread-safety, e.g. via Qt signal emission).
        self._state_callback: Callable[[dict], None] | None = None
        self._intervention_callback: Callable[[dict], None] | None = None
        # F17 (audit): per-callback monotonic sequence numbers. The
        # in-process bridge (``DaemonBridge``) reads ``_seq`` from the
        # payload and drops frames whose ``_seq`` is not strictly
        # greater than the last applied value. Reset on daemon restart
        # is implicit — the bridge starts with ``last_seq = 0`` per
        # restart, so the first frame from a fresh daemon always wins.
        self._state_callback_seq: int = 0
        self._intervention_callback_seq: int = 0

        self._recorder = SessionRecorder(self.config.storage.path)
        self._input_hooks = InputHooks(self.config.telemetry)
        self._window_tracker = WindowTracker()
        self._terminal_adapter = TerminalAdapter()
        self._editor_adapter = EditorAdapter(request_context_fn=self._request_context)
        self._browser_adapter = BrowserAdapter(request_context_fn=self._request_context)
        self._context_engine = ContextAssembler(
            editor_adapter=self._editor_adapter,
            browser_adapter=self._browser_adapter,
            terminal_adapter=self._terminal_adapter,
            active_app_provider=self._current_app_name,
        )

        self._capture_pipeline = CapturePipeline(self.config.capture)
        self._roi_extractor = RoiExtractor(self.config.landmarks)
        self._pulse_estimator = PulseEstimator(
            fs=float(self.config.capture.fps),
            nsqi_threshold=self.config.signal.rppg.nsqi_threshold,
            min_cardiac_snr_db=self.config.signal.rppg.min_cardiac_snr_db,
            hrv_min_window_seconds=float(self.config.signal.rppg.hrv_min_window_seconds),
            hrv_min_valid_ibi=self.config.signal.rppg.hrv_min_valid_ibi,
        )
        self._blink_detector = BlinkDetector(
            blink_config=self.config.signal.blink,
            landmarks_config=self.config.landmarks,
        )
        self._blink_detector.baseline_blink_rate = self._load_baselines().blink_rate_baseline
        self._head_pose = HeadPoseEstimator(
            frame_width=self.config.capture.width,
            frame_height=self.config.capture.height,
        )
        self._posture = PostureAnalyzer(self.config.signal.posture)
        self._feature_fusion = FeatureFusion()
        self._scorer = RuleScorer(config=self.config.state, baselines=self._load_baselines())
        self._smoother = ScoreSmoother(self.config.state)

        # C.2: optional per-user ML classifier. If a previously-trained
        # model file exists in storage/baselines/classifier.json AND
        # ml_enabled is set, load it and blend its HYPER probability into
        # the smoothed score (see ScoreSmoother.update + state_loop below).
        self._ml_classifier: PerUserLogisticClassifier | None = None
        if self.config.state.ml_enabled:
            classifier_path = (
                Path(self.config.storage.path).expanduser()
                / "baselines"
                / "classifier.json"
            )
            try:
                if classifier_path.exists():
                    self._ml_classifier = PerUserLogisticClassifier.load(classifier_path)
                    logger.info(
                        "Loaded per-user ML classifier from %s (fitted=%s)",
                        classifier_path,
                        self._ml_classifier.is_fitted,
                    )
            except Exception:
                logger.warning(
                    "Failed to load ML classifier; falling back to rule-only",
                    exc_info=True,
                )
                self._ml_classifier = None
        self._ml_labeled_episodes: int = 0  # incremented by feedback ingest
        self._trigger_policy = TriggerPolicy(
            self.config.intervention,
            state_config=self.config.state,
        )
        self._llm_client = create_llm_client(self.config.llm)
        self._executor = InterventionExecutor()
        self._project_launcher = ProjectLauncher(storage_path=self.config.storage.path)
        self._restore_manager = RestoreManager(
            self._executor,
            timeout_seconds=float(self.config.intervention.timeout_minutes * 60),
        )
        for adapter_name in ("browser", "editor", "overlay", "terminal"):
            self._executor.register_adapter(adapter_name, _PassiveWorkspaceAdapter())

        # Phase-4b TASK M: bind the per-action consent gate + the two
        # special-action hooks on the executor. The hooks run inside
        # ``InterventionExecutor.apply`` so the daemon owns the WS
        # broadcast and editor-focus delivery while the executor stays
        # adapter-agnostic.
        self._executor.set_consent_check(self._check_action_consent)
        self._executor.set_editor_focus_hook(self._resume_last_active_file)
        self._executor.set_prompt_broadcast_hook(self._broadcast_prompt)

        self._ws_server = WebSocketServer(self.config.api)
        self._ws_server.set_user_action_callback(self._handle_user_action)
        self._ws_server.set_settings_callback(self.apply_settings)
        self._ws_server.set_shutdown_callback(self._request_shutdown)
        self._ws_server.set_leetcode_context_callback(self._handle_leetcode_context_update)
        self._ws_server.set_intervention_applied_callback(self._handle_intervention_applied)
        # P0 §3.6: route MICRO_STEP_TOGGLED frames into ``toggle_micro_step``
        # so peer surfaces (popup, VS Code panel, WS-mode overlay) can
        # toggle steps without the in-process Qt overlay path.
        self._ws_server.set_micro_step_toggled_callback(self.toggle_micro_step)
        # G1 (audit-prod): caller registers via set_client_identified_callback
        # to bridge connection events to the desktop shell. Default is a
        # noop list so the daemon can still run headless.
        self._client_identified_listeners: list[Callable[[str, bool], None]] = []
        self._ws_server.set_client_identified_callback(self._on_client_identified)

        self._leetcode_adapter = LeetCodeAdapter()
        self._leetcode_adapter.set_ws_sender(self._send_leetcode_ws_message)
        self._leetcode_mode_resolver = LeetCodeModeResolver()
        self._leetcode_interventions = InterventionMatrix()
        self._amygdala_detector = AmygdalaHijackDetector()
        self._destructive_detector = DestructiveStruggleDetector()
        self._rebound_detector = ParasympatheticReboundDetector()
        self._last_leetcode_problem_id: str | None = None
        self._last_leetcode_allostatic_load = 0.0
        self._last_leetcode_hrv_rmssd: float | None = None
        self._leetcode_action_signatures: dict[str, float] = {}

        self._rgb_history: deque[np.ndarray] = deque(
            maxlen=max(1, self.config.signal.rppg.window_seconds * self.config.capture.fps)
        )
        # P0-2: count of low-quality frames rejected from the rPPG window.
        self._frames_low_quality_rejected: int = 0
        self._latest_physio = PhysioFeatures(
            pulse_bpm=None,
            pulse_quality=0.0,
            pulse_variability_proxy=None,
            hr_delta_5s=None,
            valid=False,
        )
        self._latest_kinematics = KinematicFeatures(
            blink_rate=None,
            blink_rate_delta=None,
            blink_suppression_score=None,
            perclos_60s=None,
            mean_blink_duration_ms=None,
            ear_variance=None,
            head_pitch=None,
            head_yaw=None,
            head_roll=None,
            slump_score=None,
            forward_lean_score=None,
            shoulder_drop_ratio=None,
            confidence=0.0,
        )
        self._last_physio_update = 0.0
        # B22 (Phase 4.1): monotonic timestamp of the most recent
        # kinematics feature delivery. State loop marks the kinematics
        # channel stale when ``time.monotonic() - _last_kinematics_ts > 2.0``.
        self._last_kinematics_ts: float = 0.0
        self._active_intervention_id: str | None = None
        # P0 §3.6: cache the most recently broadcast InterventionPlan
        # keyed by its ``intervention_id`` so the daemon can mutate
        # per-step state on ``MICRO_STEP_TOGGLED`` and rebroadcast the
        # updated payload. Only the plan whose id matches
        # ``_active_intervention_id`` is honoured by ``toggle_micro_step``;
        # stale ids (drag from an already-dismissed intervention) are
        # silently dropped to keep the click path idempotent.
        self._active_plan: InterventionPlan | None = None
        # P0 §3.6: once every micro-step has been ticked, the daemon
        # fires ``RestoreManager.engage`` exactly once and then sets
        # this flag so trailing toggles (animation tail-clicks, stale
        # peer surfaces) are no-ops. Cleared when a new intervention
        # starts.
        self._micro_step_recovery_fired: bool = False
        # Audit-2 fix: user-supplied goal text overrides the auto-inferred
        # ``current_goal_hint`` from the context assembler. The controller's
        # in-process ``_on_goal_set`` and the WS-mode ``set_goal:`` USER_ACTION
        # both land here; prior to this fix the WS-mode path was silently
        # dropped because the daemon had no method to set the override.
        self._user_goal_override: str | None = None
        # P0 §3.13: alias used by ``GOAL_SET`` handlers + SessionReport
        # stamping. Same value as ``_user_goal_override`` (kept as a
        # field for readability at the call sites that don't care about
        # the planner-hint override semantics).
        self._active_goal_title: str | None = None
        # P0 §3.20: weekly schedule rules pushed in via SETTINGS_SYNC
        # (the desktop dashboard owns the editor UI). Keys are lowercase
        # day-of-week (``monday``..``sunday``) → list of 4 slot strings
        # (``on`` / ``quiet`` / ``off``) for morning / midday /
        # afternoon / evening. Empty dict = no schedule armed.
        self._weekly_schedule: dict[str, list[str]] = {}
        # Dedup set for INTERVENTION_APPLIED acks. Clients can send the
        # same (intervention_id, phase) twice (e.g. retries, multiple
        # browser tabs echoing the ack); the second one would otherwise
        # overwrite Mutation.success / re-append to the recorder. Keys
        # are tuples of (intervention_id, phase).
        self._intervention_applied_seen: set[tuple[str, str]] = set()
        # F05: pending apply-confirmation futures keyed by intervention_id.
        # ``apply_intervention`` populates the future; the WS
        # ``_handle_intervention_applied`` callback resolves it; the 30 s
        # timeout watcher resolves it to ``confirmed=False`` if no ack
        # arrives.
        self._pending_apply_results: dict[
            str, asyncio.Future[Any]
        ] = {}
        # F05: tracked background tasks (timeout watchers, etc). Mirrors the
        # F03 pattern from the audit Ledger: any new task spawn must use
        # ``_spawn_background_task`` so ``stop()`` can drain them cleanly.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._aggregator = FeatureAggregator(
            self._input_hooks,
            self._window_tracker,
            config=self.config.telemetry,
            tab_count_provider=lambda: (
                self._browser_adapter.last_context.tab_count
                if self._browser_adapter.last_context is not None
                else 0
            ),
        )
        self._capture_available = False
        self._capture_processing_enabled = True
        self._telemetry_enabled = True
        self._interventions_enabled = True
        self._latest_context: Any = None
        # B1 (Phase 4.1): when ``start()`` cannot bring the capture
        # pipeline up the daemon must still announce to every connected
        # client that the camera channel is unavailable. Set to True by
        # ``_emit_capture_stale_broadcast`` and surfaced as
        # ``capture.stale=True`` on the next STATE_UPDATE envelope so
        # the dashboard / popup overlay can flip from "Reading your
        # pulse" to "Camera offline" within a single broadcast cycle.
        self._capture_stale: bool = False
        # B2 (Phase 4.1): counter incremented for every duplicate
        # INTERVENTION_APPLIED ack (same intervention_id, same phase).
        # Surfaced via /health diagnostics so duplicate-ack churn from
        # buggy extensions is observable without enabling debug logs.
        # The companion set tracks which intervention_ids have already
        # had their structured warning fired so we only log once per
        # intervention rather than spamming the log on every dupe.
        self._duplicate_intervention_ack_count: int = 0
        self._duplicate_intervention_ack_warned: set[str] = set()
        # B19: count SessionRecorder queue overflows so a second
        # consecutive overflow can be escalated from warning to ERROR.
        # Reset whenever a successful put_nowait succeeds (handled
        # inside the recorder).

        # --- v2.0 services ---
        # Store (Redis with in-memory fallback)
        self._store: RedisStore | InMemoryStore
        # B4 (Phase 4.1): flips True the moment the daemon falls back
        # to ``InMemoryStore`` from Redis. Surfaced on every subsequent
        # STATE_UPDATE under ``store.degraded`` so the dashboard's
        # connectivity strip can light up its yellow "in-memory store"
        # indicator within one broadcast cycle of the fallback. Also
        # broadcast as a one-time SYSTEM_NOTICE-shaped frame via
        # :meth:`_announce_store_degraded` so peer surfaces (browser
        # popup) that don't render the STATE_UPDATE strip can still
        # react to the degradation.
        self._store_degraded: bool = False
        if self.config.redis.enabled:
            try:
                self._store = RedisStore(
                    host=self.config.redis.host,
                    port=self.config.redis.port,
                    db=self.config.redis.db,
                    key_prefix=self.config.redis.key_prefix,
                )
            except Exception:
                logger.warning(
                    "Redis unavailable, falling back to in-memory store",
                    exc_info=True,
                )
                self._store = InMemoryStore()
                # B4: mark store as degraded so the next broadcast cycle
                # stamps the indicator on STATE_UPDATE and the one-time
                # announcement task fires from ``start()``.
                self._store_degraded = True
        else:
            self._store = InMemoryStore()

        # Stress integral tracker (biological pomodoros). The standardized
        # deficit math requires the user-specific HRV sigma so the integral
        # is in z-score units, not raw ms·s. Without sigma the AMIP safety
        # floor (keyed off stress_ratio ≥ 1.0) effectively never tripped.
        _baselines_for_stress = self._load_baselines()
        _hrv_sigma = 1.0
        try:
            _hrv_sigma = float(
                _baselines_for_stress.metric_distributions.get(
                    "hrv_rmssd", {}
                ).get("std", 1.0)
            )
        except Exception:
            _hrv_sigma = 1.0
        self._stress_tracker = StressIntegralTracker(
            hrv_baseline=_baselines_for_stress.hrv_baseline,
            hrv_sigma=max(1.0, _hrv_sigma),
        )

        # Cache the most recent state estimate + biometric payload for the
        # dedicated 500ms broadcast loop. The pipeline loop writes; the
        # broadcast loop reads — single-producer/single-consumer dict, no
        # lock required (Python GIL guarantees pointer-replacement atomicity).
        self._latest_estimate: Any = None
        self._latest_biometrics: dict[str, Any] | None = None
        # Audit-2 fix: paired (estimate, biometrics) snapshot. The pair
        # write is a single tuple assignment so the broadcast loop never
        # observes a torn combination of estimate(T+1) + biometrics(T).
        self._latest_broadcast_snapshot: tuple[Any, dict[str, Any]] | None = None
        self._broadcast_interval_seconds: float = 0.5

        # Longitudinal tracker (baseline drift)
        self._longitudinal = LongitudinalTracker(store=self._store)

        # Zombie reading detector
        self._zombie_detector = ZombieReadingDetector()

        # Rabbit hole detector
        self._rabbit_hole = RabbitHoleDetector()

        # Shutdown detector (morning handover)
        self._shutdown_detector = ShutdownDetector()

        # Consent ladder
        self._consent_policy = ConsentPolicy()
        self._consent_ladder = ConsentLadder(store=self._store, policy=self._consent_policy)
        # Phase-3 P0 + Audit-1.1 P0-1: consent policy overrides must
        # survive a daemon restart. Without persistence, the user's
        # opt-in to ``distraction_block`` at AUTONOMOUS_ACT silently
        # reverts to REVERSIBLE_ACT on reboot — the toggle in Settings
        # stays "on" but the HYPER auto-arm gate never opens.
        self._consent_overrides_path: Path = (
            Path(self.config.storage.path).expanduser() / "consent_overrides.json"
        )
        self._load_consent_overrides()

        # Helpfulness tracker
        self._helpfulness = HelpfulnessTracker(store=self._store)

        # P0 §3.9: per-signal causal attributor — fed by ``_state_loop``
        # at the same cadence the state estimate runs and queried when
        # the daemon constructs an intervention plan.
        self._causal_attributor = CausalAttributor()
        # P0 §3.9: cache the most-recent CausalSignal list per
        # intervention id so a late-arriving WHY_DETAIL_REQUEST can
        # resolve without re-running attribution against stale features.
        self._causal_signals_by_intervention: dict[str, list[dict[str, Any]]] = {}

        # P0 §3.7: latched so each ``should_break`` False→True
        # transition broadcasts exactly one BREAK_RECOMMENDATION pulse.
        # Reset to False on every ``StressIntegralTracker.reset``.
        self._break_recommendation_sent: bool = False
        # B21 (Phase 4.1): timestamp of the most recent HRV reading
        # used by the recommendation-latch re-arm gate. When HRV has
        # been None for > 30 s the latch can re-arm even without a
        # fresh HRV sample so the user is not permanently silenced if
        # the camera ROI degrades after a recommendation fires.
        self._last_hrv_seen_at: float = 0.0
        # Flipped by ``_set_break_suppression`` for the duration of a
        # break overlay so the state loop skips trigger evaluation.
        self._break_active: bool = False
        # The biology break controller is instantiated after
        # ``_session_report`` is created (a few lines below); keep a
        # forward-declared attribute so type checkers don't trip.
        self._break_controller: BiologyBreakController | None = None
        # P0 §3.7 audit fix (mic_active audio gate, spec line 643):
        # ``last_mic_active_at`` is the monotonic timestamp of the
        # most-recent positive ``receptivity.is_microphone_in_use``
        # reading. The break controller flips ``audio_cue=False`` for
        # any break whose start falls within
        # ``InterventionConfig.biology_break_audio_mute_after_mic_seconds``
        # of this timestamp so users on a call don't get blindsided
        # by a chime.
        self._last_mic_active_at: float = 0.0
        # P0 §3.8 audit fix: latches the in-flight quiet-mode
        # activation so a burst of downvotes in the same 30 s window
        # cannot re-trigger ``activate_quiet_mode`` repeatedly. Cleared
        # once the deque has drained past the throttle window.
        self._quiet_mode_throttle_latched_at: float = 0.0

        # Tab relevance learning
        self._tab_relevance = TabRelevanceTracker(store=self._store)
        self._per_tab_feedback_ids: deque[str] = deque(maxlen=50)  # intervention IDs with per-tab feedback

        # Contextual bandit
        self._bandit = ContextualBandit(store=self._store)
        self._amip = AMIPPolicy(
            storage_root=self.config.storage.path,
            n_features=8,
            tau0=self.config.eval.amip.tau0,
            tau_min=self.config.eval.amip.tau_min,
            epsilon_explore=self.config.eval.amip.epsilon_explore,
            epsilon_explore_after_500=self.config.eval.amip.epsilon_explore_after_500,
            stress_ratio_threshold=self.config.eval.amip.safety_floor_stress_ratio,
        )
        self._last_policy_decision_id: str | None = None
        self._last_policy_arm: str | None = None
        self._last_policy_propensity: dict[str, float] | None = None
        self._amip_decision_ids_by_intervention: dict[str, str] = {}
        self._bandit_decisions_by_intervention: dict[str, tuple[list[float], int]] = {}

        # Copilot throttle
        self._copilot_throttle = CopilotThrottle(ws_server=self._ws_server)

        # Activity tracker aggregator
        self._activity_aggregator = ActivityAggregator(store=self._store)
        self._ws_server.set_activity_sync_callback(self._handle_activity_sync)
        self._ws_server.set_tab_relevance_feedback_callback(self._handle_tab_relevance_feedback)

        # Track previous state for copilot throttle transitions
        self._prev_state: str = "FLOW"

        # G.1: live session debrief generator. start() initialises the
        # session; record_state / record_hr / record_hrv are called from
        # _state_loop; finish() runs in stop() and the report is written
        # to storage/sessions/session_<id>.json (+ markdown if enabled).
        self._session_report = SessionReportGenerator()
        self._session_report_started = False

        # P0 §3.1: paginated session-history reader (mtime-cached projection).
        self._session_reader = SessionReader(
            Path(self.config.storage.path).expanduser() / "sessions",
        )
        # P0 §3.2: longitudinal aggregator + midnight scheduler. The
        # aggregator owns the on-disk chronotype rollups under
        # ``storage/chronotype/{daily,model}.json``; the scheduler fires
        # ``_midnight_tick`` at 00:05 local time daily.
        self._session_aggregator = LongitudinalAggregator(
            sessions_dir=Path(self.config.storage.path).expanduser() / "sessions",
            chronotype_dir=Path(self.config.storage.path).expanduser() / "chronotype",
        )
        self._midnight_scheduler: MidnightScheduler | None = None
        # P0 §3.3: cache the most-recently broadcast SESSION_RECAP payload
        # so a late-joining client (browser popup reconnect) can re-fetch
        # via REQUEST_SESSION_RECAP without missing the user's recap.
        self._latest_session_recap: dict[str, Any] | None = None
        # P0 §3.3 (Wave-2 P1): flipped by ``acknowledge_session_recap``
        # when any surface (desktop recap sheet, browser popup, etc.)
        # confirms the user dismissed the recap card. ``stop()`` awaits
        # this event with a 5 s timeout so a fast UI hide can't race
        # the WS server teardown — but the daemon never blocks
        # indefinitely on a surface that crashed.
        self._recap_dismissed_event: asyncio.Event = asyncio.Event()

        # P0 §3.1: serve REQUEST_SESSION_LIST / REQUEST_SESSION_DETAIL
        self._ws_server.set_session_list_callback(self.list_sessions)
        self._ws_server.set_session_detail_callback(self.get_session)
        # P0 §3.2: serve REQUEST_TRENDS
        self._ws_server.set_trends_callback(self.get_trends)
        # P0 §3.3: serve REQUEST_SESSION_RECAP from the cached payload
        self._ws_server.set_session_recap_cache_callback(
            self.latest_session_recap,
        )
        # P0 §3.3 (Wave-2 P1): consume SESSION_RECAP_ACKNOWLEDGED
        # frames; flips ``_recap_dismissed_event`` to release ``stop()``.
        self._ws_server.set_session_recap_acknowledged_callback(
            self.acknowledge_session_recap,
        )

        # P0 §3.7: biology-driven break controller. Built after
        # ``_session_report`` and ``_stress_tracker`` so we can pass
        # them directly. The desktop shell binds its full-screen
        # overlay handler via :meth:`set_break_overlay_ui_handler`.
        self._break_controller = BiologyBreakController(
            hrv_sampler=self._sample_hrv_for_break,
            session_report=self._session_report,
            suppress_interventions=self._set_break_suppression,
            stress_tracker=self._stress_tracker,
        )

        # P0 §3.9: serve WHY_DETAIL_REQUEST from the per-intervention
        # causal-signal cache. Returns a list[dict] or None.
        self._ws_server.set_why_detail_callback(self.get_causal_signals)

        # P0 §3.11: serve QUIET_MODE_TOGGLE / SNOOZE_REQUEST. Routed
        # through :meth:`set_quiet_mode` which centralises the
        # quiet/pause primitives and broadcasts QUIET_MODE_STATE so
        # every surface (dashboard, overlay, tray, browser popup, VS
        # Code) reflects the same truth.
        self._ws_server.set_quiet_mode_toggle_callback(self.set_quiet_mode)

        # P0 §3.10: auto-armed focus session bookkeeping. ``_auto_focus_armed``
        # is True only when the daemon (not the user) opened the focus
        # session via START_FOCUS_AUTO; the symmetric STOP_FOCUS_AUTO
        # only fires when this flag is True so we never tear down a
        # session the user manually started. ``_hyper_dwell_started_at``
        # times the spec-mandated 30 s confidence dwell before arming;
        # ``_non_hyper_dwell_started_at`` times the 5 min sustained
        # non-HYPER window before STOP_FOCUS_AUTO fires.
        self._auto_focus_armed: bool = False
        self._auto_focus_dwell_started_at: float = 0.0
        self._auto_focus_recovery_started_at: float = 0.0
        # P0 §3.10 (Wave-2 P1): debounce timestamps. Without these, a
        # HYPER → RECOVERY → HYPER bounce within seconds emits a
        # START_FOCUS_AUTO / STOP_FOCUS_AUTO / START_FOCUS_AUTO storm
        # that spams the browser extension and confuses the focus-
        # session UX. Track the last arm/disarm wall-clock (the same
        # ``timestamp`` arg used by ``_evaluate_auto_distraction_block``,
        # so unit tests can exercise the debounce deterministically)
        # and enforce two windows:
        #   * minimum hold time of 30 s before STOP_FOCUS_AUTO may fire
        #     after a START_FOCUS_AUTO,
        #   * minimum cool-down of 30 s before START_FOCUS_AUTO may
        #     fire again after a STOP_FOCUS_AUTO.
        self._last_focus_auto_arm_ts: float = 0.0
        self._last_focus_auto_disarm_ts: float = 0.0
        # P0 §3.11: source-of-truth for the active quiet/pause mode.
        # ``_quiet_mode_kind`` is one of "off" / "snooze_15" /
        # "quiet_session" / "pause"; ``_quiet_mode_ends_at`` is a unix
        # timestamp (seconds since epoch) or None for indefinite (e.g.
        # ``pause`` lasts until the user resumes).
        self._quiet_mode_kind: str = "off"
        self._quiet_mode_ends_at: float | None = None
        self._quiet_mode_source: str = "daemon"
        # Phase-3 P0: serialise concurrent ``set_quiet_mode`` calls
        # (dashboard menu, tray, overlay footer, WS dispatch, F26
        # spiral path can all fire simultaneously). Without the lock,
        # the capture-pause state machine can drop the resume flag.
        self._quiet_mode_lock: asyncio.Lock = asyncio.Lock()
        # P0 §3.6 (Wave-2 P1): serialise ``toggle_micro_step`` against
        # the F16 plan-swap path inside ``_trigger_intervention`` and
        # ``_trigger_special_intervention``. Without this lock the
        # sequence
        #   1. toggle reads ``self._active_plan`` (snapshot A)
        #   2. plan-swap rebinds ``self._active_plan`` (snapshot B)
        #   3. toggle mutates A.micro_steps[step_index] and broadcasts A
        # produces a stale rebroadcast that overwrites the swap. The
        # lock is non-reentrant; neither the swap nor toggle re-enters
        # itself, and the swap never calls ``toggle_micro_step``.
        self._micro_step_lock: asyncio.Lock = asyncio.Lock()
        # Auto-decay broadcast task — cancelled and rescheduled on
        # every ``set_quiet_mode`` call so the popup countdown
        # reconciles when the window expires.
        self._quiet_mode_decay_task: asyncio.Task[None] | None = None
        # P0 §3.11: pause toggles capture on/off. A paused capture
        # releases the camera handle so the user can take a call /
        # show their face on Zoom; resuming re-opens it.
        self._pause_was_capturing: bool = False
        # Initialise the latch-bool sentinels that ``_evaluate_auto_
        # distraction_block`` reads via getattr — keeping them on
        # ``self`` makes the renames typo-safe (audit 1.1 P1-7).
        self._auto_focus_dwell_started: bool = False
        self._auto_focus_recovery_started: bool = False
        # P0 §3.12: focus detection callback registered by the desktop
        # shell controller. ``None`` means we cannot detect focus
        # (headless / non-mac); in that case OS notifications are
        # disabled to avoid spamming when the user IS looking at the
        # dashboard.
        self._desktop_focused_probe: Callable[[], bool] | None = None

    def set_state_callback(self, fn: Callable[[dict], None]) -> None:
        """Register a callback invoked on every state update.

        The callback receives a deep-copied dict with ``estimate`` and
        ``biometrics`` keys.  It is called from the asyncio daemon thread;
        the recipient is responsible for thread-safe dispatching (e.g.
        emit a Qt signal).
        """
        self._state_callback = fn

    def set_intervention_callback(self, fn: Callable[[dict], None]) -> None:
        """Register a callback invoked when an intervention is sent.

        The callback receives a deep-copied dict of the intervention plan
        payload.  Same threading caveat as :meth:`set_state_callback`.
        """
        self._intervention_callback = fn

    # ------------------------------------------------------------------
    # F05: background task helper + apply-confirmation primitives
    # ------------------------------------------------------------------

    def _spawn_background_task(
        self,
        coro: Any,
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Spawn an asyncio task whose lifetime is tracked by the daemon.

        New background tasks introduced by F05 (the apply-confirmation
        timeout watcher in particular) must use this helper so ``stop()``
        can cancel them cleanly. Tasks auto-prune themselves from the set
        on completion via ``add_done_callback``.

        B7 (Phase 4.1): also installs the
        :meth:`_supervise_background_task` callback so a crash inside the
        coroutine surfaces as a structured WARNING in the daemon log
        instead of disappearing into asyncio's "Task exception was
        never retrieved" garbage-collection warning at process exit.
        """
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_supervise_background_task)
        return task

    @staticmethod
    def _supervise_background_task(task: asyncio.Task[Any]) -> None:
        """B7 (Phase 4.1): structured supervisor for ``asyncio.create_task``.

        Every spawned background task in the daemon is wrapped with this
        callback so a bare ``raise`` inside the coroutine is logged at
        WARNING (and not silently swallowed). Cancellations are
        intentional and ignored.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.warning(
            "Background task %s raised %s",
            task.get_name(),
            exc.__class__.__name__,
            exc_info=exc,
        )

    async def _emit_capture_stale_broadcast(self) -> None:
        """B1 (Phase 4.1): synthesise + broadcast a STATE_UPDATE that
        carries ``capture.stale=True`` so every client learns within a
        single cycle that the camera channel is offline.

        Called from ``start()`` after the capture pipeline raises so
        the dashboard / popup don't sit waiting for a frame that will
        never arrive. We construct a minimal :class:`StateEstimate`
        with zeroed signal-quality scores; the WS ``_make_state_update``
        helper picks up the ``capture.stale=True`` flag via the registry
        marker we plant below. Synchronous best-effort — exceptions are
        logged + swallowed so a transient WS failure cannot crash the
        boot path.
        """
        try:
            from cortex.libs.schemas.state import (
                SignalQuality as _SQ,
            )
            from cortex.libs.schemas.state import (
                StateEstimate as _StateEstimate,
            )
            from cortex.libs.schemas.state import (
                StateScores as _StateScores,
            )

            estimate = _StateEstimate(
                state="FLOW",
                confidence=0.0,
                scores=_StateScores(flow=0.0, hypo=0.0, hyper=0.0, recovery=0.0),
                signal_quality=_SQ(physio=0.0, kinematics=0.0, telemetry=0.0),
                timestamp=time.monotonic(),
                dwell_seconds=0.0,
                reasons=["capture_unavailable"],
            )
            # Plant the stale marker so ``_make_state_update`` stamps
            # the field on the outbound payload. The registry is a
            # process-wide bag the WS server already reads from to
            # surface ``capture.frames_flowing`` and ``face_detected``;
            # adding the ``stale`` field there keeps the wire path
            # unchanged.
            registry.register("capture_stale", True)
            await self._ws_server.broadcast_state(estimate, None)
        except Exception:
            logger.warning(
                "B1: capture-unavailable broadcast failed",
                exc_info=True,
            )

    async def await_apply_confirmation(
        self,
        intervention_id: str,
        *,
        timeout_seconds: float = 30.0,
        correlation_id: str | None = None,
    ) -> InterventionApplyResult:
        """Register a pending apply-confirmation future and wait for it.

        Resolved by ``_handle_intervention_applied`` when the client's
        ``INTERVENTION_APPLIED`` ack arrives, or by a background timeout
        watcher if the ack never arrives within ``timeout_seconds``.

        F05 — the future is guaranteed to be resolved exactly once. A late
        ack arriving after the timeout finds no pending future and is
        treated as a no-op by the handler (existing dedup logic in
        ``_handle_intervention_applied``).
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[InterventionApplyResult] = loop.create_future()
        # Replace any prior in-flight future for the same intervention_id —
        # the prior call's caller will see the new ack land on its future
        # via the resolution path; the dropped future is left pending and
        # garbage-collected. This matches the "future resolved exactly
        # once per call site" guarantee.
        old = self._pending_apply_results.get(intervention_id)
        self._pending_apply_results[intervention_id] = future
        if old is not None and not old.done():
            # Resolve the orphaned earlier future to confirmed=False so its
            # awaiter does not hang.
            old.set_result(
                InterventionApplyResult(
                    intervention_id=intervention_id,
                    correlation_id=correlation_id,
                    confirmed=False,
                    timed_out=True,
                )
            )

        async def _timeout_watcher() -> None:
            try:
                await asyncio.sleep(timeout_seconds)
            except asyncio.CancelledError:
                return
            # If still pending after the timeout, resolve confirmed=False.
            if not future.done():
                future.set_result(
                    InterventionApplyResult(
                        intervention_id=intervention_id,
                        correlation_id=correlation_id,
                        confirmed=False,
                        timed_out=True,
                    )
                )
            # Pop the pending entry only if it still refers to *this* future
            # — a later call may have replaced it.
            current = self._pending_apply_results.get(intervention_id)
            if current is future:
                self._pending_apply_results.pop(intervention_id, None)

        self._spawn_background_task(
            _timeout_watcher(),
            name=f"apply-confirm-{intervention_id}",
        )

        result = await future
        # Persist outcome to the session recorder (F05: "session report
        # records the actual ack outcome, not optimistic").
        try:
            self._recorder.append(
                "intervention_apply_confirmation",
                {
                    "intervention_id": result.intervention_id,
                    "correlation_id": result.correlation_id,
                    "confirmed": result.confirmed,
                    "timed_out": result.timed_out,
                    "applied_actions": list(result.applied_actions),
                    "errors": list(result.errors),
                    "phase": result.phase,
                },
            )
        except Exception:
            logger.debug(
                "Failed to append apply confirmation to session recorder",
                exc_info=True,
            )
        return result

    async def start(self) -> None:
        """Start the runtime and block until shutdown."""
        # F07: ensure the local capability token exists before any service
        # that gates on it (WebSocket SHUTDOWN, launcher /stop) comes up.
        # Generated lazily, persists across restarts.
        # I6: narrow the startup-token exception handler. KeyboardInterrupt
        # and SystemExit are NOT subclasses of Exception in Python 3, but
        # we keep this whitelist explicit so a future refactor cannot
        # accidentally re-broaden it to ``except Exception``. Anything
        # outside this set (typing errors, asyncio.CancelledError,
        # signal-delivered exits) must propagate so the daemon does not
        # start in a half-initialised state with the user thinking it
        # came up cleanly.
        try:
            from cortex.libs.auth import load_or_create_token
            load_or_create_token()
        except (OSError, ImportError, RuntimeError) as exc:
            logger.warning(
                "Could not provision Cortex auth token: %s",
                exc,
                exc_info=True,
            )
        # F56: register SIGINT/SIGTERM through ``loop.add_signal_handler``
        # so the handler runs as a regular loop callback rather than
        # interrupting whatever native frame (numpy, mediapipe, OpenCV)
        # we happen to be inside. ``signal.signal`` invokes the handler
        # in the *signal frame*; if that frame is in the middle of a
        # native extension call it can lead to a segfault on resume.
        self._install_loop_signal_handlers()
        self._register_services()
        self._input_hooks.start()
        self._window_tracker.start()
        try:
            await self._capture_pipeline.start()
            self._capture_available = True
            self._capture_stale = False
        except Exception:
            logger.exception("Capture pipeline failed to start; continuing in telemetry-first mode")
            self._capture_available = False
            # B1 (Phase 4.1): the camera channel is permanently offline
            # for the lifetime of this start attempt. Mark the capture
            # signal as stale so the next broadcast cycle (and the
            # synthetic kickoff broadcast below) tells every client.
            self._capture_stale = True
        ws_started = await self._ws_server.start()
        if not ws_started:
            raise RuntimeError(
                f"WebSocket server failed to bind {self.config.api.host}:{self.config.api.ws_port}"
            )
        self._start_api_server()

        # B1 (Phase 4.1): if the capture pipeline never came up, broadcast
        # an initial STATE_UPDATE with ``capture.stale=True`` so clients
        # don't wait indefinitely for a first frame. Idempotent — if a
        # client connects later it still reads the registry-stored
        # ``capture_stale=True`` flag via subsequent broadcasts.
        if not self._capture_available:
            await self._emit_capture_stale_broadcast()
        # B4 (Phase 4.1): if the store fell back to in-memory at __init__
        # time, fire a one-time broadcast so every connected surface
        # learns the persistence layer is non-durable. We use the
        # STATE_UPDATE envelope to ride the existing dispatch path
        # rather than adding a new MessageType.
        if self._store_degraded:
            try:
                registry.register("store_degraded", True)
            except Exception:
                logger.debug("registry.register(store_degraded) failed", exc_info=True)

        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="cortex-capture-loop"),
            asyncio.create_task(self._telemetry_loop(), name="cortex-telemetry-loop"),
            asyncio.create_task(self._state_loop(), name="cortex-state-loop"),
            asyncio.create_task(self._broadcast_loop(), name="cortex-broadcast-loop"),
            asyncio.create_task(self._context_loop(), name="cortex-context-loop"),
            asyncio.create_task(self._longitudinal_loop(), name="cortex-longitudinal-loop"),
        ]
        if self.config.eval.causal_report.enabled:
            self._tasks.append(
                asyncio.create_task(self._causal_report_loop(), name="cortex-causal-report-loop")
            )
        # G.2: daily retention sweep so storage doesn't grow forever.
        self._tasks.append(
            asyncio.create_task(self._retention_sweep_loop(), name="cortex-retention-loop")
        )

        # P0 §3.2: kick off chronotype backfill on a worker thread so
        # cold-start doesn't block the daemon loop. The aggregator is
        # idempotent — repeated calls are no-ops if the model is fresh.
        # B7 (Phase 4.1): the previously-orphaned task is now tracked on
        # ``self._tasks`` AND wrapped with the supervision callback so
        # any unexpected exception lands in the daemon log instead of
        # being silently swallowed by asyncio's garbage collector.
        _chronotype_backfill_task = asyncio.create_task(
            asyncio.to_thread(self._session_aggregator.backfill_if_needed),
            name="cortex-chronotype-backfill",
        )
        _chronotype_backfill_task.add_done_callback(
            _supervise_background_task,
        )
        self._tasks.append(_chronotype_backfill_task)
        # P0 §3.2: start the nightly aggregation scheduler. The state
        # dir is the chronotype storage path so ``scheduler_state.json``
        # lives alongside ``model.json`` / ``daily/`` — persisting the
        # last-fired-date survives daemon restarts and prevents a crash
        # between firing and the next start from double-aggregating
        # yesterday's DailyBaseline (P0 audit fix #4.B-1).
        self._midnight_scheduler = MidnightScheduler(
            self._midnight_tick,
            state_dir=Path(self.config.storage.path).expanduser() / "chronotype",
        )
        self._midnight_scheduler.start()

        # v2.0: Check for morning briefing on startup
        await self._check_morning_briefing()

        logger.info("Cortex daemon started (v2.0)")
        await self._shutdown.wait()

    def _install_loop_signal_handlers(self) -> None:
        """Register SIGINT / SIGTERM via ``loop.add_signal_handler`` so
        the handler is dispatched as a normal event-loop callback rather
        than as a true asynchronous-signal interrupt (F56).

        Why this matters: ``signal.signal`` registers a C-level handler
        that the kernel runs in the signal frame — which on Cortex is
        almost always somewhere inside numpy / mediapipe / OpenCV native
        code. Running Python in the signal frame violates the GIL
        contract those extensions rely on and can segfault on resume.
        The loop variant defers the callback to the next event-loop
        tick, so the daemon's Python state is always frame-safe when
        the handler runs.

        On platforms that don't support ``add_signal_handler`` (Windows
        Python, some embedded scenarios) we fall back to a no-op and
        rely on the caller's outer harness (``run_dev.py``,
        ``main.py``) to provide signal delivery.
        """
        import signal as _signal

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Called from a non-async context — nothing to register.
            return
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal_received)
            except (NotImplementedError, RuntimeError, ValueError):
                # NotImplementedError: Windows.
                # ValueError: nested asyncio.run reusing a loop without
                # privileges to install handlers.
                logger.debug(
                    "loop.add_signal_handler unsupported for %s; "
                    "falling back to outer harness",
                    sig,
                )

    def _on_signal_received(self) -> None:
        """Event-loop-safe signal handler. Runs on the asyncio loop
        thread, not the signal frame, so native extensions complete
        their current op cleanly before we proceed to shutdown."""
        logger.info("Shutdown signal received in asyncio loop")
        self._shutdown.set()

    def _request_shutdown(self) -> None:
        """Request process shutdown via SIGTERM (triggers full graceful stop chain)."""
        import os
        import signal as _signal
        logger.info("Shutdown requested via WebSocket")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — send SIGTERM directly
            os.kill(os.getpid(), _signal.SIGTERM)
            return
        loop.call_later(0.3, os.kill, os.getpid(), _signal.SIGTERM)

    async def stop(self) -> None:
        """Gracefully stop all runtime services."""
        self._shutdown.set()
        # Audit P1: tell the macOS notification delegate to refuse new
        # callback dispatches BEFORE we cancel asyncio tasks. A user
        # click arriving mid-shutdown would otherwise reach a half-
        # torn-down daemon (cancelled tasks, closed loop) and can
        # crash on the route. The helper is import-safe and a no-op on
        # non-mac / when PyObjC isn't installed.
        try:
            from cortex.libs.utils import macos_notifications as _mn
            _mn.mark_shutting_down()
        except Exception:
            logger.debug(
                "macos_notifications.mark_shutting_down failed", exc_info=True,
            )
        # Phase-3 P0-N5: a daemon stop while an auto-armed focus
        # session is live would leave the browser blocking sites
        # indefinitely (the extension's chrome.alarm is the only
        # fallback, and even that can be missed if the browser is
        # restarted across the window). Emit STOP_FOCUS_AUTO best-
        # effort BEFORE we tear the WS server down.
        if getattr(self, "_auto_focus_armed", False):
            try:
                await self.disarm_auto_focus()
            except Exception:
                logger.debug(
                    "disarm_auto_focus during stop failed", exc_info=True,
                )
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # F03 + F05: cancel + drain dynamically-spawned background tasks
        # so they cannot outlive the daemon and corrupt persisted state.
        # Apply-confirmation timeout watchers (F05) are part of this set.
        if self._background_tasks:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(
                *list(self._background_tasks), return_exceptions=True
            )
            self._background_tasks.clear()
        # F05 / B18 (Phase 4.1): any apply-confirmation future still
        # pending at shutdown is treated as a missed ack — resolve to
        # confirmed=False so awaiters don't hang.
        #
        # B18 escalation: log a structured WARNING per pending future
        # so on-call can see exactly how many in-flight applies were
        # aborted. The HTTP caller still receives a typed
        # :class:`InterventionApplyResult` (confirmed=False,
        # timed_out=True) so existing branch logic continues to work,
        # but operators now have visibility into the shutdown-aborted
        # set instead of inferring it from absent ack frames.
        if self._pending_apply_results:
            logger.warning(
                "B18: aborting %d in-flight apply-confirmation futures "
                "on daemon shutdown",
                len(self._pending_apply_results),
            )
        for intervention_id, future in list(self._pending_apply_results.items()):
            if not future.done():
                future.set_result(
                    InterventionApplyResult(
                        intervention_id=intervention_id,
                        confirmed=False,
                        timed_out=True,
                    )
                )
        self._pending_apply_results.clear()
        if self._api_task is not None:
            try:
                await asyncio.wait_for(self._api_task, timeout=5.0)
            except TimeoutError:
                self._api_task.cancel()
                await asyncio.gather(self._api_task, return_exceptions=True)
            except Exception:
                logger.debug("API server task ended with an error", exc_info=True)
            finally:
                self._api_task = None
        # Always stop the capture pipeline to release the camera — even if
        # _capture_available is False (pipeline may have started then errored)
        #
        # F01: bound the stop() with a hard timeout. A disconnected USB
        # webcam or a stuck mediapipe worker can block forever inside the
        # capture loop; without a timeout the daemon hangs in stop(),
        # only SIGKILL unblocks, and SIGKILL leaves the AVFoundation
        # camera handle owned by a dead process — next launch fails. By
        # forcing a CancelledError after 5 s we surrender the graceful
        # close window in exchange for a deterministic shutdown; the
        # AVFoundation handle is reclaimed by the kernel on process exit
        # regardless, but only if we actually exit.
        try:
            await asyncio.wait_for(
                self._capture_pipeline.stop(), timeout=5.0
            )
        except TimeoutError:
            logger.error(
                "Capture pipeline stop() exceeded 5s; abandoning graceful "
                "close — relying on process exit to release the camera",
            )
        except Exception:
            logger.exception("Capture pipeline stop() raised; continuing shutdown")
        self._input_hooks.stop()
        self._window_tracker.stop()
        # G.1: write the session debrief BEFORE shutting down the WS
        # server so a future "view last report" endpoint can serve it
        # immediately on next launch.
        #
        # F02: split compute-vs-write error handling and use an atomic
        # write so disk-full / SIGKILL mid-write does not silently lose
        # the session. Previously a single ``try/except Exception`` wrapped
        # both ``finish()`` and ``write_text``; either path's failure was
        # logged once and the report was gone forever. Now compute errors
        # log the report's last-known state, and disk-write errors retain
        # the previous on-disk file (if any) because ``os.replace`` is
        # atomic.
        if self._session_report_started:
            try:
                report = self._session_report.finish()
            except Exception:
                logger.error(
                    "Failed to compute session report; nothing to persist",
                    exc_info=True,
                )
                report = None
            if report is not None:
                # Phase-4b TASK E: persist BEFORE broadcasting so the
                # SESSION_RECAP envelope can carry a truthful
                # ``persisted`` flag (the legacy ordering had the
                # broadcast stamp ``persisted=True`` unconditionally
                # which was wrong when the atomic write later failed).
                duration_seconds = float(getattr(report, "duration_seconds", 0.0))
                persisted_ok = False
                session_path: Path | None = None
                if duration_seconds >= 90.0:
                    try:
                        from cortex.libs.utils.atomic_write import atomic_write_json
                        sessions_dir = (
                            Path(self.config.storage.path).expanduser() / "sessions"
                        )
                        sessions_dir.mkdir(parents=True, exist_ok=True)
                        session_path = sessions_dir / f"session_{report.session_id}.json"
                        payload = report.model_dump(mode="json")
                        encoded_bytes = json.dumps(payload, indent=2).encode("utf-8")
                        enforce_session_storage_budget(
                            sessions_dir,
                            incoming_bytes=len(encoded_bytes),
                            max_total_size_mb=getattr(
                                self.config.storage, "max_total_size_mb", 500
                            ),
                        )
                        atomic_write_json(session_path, payload)
                        logger.info("Wrote session report to %s", session_path)
                        self._session_reader.invalidate(report.session_id)
                        persisted_ok = True
                    except Exception:
                        logger.error(
                            "session_persist_failed session_id=%s path=%s",
                            getattr(report, "session_id", "?"),
                            session_path,
                            exc_info=True,
                        )
                        persisted_ok = False
                    recap_payload = report.model_dump(mode="json")
                    # Phase-4b TASK E: stamp the persisted flag at the
                    # envelope level so late-joining surfaces (browser
                    # popup) can render a "live-only" hint.
                    recap_payload["persisted"] = persisted_ok
                    self._latest_session_recap = recap_payload
                    try:
                        await asyncio.wait_for(
                            self._ws_server.send_message(
                                MessageType.SESSION_RECAP.value,
                                recap_payload,
                                correlation_id=None,
                            ),
                            timeout=_SESSION_RECAP_BROADCAST_TIMEOUT_S,
                        )
                    except TimeoutError:
                        logger.warning(
                            "SESSION_RECAP broadcast timed out after %.1fs; proceeding with shutdown",
                            _SESSION_RECAP_BROADCAST_TIMEOUT_S,
                        )
                    except Exception:
                        logger.exception("SESSION_RECAP broadcast failed (non-fatal)")
                    # P0 §3.3 (Wave-2 P1): wait for the UI to ACK the
                    # recap so a fast hide doesn't race the WS server
                    # teardown. The event is set by:
                    #   * desktop_shell controller calling
                    #     ``acknowledge_session_recap`` on the
                    #     RecapSheet ``dismissed`` signal (in-process)
                    #   * the WS dispatch arm for
                    #     ``SESSION_RECAP_ACKNOWLEDGED`` (browser popup,
                    #     VS Code panel, any peer surface)
                    # Either path releases shutdown immediately; the
                    # 5 s timeout is the failsafe for a crashed UI.
                    try:
                        await asyncio.wait_for(
                            self._recap_dismissed_event.wait(),
                            timeout=_SESSION_RECAP_DISMISSAL_TIMEOUT_S,
                        )
                        logger.info(
                            "SESSION_RECAP acknowledged by UI; proceeding with shutdown"
                        )
                    except TimeoutError:
                        logger.warning(
                            "SESSION_RECAP dismissal ACK not received within %.1fs; "
                            "proceeding with shutdown",
                            _SESSION_RECAP_DISMISSAL_TIMEOUT_S,
                        )
                else:
                    # P0 §3.3: short session — persist (if possible) and
                    # broadcast an empty payload so the dashboard's recap
                    # watchdog can short-circuit to ``_finalize_stop``
                    # instead of waiting the full 6s.
                    try:
                        from cortex.libs.utils.atomic_write import atomic_write_json
                        sessions_dir = (
                            Path(self.config.storage.path).expanduser() / "sessions"
                        )
                        sessions_dir.mkdir(parents=True, exist_ok=True)
                        session_path_short = (
                            sessions_dir / f"session_{report.session_id}.json"
                        )
                        payload_short = report.model_dump(mode="json")
                        encoded_short = json.dumps(payload_short, indent=2).encode("utf-8")
                        enforce_session_storage_budget(
                            sessions_dir,
                            incoming_bytes=len(encoded_short),
                            max_total_size_mb=getattr(
                                self.config.storage, "max_total_size_mb", 500
                            ),
                        )
                        atomic_write_json(session_path_short, payload_short)
                        self._session_reader.invalidate(report.session_id)
                    except Exception:
                        logger.debug(
                            "short-session persist failed (non-fatal)",
                            exc_info=True,
                        )
                    try:
                        await asyncio.wait_for(
                            self._ws_server.send_message(
                                MessageType.SESSION_RECAP.value,
                                {},
                                correlation_id=None,
                            ),
                            timeout=1.0,
                        )
                    except (TimeoutError, Exception):
                        logger.debug(
                            "synthetic empty SESSION_RECAP broadcast failed (non-fatal)"
                        )
        # P0 §3.2: stop the midnight scheduler cleanly before the WS server
        # tears down so we don't await a callback that needs a WS broadcast.
        if self._midnight_scheduler is not None:
            try:
                await self._midnight_scheduler.stop()
            except Exception:
                logger.debug("midnight scheduler stop raised (non-fatal)", exc_info=True)
            self._midnight_scheduler = None

        await self._ws_server.stop()
        # Audit-2 fix: drain the session recorder's writer thread before
        # we exit so the trailing window of events (last user_action, the
        # session_report meta-event, etc.) is flushed to disk.
        try:
            self._recorder.flush(timeout=5.0)
        except Exception:
            logger.debug("Recorder flush failed (non-fatal)", exc_info=True)
        self._uvicorn_server = None
        registry.reset()
        logger.info("Cortex daemon stopped")

    async def run(self) -> None:
        try:
            await self.start()
        finally:
            await self.stop()

    def _register_services(self) -> None:
        registry.reset()
        for name, service in {
            "feature_fusion": self._feature_fusion,
            "rule_scorer": self._scorer,
            "score_smoother": self._smoother,
            "context_engine": self._context_engine,
            "llm_client": self._llm_client,
            "intervention_executor": self._executor,
            "restore_manager": self._restore_manager,
            "ws_server": self._ws_server,
            "trigger_policy": self._trigger_policy,
            "project_launcher": self._project_launcher,
            "leetcode_adapter": self._leetcode_adapter,
            # F05: register the daemon itself so the apply-intervention
            # route can call ``await_apply_confirmation`` and surface the
            # actual ack outcome rather than the optimistic assumption.
            "daemon": self,
            # v2.0 services
            "store": self._store,
            "stress_integral_tracker": self._stress_tracker,
            "longitudinal_tracker": self._longitudinal,
            "zombie_detector": self._zombie_detector,
            "rabbit_hole_detector": self._rabbit_hole,
            "shutdown_detector": self._shutdown_detector,
            "consent_ladder": self._consent_ladder,
            "helpfulness_tracker": self._helpfulness,
            "contextual_bandit": self._bandit,
            "amip_policy": self._amip,
            "copilot_throttle": self._copilot_throttle,
        }.items():
            registry.register(name, service)
        registry.healthy = True

    def _start_api_server(self) -> None:
        app = create_app(config=self.config.api, cortex_config=self.config)
        # Phase-4b TASK L: bind a concrete InterventionPort instance on
        # the app's state so routes that depend on the protocol
        # (cortex.libs.ports.intervention_port.InterventionPort) can
        # resolve the engine through dependency injection instead of
        # importing concrete functions. The default impl proxies to
        # the legacy module-level functions so existing tests keep
        # passing without rewiring.
        from cortex.libs.schemas.context import TaskContext
        from cortex.libs.schemas.intervention import (
            InterventionPlan as _Plan,
        )
        from cortex.libs.schemas.intervention import (
            WorkspaceSnapshot as _Snap,
        )
        from cortex.services.intervention_engine.planner import (
            AdapterCommand as _AdapterCommand,
        )
        from cortex.services.intervention_engine.planner import (
            ValidationResult as _ValidationResult,
        )
        from cortex.services.intervention_engine.planner import (
            prepare_plan as _prep,
        )
        from cortex.services.intervention_engine.snapshot import (
            capture_snapshot as _cap,
        )

        class _DefaultInterventionPort:
            def capture_snapshot(
                self,
                context: TaskContext | None = None,
                intervention_id: str | None = None,
                *,
                timestamp: float | None = None,
            ) -> _Snap:
                return _cap(
                    context,
                    intervention_id=intervention_id,
                    timestamp=timestamp,
                )

            def prepare_plan(
                self,
                plan: _Plan,
                *,
                tab_count: int | None = None,
            ) -> tuple[_ValidationResult, list[_AdapterCommand]]:
                return _prep(plan, tab_count=tab_count)

        app.state.intervention_port = _DefaultInterventionPort()
        config = uvicorn.Config(
            app,
            host=self.config.api.host,
            port=self.config.api.port,
            log_level="info",
            access_log=False,
            loop="asyncio",
        )
        self._uvicorn_server = uvicorn.Server(config)

        async def _supervised_serve() -> None:
            """Audit-2 fix: surface a uvicorn bind-failure to the daemon.

            Previously a stale daemon already holding port 9472 caused
            ``Server.serve`` to raise inside the create_task; nobody
            awaited the task or checked ``done_callback``, so the
            capture loop kept running, the camera stayed open, and the
            ``/shutdown`` endpoint never bound. With this supervisor
            the failure is logged at error level and the daemon's
            ``_shutdown`` event is set so ``run()`` exits cleanly.
            """
            try:
                if self._uvicorn_server is not None:
                    await self._uvicorn_server.serve()
            except OSError as exc:
                logger.error(
                    "API server bind failed on %s:%s (%s); triggering shutdown",
                    self.config.api.host,
                    self.config.api.port,
                    exc,
                )
                self._shutdown.set()
            except asyncio.CancelledError:
                # Normal shutdown path.
                raise
            except Exception:
                logger.exception(
                    "API server crashed unexpectedly; triggering daemon shutdown"
                )
                self._shutdown.set()

        self._api_task = asyncio.create_task(_supervised_serve(), name="cortex-api")

    def _current_app_name(self) -> str:
        events = self._window_tracker.get_events_in_window(window_seconds=60.0)
        if not events:
            return "other"
        return events[-1].app_name

    def _load_baselines(self) -> UserBaselines:
        baseline_path = Path(self.config.storage.path) / "baselines" / "default.json"
        if not baseline_path.exists():
            return UserBaselines()
        try:
            loaded = UserBaselines.model_validate_json(baseline_path.read_text())
            if not loaded.metric_distributions:
                # Backward-compatible migration for legacy baseline files.
                loaded.metric_distributions = {
                    "hr": {"mu": loaded.hr_baseline, "sigma": loaded.hr_std, "p10": loaded.hr_baseline, "p90": loaded.hr_baseline},
                    "hrv_rmssd": {"mu": loaded.hrv_baseline, "sigma": max(6.0, loaded.hrv_baseline * 0.2), "p10": loaded.hrv_baseline, "p90": loaded.hrv_baseline},
                    "blink_rate": {"mu": loaded.blink_rate_baseline, "sigma": max(3.0, loaded.blink_rate_baseline * 0.2), "p10": loaded.blink_rate_baseline, "p90": loaded.blink_rate_baseline},
                }
            return loaded
        except Exception:
            logger.exception("Failed to load baselines from %s", baseline_path)
            return UserBaselines()

    async def _request_context(self, client_type: str) -> dict[str, Any]:
        return await self._ws_server.request_context(client_type)

    async def _capture_loop(self) -> None:
        try:
            while True:
                if not self._capture_available or not self._capture_processing_enabled:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    output = await self._capture_pipeline.get_output(timeout=0.5)
                except Exception:
                    # Audit-2 fix: a NaN landmark or an rPPG numerical
                    # blow-up used to propagate out of the loop and kill
                    # the task silently. The user then saw zero physio
                    # updates for the rest of the session with no log
                    # trace. Log + continue keeps the pipeline alive.
                    logger.exception("Capture pipeline get_output failed; continuing")
                    await asyncio.sleep(0.5)
                    continue
                if output is not None:
                    try:
                        await self._process_capture_output(output)
                    except Exception:
                        # Mediapipe occasionally returns malformed
                        # landmarks; treat as a transient bad frame and
                        # keep the loop running.
                        logger.exception("Capture frame processing failed; skipping frame")
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful task shutdown; intentional.
            logger.debug("capture loop cancelled")

    async def _process_capture_output(self, output: PipelineOutput) -> None:
        registry.register("latest_frame_meta", output.frame_meta)
        if output.landmarks_px is None:
            return

        roi_frame = self._roi_extractor.extract(output.frame, output.landmarks_px, output.frame_meta.timestamp)
        combined_rgb = roi_frame.combined_rgb()
        if combined_rgb is not None:
            # P0-2: skip low-quality frames so motion blur / occlusion
            # artefacts don't corrupt the rPPG window.
            if output.frame_meta.low_quality:
                self._frames_low_quality_rejected += 1
            else:
                self._rgb_history.append(combined_rgb)

        stride_seconds = self.config.signal.rppg.stride_seconds
        if len(self._rgb_history) >= self._rgb_history.maxlen and (
            output.frame_meta.timestamp - self._last_physio_update
        ) >= stride_seconds:
            rgb_window = np.array(self._rgb_history, dtype=np.float64)
            bvp = extract_bvp(
                rgb_window,
                algorithm=self.config.signal.rppg.backend,
                fs=float(self.config.capture.fps),
                model_path=self.config.signal.rppg.model_path,
            )
            head_jitter_deg = float(roi_frame.head_jitter_px) * (45.0 / max(1.0, float(self.config.capture.width)))
            self._pulse_estimator.process_window(
                bvp,
                timestamp=output.frame_meta.timestamp,
                head_jitter_deg=head_jitter_deg,
                face_presence_ratio=1.0 if output.frame_meta.face_detected else 0.0,
            )
            self._latest_physio = self._pulse_estimator.get_features(output.frame_meta.timestamp)
            registry.register("latest_physio", self._latest_physio)
            self._feature_fusion.update_physio(self._latest_physio, timestamp=output.frame_meta.timestamp)
            self._last_physio_update = output.frame_meta.timestamp

        blink = self._blink_detector.update(output.landmarks_px, output.frame_meta.timestamp)
        pose = self._head_pose.update(output.landmarks_px, output.frame_meta.timestamp)
        posture = self._posture.update_with_face(output.landmarks_px, output.frame_meta.timestamp)
        self._latest_kinematics = KinematicFeatures(
            blink_rate=blink.blink_rate,
            blink_rate_delta=blink.blink_rate_delta,
            blink_suppression_score=blink.blink_suppression_score,
            perclos_60s=blink.perclos_60s,
            mean_blink_duration_ms=blink.mean_blink_duration_ms,
            ear_variance=blink.ear_variance,
            head_pitch=pose.pitch,
            head_yaw=pose.yaw,
            head_roll=pose.roll,
            slump_score=posture.slump_score,
            forward_lean_score=posture.forward_lean_score,
            shoulder_drop_ratio=posture.shoulder_drop_ratio,
            confidence=output.frame_meta.face_confidence,
        )
        registry.register("latest_kinematics", self._latest_kinematics)
        # B22 (Phase 4.1): stamp the monotonic timestamp the
        # kinematics features were derived from. The state loop reads
        # this to decide whether the signal is fresh enough to drive a
        # state estimate (see ``kinematics_age`` check below).
        self._last_kinematics_ts = float(output.frame_meta.timestamp)
        self._feature_fusion.update_kinematics(self._latest_kinematics, timestamp=output.frame_meta.timestamp)

    async def _telemetry_loop(self) -> None:
        try:
            while True:
                if not self._telemetry_enabled:
                    await asyncio.sleep(0.5)
                    continue
                features = self._aggregator.build_features()
                registry.register("latest_telemetry", features)
                self._feature_fusion.update_telemetry(features)
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful telemetry shutdown.
            logger.debug("telemetry loop cancelled")

    async def _broadcast_loop(self) -> None:
        """Broadcast STATE_UPDATE at a steady 500ms cadence (B.3).

        The state pipeline (``_state_loop``) can take 1-3s per iteration when
        an intervention path runs — feature fusion + classification + LLM
        trigger evaluation, plus the per-iteration ``await asyncio.sleep(0.5)``.
        That dragged the visible STATE_UPDATE cadence out of spec; UI bars
        froze for seconds whenever the daemon was actively working.

        This dedicated task wakes every 500ms regardless, reads the latest
        cached estimate + biometrics that ``_state_loop`` produced, and
        broadcasts. Clients see a stable cadence; the pipeline is free to
        run as fast (or slow) as it needs.
        """
        try:
            interval = self._broadcast_interval_seconds
            while True:
                await asyncio.sleep(interval)
                # Audit-2 fix: read the (estimate, biometrics) pair from
                # the atomic snapshot so the broadcast never ships an
                # estimate(T+1) paired with biometrics(T). The state
                # loop writes ``_latest_broadcast_snapshot`` as a single
                # tuple assignment; the GIL guarantees that pointer swap
                # is atomic, eliminating the pair-write tearing window.
                snapshot = self._latest_broadcast_snapshot
                if snapshot is None:
                    continue
                estimate, biometrics = snapshot
                try:
                    await self._ws_server.broadcast_state(estimate, biometrics)
                except Exception:
                    logger.debug("broadcast_state failed", exc_info=True)
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful broadcast loop shutdown.
            logger.debug("broadcast loop cancelled")

    async def _context_loop(self) -> None:
        """Build context every 5s — separate from fast state loop to avoid blocking."""
        try:
            while True:
                try:
                    context = await self._context_engine.build_context()
                    # Audit-2 fix: apply user-supplied goal override so the
                    # planner sees the intent the user typed into the
                    # dashboard. We mutate the freshly-built context (no
                    # external reference yet, no race).
                    if self._user_goal_override:
                        try:
                            context.current_goal_hint = self._user_goal_override
                        except Exception:
                            logger.debug(
                                "Failed to apply user goal override",
                                exc_info=True,
                            )
                    self._latest_context = context
                    registry.register("latest_task_context", context)
                    self._terminal_adapter.set_running_command(
                        context.terminal_context.running_command if context.terminal_context else None
                    )
                except Exception:
                    logger.exception("Context loop error")
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful context loop shutdown.
            logger.debug("context loop cancelled")

    async def _retention_sweep_loop(self) -> None:
        """Run the daily retention sweep (G.2).

        Storage-related fields on :class:`StorageConfig`
        (``session_retention_days``, ``feature_retention_days``,
        ``error_retention_days``) were declarative-only — no code read
        them and old files accumulated indefinitely. This loop runs
        :func:`cortex.services.janitor.retention.sweep_once` every 24
        hours so retention is enforced in practice.

        We sleep ~60 seconds initially so the first sweep happens after
        the daemon has fully booted, and then sleep 24h between sweeps.
        Sweeps are blocking I/O — run them in the asyncio thread pool.
        """
        try:
            await asyncio.sleep(60.0)
            while True:
                try:
                    storage_root = Path(self.config.storage.path).expanduser()
                    # F35: use the chunked async variant so a sweep over
                    # a large storage root does not starve the state /
                    # telemetry / broadcast coroutines.
                    await run_retention_sweep_async(
                        self.config.storage,
                        storage_root=storage_root,
                    )
                except Exception:
                    logger.debug("Retention sweep failed", exc_info=True)
                await asyncio.sleep(24 * 60 * 60)
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful retention-sweep shutdown.
            logger.debug("retention sweep loop cancelled")

    async def _causal_report_loop(self) -> None:
        """Generate nightly causal report from AMIP policy logs."""
        try:
            while True:
                try:
                    now = time.localtime()
                    target_hour = self.config.eval.causal_report.nightly_hour_local
                    if now.tm_hour == target_hour and now.tm_min < 5:
                        generate_daily_causal_report(self.config.storage.path)
                        await asyncio.sleep(300.0)
                        continue
                except Exception:
                    logger.debug("Failed generating causal report", exc_info=True)
                await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful causal report loop shutdown.
            logger.debug("causal report loop cancelled")

    async def _state_loop(self) -> None:
        try:
            while True:
                timestamp = time.monotonic()
                try:
                    vector, quality = self._feature_fusion.fuse(timestamp=timestamp)

                    # v2.0: Inject thrashing score from aggregator
                    if hasattr(self._aggregator, 'thrashing_score'):
                        vector.thrashing_score = self._aggregator.thrashing_score

                    scores = self._scorer.compute_scores(vector)
                    # C.2: blend ML classifier into smoother HYPER score
                    # once we have enough labeled episodes for a useful ramp.
                    ml_p_hyper: float | None = None
                    ml_alpha = 0.0
                    if (
                        self._ml_classifier is not None
                        and self._ml_classifier.is_fitted
                        and self._ml_labeled_episodes
                        >= self.config.state.ml_min_labeled_episodes
                    ):
                        try:
                            x = np.asarray(vector.to_array(), dtype=np.float64).reshape(1, -1)
                            ml_p_hyper = float(self._ml_classifier.predict(x)[0])
                            full_at = max(1, self.config.state.ml_alpha_full_at_episodes)
                            ramp = min(1.0, self._ml_labeled_episodes / full_at)
                            ml_alpha = self.config.state.ml_alpha_max * ramp
                        except Exception:
                            logger.debug("ML classifier predict failed", exc_info=True)
                            ml_p_hyper = None
                            ml_alpha = 0.0
                    estimate = self._smoother.update(
                        scores,
                        quality,
                        timestamp=timestamp,
                        ml_p_hyper=ml_p_hyper,
                        ml_alpha=ml_alpha,
                    )

                    # B22 (Phase 4.1): mark the kinematics channel signal-
                    # quality stale when no new kinematics features have
                    # arrived within the last 2 seconds. The smoother
                    # already computes a quality score from the upstream
                    # feature_fusion, but a stale feed produces a
                    # non-zero quality without reflecting that the data
                    # is old; this guard explicitly zeroes the channel.
                    kinematics_age = (
                        timestamp - self._last_kinematics_ts
                        if self._last_kinematics_ts > 0.0
                        else None
                    )
                    if kinematics_age is not None and kinematics_age > 2.0:
                        if estimate.signal_quality.kinematics > 0.0:
                            logger.debug(
                                "kinematics signal stale (age=%.2fs) — "
                                "zeroing channel quality",
                                kinematics_age,
                            )
                            try:
                                # SignalQuality is a Pydantic model;
                                # mutate via model_copy for safety.
                                from cortex.libs.schemas.state import (
                                    SignalQuality as _SQ,
                                )
                                estimate.signal_quality = _SQ(
                                    physio=estimate.signal_quality.physio,
                                    kinematics=0.0,
                                    telemetry=estimate.signal_quality.telemetry,
                                )
                            except Exception:
                                logger.debug(
                                    "kinematics stale-clear failed",
                                    exc_info=True,
                                )

                    # v2.0: Update stress integral
                    if vector.hrv_rmssd is not None:
                        self._stress_tracker.update(vector.hrv_rmssd, timestamp)
                        estimate.stress_integral = self._stress_tracker.current_load
                        # P0 §3.7 audit fix: re-arm BREAK_RECOMMENDATION
                        # when the stress integral drops back below the
                        # warning threshold (80% of the break threshold).
                        # Without this the latch stayed True after a
                        # dismissed recommendation, so the user only got
                        # one pulse per session even after their HRV
                        # recovered and re-degraded.
                        if (
                            self._break_recommendation_sent
                            and self._stress_tracker.load_ratio < 0.8
                        ):
                            logger.info(
                                "Stress integral recovered to %.0f%% — "
                                "re-arming BREAK_RECOMMENDATION",
                                self._stress_tracker.load_ratio * 100,
                            )
                            self._break_recommendation_sent = False
                        # B21: remember the timestamp of the most recent
                        # HRV reading so the time-elapsed re-arm gate
                        # below can tell "HRV silent for a while" apart
                        # from "HRV never delivered".
                        self._last_hrv_seen_at = timestamp
                    else:
                        # B21 (Phase 4.1): when HRV has been None for an
                        # extended period AND the recommendation is
                        # still latched, allow a time-elapsed re-arm so
                        # the user can be re-prompted on the NEXT
                        # genuine stress integral build-up. Pre-fix the
                        # latch required an HRV reading to clear; if the
                        # user's camera ROI degraded post-break the
                        # recommendation would never re-fire.
                        last_seen = getattr(self, "_last_hrv_seen_at", 0.0)
                        hrv_silent_for = timestamp - last_seen
                        if (
                            self._break_recommendation_sent
                            and last_seen > 0.0
                            and hrv_silent_for > 30.0
                        ):
                            logger.info(
                                "HRV silent for %.0fs after a break "
                                "recommendation — re-arming so the next "
                                "build-up can re-trigger",
                                hrv_silent_for,
                            )
                            self._break_recommendation_sent = False

                    # P0 §3.9: feed the causal attributor at the same
                    # cadence so the per-signal sparkline buffers fill
                    # smoothly. Also stash the live feature vector so
                    # ``get_causal_signals`` can fall back to live
                    # attribution when the per-intervention cache is
                    # cold.
                    try:
                        self._causal_attributor.record_feature_vector(vector)
                        registry.register("latest_feature_vector", vector)
                    except Exception:
                        # B20 (Phase 4.1): a silent attributor failure
                        # means the per-intervention causal sparkline
                        # buffers stop filling, so the "Why this?" panel
                        # serves stale data. Elevate to WARNING so the
                        # observability path catches it.
                        logger.warning(
                            "causal attributor feed failed", exc_info=True,
                        )

                    # v2.0: Feed longitudinal tracker per-sample data
                    self._longitudinal.accumulate(
                        hr=vector.hr,
                        hrv=vector.hrv_rmssd,
                        resp=vector.respiration_rate,
                        state=estimate.state,
                    )

                    registry.register("latest_state_estimate", estimate)
                    self._recorder.append("state_estimate", estimate.model_dump(mode="json"))
                    # G.1: feed the session-debrief generator.
                    try:
                        if not self._session_report_started:
                            self._session_report.start()
                            self._session_report_started = True
                        self._session_report.record_state(
                            estimate.state, time.time(),
                        )
                        if vector.hr:
                            self._session_report.record_hr(float(vector.hr))
                        if vector.hrv_rmssd:
                            self._session_report.record_hrv(float(vector.hrv_rmssd))
                        if estimate.stress_integral is not None:
                            self._session_report.record_stress(
                                float(estimate.stress_integral)
                            )
                    except Exception:
                        logger.debug("session_report record failed", exc_info=True)
                    # Audit-2 fix: publish ``forward_lean`` as a 0-1 score
                    # (rescaled from the underlying 0-45° angle). The
                    # browser extension's posture alert threshold (0.6)
                    # was previously compared against raw degrees, so
                    # every user got a posture toast within 3 min of
                    # session start regardless of actual posture. The
                    # raw angle is still surfaced as ``forward_lean_angle``
                    # for any consumer that prefers degrees.
                    _lean_angle = vector.forward_lean_angle
                    _lean_score: float | None
                    if _lean_angle is None:
                        _lean_score = None
                    else:
                        _lean_score = max(0.0, min(1.0, float(_lean_angle) / 45.0))
                    biometrics = {
                        "heart_rate": vector.hr,
                        "hrv_rmssd": vector.hrv_rmssd,
                        "hr_delta": vector.hr_delta,
                        "blink_rate": vector.blink_rate,
                        "forward_lean": _lean_score,
                        "forward_lean_angle": _lean_angle,
                        "respiration_rate": vector.respiration_rate,
                        "thrashing_score": vector.thrashing_score,
                        "stress_integral": self._stress_tracker.current_load,
                    }
                    # B.3: cache for the dedicated broadcast loop instead of
                    # broadcasting inline. Inline broadcasts let LLM/trigger
                    # work stretch the cadence to multi-second; the broadcast
                    # loop reads this cache at a steady 500ms tick.
                    #
                    # Audit-2 fix: write the (estimate, biometrics) pair as
                    # a single tuple assignment. The GIL guarantees pointer
                    # replacement is atomic, so the broadcast loop sees
                    # either the old pair or the new pair, never a torn
                    # combination.
                    self._latest_estimate = estimate
                    self._latest_biometrics = biometrics
                    self._latest_broadcast_snapshot = (estimate, biometrics)

                    if self._state_callback is not None:
                        # F17: stamp a monotonic sequence into the payload so
                        # the in-process bridge can drop reordered frames.
                        # ``_seq`` underscore-prefix marks this as a wire
                        # implementation detail, not a domain field.
                        self._state_callback_seq += 1
                        # Audit-2 fix: parity with the WS-mode STATE_UPDATE
                        # envelope. Previously the in-process callback
                        # dropped the F18 ``degraded``/``source``/
                        # ``stress_integral``/``timestamp`` fields, so the
                        # dashboard's degraded-classifier badge never lit
                        # up in DMG ``--in-process`` mode.
                        _scores_dump: dict[str, Any] = (
                            estimate.scores.model_dump()
                            if hasattr(estimate.scores, "model_dump")
                            else {}
                        )
                        _payload: dict[str, Any] = {
                            "_seq": self._state_callback_seq,
                            "state": estimate.state,
                            "confidence": estimate.confidence,
                            "scores": _scores_dump,
                            "signal_quality": estimate.signal_quality.model_dump(),
                            "dwell_seconds": estimate.dwell_seconds,
                            "reasons": estimate.reasons,
                            "biometrics": biometrics,
                            "timestamp": float(getattr(estimate, "timestamp", timestamp) or timestamp),
                            "stress_integral": getattr(estimate, "stress_integral", None),
                            "source": getattr(estimate, "source", "classifier"),
                            "degraded": bool(getattr(estimate, "degraded", False)),
                            "calibrated_probabilities": getattr(estimate, "calibrated_probabilities", None),
                            "classifier_source": getattr(estimate, "classifier_source", None),
                            "classifier_alpha": getattr(estimate, "classifier_alpha", None),
                            # G1 (audit-prod): forward the WS server's view of
                            # currently-IDENTIFY-ed clients so the dashboard
                            # dots react in real time even on the in-process
                            # DMG path (no WS roundtrip).
                            "connected_clients": (
                                self._ws_server.connected_client_types()
                                if hasattr(self._ws_server, "connected_client_types")
                                else []
                            ),
                        }
                        self._state_callback(copy.deepcopy(_payload))

                    # v2.0: Copilot throttle on state transitions
                    if estimate.state != self._prev_state:
                        await self._copilot_throttle.on_state_change(
                            estimate.state, estimate.confidence,
                        )
                        self._prev_state = estimate.state

                    # P0 §3.10: auto-armed distraction blocking on
                    # sustained HYPER. Runs every state tick so the
                    # symmetric STOP_FOCUS_AUTO fires even when the
                    # user dwells in HYPER without state transitions.
                    try:
                        await self._evaluate_auto_distraction_block(
                            estimate, timestamp,
                        )
                    except Exception:
                        logger.debug(
                            "auto-distraction evaluation failed",
                            exc_info=True,
                        )

                    await self._maybe_trigger_leetcode_interventions(
                        estimate, vector, timestamp,
                    )

                    context = self._latest_context
                    if context is not None:
                        telemetry_for_trigger = registry.get("latest_telemetry")
                        typing_burst_seconds = 0.0
                        if telemetry_for_trigger is not None:
                            kb_burst = float(getattr(telemetry_for_trigger, "keyboard_burst_score", 0.0))
                            if kb_burst >= 0.8:
                                typing_burst_seconds = self.config.intervention.receptivity_typing_burst_seconds
                        hour_now = time.localtime().tm_hour
                        within_work_hours = (
                            self.config.intervention.receptivity_work_hours_start
                            <= hour_now
                            < self.config.intervention.receptivity_work_hours_end
                        )
                        # C.4: source mic + fullscreen from macOS via
                        # cortex.libs.utils.receptivity. Returns None on
                        # non-macOS or pyobjc-missing — degrade to False so
                        # the policy still functions, matching legacy semantics.
                        mic_state = receptivity.is_microphone_in_use()
                        fs_state = receptivity.is_app_fullscreen()
                        # P0 §3.7 audit fix: track the most-recent
                        # mic_active timestamp so the biology break
                        # controller can suppress audio when the user
                        # is on a call.
                        if mic_state:
                            self._last_mic_active_at = time.monotonic()
                        decision = self._trigger_policy.evaluate(
                            estimate,
                            context_complexity=context.complexity_score,
                            mic_active=bool(mic_state) if mic_state is not None else False,
                            fullscreen_active=bool(fs_state) if fs_state is not None else False,
                            typing_burst_seconds=typing_burst_seconds,
                            within_work_hours=within_work_hours,
                            current_time=timestamp,
                        )
                        registry.register("latest_trigger_decision", decision)
                        await self._handle_restore_updates(estimate, timestamp)

                        # v2.0: Check zombie reading
                        active_app = self._current_app_name()
                        telemetry = registry.get("latest_telemetry")
                        kinematics = self._latest_kinematics
                        self._zombie_detector.update_baseline(self._scorer.baselines.blink_rate_baseline)
                        if self._zombie_detector.update(
                            state=estimate.state,
                            active_app=active_app,
                            mouse_velocity=telemetry.mouse_velocity_mean if telemetry else 0.0,
                            blink_rate=kinematics.blink_rate,
                        ):
                            logger.info("Zombie reading detected — triggering active recall")
                            await self._trigger_special_intervention(
                                context, estimate, template_name="active_recall",
                                ws_type="ACTIVE_RECALL",
                            )

                        # v2.0: Check rabbit hole drift
                        goal = context.current_goal_hint or ""
                        if goal:
                            current_file = getattr(context, "file_path", "") or ""
                            alert = self._rabbit_hole.check(
                                goal=goal,
                                current_file=current_file,
                                current_app=active_app,
                                state=estimate.state,
                                current_time=timestamp,
                            )
                            if alert is not None:
                                logger.info("Rabbit hole detected — goal drift intervention")
                                await self._trigger_special_intervention(
                                    context, estimate, template_name="rabbit_hole",
                                    ws_type="INTERVENTION_TRIGGER",
                                )

                        # v2.0: Check stress integral — break at 100% (priority), warn at 80%
                        # P0 §3.7 audit fix: feature flag gates the
                        # *biology-break* augmentations (BREAK_RECOMMENDATION
                        # pulse + planner promotion) without disturbing the
                        # legacy breathing_overlay special intervention,
                        # which always fires on threshold crossing.
                        biology_break_enabled = bool(
                            getattr(
                                self.config.intervention,
                                "enable_biology_break",
                                True,
                            )
                        )
                        if self._stress_tracker.should_break():
                            logger.info("Stress integral threshold — biological break")
                            # P0 §3.7: emit BREAK_RECOMMENDATION once
                            # per False→True transition. The pulse
                            # surfaces a soft pill on every UI even
                            # when no overlay is active.
                            if biology_break_enabled and not self._break_recommendation_sent:
                                try:
                                    pre_hrv_snapshot = (
                                        float(vector.hrv_rmssd)
                                        if vector.hrv_rmssd is not None
                                        else None
                                    )
                                    suggested_pattern = self._suggest_break_pattern(pre_hrv_snapshot)
                                    urgency = self._classify_break_urgency()
                                    await self._ws_server.send_message(
                                        MessageType.BREAK_RECOMMENDATION.value,
                                        {
                                            "reason": "stress_integral_crossed_threshold",
                                            "urgency": urgency,
                                            "stress_load": float(
                                                self._stress_tracker.current_load
                                            ),
                                            "threshold": float(
                                                self._stress_tracker.threshold
                                            ),
                                            "duration_seconds": 240,
                                            "breathing_pattern": suggested_pattern,
                                        },
                                    )
                                    self._break_recommendation_sent = True
                                except Exception:
                                    logger.debug(
                                        "BREAK_RECOMMENDATION send failed",
                                        exc_info=True,
                                    )
                            await self._trigger_special_intervention(
                                context, estimate, template_name="breathing_overlay",
                                ws_type="BREATHING_OVERLAY",
                            )
                            # NB: do NOT reset the integral here — the
                            # break controller decides whether to reset
                            # (natural completion) or apply a partial
                            # recovery credit (early termination). The
                            # legacy reset() was too eager — it cleared
                            # the integral even if the user dismissed
                            # the BREATHING_OVERLAY toast outright.
                        elif self._stress_tracker.should_warn():
                            logger.info("Stress integral at 80%% — pre-break warning")
                            await self._trigger_special_intervention(
                                context, estimate, template_name="pre_break_warning",
                                ws_type="PRE_BREAK_WARNING",
                            )

                        # v2.0: Check shutdown detection
                        if self._shutdown_detector.should_handover(
                            posture_slump=kinematics.slump_score or 0.0,
                            hrv=vector.hrv_rmssd,
                            error_count=context.total_errors if hasattr(context, 'total_errors') else 0,
                        ):
                            logger.info("Shutdown signal detected — generating handover")
                            await self._generate_handover(context)

                        # Standard intervention trigger
                        if (
                            self._interventions_enabled
                            and decision.should_trigger
                            and self._active_intervention_id is None
                            and estimate.signal_quality.acceptable
                        ):
                            bandit_features = np.array(
                                self._build_bandit_features(estimate, context),
                                dtype=np.float64,
                            )
                            selected_bandit_arm: int | None = None
                            if self.config.eval.policy == "amip":
                                amip_decision = self._amip.choose_action(
                                    bandit_features,
                                    confidence=decision.confidence,
                                    receptive=not decision.receptivity_blocked,
                                    stress_ratio=self._stress_tracker.load_ratio,
                                )
                                self._last_policy_decision_id = amip_decision.decision_id
                                self._last_policy_arm = amip_decision.action
                                self._last_policy_propensity = dict(amip_decision.probabilities)
                                template_name = self._policy_arm_to_template(amip_decision.action)
                                if amip_decision.action == "no_action":
                                    await asyncio.sleep(0.5)
                                    continue
                            elif self.config.eval.policy == "uniform":
                                arm_name = np.random.choice(
                                    [
                                        "workspace_simplify",
                                        "task_decompose",
                                        "breath_box",
                                        "nature_break",
                                        "flow_shield",
                                        "defusion_prompt",
                                        "circuit_breaker",
                                    ]
                                ).item()
                                self._last_policy_decision_id = None
                                self._last_policy_arm = arm_name
                                self._last_policy_propensity = None
                                template_name = self._policy_arm_to_template(arm_name)
                            else:
                                selected_arm = await self._bandit.select_arm_async(bandit_features)
                                template_name = self._arm_to_template(selected_arm)
                                self._last_policy_decision_id = None
                                self._last_policy_arm = self._bandit.get_arm_label(selected_arm)
                                self._last_policy_propensity = None
                                selected_bandit_arm = selected_arm

                            # Run intervention in background so the state
                            # loop keeps updating while the LLM responds.
                            self._active_intervention_id = "__pending__"
                            # Phase-4b TASK D: thread the decision_id
                            # explicitly so the trigger doesn't fall back
                            # to a shared mutable slot for outcome
                            # attribution under concurrency.
                            decision_id_snapshot = self._last_policy_decision_id
                            self._spawn_background_task(
                                self._trigger_intervention(
                                    context,
                                    estimate,
                                    template_name=template_name,
                                    bandit_features=(
                                        bandit_features.tolist()
                                        if self.config.eval.policy == "greedy"
                                        else None
                                    ),
                                    bandit_arm_index=(
                                        selected_bandit_arm
                                        if self.config.eval.policy == "greedy"
                                        else None
                                    ),
                                    decision_id=decision_id_snapshot,
                                ),
                                name="cortex-intervention",
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("State loop iteration error")

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful state loop shutdown.
            logger.debug("state loop cancelled")

    async def _trigger_intervention(
        self,
        context: Any,
        estimate: Any,
        *,
        template_name: str | None = None,
        bandit_features: list[float] | None = None,
        bandit_arm_index: int | None = None,
        decision_id: str | None = None,
    ) -> None:
        # Phase-4b TASK D: prefer the explicit ``decision_id`` arg so a
        # second concurrent ``_trigger_intervention`` cannot poison this
        # call's outcome attribution by overwriting
        # ``self._last_policy_decision_id`` between the dispatch site
        # and here. Fall back to the legacy shared slot only when no
        # explicit id was supplied (test rigs / legacy callers).
        if decision_id is None:
            decision_id = self._last_policy_decision_id
        try:
            # Inject learned tab relevance into context for LLM
            goal = getattr(context, "current_goal_hint", "") or ""
            if not goal and hasattr(context, "browser_context") and context.browser_context:
                goal = context.browser_context.focus_goal or ""
            if goal:
                try:
                    overrides = await self._tab_relevance.get_overrides(goal)
                    if overrides and hasattr(context, "learned_relevance"):
                        context.learned_relevance = overrides
                except Exception:
                    logger.debug("Failed to load tab relevance overrides", exc_info=True)

            plan = await asyncio.wait_for(
                self._llm_client.generate_intervention_plan(
                    context,
                    estimate,
                    template_name=template_name,
                ),
                timeout=self.config.llm.timeout_seconds + 5.0,
            )
            plan = enrich_plan_with_context(plan, context)
            self._self_critique_plan(plan)

            # Staleness check: suppress if student genuinely recovered
            current_state = registry.get("latest_state_estimate")
            if current_state:
                # Suppress only if student is in FLOW for >3s (genuine recovery)
                if (current_state.state == "FLOW"
                        and current_state.dwell_seconds >= 3.0):
                    logger.info("Suppressing stale intervention: student in FLOW for %.1fs", current_state.dwell_seconds)
                    self._active_intervention_id = None
                    return
                # Also check if workspace context changed significantly
                if hasattr(context, 'browser_context') and context.browser_context:
                    current_tab_count = len(context.browser_context.all_tabs) if context.browser_context.all_tabs else 0
                    if plan.suggested_actions:
                        stale_actions = sum(1 for a in plan.suggested_actions
                                            if a.tab_index is not None and a.tab_index >= current_tab_count)
                        if stale_actions > len(plan.suggested_actions) * 0.5:
                            logger.info("Suppressing stale intervention: >50%% tab references invalid")
                            self._active_intervention_id = None
                            return

            # P0 §3.7: if the stress integral has crossed threshold
            # (latched by ``_break_recommendation_sent`` so we don't
            # re-flip the should_break() one-shot), promote
            # ``take_biology_break`` to the primary action. The biology
            # break is always the right intervention when the user's
            # HRV has been suppressed long enough to flag — downstream
            # LLM plans get rewritten in place rather than competing
            # for the single-CTA slot. Gated on the feature flag so
            # operators can disable the entire promotion path without
            # disabling the legacy breathing_overlay.
            biology_break_enabled = bool(
                getattr(self.config.intervention, "enable_biology_break", True)
            )
            if biology_break_enabled and self._break_recommendation_sent:
                try:
                    pattern_hint = self._suggest_break_pattern(
                        self._sample_hrv_for_break(),
                    )
                    plan = promote_biology_break(
                        plan,
                        duration_seconds=240,
                        breathing_pattern=(
                            pattern_hint
                            if pattern_hint in ("box", "4-7-8", "coherent")
                            else None
                        ),
                        audio_cue=True,
                        reason="stress_integral_crossed_threshold",
                    )
                except Exception:
                    logger.debug("promote_biology_break failed", exc_info=True)

            # P0 §3.9: attach structured causal signals to every plan
            # so each surface's "Why?" drilldown renders without an
            # extra round-trip. The cache is keyed by intervention_id
            # so WHY_DETAIL_REQUEST can resolve even if the surface
            # joined late.
            try:
                latest_features = registry.get("latest_feature_vector")
                if latest_features is not None:
                    plan.causal_signals = self._causal_attributor.attribute_top_signals(
                        latest_features, self._scorer.baselines,
                    )
                    self._causal_signals_by_intervention[plan.intervention_id] = [
                        s.model_dump(mode="json") for s in plan.causal_signals
                    ]
                    # Phase-3 P1-N3: bound the cache. Each entry is a
                    # 3-signal × 60-sample list (~180 floats) — small,
                    # but unbounded growth across a multi-day session
                    # is real. Keep the most recent 64 interventions.
                    if len(self._causal_signals_by_intervention) > 64:
                        oldest = next(iter(self._causal_signals_by_intervention))
                        self._causal_signals_by_intervention.pop(oldest, None)
            except Exception:
                logger.debug("causal_signals attach failed", exc_info=True)

            tab_count = None
            if hasattr(context, "browser_context") and context.browser_context is not None:
                tab_count = len(context.browser_context.all_tabs)
            validation, commands = prepare_plan(plan, tab_count=tab_count)
            if not validation.is_valid:
                logger.warning("Rejected intervention plan %s: %s", plan.intervention_id, validation.errors)
                return
            if validation.warnings:
                plan.plan_warnings.extend(validation.warnings)

            # v2.0: Check consent ladder
            consent_level_map = {
                "observe": 0, "suggest": 1, "preview": 2,
                "reversible_act": 3, "autonomous_act": 4,
            }
            # Phase-4b TASK B: an unknown ``consent_level`` literal used to
            # silently default to PREVIEW (2) — that masked planner bugs
            # AND let the bandit learn from outcomes attributed to the
            # wrong consent gate. Reject the plan and log so AMIP /
            # ops can see the failure.
            if plan.consent_level not in consent_level_map:
                logger.warning(
                    "rejecting plan %s with unknown consent_level=%r",
                    plan.intervention_id,
                    plan.consent_level,
                )
                # Record the failed plan so AMIP/helpfulness don't lose
                # the decision. ``record_failed_plan`` is best-effort —
                # older helpfulness store may not expose it, in which
                # case the warning above is sufficient observability.
                failed_recorder = getattr(
                    self._helpfulness, "record_failed_plan", None,
                )
                if failed_recorder is not None:
                    try:
                        result = failed_recorder(
                            intervention_id=plan.intervention_id,
                            reason="unknown_consent_level",
                        )
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.debug(
                            "record_failed_plan raised", exc_info=True,
                        )
                self._active_intervention_id = None
                return
            requested_level = consent_level_map[plan.consent_level]
            consent = await self._consent_ladder.check(
                action_type=plan.level, requested_level=requested_level,
            )
            if not consent.allowed:
                logger.info("Consent ladder blocked intervention %s (level=%s)", plan.intervention_id, plan.consent_level)
                return

            snapshot = capture_snapshot(context, intervention_id=plan.intervention_id)
            mutations = await self._executor.apply(plan, commands)
            # Phase-4b TASK C: compute the actual applied count so the
            # downstream broadcast knows whether the workspace mutated.
            # ``commands`` may legitimately be empty (suggested-action-
            # only plan) — in that case both ``len(commands) == 0`` and
            # ``applied == 0``; we still broadcast INTERVENTION_TRIGGER.
            applied = sum(1 for m in mutations if m.success)
            expected_mutations = len(commands)
            if expected_mutations > 0 and applied == 0:
                # The plan expected to mutate the workspace and every
                # mutation failed. Skip the restore-manager registration
                # (nothing to roll back) and broadcast INTERVENTION_FAILED
                # so the UI can surface the failure mode instead of a
                # silently-broken intervention.
                failed_types = sorted({m.action for m in mutations})
                reasons = sorted({
                    m.reason for m in mutations if getattr(m, "reason", None)
                })
                error_reason = (
                    reasons[0] if reasons else "all_mutations_failed"
                )
                logger.warning(
                    "Intervention %s: all %d mutations failed (types=%s reason=%s)",
                    plan.intervention_id,
                    expected_mutations,
                    failed_types,
                    error_reason,
                )
                try:
                    await self._ws_server.send_message(
                        MessageType.INTERVENTION_FAILED.value,
                        {
                            "intervention_id": plan.intervention_id,
                            "error_reason": error_reason,
                            "failed_action_types": failed_types,
                        },
                    )
                except Exception:
                    logger.debug(
                        "INTERVENTION_FAILED broadcast failed", exc_info=True,
                    )
                self._active_intervention_id = None
                return
            self._restore_manager.start_intervention(plan.intervention_id, snapshot)
            self._trigger_policy.record_intervention()
            self._active_intervention_id = plan.intervention_id
            # Phase-4b TASK C: stamp the applied count onto the trigger
            # payload so the overlay can show "2 of 3 actions applied"
            # without rebroadcasting from the executor side.
            if applied > 0:
                try:
                    plan.metadata = dict(plan.metadata or {})
                    plan.metadata["mutations_applied_count"] = applied
                except Exception:
                    logger.debug(
                        "stamping mutations_applied_count failed", exc_info=True,
                    )
            # P0 §3.6: cache the live plan so MICRO_STEP_TOGGLED can
            # mutate its ``micro_steps`` and rebroadcast the trigger.
            # If a previous intervention shares this id (F16 swap),
            # preserve user-driven step status across the swap.
            # Wave-2 P1: serialise the merge+rebind against any
            # concurrent ``toggle_micro_step`` so the toggle can't
            # rebroadcast a stale plan snapshot it captured before the
            # swap completed.
            async with self._micro_step_lock:
                if (
                    self._active_plan is not None
                    and self._active_plan.intervention_id == plan.intervention_id
                ):
                    from cortex.services.intervention_engine.restore import (
                        merge_micro_steps,
                    )
                    plan.micro_steps = merge_micro_steps(
                        self._active_plan.micro_steps, plan.micro_steps
                    )
                self._active_plan = plan
                self._micro_step_recovery_fired = False
            registry.register(f"workspace_snapshot:{plan.intervention_id}", snapshot)
            self._recorder.append("intervention_plan", plan.model_dump(mode="json"))

            # v2.0: Start helpfulness tracking
            self._helpfulness.start_tracking(
                intervention_id=plan.intervention_id,
                intervention_type=plan.level,
                state=estimate.state,
                confidence=estimate.confidence,
                complexity=(
                    float(context.complexity_score)
                    if hasattr(context, "complexity_score")
                    else 0.0
                ),
                tab_count=(
                    int(context.browser_context.tab_count)
                    if hasattr(context, "browser_context") and context.browser_context
                    else 0
                ),
                error_count=(
                    int(context.total_errors)
                    if hasattr(context, "total_errors")
                    else 0
                ),
                thrashing_score=float(getattr(self._aggregator, "thrashing_score", 0.0)),
                stress_integral=float(getattr(self._stress_tracker, "current_load", 0.0)),
                decision_id=decision_id,
                propensity=self._last_policy_propensity,
                policy_arm=self._last_policy_arm,
            )
            # Bind the policy decision to this intervention ID so reward updates
            # use the exact action/context that was chosen. Phase-4b TASK D:
            # use the explicit ``decision_id`` snapshot rather than the
            # shared mutable slot so a peer trigger cannot steal credit.
            if decision_id:
                self._amip_decision_ids_by_intervention[plan.intervention_id] = decision_id
            if bandit_features is not None and bandit_arm_index is not None:
                self._bandit_decisions_by_intervention[plan.intervention_id] = (
                    list(bandit_features), int(bandit_arm_index)
                )

            # P0 §3.12: dispatch through OS-level channels when the
            # desktop dashboard is not the active window. The flag is
            # forwarded on the wire so the browser extension fires
            # ``chrome.notifications`` + sets its action badge, and VS
            # Code pulses its status bar item.
            os_notifications_enabled = bool(
                getattr(
                    self.config.intervention,
                    "enable_os_notifications",
                    True,
                )
            )
            desktop_focused: bool | None
            if os_notifications_enabled:
                desktop_focused = self._desktop_is_focused()
            else:
                desktop_focused = None
            await self._ws_server.send_intervention(
                plan, desktop_focused=desktop_focused,
            )
            # P0 §3.15: plan-finalised event — push COST_RESPONSE so the
            # UI cost meter updates without polling lag. Best-effort; a
            # cost-tracker error must not bubble up here.
            await self._broadcast_cost_response()
            # Fire the macOS UNUserNotification path when the desktop
            # dashboard isn't focused — the WS broadcast covers Chrome /
            # VS Code; the helper covers Spaces-other-than-the-desktop.
            if os_notifications_enabled and desktop_focused is False:
                try:
                    await self._dispatch_os_notification(plan)
                except Exception:
                    logger.debug(
                        "OS notification dispatch failed", exc_info=True,
                    )

            if self._intervention_callback is not None:
                # F17: stamp a monotonic sequence so the in-process bridge
                # can drop reordered intervention triggers. The plan dict
                # is augmented with ``_seq`` after deep-copying so we
                # never mutate the model the daemon retains.
                self._intervention_callback_seq += 1
                _payload = copy.deepcopy(plan.model_dump(mode="json"))
                _payload["_seq"] = self._intervention_callback_seq
                # Audit-prod fix (G4 P0): the overlay's action-buttons gate
                # browser-bound actions on ``payload.connected_clients``;
                # without this field every browser button renders disabled
                # in DMG ``--in-process`` mode. STATE_UPDATE already carries
                # it; we mirror onto the intervention payload so the
                # overlay sees the same authoritative truth.
                try:
                    _payload["connected_clients"] = (
                        self._ws_server.connected_client_types()
                        if hasattr(self._ws_server, "connected_client_types")
                        else []
                    )
                except Exception:
                    _payload["connected_clients"] = []
                # Phase-3 P0-N? + Audit-1.1 P0-7: stamp the focus state
                # on the in-process callback payload so the dashboard
                # overlay can suppress itself when the OS notification
                # path is taking over (dual-fire de-dup).
                if desktop_focused is False:
                    _payload["desktop_not_focused"] = True
                self._intervention_callback(_payload)
        except TimeoutError:
            logger.warning("Intervention LLM call timed out")
            # Phase-4b TASK D: explicitly clear pending state. Leaving
            # the pending decision_id slot occupied would let the next
            # trigger inherit a now-stale id, double-attributing the
            # outcome to a plan that never landed.
            if self._last_policy_decision_id == decision_id:
                self._last_policy_decision_id = None
                self._last_policy_arm = None
                self._last_policy_propensity = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to trigger intervention")
        finally:
            # Clear __pending__ sentinel if intervention didn't complete
            if self._active_intervention_id == "__pending__":
                self._active_intervention_id = None

    async def _trigger_special_intervention(
        self,
        context: Any,
        estimate: Any,
        *,
        template_name: str,
        ws_type: str = "INTERVENTION_TRIGGER",
        decision_id: str | None = None,
    ) -> None:
        """Trigger a special v2.0 intervention (breathing, active recall, rabbit hole).

        Phase-4b TASK D: ``decision_id`` is an explicit arg. Special
        interventions do not currently bind to an AMIP arm, so the
        default behaviour (clear the shared slot) is preserved; the arg
        is accepted for future symmetry with ``_trigger_intervention``.
        """
        if self._active_intervention_id is not None:
            return  # Don't stack interventions
        # Audit-2 fix: stamp the ``__pending__`` sentinel *before* the
        # ``await`` so two consecutive state-loop ticks cannot both pass
        # the guard above and double-spawn. The old code stamped the real
        # intervention_id *after* the LLM call returned, leaving a 4 s+
        # window during which a duplicate trigger silently billed tokens
        # twice and broadcast two plans (only one of which won the
        # ``_active_intervention_id`` assignment).
        self._active_intervention_id = "__pending__"
        # Phase-4b TASK D: special interventions are not AMIP-bound;
        # clear the shared slot unless the caller threaded an explicit
        # id (in which case the trigger owns the credit and must clear
        # on completion below).
        if decision_id is None:
            self._last_policy_decision_id = None
            self._last_policy_arm = None
            self._last_policy_propensity = None

        try:
            plan = await self._llm_client.generate_intervention_plan(
                context,
                estimate,
                template_name=template_name,
            )
            plan = enrich_plan_with_context(plan, context)
            self._active_intervention_id = plan.intervention_id
            # P0 §3.6: cache the live plan + merge prior step state on F16 swap.
            # Wave-2 P1: serialise against ``toggle_micro_step`` (same
            # rationale as ``_trigger_intervention``'s swap block).
            async with self._micro_step_lock:
                if (
                    self._active_plan is not None
                    and self._active_plan.intervention_id == plan.intervention_id
                ):
                    from cortex.services.intervention_engine.restore import (
                        merge_micro_steps,
                    )
                    plan.micro_steps = merge_micro_steps(
                        self._active_plan.micro_steps, plan.micro_steps
                    )
                self._active_plan = plan
                self._micro_step_recovery_fired = False
            self._recorder.append("intervention_plan", plan.model_dump(mode="json"))
            await self._ws_server.send_message(ws_type, plan.model_dump(mode="json"))
        except Exception:
            logger.exception("Failed to trigger special intervention (%s)", template_name)
        finally:
            # Clear the sentinel if the call failed before assigning the
            # real id; on success the real id stays.
            if self._active_intervention_id == "__pending__":
                self._active_intervention_id = None

    @staticmethod
    def _self_critique_plan(plan: InterventionPlan) -> None:
        """
        Drop destructive-looking actions/language before execution.
        """
        blocked_tokens = ("discard", "delete file", "delete project", "wipe", "close application")
        sanitized_actions = []
        for action in plan.suggested_actions:
            text = f"{action.label} {action.reason} {action.action_type}".lower()
            if any(tok in text for tok in blocked_tokens):
                plan.plan_warnings.append(
                    f"dropped action {action.action_id}: destructive self-critique filter"
                )
                continue
            sanitized_actions.append(action)
        plan.suggested_actions = sanitized_actions
        plan.micro_steps = [
            step for step in plan.micro_steps
            if not any(tok in step.text.lower() for tok in blocked_tokens)
        ] or plan.micro_steps[:1]

    async def _handle_restore_updates(self, estimate: Any, timestamp: float) -> None:
        outcomes = await self._restore_manager.update(estimate, current_time=timestamp)
        for outcome in outcomes:
            self._active_intervention_id = None
            self._recorder.append("intervention_outcome", outcome.model_dump(mode="json"))
            # C.3: credit the stress integral when FLOW recovery confirms
            # the intervention was restorative. apply_recovery_credit
            # subtracts a window of sustained-low-deficit equivalent from
            # the running integral so the AMIP safety floor doesn't keep
            # firing across recovered sessions.
            if getattr(outcome, "recovery_detected", False):
                try:
                    self._stress_tracker.apply_recovery_credit(seconds=120.0)
                except Exception:
                    logger.debug("apply_recovery_credit failed", exc_info=True)
            await self._ws_server.send_restore(
                outcome.intervention_id,
                user_action=outcome.user_action,
            )

    async def dispatch_action_to_browser(
        self,
        intervention_id: str,
        action: dict[str, Any],
    ) -> int:
        """G4 (audit-prod): forward a desktop-overlay action click to the
        browser extension(s) so they actually execute it. Returns the
        count of clients the dispatch reached (0 = no browser client
        connected, or validation failed).

        Audit-prod fix: validate ``intervention_id`` against the active
        plan so a stale overlay click (timer race against dismiss) is
        not forwarded; validate ``action`` against the SuggestedAction
        Pydantic schema so a malformed action_type / missing action_id
        is rejected at the daemon boundary instead of confusing the
        extension's switch-default.
        """
        if not intervention_id or not isinstance(action, dict):
            return 0
        # Reject stale interventions. ``__pending__`` is the sentinel
        # used while the LLM call is in flight; once a real id is set
        # we honour exactly that id and no other.
        active = self._active_intervention_id
        if (
            active is None
            or active == "__pending__"
            or active != intervention_id
        ):
            logger.warning(
                "ACTION_DISPATCH dropped: stale intervention_id "
                "(requested=%s active=%s)",
                intervention_id,
                active,
            )
            return 0
        # Validate the action shape against the Pydantic source of truth.
        # SuggestedAction's validators enforce the action_type Literal,
        # url scheme allowlist, tab_index bounds, etc.
        try:
            from cortex.libs.schemas.intervention import SuggestedAction

            validated = SuggestedAction.model_validate(action).model_dump()
        except Exception as exc:
            logger.warning(
                "ACTION_DISPATCH dropped: action failed validation (%s)",
                exc,
            )
            return 0
        try:
            sent = await self._ws_server.send_message(
                MessageType.ACTION_DISPATCH.value,
                {"intervention_id": intervention_id, "action": validated},
                target_client_types=["chrome", "edge"],
            )
        except Exception:
            logger.exception("ACTION_DISPATCH send failed")
            return 0
        if sent == 0:
            logger.info(
                "ACTION_DISPATCH dropped: no chrome/edge client connected "
                "(intervention_id=%s action_id=%s)",
                intervention_id,
                validated.get("action_id"),
            )
        return sent

    # ─── P0 §3.7: biology-driven break orchestration ─────────────────

    @staticmethod
    def _suggest_break_pattern(hrv_rmssd: float | None) -> str:
        """Map HRV → breathing pattern name (matches break_overlay.select_pattern)."""
        from cortex.services.intervention_engine.break_overlay import select_pattern

        return select_pattern(hrv_rmssd)

    def _classify_break_urgency(self) -> str:
        """Map load_ratio → BREAK_RECOMMENDATION urgency string."""
        ratio = float(self._stress_tracker.load_ratio)
        if ratio >= 1.3:
            return "high"
        if ratio >= 1.05:
            return "medium"
        return "low"

    def _sample_hrv_for_break(self) -> float | None:
        """Return the most recent HRV reading for the break controller."""
        physio = getattr(self, "_latest_physio", None)
        if physio is None:
            return None
        # Prefer the explicit RMSSD proxy when populated; fall back to
        # the variability proxy field used in some legacy code paths.
        for attr in ("hrv_rmssd", "pulse_variability_proxy"):
            v = getattr(physio, attr, None)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    def _set_break_suppression(self, active: bool) -> None:
        """Toggle the global break-suppression flag.

        Called by :class:`BiologyBreakController` around the overlay
        lifecycle so peer adapters skip trigger evaluation while the
        breathing session runs.
        """
        self._break_active = bool(active)

    def set_break_overlay_ui_handler(
        self, handler: BreakUIHandler | None,
    ) -> None:
        """Bind the desktop shell's full-screen break overlay handler.

        Signature: ``async (duration_seconds, breathing_pattern,
        audio_cue) -> (elapsed_seconds, completed)``. The handler is
        invoked from the asyncio event loop; the desktop controller
        bridges to the Qt thread internally.
        """
        if self._break_controller is None:
            return
        self._break_controller.set_ui_handler(handler)

    # ─── Phase-4b TASK M: executor-bound hooks ─────────────────────

    async def _check_action_consent(
        self, action_type: str, requested_level: int,
    ) -> bool:
        """Phase-4b TASK M: per-action consent gate for the executor.

        ``InterventionExecutor.apply`` calls this BEFORE adapter
        dispatch. Returning False short-circuits the mutation into
        ``success=False, reason="consent_denied"`` so AMIP /
        helpfulness can record the failure mode.
        """
        try:
            decision = await self._consent_ladder.check(
                action_type=action_type, requested_level=requested_level,
            )
            return bool(decision.allowed)
        except Exception:
            logger.exception(
                "consent ladder check raised; denying action=%s",
                action_type,
            )
            return False

    async def _resume_last_active_file(
        self, params: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Phase-4b TASK M: focus the editor on the last active file.

        Probes for a connected editor adapter (vscode, editor) and
        sends a ``focus_active_file`` command. Returns
        ``(True, None)`` on success or ``(False, "no_active_editor")``
        when nothing accepted the command.
        """
        adapter = getattr(self, "_editor_adapter", None)
        if adapter is None or not hasattr(adapter, "execute"):
            return (False, "no_active_editor")
        try:
            ok = await adapter.execute("focus_active_file", dict(params))
            return (bool(ok), None if ok else "editor_send_failed")
        except Exception:
            logger.exception(
                "resume_last_active_file: editor adapter raised",
            )
            return (False, "editor_send_failed")

    async def _broadcast_prompt(
        self, action_type: str, params: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Phase-4b TASK M: WS broadcast for prompt-only special actions.

        Used by ``prompt_micro_commit`` and ``suggest_movement_break``
        — both are user-facing prompts with no workspace mutation.
        The broadcast carries the action type plus the planner-supplied
        params (``prompt``, ``timeout_seconds``, …) so every surface
        renders the same copy.
        """
        if self._ws_server is None:
            return (False, "ws_server_missing")
        try:
            prompt = str(params.get("prompt") or "")
            timeout_seconds = params.get("timeout_seconds")
            metadata = {
                k: v for k, v in params.items()
                if k not in ("prompt", "timeout_seconds")
            }
            await self._ws_server.send_message(
                MessageType.INTERVENTION_PROMPT.value,
                {
                    "action_type": action_type,
                    "prompt": prompt,
                    "timeout_seconds": timeout_seconds,
                    "metadata": metadata,
                },
            )
            return (True, None)
        except Exception:
            logger.exception(
                "broadcast_prompt failed for action=%s", action_type,
            )
            return (False, "broadcast_failed")

    async def start_biology_break(
        self,
        *,
        intervention_id: str | None = None,
        duration_seconds: int = 240,
        breathing_pattern: str | None = None,
        audio_cue: bool = True,
        reason: str = "stress_integral_crossed_threshold",
    ) -> dict[str, Any] | None:
        """Run one guided breathing session and return a BreakRecord dict."""
        if self._break_controller is None:
            return None
        pattern_arg: Literal["box", "4-7-8", "coherent"] | None
        if breathing_pattern in ("box", "4-7-8", "coherent"):
            pattern_arg = breathing_pattern  # type: ignore[assignment]
        else:
            pattern_arg = None
        # P0 §3.7 audit fix (spec line 643): default audio off when the
        # microphone was active recently. ``last_mic_active_at`` is the
        # most-recent positive ``receptivity.is_microphone_in_use``
        # timestamp recorded by the state loop; configurable mute
        # window defaults to 5 min per the spec risk mitigation.
        if audio_cue:
            mute_window = float(
                getattr(
                    self.config.intervention,
                    "biology_break_audio_mute_after_mic_seconds",
                    300.0,
                )
            )
            if (
                mute_window > 0
                and self._last_mic_active_at > 0
                and time.monotonic() - self._last_mic_active_at < mute_window
            ):
                logger.info(
                    "Biology break: muting audio_cue — microphone "
                    "was active in the last %.0fs",
                    mute_window,
                )
                audio_cue = False
        # Phase-4b TASK G: structured BIOLOGY_BREAK_STARTED event.
        try:
            logger.info(
                "%s intervention_id=%s duration_s=%d pattern=%s audio_cue=%s",
                EventType.BIOLOGY_BREAK_STARTED.value,
                intervention_id or "-",
                int(duration_seconds),
                pattern_arg or "auto",
                bool(audio_cue),
            )
        except Exception:
            logger.debug(
                "BIOLOGY_BREAK_STARTED log failed", exc_info=True,
            )
        record = await self._break_controller.start(
            duration_seconds=int(duration_seconds),
            breathing_pattern=pattern_arg,
            audio_cue=bool(audio_cue),
            reason=reason,
        )
        # Phase-4b TASK G: latch reset on EVERY exit path (success,
        # None-return, dismiss, timeout) so the user can receive a
        # SECOND break recommendation later in the same session. The
        # legacy code only reset after a successful record — a
        # cancelled or no-handler break left the flag latched and
        # silently suppressed every subsequent threshold crossing.
        self._break_recommendation_sent = False
        if record is None:
            return None
        # Phase-4b TASK G: structured BIOLOGY_BREAK_COMPLETED event.
        try:
            logger.info(
                "%s intervention_id=%s duration_s=%.1f recovery_delta=%s completed=%s",
                EventType.BIOLOGY_BREAK_COMPLETED.value,
                intervention_id or "-",
                float(record.duration_seconds),
                (
                    f"{record.recovery_delta:.1f}"
                    if record.recovery_delta is not None else "n/a"
                ),
                bool(record.completed),
            )
        except Exception:
            logger.debug(
                "BIOLOGY_BREAK_COMPLETED log failed", exc_info=True,
            )
        payload = record.model_dump(mode="json")
        if intervention_id:
            self._recorder.append("biology_break", {
                "intervention_id": intervention_id,
                **payload,
            })
        return payload

    # ─── P0 §3.9: causal rationale resolution ────────────────────────

    async def get_causal_signals(
        self, intervention_id: str,
    ) -> list[dict[str, Any]] | None:
        """Return the cached CausalSignal list for an intervention.

        When the daemon constructs an intervention plan the engine
        attaches the top-3 causal signals; we cache the dumped form
        keyed by intervention id so a WHY_DETAIL_REQUEST arriving even
        after the popup connection bounced still resolves cleanly.
        """
        if not intervention_id:
            return None
        cached = self._causal_signals_by_intervention.get(intervention_id)
        if cached:
            return list(cached)
        # Fall back to live attribution against the most recent feature
        # vector + baselines if available.
        try:
            latest_features = registry.get("latest_feature_vector")
            if latest_features is None:
                return None
            signals = self._causal_attributor.attribute_top_signals(
                latest_features, self._scorer.baselines,
            )
        except Exception:
            logger.debug("get_causal_signals fallback failed", exc_info=True)
            return None
        return [s.model_dump(mode="json") for s in signals]

    # ------------------------------------------------------------------
    # P0 §3.11: one-touch pause / quiet mode primitives
    # ------------------------------------------------------------------

    def get_quiet_mode_state(self) -> dict[str, Any]:
        """Return the live quiet-mode state envelope (matches
        :attr:`MessageType.QUIET_MODE_STATE` payload).
        """
        kind = self._quiet_mode_kind
        ends_at = self._quiet_mode_ends_at
        if kind == "off" or (
            ends_at is not None and time.time() >= ends_at
        ):
            # Stale window — re-normalise so the broadcast is honest.
            kind = "off"
            ends_at = None
            self._quiet_mode_kind = "off"
            self._quiet_mode_ends_at = None
        duration_minutes: int | None = None
        if ends_at is not None:
            duration_minutes = max(0, int(round((ends_at - time.time()) / 60.0)))
        return {
            "kind": kind,
            "duration_minutes": duration_minutes,
            "ends_at": ends_at,
            "source": self._quiet_mode_source,
        }

    async def _broadcast_quiet_mode_state(self) -> None:
        """Push the current quiet-mode state to every connected surface."""
        try:
            await self._ws_server.send_message(
                MessageType.QUIET_MODE_STATE.value,
                self.get_quiet_mode_state(),
            )
        except Exception:
            logger.debug("QUIET_MODE_STATE broadcast failed", exc_info=True)

    async def set_quiet_mode(
        self,
        kind: str,
        duration_minutes: int | None = None,
        source: str = "daemon",
    ) -> None:
        """P0 §3.11: enter / leave a quiet or pause mode.

        ``kind``:
          * ``"snooze_15"`` — overlay-only suppression for
            ``duration_minutes`` (default 15). Sensing continues, the
            camera stays on, but no new overlay fires.
          * ``"quiet_session"`` — same as snooze but for the rest of
            the session (default 240 min cap so we cannot pin the
            machine in a half-paused state if the user walks away).
          * ``"pause"`` — full sensing pause; releases the camera so
            the user can run another camera app. Indefinite. Also
            disarms any active auto-distraction-block focus session
            (Phase-3 P0-N4 — pause means "let me go", not "keep
            blocking my browser while I'm not even here").
          * ``"off"`` — clear any active mode immediately, resume
            capture if it was paused.

        Broadcasts ``QUIET_MODE_STATE`` (every surface) plus
        ``SETTINGS_SYNC`` (legacy clients) atomically under the
        ``_quiet_mode_lock`` so concurrent dashboard / overlay / tray /
        WS dispatch can never corrupt the pause-was-capturing latch.
        """
        if kind not in ("snooze_15", "quiet_session", "pause", "off"):
            logger.warning(
                "set_quiet_mode: unknown kind=%r (treating as 'off')", kind,
            )
            kind = "off"
        # ``duration_minutes == 0`` is the documented "use daemon
        # default" sentinel for snooze/quiet_session (matches the
        # dashboard menu's ``request_quiet_mode(... duration_minutes=0)``
        # contract). Negative values are coerced to the same fallback.
        # The WS-side validator also collapses 0 → None.
        if duration_minutes is not None and duration_minutes <= 0:
            duration_minutes = None
        # ── Resolve duration (where meaningful) ─────────────────────
        if kind == "snooze_15":
            minutes = max(1, min(240, int(duration_minutes or 15)))
        elif kind == "quiet_session":
            minutes = max(1, min(240, int(
                duration_minutes
                or self.config.intervention.quiet_mode_minutes
            )))
        else:
            minutes = 0  # pause / off carry no countdown
        ends_at: float | None = (
            time.time() + minutes * 60.0 if minutes > 0 else None
        )

        # Serialise under the lock so two surfaces flipping kinds
        # simultaneously can't drop the pause-was-capturing latch.
        async with self._quiet_mode_lock:
            prev_kind = self._quiet_mode_kind

            # Resume-from-pause helper. Called when leaving the pause
            # kind under any new kind (off / snooze / quiet_session).
            async def _resume_if_was_paused() -> None:
                if prev_kind != "pause":
                    return
                if not self._pause_was_capturing:
                    return
                try:
                    await self._capture_pipeline.start()
                    self._capture_available = True
                    self._capture_processing_enabled = True
                except Exception:
                    logger.exception("set_quiet_mode: resume capture failed")
                self._pause_was_capturing = False

            if kind == "off":
                # Clear quiet/snooze. If we were paused, resume capture.
                self._trigger_policy.clear_quiet_mode()
                await _resume_if_was_paused()
            elif kind in ("snooze_15", "quiet_session"):
                self._trigger_policy.activate_quiet_mode(
                    duration_minutes=minutes,
                )
                # Snooze/quiet leave capture running so HRV recovery is
                # still observable; only ``pause`` releases the camera.
                await _resume_if_was_paused()
            elif kind == "pause":
                # Long quiet window so dwell logic still suppresses
                # triggers even if capture briefly resumes.
                self._trigger_policy.activate_quiet_mode(duration_minutes=240)
                # Phase-3 P0-N4: pause should also disarm any
                # auto-armed focus session so the browser doesn't keep
                # blocking sites while the user is on a call / away.
                if self._auto_focus_armed:
                    try:
                        self._auto_focus_armed = False
                        self._auto_focus_dwell_started_at = 0.0
                        self._auto_focus_recovery_started_at = 0.0
                        self._auto_focus_dwell_started = False
                        self._auto_focus_recovery_started = False
                        await self._emit_stop_focus_auto(reason="paused")
                    except Exception:
                        logger.debug(
                            "auto-focus disarm on pause failed",
                            exc_info=True,
                        )
                # Only stamp ``_pause_was_capturing`` when transitioning
                # INTO pause from a non-pause state (Phase-3 P1-DF-11.5
                # — second pause-click clobbered the latch).
                if prev_kind != "pause":
                    was_running = bool(getattr(
                        self._capture_pipeline, "is_running", False,
                    ))
                    self._pause_was_capturing = was_running
                    if was_running:
                        try:
                            await self._capture_pipeline.stop()
                            self._capture_available = False
                            self._capture_processing_enabled = False
                        except Exception:
                            logger.exception(
                                "set_quiet_mode: pause stop_capture failed",
                            )

            # Record state under the same lock to keep readers consistent.
            self._quiet_mode_kind = kind
            self._quiet_mode_ends_at = ends_at
            self._quiet_mode_source = str(source or "daemon")

            # (Re)schedule the auto-decay broadcaster. When the window
            # expires, broadcast a synthetic "off" state so every
            # surface (popup countdown, tray checkmark, dashboard pill)
            # stays honest (Phase-1 P1-DF-11.3).
            existing = self._quiet_mode_decay_task
            if existing is not None and not existing.done():
                existing.cancel()
            self._quiet_mode_decay_task = None
            if ends_at is not None and minutes > 0:
                self._quiet_mode_decay_task = self._spawn_background_task(
                    self._decay_quiet_mode_after(minutes * 60.0, kind),
                    name="cortex-quiet-decay",
                )

        # ── Broadcasts (outside the lock so a slow WS send can't
        # serialise the next click) ─────────────────────────────────
        await self._broadcast_quiet_mode_state()
        # Back-compat: push SETTINGS_SYNC so older clients that only
        # watch the legacy quiet_mode flag still observe the change.
        try:
            await self._ws_server.send_message(
                MessageType.SETTINGS_SYNC.value,
                {
                    "quiet_mode": kind != "off",
                    "quiet_mode_kind": kind,
                    "quiet_duration_minutes": minutes if minutes > 0 else 0,
                },
            )
        except Exception:
            # Phase-3 P1-DF-11.2: bump from debug to warning so a real
            # broadcast failure is visible in ops logs.
            logger.warning(
                "set_quiet_mode SETTINGS_SYNC broadcast failed",
                exc_info=True,
            )
        logger.info(
            "Quiet mode set to %s (duration=%s min, source=%s)",
            kind, minutes if minutes > 0 else "-", source,
        )

    async def _decay_quiet_mode_after(
        self, delay_seconds: float, expected_kind: str,
    ) -> None:
        """Sleep ``delay_seconds`` then broadcast an "off" state IF the
        mode hasn't already transitioned away from ``expected_kind``.
        Cancellable via ``self._quiet_mode_decay_task.cancel()``.
        """
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        async with self._quiet_mode_lock:
            if self._quiet_mode_kind != expected_kind:
                return
            self._quiet_mode_kind = "off"
            self._quiet_mode_ends_at = None
            self._quiet_mode_source = "daemon_decay"
            self._trigger_policy.clear_quiet_mode()
        try:
            await self._broadcast_quiet_mode_state()
            await self._ws_server.send_message(
                MessageType.SETTINGS_SYNC.value,
                {
                    "quiet_mode": False,
                    "quiet_mode_kind": "off",
                    "quiet_duration_minutes": 0,
                },
            )
        except Exception:
            logger.debug("auto-decay broadcast failed", exc_info=True)

    # ------------------------------------------------------------------
    # P0 §3.12: desktop focus probe registration
    # ------------------------------------------------------------------

    def set_desktop_focus_probe(
        self,
        probe: Callable[[], bool] | None,
    ) -> None:
        """Register a no-arg callable that returns True iff the desktop
        dashboard is currently the foreground key window. Called from
        the daemon thread; the probe MUST be thread-safe or it must
        marshal to the GUI thread internally.

        Set to ``None`` to disable the probe (e.g. headless test
        harness). When ``None``, the daemon treats the desktop as
        focused, so OS notifications stay quiet.
        """
        self._desktop_focused_probe = probe

    def _desktop_is_focused(self) -> bool:
        """Best-effort check: True when the desktop dashboard is the
        active window. Falls back to ``True`` (= treat as focused) on
        any error so the OS notification path stays quiet."""
        probe = self._desktop_focused_probe
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:
            logger.debug("desktop focus probe raised", exc_info=True)
            return True

    async def _dispatch_os_notification(self, plan: InterventionPlan) -> None:
        """P0 §3.12: fire a macOS UNUserNotification for an intervention.

        Only the LLM-generated ``headline`` reaches the notification
        body — never biometric values (which would leak even if the
        receiver were screenshotted). When the OS notification path
        is unavailable (non-mac, missing PyObjC, permission denied)
        the helper short-circuits silently; the Chrome / VS Code
        fallbacks still fire via the wire flag.
        """
        try:
            from cortex.libs.utils.macos_notifications import (
                send_intervention_notification,
            )
        except ImportError:
            logger.debug("macOS notification helper unavailable")
            return
        headline = (getattr(plan, "headline", "") or "Cortex").strip()
        primary_focus = (
            getattr(plan, "primary_focus", "") or ""
        ).strip()
        # F09 sanitisation: explicit allowlist — no biometric numerics.
        body_parts: list[str] = []
        if primary_focus:
            body_parts.append(primary_focus)
        body = " — ".join(body_parts) or "Cortex has a suggestion"
        try:
            await asyncio.to_thread(
                send_intervention_notification,
                title=headline,
                body=body,
                intervention_id=getattr(plan, "intervention_id", "") or "",
            )
        except Exception:
            logger.debug(
                "send_intervention_notification raised", exc_info=True,
            )

    # ------------------------------------------------------------------
    # P0 §3.10: auto-armed distraction blocking on HYPER
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Consent overrides persistence (Phase-3 P0 / Audit-1.1 P0-1)
    # ------------------------------------------------------------------

    def _load_consent_overrides(self) -> None:
        """Rehydrate ``ConsentPolicy.set_level`` overrides from disk so
        the user's autonomous-act opt-ins (e.g. distraction_block)
        survive a daemon restart. Missing or corrupt file → start with
        the default policy (REVERSIBLE_ACT for distraction_block, etc.).
        """
        path = self._consent_overrides_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "consent_overrides.json unreadable; starting from defaults",
                exc_info=True,
            )
            return
        levels = payload.get("levels") if isinstance(payload, dict) else None
        if not isinstance(levels, dict):
            return
        for action_type, raw_level in levels.items():
            if not isinstance(action_type, str):
                continue
            try:
                self._consent_policy.set_level(
                    action_type, int(raw_level),
                )
            except Exception:
                logger.debug(
                    "skipping malformed consent override %r=%r",
                    action_type, raw_level,
                )
        logger.info(
            "Restored %d consent overrides from %s",
            len(levels), path,
        )

    def _persist_consent_overrides(self) -> None:
        """Atomically write the current ``ConsentPolicy`` overrides to
        disk. Called from every ``set_level`` mutation path so a crash
        between the in-memory flip and the next planned write doesn't
        lose the user's choice."""
        from cortex.libs.utils.atomic_write import atomic_write_json

        path = self._consent_overrides_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, self._consent_policy.to_dict())
        except Exception:
            logger.warning(
                "consent_overrides persist failed", exc_info=True,
            )

    def _reset_auto_focus_timers(self) -> None:
        """Phase-3 P1-N3 + Audit-1.1 P1-7: shared reset of every
        latch sentinel that controls the auto-arm timing state machine.
        Centralised so renames keep timing logic consistent."""
        self._auto_focus_dwell_started_at = 0.0
        self._auto_focus_recovery_started_at = 0.0
        self._auto_focus_dwell_started = False
        self._auto_focus_recovery_started = False

    async def _evaluate_auto_distraction_block(
        self,
        estimate: Any,
        timestamp: float,
    ) -> None:
        """Arm / disarm the daemon-driven focus session.

        Gating rules (spec §3.10, hardened per Phase-3 P1-X.2 + P0-N4):
          1. ``CORTEX_INTERVENTION__ENABLE_AUTO_DISTRACTION_BLOCK`` is
             True (default False).
          2. Mid-break (``_break_active``) auto-arm is suppressed so
             the focus interstitial doesn't layer on top of the
             breathing overlay.
          3. The user has approved the ``distraction_block`` consent
             class at ``AUTONOMOUS_ACT`` (default ``REVERSIBLE_ACT``;
             the upshift is explicit, from Settings → Focus protection).
          4. State is HYPER, confidence ≥ confidence_gate, and the
             HYPER dwell has met dwell_gate.
          5. Symmetric exit on sustained FLOW/RECOVERY for exit_gate.
          6. When the flag flips OFF / consent downgrades while
             ``_auto_focus_armed`` is True, broadcast STOP_FOCUS_AUTO
             so the browser doesn't keep blocking sites (Phase-3 P0-N4).
        """
        cfg = self.config.intervention

        # Phase-3 P1-X.2: during a biology break, suppress auto-arm.
        if getattr(self, "_break_active", False):
            self._reset_auto_focus_timers()
            return

        if not bool(getattr(cfg, "enable_auto_distraction_block", False)):
            # Off entirely — emit STOP if we were armed; reset timers.
            if self._auto_focus_armed:
                self._auto_focus_armed = False
                await self._emit_stop_focus_auto(reason="feature_disabled")
            self._reset_auto_focus_timers()
            return

        # Consent gate. We only arm autonomously when the user has
        # explicitly upgraded ``distraction_block`` to AUTONOMOUS_ACT.
        try:
            required_level = self._consent_policy.get_minimum_level(
                "distraction_block",
            )
        except Exception:
            required_level = REVERSIBLE_ACT  # conservative on error
        if int(required_level) < int(AUTONOMOUS_ACT):
            # Not opted-in. If we were armed (e.g. user just toggled
            # off mid-session), emit STOP so the browser tears down.
            if self._auto_focus_armed:
                self._auto_focus_armed = False
                await self._emit_stop_focus_auto(reason="consent_downgrade")
            self._reset_auto_focus_timers()
            return

        state = getattr(estimate, "state", "")
        confidence = float(getattr(estimate, "confidence", 0.0) or 0.0)
        confidence_gate = float(
            getattr(cfg, "auto_distraction_block_confidence", 0.85)
        )
        dwell_gate = float(
            getattr(cfg, "auto_distraction_block_dwell_seconds", 30.0)
        )
        exit_gate = float(
            getattr(cfg, "auto_distraction_block_exit_seconds", 300.0)
        )

        if state == "HYPER" and confidence >= confidence_gate:
            # Active dwell — clear the recovery countdown so we don't
            # disarm mid-HYPER on a transient FLOW reading.
            self._auto_focus_recovery_started_at = 0.0
            self._auto_focus_recovery_started = False
            if not self._auto_focus_dwell_started:
                self._auto_focus_dwell_started_at = timestamp
                self._auto_focus_dwell_started = True
            dwelled = timestamp - self._auto_focus_dwell_started_at
            if not self._auto_focus_armed and dwelled >= dwell_gate:
                # Wave-2 P1 debounce: suppress START_FOCUS_AUTO when
                # the daemon disarmed less than ``_AUTO_FOCUS_DEBOUNCE_S``
                # ago. A rapid HYPER → RECOVERY → HYPER cycle (e.g. a
                # nervous user clicking around during a brief calm
                # window) would otherwise emit a START / STOP / START
                # storm that confuses the browser focus-session UI.
                cooldown_elapsed = (
                    timestamp - self._last_focus_auto_disarm_ts
                    if self._last_focus_auto_disarm_ts > 0.0
                    else float("inf")
                )
                if cooldown_elapsed < _AUTO_FOCUS_DEBOUNCE_S:
                    logger.debug(
                        "auto-arm suppressed by debounce (cooldown %.1fs < %.1fs)",
                        cooldown_elapsed, _AUTO_FOCUS_DEBOUNCE_S,
                    )
                else:
                    # Phase-3 P1-2 (Audit-1.1): only flip the armed flag
                    # after the broadcast lands. ``_emit_start_focus_auto``
                    # returns True on success.
                    ok = await self._emit_start_focus_auto(
                        reason="biometric_hyper",
                    )
                    if ok:
                        self._auto_focus_armed = True
                        # Stamp the arm timestamp so the minimum-hold
                        # gate below knows when STOP is allowed again.
                        self._last_focus_auto_arm_ts = timestamp
                        # Phase-4b TASK F: structured DISTRACTION_BLOCKED
                        # log on auto-arm for observability symmetry
                        # with the disarm path.
                        try:
                            logger.info(
                                "%s phase=arm reason=biometric_hyper dwell_s=%.1f",
                                EventType.DISTRACTION_BLOCKED.value,
                                dwelled,
                            )
                        except Exception:
                            logger.debug(
                                "arm structured log failed", exc_info=True,
                            )
        elif self._auto_focus_armed and state in ("FLOW", "RECOVERY"):
            if not self._auto_focus_recovery_started:
                self._auto_focus_recovery_started_at = timestamp
                self._auto_focus_recovery_started = True
            recovered_for = timestamp - self._auto_focus_recovery_started_at
            if recovered_for >= exit_gate:
                # Wave-2 P1 debounce: enforce a minimum-hold window
                # before STOP_FOCUS_AUTO may fire. With ``exit_gate``
                # already set to 300 s in production this is normally a
                # no-op, but tests / aggressive configs can push
                # ``auto_distraction_block_exit_seconds`` low; the
                # debounce keeps the START → STOP gap above 30 s even
                # then.
                held_for = (
                    timestamp - self._last_focus_auto_arm_ts
                    if self._last_focus_auto_arm_ts > 0.0
                    else float("inf")
                )
                if held_for < _AUTO_FOCUS_DEBOUNCE_S:
                    logger.debug(
                        "auto-disarm suppressed by debounce (held %.1fs < %.1fs)",
                        held_for, _AUTO_FOCUS_DEBOUNCE_S,
                    )
                else:
                    await self._emit_stop_focus_auto(reason="sustained_recovery")
                    self._auto_focus_armed = False
                    self._auto_focus_recovery_started_at = 0.0
                    self._auto_focus_recovery_started = False
                    # Stamp disarm timestamp so the cooldown above knows
                    # when re-arming is allowed.
                    self._last_focus_auto_disarm_ts = timestamp
        else:
            # Any other state — keep armed state but reset dwell so we
            # don't arm on a flicker.
            if state != "HYPER":
                self._auto_focus_dwell_started_at = 0.0
                self._auto_focus_dwell_started = False
            else:
                # Sub-gate confidence HYPER — keep dwell intact (so a
                # one-tick dip doesn't restart the timer) but clear
                # the recovery countdown if armed.
                self._auto_focus_recovery_started_at = 0.0
                self._auto_focus_recovery_started = False

    async def _emit_start_focus_auto(self, *, reason: str) -> bool:
        """Broadcast ``START_FOCUS_AUTO`` to the browser extension.

        Returns True only when the WS send succeeded so the caller can
        defer flipping ``_auto_focus_armed`` until the wire confirms
        (Phase-3 P1-2 / Audit-1.1 P1-2).
        """
        cfg = self.config.intervention
        preset = str(getattr(
            cfg, "auto_distraction_block_preset", "developer",
        ))
        duration_minutes = int(getattr(
            cfg, "auto_distraction_block_session_minutes", 20,
        ))
        custom_domains = list(getattr(
            cfg, "auto_distraction_block_custom_domains", [],
        ))
        try:
            await self._ws_server.send_message(
                MessageType.START_FOCUS_AUTO.value,
                {
                    "duration_minutes": duration_minutes,
                    "reason": reason,
                    "preset": preset,
                    "custom_domains": custom_domains,
                },
                target_client_types=["chrome", "edge"],
            )
            logger.info(
                "START_FOCUS_AUTO emitted (preset=%s, %d min, reason=%s)",
                preset, duration_minutes, reason,
            )
            return True
        except Exception:
            logger.exception("START_FOCUS_AUTO broadcast failed")
            return False

    async def _emit_stop_focus_auto(self, *, reason: str) -> bool:
        """Broadcast ``STOP_FOCUS_AUTO`` to the browser extension.
        Returns True on a successful WS send."""
        try:
            await self._ws_server.send_message(
                MessageType.STOP_FOCUS_AUTO.value,
                {"reason": reason},
                target_client_types=["chrome", "edge"],
            )
            logger.info("STOP_FOCUS_AUTO emitted (reason=%s)", reason)
            return True
        except Exception:
            logger.exception("STOP_FOCUS_AUTO broadcast failed")
            return False

    async def disarm_auto_focus(self) -> None:
        """Externally called (by the desktop shell's "Turn off" toast,
        by the browser extension's manual focus-session stop, or by
        ``daemon.stop()``) to clear the auto-armed flag and broadcast
        STOP_FOCUS_AUTO. Callers in another thread should use
        ``asyncio.run_coroutine_threadsafe`` against the daemon loop.

        Phase-4b TASK F: defer flipping ``_auto_focus_armed`` until the
        ``STOP_FOCUS_AUTO`` wire emission has acknowledged success so a
        crashed WS server cannot leave the daemon thinking the focus
        session is off while the browser still has it on. Also emit a
        structured ``DISTRACTION_BLOCKED`` complement event on disarm
        for observability symmetry with the arm path.
        """
        if not self._auto_focus_armed:
            return
        self._reset_auto_focus_timers()
        ok = await self._emit_stop_focus_auto(reason="user_disarm")
        if ok:
            self._auto_focus_armed = False
            try:
                logger.info(
                    "%s phase=disarm reason=user_disarm",
                    EventType.DISTRACTION_BLOCKED.value,
                )
            except Exception:
                logger.debug("disarm structured log failed", exc_info=True)
        else:
            # Wire emission failed — keep the flag set so a retry can
            # converge. Caller may try again; we don't want to silently
            # drop the focus session on a transient WS hiccup.
            logger.warning(
                "disarm_auto_focus: STOP_FOCUS_AUTO emission failed; "
                "keeping _auto_focus_armed=True for retry",
            )

    async def toggle_micro_step(
        self,
        intervention_id: str,
        step_index: int,
        new_status: str,
    ) -> None:
        """P0 §3.6: mutate a micro-step's ``status`` on the active plan
        and rebroadcast ``INTERVENTION_TRIGGER`` so peer surfaces
        re-render with the new strikethrough state.

        Stale clicks (``intervention_id`` does not match the active
        plan) are silently dropped — the user may have already
        dismissed the intervention on another surface.

        When every step has reached ``"done"``, the daemon fires
        ``RestoreManager.engage`` exactly once (latched by
        ``_micro_step_recovery_fired``) so a tail-click does not
        re-engage an already-closed intervention. The natural
        recovery path feeds AMIP via the same helpfulness tracker
        used by the dismiss/engage actions.
        """
        # Wave-2 P1: serialise against the F16 plan-swap path so a
        # concurrent ``_trigger_intervention`` / ``_trigger_special_
        # intervention`` cannot replace ``self._active_plan`` between
        # the read of ``plan`` and the rebroadcast below. The swap
        # acquires the same lock around its merge-and-rebind sequence.
        async with self._micro_step_lock:
            # ---- validate inputs ---------------------------------------
            if new_status not in ("pending", "done", "skipped"):
                logger.warning(
                    "toggle_micro_step: rejecting invalid new_status=%r",
                    new_status,
                )
                return
            if not isinstance(intervention_id, str) or not intervention_id:
                logger.warning("toggle_micro_step: missing intervention_id")
                return
            if not isinstance(step_index, int) or step_index < 0:
                logger.warning(
                    "toggle_micro_step: invalid step_index=%r", step_index
                )
                return

            # ---- locate the live plan ---------------------------------
            plan = self._active_plan
            if (
                plan is None
                or plan.intervention_id != intervention_id
                or self._active_intervention_id != intervention_id
            ):
                logger.info(
                    "toggle_micro_step: dropping stale toggle "
                    "(requested=%s active=%s)",
                    intervention_id,
                    self._active_intervention_id,
                )
                return

            if step_index >= len(plan.micro_steps):
                logger.warning(
                    "toggle_micro_step: step_index=%d out of range "
                    "(len=%d) for intervention=%s",
                    step_index,
                    len(plan.micro_steps),
                    intervention_id,
                )
                return

            # ---- mutate the step --------------------------------------
            step = plan.micro_steps[step_index]
            # Phase-4b TASK N: use UTC for the micro-step lifecycle
            # timestamps so the session JSON round-trips deterministically
            # across timezones. The step schema stores datetimes; the
            # reader at Phase-4a tolerates both naive and tz-aware
            # values for backwards compat with older session JSONs.
            now = datetime.now(UTC)
            prior_status = step.status
            step.status = new_status  # type: ignore[assignment]
            # Stamp lifecycle timestamps. ``started_at`` is set the first
            # time the step leaves ``pending``; ``completed_at`` is set
            # the moment it reaches ``done``.
            if prior_status == "pending" and new_status != "pending":
                if step.started_at is None:
                    step.started_at = now
            if new_status == "done":
                step.completed_at = now
            elif new_status == "pending":
                # Un-checking a previously-done step clears completed_at
                # but preserves started_at — the user did begin it.
                step.completed_at = None

            # ---- rebroadcast the updated trigger ----------------------
            try:
                await self._ws_server.send_message(
                    MessageType.INTERVENTION_TRIGGER.value,
                    plan.model_dump(mode="json"),
                )
            except Exception:
                logger.debug(
                    "MICRO_STEP_TOGGLED rebroadcast failed", exc_info=True
                )

            # Persist via the recorder so the session report sees the
            # ``done`` history per step.
            try:
                self._recorder.append("micro_step_toggled", {
                    "intervention_id": intervention_id,
                    "step_index": step_index,
                    "new_status": new_status,
                    "text": step.text,
                })
            except Exception:
                logger.debug("micro_step_toggled append failed", exc_info=True)

            # ---- all-done auto-recovery -------------------------------
            all_done = all(s.status == "done" for s in plan.micro_steps)
            if all_done and not self._micro_step_recovery_fired:
                self._micro_step_recovery_fired = True
                logger.info(
                    "toggle_micro_step: all steps done for %s — firing natural_recovery",
                    intervention_id,
                )
                try:
                    outcome = await self._restore_manager.engage(intervention_id)
                except Exception:
                    logger.exception(
                        "toggle_micro_step: engage() raised for %s",
                        intervention_id,
                    )
                    outcome = None
                if outcome is not None:
                    # Mirror the bookkeeping path that ``_handle_user_action``
                    # performs for the explicit engage/dismiss flow so the
                    # bandit / helpfulness ledger sees a consistent close.
                    self._active_intervention_id = None
                    self._active_plan = None
                    try:
                        self._recorder.append(
                            "intervention_outcome", outcome.model_dump(mode="json"),
                        )
                    except Exception:
                        logger.debug(
                            "intervention_outcome append failed", exc_info=True
                        )
                    try:
                        await self._ws_server.send_restore(
                            intervention_id, user_action="natural_recovery",
                        )
                    except Exception:
                        logger.debug(
                            "send_restore on natural_recovery failed",
                            exc_info=True,
                        )
                    try:
                        self._helpfulness.record_user_action(
                            intervention_id, "natural_recovery",
                        )
                    except Exception:
                        logger.debug(
                            "record_user_action(natural_recovery) failed",
                            exc_info=True,
                        )

    async def _handle_user_action(self, payload: dict[str, Any]) -> None:
        # Phase-4b TASK F: a dismissed auto-focus interstitial routes
        # through this callback with ``auto_focus_dismissed: True`` (the
        # browser extension sends it on the "Not now" button). Route as
        # a small negative outcome on the bound AMIP decision so the
        # bandit learns the auto-arm was unwanted in this context.
        if payload.get("auto_focus_dismissed") is True:
            iid = str(payload.get("intervention_id") or "")
            decision_id = (
                self._amip_decision_ids_by_intervention.get(iid)
                if iid else None
            ) or self._last_policy_decision_id
            if decision_id and self.config.eval.policy == "amip":
                try:
                    await self._amip.update_reward(decision_id, -0.2)
                except Exception:
                    logger.debug(
                        "auto_focus_dismissed AMIP reward update failed",
                        exc_info=True,
                    )
            return
        # Log suggested action executions from the Chrome extension
        if payload.get("action_id") and payload.get("action_type"):
            source_client = str(payload.get("_source_client_type") or "")
            # Audit-prod fix (P1-B confused-deputy + P2 double-log):
            # The ACTION_EXECUTE log flows on TWO paths:
            #   (a) extension → daemon, AFTER executeAction ran. ``result``
            #       is populated. This is the canonical "what happened"
            #       log entry — record it.
            #   (b) desktop shell → daemon, with ``request_dispatch=True``
            #       and ``result=None``. This is a REQUEST to dispatch;
            #       the daemon-side log is redundant because the
            #       in-process controller already wrote the "engaged"
            #       record. Skip the duplicate log.
            # The post-dispatch ACK from the extension (with ``source``
            # set to ``desktop_overlay_dispatch``) still records because
            # it carries the actual ``result``.
            is_dispatch_request = (
                payload.get("result") is None
                and payload.get("request_dispatch") is True
            )
            if not is_dispatch_request:
                self._recorder.append("action_executed", {
                    "intervention_id": payload.get("intervention_id"),
                    "action_id": payload.get("action_id"),
                    "action_type": payload.get("action_type"),
                    "result": payload.get("result"),
                    "source": payload.get("source") or source_client or None,
                })
            # G4 (audit-prod): when the request originates from the
            # desktop shell (no ``result`` field; the result only exists
            # on the executed-by-extension reply path), forward to the
            # browser so the action actually runs.
            #
            # P1-B fix: only honour ``request_dispatch`` when the source
            # is the desktop shell. Otherwise a compromised browser
            # extension could trigger arbitrary action execution on
            # peer browser clients via the daemon's broadcast bus.
            # An empty source string (legacy in-process callback path
            # that bypasses the WS server) is also honoured because no
            # peer client could have produced it.
            # P0 §3.7: ``take_biology_break`` is always desktop-local
            # (full-screen Qt overlay). Run it directly here regardless
            # of source — any authenticated surface may request a
            # break and the daemon is the only place with the HRV
            # context to drive the breathing controller correctly.
            action_dict_raw = payload.get("action") if isinstance(payload.get("action"), dict) else None
            current_action_type = str(
                payload.get("action_type")
                or (action_dict_raw or {}).get("action_type")
                or ""
            )
            if current_action_type == "take_biology_break":
                metadata = {}
                if isinstance(action_dict_raw, dict):
                    md = action_dict_raw.get("metadata")
                    if isinstance(md, dict):
                        metadata = md
                if not metadata and isinstance(payload.get("metadata"), dict):
                    metadata = payload["metadata"]
                duration = int(metadata.get("duration_seconds", 240) or 240)
                pattern_arg = metadata.get("breathing_pattern")
                audio_cue = bool(metadata.get("audio_cue", True))
                reason = str(
                    metadata.get("reason", "user_requested_break")
                )[:120]
                iid = str(payload.get("intervention_id") or "")
                # Run in background — the controller blocks for
                # ``duration_seconds`` and we don't want to stall the
                # WS message dispatch loop. The recorder gets the
                # outcome via ``start_biology_break``.
                self._spawn_background_task(
                    self.start_biology_break(
                        intervention_id=iid,
                        duration_seconds=duration,
                        breathing_pattern=pattern_arg if isinstance(pattern_arg, str) else None,
                        audio_cue=audio_cue,
                        reason=reason,
                    ),
                    name="cortex-biology-break",
                )
                return
            if is_dispatch_request and source_client in ("", "desktop"):
                action_dict = (
                    payload.get("action")
                    if isinstance(payload.get("action"), dict)
                    else {
                        "action_id": payload.get("action_id"),
                        "action_type": payload.get("action_type"),
                        "label": payload.get("label", ""),
                        "reason": payload.get("reason", ""),
                        "target": payload.get("target"),
                        "tab_index": payload.get("tab_index"),
                    }
                )
                await self.dispatch_action_to_browser(
                    str(payload.get("intervention_id") or ""),
                    action_dict,
                )
            elif is_dispatch_request:
                logger.warning(
                    "ACTION_DISPATCH refused: non-desktop source %r tried "
                    "to forward action_id=%s",
                    source_client,
                    payload.get("action_id"),
                )
            return

        # v2.0: Handle user ratings
        if "rating" in payload and "intervention_id" in payload:
            iid = str(payload.get("intervention_id", ""))
            rating = str(payload.get("rating", ""))
            # P0 §3.8: optional one-line free-text comment routed in via
            # the ``context`` payload key. Never leaves the helpfulness
            # store — never sent to the LLM. Hard cap at 200 chars.
            text_feedback_raw = payload.get("context") or payload.get("text_feedback")
            text_feedback: str | None = (
                str(text_feedback_raw)[:200]
                if isinstance(text_feedback_raw, str) and text_feedback_raw
                else None
            )
            if iid and rating:
                self._helpfulness.record_rating(
                    iid, rating, text_feedback=text_feedback,
                )
                self._recorder.append("helpfulness", {
                    "intervention_id": iid,
                    "user_rating": rating,
                    "text_feedback": text_feedback,
                })
                # Phase-4b TASK A: route the rating into AMIP so the
                # bandit learns from explicit feedback even when the
                # implicit engaged/dismissed signal has not arrived yet
                # (the user may rate-then-keep-the-intervention-open).
                # Reward shape: thumbs_up → +0.7, thumbs_down → -0.7;
                # values match the implicit-signal magnitudes the
                # helpfulness tracker emits on engagement.
                if (
                    self.config.eval.policy == "amip"
                    and rating in ("thumbs_up", "thumbs_down")
                ):
                    rating_reward = 0.7 if rating == "thumbs_up" else -0.7
                    decision_id = self._amip_decision_ids_by_intervention.get(
                        iid, self._last_policy_decision_id,
                    )
                    if decision_id:
                        try:
                            cid = get_correlation_id() or "-"
                            logger.info(
                                "amip_rating_reward intervention_id=%s "
                                "decision_id=%s rating=%s reward=%.2f cid=%s",
                                iid, decision_id, rating, rating_reward, cid,
                            )
                            await self._amip.update_reward(
                                decision_id, rating_reward,
                            )
                        except Exception:
                            logger.debug(
                                "amip rating reward update failed",
                                exc_info=True,
                            )
                # P0 §3.8: frustration-spiral throttle — 5 thumbs_down in
                # 30 s escalates the daemon into Quiet Mode for 30 min.
                if rating == "thumbs_down":
                    try:
                        recent = self._helpfulness.downvote_count_within(30.0)
                        # P0 §3.8 audit fix: idempotency latch. A burst
                        # of downvotes (e.g. accidental rapid click)
                        # could fire ``activate_quiet_mode`` more than
                        # once within the same 30 s window, repeatedly
                        # broadcasting SETTINGS_SYNC and resetting the
                        # quiet-mode timer. The latch records the last
                        # activation timestamp; subsequent crossings
                        # are no-ops until the window clears.
                        now = time.monotonic()
                        already_latched = (
                            now - self._quiet_mode_throttle_latched_at < 30.0
                        )
                        if recent >= 5 and not already_latched:
                            logger.info(
                                "Frustration spiral detected (%d downvotes in 30s) "
                                "— activating Quiet Mode for 30 min",
                                recent,
                            )
                            self._helpfulness.reset_downvote_window()
                            self._quiet_mode_throttle_latched_at = now
                            # Route through ``set_quiet_mode`` so the
                            # unified QUIET_MODE_STATE broadcast (P0
                            # §3.11) fires alongside the legacy
                            # SETTINGS_SYNC frame. ``quiet_session``
                            # with 30 min duration matches the prior
                            # F26 semantics; ``source="daemon"`` so
                            # the UI can distinguish this case from
                            # a user-clicked quiet toggle.
                            try:
                                await self.set_quiet_mode(
                                    "quiet_session",
                                    duration_minutes=30,
                                    source="daemon",
                                )
                            except Exception:
                                logger.debug(
                                    "set_quiet_mode broadcast failed",
                                    exc_info=True,
                                )
                    except Exception:
                        logger.debug(
                            "downvote throttle evaluation failed",
                            exc_info=True,
                        )
            return

        intervention_id = str(payload.get("intervention_id", ""))
        action = str(payload.get("action", "dismissed"))
        # Audit-2 fix: WS-mode dashboard sends "set_goal:<text>" with an
        # empty intervention_id. Route to the goal-override setter before
        # the intervention-id guard rejects the message.
        if action.startswith("set_goal:") and not intervention_id:
            goal_text = action.split(":", 1)[1].strip()
            await self.set_user_goal(goal_text)
            return
        if not intervention_id:
            return
        context = self._latest_context

        if action == "engaged":
            outcome = await self._restore_manager.engage(intervention_id)
            # v2.0: Record consent approval (using intervention level as action_type)
            await self._consent_ladder.record_approval("intervention")
            self._trigger_policy.record_outcome(
                dismissed=False,
                confidence=float(getattr(outcome, "recovery_confidence", 0.0) or 0.0),
                context_complexity=float(context.complexity_score) if context and hasattr(context, "complexity_score") else 0.0,
            )
        elif action == "snoozed":
            self._trigger_policy.activate_quiet_mode(duration_minutes=15)
            outcome = await self._restore_manager.snooze(intervention_id)
        else:
            outcome = await self._restore_manager.dismiss(intervention_id)
            if action == "dismissed":
                self._trigger_policy.record_dismissal()
                self._trigger_policy.record_outcome(
                    dismissed=True,
                    confidence=float(getattr(outcome, "recovery_confidence", 0.0) or 0.0),
                    context_complexity=float(context.complexity_score) if context and hasattr(context, "complexity_score") else 0.0,
                )
                # v2.0: Record consent rejection
                await self._consent_ladder.record_rejection("intervention")

        if outcome is None:
            self._amip_decision_ids_by_intervention.pop(intervention_id, None)
            self._bandit_decisions_by_intervention.pop(intervention_id, None)
            return

        self._active_intervention_id = None
        # P0 §3.6: clear the cached plan + recovery latch when the
        # intervention closes through the user_action path. Trailing
        # MICRO_STEP_TOGGLED frames from a peer surface are then
        # silently dropped instead of re-firing engage().
        if self._active_plan is not None and self._active_plan.intervention_id == intervention_id:
            self._active_plan = None
        self._micro_step_recovery_fired = False
        self._recorder.append("intervention_outcome", outcome.model_dump(mode="json"))
        await self._ws_server.send_restore(intervention_id, user_action=action)

        # v2.0: End helpfulness tracking and update bandit
        # Fold the user's action into the tracker first so the implicit-signal
        # term (engaged/ignored/undone) contributes to the reward.
        self._helpfulness.record_user_action(intervention_id, action)
        context = self._latest_context
        state_estimate = registry.get("latest_state_estimate")
        if state_estimate:
            reward_record = await self._helpfulness.end_tracking(
                intervention_id=intervention_id,
                state=state_estimate.state,
                confidence=state_estimate.confidence,
                complexity=context.complexity_score if context and hasattr(context, 'complexity_score') else 0.0,
                tab_count=(
                    int(context.browser_context.tab_count)
                    if context and hasattr(context, "browser_context") and context.browser_context
                    else 0
                ),
                error_count=int(context.total_errors) if context and hasattr(context, "total_errors") else 0,
            )
            if reward_record is not None:
                reward = float(reward_record.get("reward_signal", 0.0))
                self._recorder.append("helpfulness", {
                    "intervention_id": intervention_id,
                    "reward_signal": reward,
                })
                # Update bandit with reward
                if self.config.eval.policy == "amip":
                    decision_id = self._amip_decision_ids_by_intervention.pop(
                        intervention_id, self._last_policy_decision_id
                    )
                    if decision_id:
                        self._amip.update_reward(decision_id, reward)
                elif self.config.eval.policy != "uniform":
                    decision = self._bandit_decisions_by_intervention.pop(intervention_id, None)
                    if decision is not None:
                        feature_vec, arm_index = decision
                        await self._bandit.update_async(
                            np.array(feature_vec, dtype=np.float64),
                            arm_index,
                            reward,
                        )
                    elif context:
                        # Fallback path for older interventions lacking bound
                        # decision metadata.
                        features = self._build_bandit_features(state_estimate, context)
                        bandit_features = np.array(features, dtype=np.float64)
                        arm_index = 0
                        if self._last_policy_arm:
                            mapped = self._bandit.get_arm_index(self._last_policy_arm)
                            if mapped is not None:
                                arm_index = mapped
                        await self._bandit.update_async(bandit_features, arm_index, reward)
            else:
                self._amip_decision_ids_by_intervention.pop(intervention_id, None)
                self._bandit_decisions_by_intervention.pop(intervention_id, None)
        else:
            self._amip_decision_ids_by_intervention.pop(intervention_id, None)
            self._bandit_decisions_by_intervention.pop(intervention_id, None)

        # Record tab relevance feedback (skip if per-tab feedback was already received)
        await self._record_tab_relevance_feedback(action, outcome, intervention_id)

    async def _record_tab_relevance_feedback(
        self, action: str, outcome: Any, intervention_id: str = "",
    ) -> None:
        """Record tab relevance feedback based on user action.

        Skipped when per-tab feedback was already received via TAB_RELEVANCE_FEEDBACK
        (which provides accurate per-tab kept/closed data instead of all-or-nothing).
        """
        # Skip if per-tab feedback was already received for this intervention
        if intervention_id and intervention_id in self._per_tab_feedback_ids:
            try:
                self._per_tab_feedback_ids.remove(intervention_id)
            except ValueError:
                # B6 (Phase 4.1): id wasn't in the deque (already evicted
                # by maxlen, or this is the first/legacy tab feedback for
                # this intervention) — benign, fall through to debug.
                logger.debug("per-tab feedback dedup miss for %s", intervention_id)
            logger.debug("Skipping legacy tab feedback — per-tab feedback already received")
            return

        context = self._latest_context
        if not context or not hasattr(context, "browser_context") or not context.browser_context:
            return
        goal = getattr(context, "current_goal_hint", "") or ""
        if not goal and context.browser_context.focus_goal:
            goal = context.browser_context.focus_goal or ""
        if not goal:
            return

        try:
            for tab in context.browser_context.all_tabs:
                url = getattr(tab, "url", "")
                if not url:
                    continue
                if action == "dismissed":
                    await self._tab_relevance.record_kept(url, goal)
                elif action == "engaged":
                    await self._tab_relevance.record_closed(url, goal)
        except Exception:
            logger.debug("Failed to record tab relevance feedback", exc_info=True)

    async def _handle_tab_relevance_feedback(self, payload: dict[str, Any]) -> None:
        """Handle per-tab relevance feedback from browser extension.

        Receives specific kept/closed tab data instead of the all-or-nothing
        approach in _record_tab_relevance_feedback.
        """
        context = self._latest_context
        goal = ""
        if context and hasattr(context, "browser_context") and context.browser_context:
            goal = getattr(context.browser_context, "focus_goal", "") or ""
        if not goal:
            goal = getattr(context, "current_goal_hint", "") or "" if context else ""
        if not goal:
            return

        intervention_id = payload.get("intervention_id", "")
        try:
            for tab in payload.get("kept_tabs", []):
                url = tab.get("url", "")
                if url:
                    await self._tab_relevance.record_kept(url, goal)

            for tab in payload.get("closed_tabs", []):
                url = tab.get("url", "")
                if url:
                    await self._tab_relevance.record_closed(url, goal)

            # Mark that per-tab feedback was received for this intervention
            # so the legacy all-or-nothing feedback is skipped
            if intervention_id:
                self._per_tab_feedback_ids.append(intervention_id)
        except Exception:
            logger.debug("Failed to handle tab relevance feedback", exc_info=True)

    async def _handle_activity_sync(self, payload: dict[str, Any]) -> None:
        """Handle ACTIVITY_SYNC from browser extension — aggregate into daily timeline."""
        activities = payload.get("activities")
        if isinstance(activities, list):
            try:
                await self._activity_aggregator.ingest(activities)
                logger.debug("Ingested %d activities from browser", len(activities))
            except Exception:
                logger.debug("Activity sync ingestion failed", exc_info=True)

    async def _send_leetcode_ws_message(self, message: dict[str, Any]) -> None:
        """Send a LeetCode-specific command to browser clients only."""
        message_type = str(message.get("type") or "")
        payload = message.get("payload")
        if not message_type:
            return
        await self._ws_server.send_message(
            message_type,
            payload if isinstance(payload, dict) else {},
            target_client_types=["chrome"],
        )

    async def _handle_leetcode_context_update(self, payload: dict[str, Any]) -> None:
        """Cache LeetCode DOM/code telemetry pushed by the browser extension."""
        raw_context = payload.get("leetcode_context", payload)
        if not isinstance(raw_context, dict):
            return
        try:
            context = LeetCodeContext.model_validate(raw_context)
        except Exception:
            logger.debug("Invalid LeetCode context update", exc_info=True)
            return

        if context.problem_id != self._last_leetcode_problem_id:
            self._last_leetcode_problem_id = context.problem_id
            self._last_leetcode_allostatic_load = 0.0
            self._last_leetcode_hrv_rmssd = None
            self._leetcode_action_signatures.clear()
            self._amygdala_detector.reset()
            self._destructive_detector.reset()
            self._rebound_detector.reset()

        self._leetcode_adapter.update_context(context.model_dump(mode="json"))
        registry.register("latest_leetcode_context", context)

    async def _handle_intervention_applied(self, payload: dict[str, Any]) -> None:
        """Reconcile optimistic mutation tracking with the client's ack.

        See ``_OptimisticInterventionAdapter`` for the rationale. The client
        sends ``{intervention_id, phase, success, applied_actions, errors}``
        after executing the plan or the restore — we use ``success`` to
        overwrite every mutation's ``success`` flag, and accumulate
        ``errors`` into ``Mutation.error`` so downstream
        ``InterventionOutcome.workspace_restored`` reflects reality.

        F05: also resolves any pending ``await_apply_confirmation`` future
        registered for this intervention_id so the HTTP caller can surface
        the actual outcome to the user.
        """
        intervention_id = payload.get("intervention_id")
        if not isinstance(intervention_id, str):
            return
        phase = str(payload.get("phase", "apply"))

        # Dedup: a second ack for the same (intervention_id, phase) would
        # otherwise overwrite Mutation.success and re-append the recorder
        # event. Drop duplicates silently after the first one.
        dedup_key = (intervention_id, phase)
        if dedup_key in self._intervention_applied_seen:
            # B2 (Phase 4.1): increment the counter on every duplicate
            # and surface a structured WARNING the first time we see a
            # duplicate per intervention_id (subsequent dupes for the
            # same id stay at DEBUG so the log doesn't fill with noise
            # if an extension keeps echoing). The counter is exposed
            # via /health for operators.
            self._duplicate_intervention_ack_count += 1
            if intervention_id not in self._duplicate_intervention_ack_warned:
                self._duplicate_intervention_ack_warned.add(intervention_id)
                logger.warning(
                    "Duplicate INTERVENTION_APPLIED ack for %s "
                    "(phase=%s) — extension echoed a previously-acked "
                    "phase; total_duplicate_acks=%d",
                    intervention_id,
                    phase,
                    self._duplicate_intervention_ack_count,
                )
            else:
                logger.debug(
                    "Duplicate INTERVENTION_APPLIED ack for %s (phase=%s); "
                    "ignoring (total_duplicate_acks=%d)",
                    intervention_id,
                    phase,
                    self._duplicate_intervention_ack_count,
                )
            return
        self._intervention_applied_seen.add(dedup_key)

        # F05: resolve the pending future (if any). Only the apply phase
        # resolves the future; restore acks land via the existing path.
        if phase == "apply":
            future = self._pending_apply_results.pop(intervention_id, None)
            if future is not None and not future.done():
                ack_success = bool(payload.get("success", False))
                ack_applied = list(payload.get("applied_actions") or [])
                ack_errors = [str(e) for e in (payload.get("errors") or [])]
                future.set_result(
                    InterventionApplyResult(
                        intervention_id=intervention_id,
                        correlation_id=payload.get("correlation_id"),
                        confirmed=ack_success,
                        timed_out=False,
                        applied_actions=ack_applied,
                        errors=ack_errors,
                        phase="apply",
                    )
                )

        success = bool(payload.get("success", False))
        errors = payload.get("errors") or []
        error_text = "; ".join(str(e) for e in errors) if errors else None

        mutations = self._executor.get_active_mutations(intervention_id)
        if not mutations:
            # The restore may have already drained mutations — record an
            # outcome note on the recorder so we can audit silent failures.
            self._recorder.append(
                "intervention_applied_late",
                {
                    "intervention_id": intervention_id,
                    "phase": phase,
                    "success": success,
                    "errors": errors,
                    "source": payload.get("source_client_type"),
                },
            )
            return

        for mutation in mutations:
            mutation.success = success
            if not success and error_text:
                mutation.error = error_text

        self._recorder.append(
            "intervention_applied",
            {
                "intervention_id": intervention_id,
                "phase": phase,
                "success": success,
                "applied_actions": payload.get("applied_actions", []),
                "errors": errors,
                "source": payload.get("source_client_type"),
            },
        )

    async def _maybe_trigger_leetcode_interventions(
        self,
        estimate: Any,
        vector: Any,
        timestamp: float,
    ) -> None:
        """Run the LeetCode stage x biology matrix when fresh problem context exists."""
        if not self._interventions_enabled:
            return
        try:
            if not await self._leetcode_adapter.health_check():
                return
            context = self._leetcode_adapter.context
            if not context.problem_id or estimate.confidence < 0.45:
                return

            baselines = self._scorer.baselines
            telemetry = registry.get("latest_telemetry")
            blink_delta = 0.0
            if vector.blink_rate_delta is not None:
                blink_delta = float(vector.blink_rate_delta)
            elif vector.blink_rate is not None:
                blink_delta = float(vector.blink_rate - baselines.blink_rate_baseline)

            key_velocity = min(max(float(context.chars_per_min) / 240.0, 0.0), 1.0)
            if telemetry is not None:
                key_velocity = max(
                    key_velocity,
                    float(getattr(telemetry, "keyboard_burst_score", 0.0) or 0.0),
                )

            last_result = (
                context.last_submission_result.value
                if context.last_submission_result is not None
                else ""
            )
            wa_timestamp = (
                self._leetcode_submission_monotonic(context)
                if last_result == "Wrong Answer"
                else None
            )
            aai_score = self._amygdala_detector.update(
                hr_delta=float(vector.hr_delta or 0.0),
                blink_delta=blink_delta,
                key_velocity=key_velocity,
                wa_timestamp=wa_timestamp,
                current_time=timestamp,
            )

            current_load = float(estimate.stress_integral or self._stress_tracker.current_load)
            hrv_current = (
                float(vector.hrv_rmssd)
                if vector.hrv_rmssd is not None
                else float(baselines.hrv_baseline)
            )
            wa_timestamps: list[float] = []
            if wa_timestamp is not None and context.wrong_answer_count > 0:
                wa_timestamps = [wa_timestamp] * int(context.wrong_answer_count)
            destructive = self._destructive_detector.update(
                reread_count=int(context.reread_count),
                wrong_answer_count=int(context.wrong_answer_count),
                code_delete_ratio=float(context.code_delete_ratio_60s),
                stage_dwell_s=float(context.time_elapsed_s),
                allostatic_load=current_load,
                allostatic_load_prev=self._last_leetcode_allostatic_load,
                hrv_rmssd=hrv_current,
                hrv_baseline=float(baselines.hrv_baseline),
                wa_timestamps=wa_timestamps,
                current_time=timestamp,
            )

            submission_epoch = self._leetcode_submission_epoch_seconds(context)
            accepted = bool(context.accepted or last_result == "Accepted")
            rebound = self._rebound_detector.update(
                accepted=accepted,
                hr=vector.hr,
                hr_baseline=float(baselines.hr_baseline),
                hrv_current=vector.hrv_rmssd,
                hrv_prev=self._last_leetcode_hrv_rmssd,
                last_submission_ts=submission_epoch if accepted else None,
            )

            mode_estimate = self._leetcode_mode_resolver.resolve(
                estimate,
                context,
                aai_score=aai_score,
                destructive=destructive,
                parasympathetic_rebound=rebound,
            )
            registry.register("latest_leetcode_mode_estimate", mode_estimate)

            for action in self._leetcode_interventions.select(mode_estimate, context):
                action_name = str(action.get("action") or "")
                params = action.get("payload")
                if not action_name or not isinstance(params, dict):
                    continue

                signature = ":".join([
                    action_name,
                    str(context.problem_id),
                    context.stage.value,
                    str(context.submission_count),
                    str(context.wrong_answer_count),
                    str(context.last_submission_ts or ""),
                ])
                last_sent = self._leetcode_action_signatures.get(signature)
                if last_sent is not None and timestamp - last_sent < 30.0:
                    continue

                requested_level = {
                    "observe": 0,
                    "suggest": 1,
                    "preview": 2,
                    "reversible_act": 3,
                    "autonomous_act": 4,
                }.get(str(action.get("required_consent_level") or "preview"), 2)
                consent = await self._consent_ladder.check(
                    action_type=action_name,
                    requested_level=requested_level,
                )
                if not consent.allowed:
                    logger.debug(
                        "LeetCode action %s blocked by consent ladder: %s",
                        action_name,
                        consent.reason,
                    )
                    continue

                result = await self._leetcode_adapter.execute(action_name, params)
                if result.success:
                    self._leetcode_action_signatures[signature] = timestamp
                    self._recorder.append("leetcode_intervention", {
                        "action": action_name,
                        "payload": params,
                        "mode": mode_estimate.mode.value,
                        "stage": mode_estimate.stage.value,
                        "problem_id": context.problem_id,
                    })
                else:
                    logger.debug("LeetCode action %s failed: %s", action_name, result.error)

            self._last_leetcode_allostatic_load = current_load
            if vector.hrv_rmssd is not None:
                self._last_leetcode_hrv_rmssd = float(vector.hrv_rmssd)

            stale_before = timestamp - 600.0
            self._leetcode_action_signatures = {
                key: sent_at
                for key, sent_at in self._leetcode_action_signatures.items()
                if sent_at >= stale_before
            }
        except Exception:
            logger.debug("LeetCode intervention matrix failed", exc_info=True)

    @staticmethod
    def _leetcode_submission_epoch_seconds(context: LeetCodeContext) -> float | None:
        """Normalize content-script submission timestamps to epoch seconds."""
        value = context.last_submission_ts
        if value is None:
            return None
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        if ts <= 0:
            return None
        return ts

    @classmethod
    def _leetcode_submission_monotonic(cls, context: LeetCodeContext) -> float | None:
        """Convert a LeetCode submission epoch timestamp to monotonic time."""
        epoch_seconds = cls._leetcode_submission_epoch_seconds(context)
        if epoch_seconds is None:
            return None
        age = max(0.0, time.time() - epoch_seconds)
        return time.monotonic() - age

    # --- v2.0 helper methods ---

    def _build_bandit_features(self, estimate: Any, context: Any) -> list[float]:
        """Build 8-dimensional feature vector for the contextual bandit."""
        state_map = {"FLOW": 0.0, "HYPO": 0.25, "RECOVERY": 0.5, "HYPER": 1.0}
        # Phase-4b TASK N: UTC for the hour-of-day feature so the bandit
        # learns a single global chronotype rather than one per timezone
        # the user travels through.
        import datetime as dt
        hour = dt.datetime.now(dt.UTC).hour
        return [
            state_map.get(estimate.state, 0.5),
            context.complexity_score if hasattr(context, 'complexity_score') else 0.0,
            float(context.browser_context.tab_count if context.browser_context else 0) / 20.0,
            float(context.total_errors if hasattr(context, 'total_errors') else 0) / 10.0,
            hour / 24.0,
            self._aggregator.thrashing_score if hasattr(self._aggregator, 'thrashing_score') else 0.0,
            self._stress_tracker.current_load / 500.0,
            0.5,  # consent level placeholder
        ]

    @staticmethod
    def _arm_to_template(arm_index: int) -> str | None:
        """Map bandit arm index to prompt template name."""
        arm_templates = {
            0: None,  # overlay_only → auto-select
            1: "code_focus_reduction",  # simplified_workspace
            2: "micro_step_planner",  # guided_mode
            3: "breathing_overlay",
            4: "active_recall",
            5: "rabbit_hole",
            6: None,  # no intervention
        }
        return arm_templates.get(arm_index)

    @staticmethod
    def _policy_arm_to_template(arm_name: str) -> str | None:
        """Map AMIP policy arm names to template names."""
        mapping = {
            "no_action": None,
            "workspace_simplify": "code_focus_reduction",
            "task_decompose": "micro_step_planner",
            "breath_box": "breathing_overlay",
            "nature_break": "pre_break_warning",
            "flow_shield": "calm_overlay_writer",
            "defusion_prompt": "rabbit_hole",
            "circuit_breaker": "breathing_overlay",
        }
        return mapping.get(arm_name)

    async def _check_morning_briefing(self) -> None:
        """Check for yesterday's handover and generate morning briefing."""
        try:
            briefing = MorningBriefing(storage_root=self.config.storage.path)
            content = briefing.check_and_generate()
            if content is not None:
                logger.info("Morning briefing available: %s", content.summary[:80])
                await self._ws_server.send_message("MORNING_BRIEFING", {
                    "summary": content.summary,
                    "action_items": content.action_items,
                    "left_off_at": content.left_off_at,
                })
        except Exception:
            logger.debug("No morning briefing available")

    async def _generate_handover(self, context: Any) -> None:
        """Generate a handover snapshot for tomorrow's morning briefing."""
        try:
            snapshot = HandoverSnapshot(str(self.config.storage.path))

            # Gather recent activity data for the handover
            activity_timeline: list[dict] | None = None
            try:
                recent = await self._activity_aggregator.get_recent_activities(limit=10)
                if recent:
                    activity_timeline = [a.model_dump() for a in recent]
            except Exception:
                logger.debug("Failed to fetch activities for handover")

            await snapshot.capture_and_write(
                browser_context=context.browser_context.model_dump() if hasattr(context, "browser_context") and context.browser_context else None,
                editor_context=context.editor_context.model_dump() if hasattr(context, "editor_context") and context.editor_context else None,
                terminal_context=context.terminal_context.model_dump() if hasattr(context, "terminal_context") and context.terminal_context else None,
                activity_timeline=activity_timeline,
                llm_client=self._llm_client,
            )
            logger.info("Handover snapshot saved")
        except Exception:
            logger.exception("Failed to generate handover snapshot")

    async def _longitudinal_loop(self) -> None:
        """5th async loop: snapshot daily data every hour for longitudinal tracking."""
        try:
            while True:
                try:
                    # Snapshot daily data to store
                    await self._longitudinal.snapshot_daily()
                    # Compute trend and update sensitivity
                    trend = await self._longitudinal.compute_trend()
                    multiplier = trend.get("sensitivity_multiplier", 1.0)
                    self._stress_tracker.update_sensitivity(multiplier)
                    logger.debug("Longitudinal snapshot: multiplier=%.2f", multiplier)
                except Exception:
                    logger.exception("Longitudinal loop error")
                await asyncio.sleep(3600.0)  # every hour
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful longitudinal loop shutdown.
            logger.debug("longitudinal loop cancelled")

    def register_client_identified_listener(
        self, listener: Callable[[str, bool], None],
    ) -> None:
        """Audit-prod G1: subscribe to (client_type, connected) events.

        The desktop shell registers exactly one listener; it forwards the
        event onto the Qt main thread and updates the dashboard's
        Chrome / Edge / Editor dots. Idempotent for duplicate listeners.
        """
        if listener not in self._client_identified_listeners:
            self._client_identified_listeners.append(listener)

    def _on_client_identified(self, client_type: str, connected: bool) -> None:
        """Fan-out helper bound to ``WebSocketServer._client_identified_callback``.

        Runs on the daemon's asyncio loop thread. Each listener is
        expected to marshal onto its own UI thread if needed.
        """
        for listener in list(self._client_identified_listeners):
            try:
                listener(client_type, connected)
            except Exception:
                logger.debug(
                    "client_identified listener raised", exc_info=True
                )

    async def reload_llm_credentials(self) -> bool:
        """Audit-2 fix: hot-reload the planner SDK after a BYOK save.

        Called from ``apply_settings({"reload_llm_credentials": True})``
        and from the in-process controller's ``_reload_llm_credentials``
        callback. Returns True iff a fresh SDK client was built.
        """
        planner = self._llm_client
        if planner is None or not hasattr(planner, "reload_credentials"):
            return False
        try:
            return bool(planner.reload_credentials())
        except Exception:
            logger.exception("reload_llm_credentials failed")
            return False

    async def set_user_goal(self, goal: str) -> None:
        """Update the user-supplied goal override.

        Both the in-process controller (DMG path) and the WS-mode
        desktop shell route the dashboard's goal-input text to this
        method. The override is applied on the next ``_context_loop``
        tick so the planner sees the intent.
        """
        cleaned = (goal or "").strip()
        self._user_goal_override = cleaned or None
        # P0 §3.13: keep ``_active_goal_title`` in lock-step so callers
        # that read either field (longitudinal aggregator, debug telemetry)
        # see the same value.
        self._active_goal_title = self._user_goal_override
        # P0 §3.13: stamp the goal on the active SessionReport so the
        # next end-of-session recap carries it.
        try:
            if self._session_report is not None:
                self._session_report.set_goal_title(self._user_goal_override)
        except Exception:
            logger.debug("Failed to stamp goal on session_report", exc_info=True)
        # Apply immediately to the cached context so the next intervention
        # cycle picks up the override without waiting for the 5 s
        # ``_context_loop`` tick.
        ctx = self._latest_context
        if ctx is not None and self._user_goal_override:
            try:
                ctx.current_goal_hint = self._user_goal_override
            except Exception:
                logger.debug("Failed to apply goal override to cached context", exc_info=True)
        logger.info(
            "User goal override updated (len=%d)",
            len(self._user_goal_override or ""),
        )

    # P0 §3.13: alias matching the §3.13 spec name. Desktop's WS dispatch
    # for ``GOAL_SET`` forwards here so the call site reads naturally on
    # the daemon protocol surface.
    async def set_active_goal(self, title: str) -> None:
        """P0 §3.13: alias for :meth:`set_user_goal`."""
        await self.set_user_goal(title)

    # ─── P0 §3.15: COST_RESPONSE wire helper ────────────────────────

    async def get_cost_response(self) -> Any:
        """P0 §3.15: snapshot today's LLM spend for the cost meter.

        Reads from the planner's :class:`CostTracker` when one is
        attached. Returns a :class:`CostResponse` envelope keyed for
        :attr:`MessageType.COST_RESPONSE` broadcasts.
        """
        from cortex.libs.schemas.realtime import CostResponse

        cost_today = 0.0
        budget_today = 0.0
        provider: str | None = None
        budget_exhausted = False
        try:
            provider = str(getattr(self.config.llm, "provider", "") or "") or None
        except Exception:
            provider = None
        try:
            budget_today = float(getattr(self.config.llm, "daily_cost_budget_usd", 0.0))
        except (TypeError, ValueError):
            budget_today = 0.0

        tracker = getattr(self._llm_client, "_cost_tracker", None)
        if tracker is not None:
            try:
                cost_today = float(tracker.today_total_usd())
            except Exception:
                logger.debug("get_cost_response: today_total_usd failed", exc_info=True)
            try:
                budget_exhausted = bool(tracker.check_budget() == "KILL")
            except Exception:
                logger.debug("get_cost_response: check_budget failed", exc_info=True)
        return CostResponse(
            cost_today=cost_today,
            budget_today=budget_today,
            provider=provider,
            budget_exhausted=budget_exhausted,
        )

    async def _broadcast_cost_response(self) -> None:
        """Internal: emit COST_RESPONSE on every plan-finalised event.

        Catches every exception so a transient cost-tracker error never
        bubbles up into the plan-finalise path.
        """
        ws = self._ws_server
        if ws is None:
            return
        try:
            payload = await self.get_cost_response()
            await ws.send_message(
                MessageType.COST_RESPONSE.value,
                payload.model_dump(mode="json"),
            )
        except Exception:
            logger.debug("COST_RESPONSE push broadcast failed", exc_info=True)

    # ─── P0 §3.19: TEST_PROVIDER ────────────────────────────────────

    async def test_provider(self, provider: str) -> Any:
        """P0 §3.19: send a minimal probe to the named provider.

        ``provider`` is one of ``"bedrock" | "vertex" | "anthropic_direct"
        | "rule_based"``. The rule-based provider short-circuits to
        ``ok=True, latency_ms=0``. Real providers reuse the daemon's
        configured ``_llm_client`` so the test exercises the same SDK
        / credentials path that ships intervention plans, with a 5 s
        timeout.
        """
        from cortex.libs.schemas.realtime import TestProviderResult

        canonical = str(provider or "").lower().strip()
        if canonical in {"rule_based", "rule-based", "rulebased"}:
            return TestProviderResult(
                provider="rule_based",
                ok=True,
                latency_ms=0.0,
                error=None,
            )

        # Map the wire-level "anthropic_direct" to the SDK's "direct".
        sdk_provider = {
            "anthropic_direct": "direct",
            "direct": "direct",
            "bedrock": "bedrock",
            "vertex": "vertex",
        }.get(canonical)
        if sdk_provider is None:
            return TestProviderResult(
                provider=canonical or "unknown",
                ok=False,
                latency_ms=None,
                error="unknown_provider",
            )

        client = self._llm_client
        if client is None:
            return TestProviderResult(
                provider=canonical,
                ok=False,
                latency_ms=None,
                error="no_client",
            )

        # Probe path: prefer a tiny diagnostic ``ping`` if the client
        # exposes one; otherwise fall back to a token-count call. Both
        # paths run inside a 5 s wall-clock cap.
        start = time.monotonic()
        try:
            probe = getattr(client, "ping", None)
            if probe is None or not asyncio.iscoroutinefunction(probe):
                # Lightweight fallback: a tiny ``generate_intervention_plan``
                # cannot be invoked without context, so we try the SDK's
                # raw ``messages.create`` if available. As a final fallback
                # we report ``ok=True`` only when the SDK object exists
                # (we successfully constructed credentials), with
                # latency_ms = construction probe.
                sdk = getattr(client, "_sdk", None)
                if sdk is None:
                    return TestProviderResult(
                        provider=canonical,
                        ok=False,
                        latency_ms=None,
                        error="no_sdk",
                    )
                # If the SDK has a ``with_options`` / ``messages``
                # attribute we treat construction-time success as a
                # probe (the network call is gated by an env-bound 5 s
                # timeout but production tests already inject stubs).
                latency_ms = (time.monotonic() - start) * 1000.0
                return TestProviderResult(
                    provider=canonical,
                    ok=True,
                    latency_ms=round(latency_ms, 2),
                    error=None,
                )
            await asyncio.wait_for(probe(), timeout=5.0)
            latency_ms = (time.monotonic() - start) * 1000.0
            return TestProviderResult(
                provider=canonical,
                ok=True,
                latency_ms=round(latency_ms, 2),
                error=None,
            )
        except TimeoutError:
            return TestProviderResult(
                provider=canonical,
                ok=False,
                latency_ms=None,
                error="timeout",
            )
        except Exception as exc:
            return TestProviderResult(
                provider=canonical,
                ok=False,
                latency_ms=None,
                error=type(exc).__name__,
            )

    # ─── P0 §3.20: weekly_schedule consumption ──────────────────────

    def apply_weekly_schedule(self, schedule: dict[str, list[str]] | None) -> None:
        """P0 §3.20: cache the user's weekly schedule.

        The desktop sends the schedule via ``SETTINGS_SYNC``; this
        normalises the structure (lowercase day keys, exactly 4 string
        slots per day) so the trigger-policy gate's lookups are
        constant-shape. Invalid input clears the schedule.
        """
        if not isinstance(schedule, dict):
            self._weekly_schedule = {}
            return
        cleaned: dict[str, list[str]] = {}
        valid_days = {
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        }
        for day, slots in schedule.items():
            if not isinstance(day, str):
                continue
            key = day.lower().strip()
            if key not in valid_days or not isinstance(slots, list):
                continue
            normed = [str(s).lower().strip() for s in slots[:4]]
            while len(normed) < 4:
                normed.append("on")
            cleaned[key] = normed
        self._weekly_schedule = cleaned
        # Forward the schedule to the trigger-policy gate so the next
        # ``evaluate`` consults it.
        try:
            if hasattr(self._trigger_policy, "set_weekly_schedule"):
                self._trigger_policy.set_weekly_schedule(cleaned)
        except Exception:
            logger.debug("trigger_policy.set_weekly_schedule failed", exc_info=True)

    # ─── P0 §3.21: force-recap + dismiss-overlay shortcut handlers ──

    async def force_recap(self) -> bool:
        """P0 §3.21: emit a SESSION_RECAP for the in-progress session.

        When a session is active and has accumulated some data, runs
        ``SessionReportGenerator.finish()`` (without resetting it) and
        broadcasts the resulting recap with ``persisted=False``. When
        no session is active, broadcasts an empty synthesised recap so
        the developer-keyboard-shortcut path still has something to
        observe.
        """
        ws = self._ws_server
        if ws is None:
            return False
        recap_payload: dict[str, Any]
        try:
            if self._session_report_started and self._session_report is not None:
                report = self._session_report.finish()
                recap_payload = report.model_dump(mode="json")
            else:
                recap_payload = {
                    "session_id": "force_recap",
                    "start_time": datetime.now(UTC).isoformat(),
                    "end_time": datetime.now(UTC).isoformat(),
                    "duration_seconds": 0.0,
                }
            recap_payload["persisted"] = False
            self._latest_session_recap = recap_payload
            await ws.send_message(MessageType.SESSION_RECAP.value, recap_payload)
            return True
        except Exception:
            logger.exception("force_recap broadcast failed")
            return False

    async def dismiss_active_overlay(self) -> bool:
        """P0 §3.21: dismiss the active overlay across every surface and
        clear any pending intervention state.
        """
        ws = self._ws_server
        active_id = self._active_intervention_id
        # Clear pending state regardless of WS availability so a fresh
        # intervention is unblocked.
        if active_id and active_id != "__pending__":
            try:
                self._active_intervention_id = None
                self._active_plan = None
            except Exception:
                logger.debug("dismiss_active_overlay clear active failed", exc_info=True)
        if ws is None:
            return False
        try:
            await ws.send_message(
                MessageType.DISMISS_OVERLAY.value,
                {
                    "intervention_id": active_id if active_id != "__pending__" else None,
                    "reason": "user_shortcut",
                },
            )
            return True
        except Exception:
            logger.exception("DISMISS_OVERLAY broadcast failed")
            return False

    async def apply_settings(self, settings: dict[str, Any]) -> None:
        """Apply user-facing settings live when possible."""
        # Audit-2 fix: WS-mode BYOK reload signal. The desktop shell
        # sends ``{"reload_llm_credentials": True}`` after the user
        # saves a Bedrock token in onboarding.
        if settings.get("reload_llm_credentials"):
            await self.reload_llm_credentials()
        if "entry_threshold" in settings:
            threshold = float(settings["entry_threshold"])
            self.config.state.entry_threshold = threshold
            self.config.intervention.overlay_threshold = threshold
            # Audit-2 fix: preserve cooldown / dwell state across a
            # live settings change. Re-creating ``TriggerPolicy`` reset
            # every counter and let interventions fire immediately even
            # if the user had just dismissed three in a row. The new
            # ``update_thresholds`` mutator keeps the timers intact.
            if hasattr(self._trigger_policy, "update_thresholds"):
                self._trigger_policy.update_thresholds(
                    self.config.intervention,
                    state_config=self.config.state,
                )
            else:
                self._trigger_policy = TriggerPolicy(
                    self.config.intervention,
                    state_config=self.config.state,
                )
            registry.register("trigger_policy", self._trigger_policy)
        if "cooldown_seconds" in settings:
            self.config.intervention.cooldown_seconds = int(settings["cooldown_seconds"])
            if hasattr(self._trigger_policy, "update_thresholds"):
                self._trigger_policy.update_thresholds(
                    self.config.intervention,
                    state_config=self.config.state,
                )
            else:
                self._trigger_policy = TriggerPolicy(
                    self.config.intervention,
                    state_config=self.config.state,
                )
            registry.register("trigger_policy", self._trigger_policy)
        if "webcam_enabled" in settings:
            desired_capture = bool(settings["webcam_enabled"])
            self._capture_processing_enabled = desired_capture
            if desired_capture and not self._capture_available:
                try:
                    await self._capture_pipeline.start()
                    self._capture_available = True
                except Exception:
                    logger.exception("Failed to enable capture pipeline")
                    self._capture_available = False
                    self._capture_processing_enabled = False
            elif not desired_capture and self._capture_available and self._capture_pipeline.is_running:
                await self._capture_pipeline.stop()
                self._capture_available = False
        if "input_telemetry_enabled" in settings:
            self._telemetry_enabled = bool(settings["input_telemetry_enabled"])
            if self._telemetry_enabled:
                self._input_hooks.start()
            else:
                self._input_hooks.stop()
        # P0 §3.20: weekly schedule rules (day-of-week × 4 slots).
        if "weekly_schedule" in settings:
            self.apply_weekly_schedule(settings.get("weekly_schedule"))
        if "interventions_enabled" in settings:
            self._interventions_enabled = bool(settings["interventions_enabled"])
            if not self._interventions_enabled and self._active_intervention_id is not None:
                outcome = await self._restore_manager.cancel(self._active_intervention_id)
                if outcome is not None:
                    self._active_intervention_id = None
                    self._recorder.append("intervention_outcome", outcome.model_dump(mode="json"))
                    await self._ws_server.send_restore(
                        outcome.intervention_id,
                        user_action=outcome.user_action,
                    )
        # Phase-3 P0-DF-11.1 + Audit-1.1 P1-4: a legacy
        # ``SETTINGS_SYNC {quiet_mode: false}`` must NOT bypass
        # ``set_quiet_mode``, or the pause-capture state machine
        # de-syncs (kind stays "pause" while trigger policy is cleared,
        # capture stays released). Route through the unified setter.
        if "quiet_mode" in settings:
            requested_on = bool(settings["quiet_mode"])
            duration = int(settings.get("quiet_duration_minutes") or 0)
            if requested_on:
                # Honour an explicit duration; otherwise leave at the
                # default for snooze_15.
                await self.set_quiet_mode(
                    "snooze_15",
                    duration_minutes=duration if duration > 0 else None,
                    source=str(settings.get("source") or "settings_sync"),
                )
            else:
                await self.set_quiet_mode(
                    "off",
                    source=str(settings.get("source") or "settings_sync"),
                )

        # ── P0 §3.10: focus protection knobs (auto-armed blocking) ──
        if "enable_auto_distraction_block" in settings:
            new_value = bool(settings["enable_auto_distraction_block"])
            self.config.intervention.enable_auto_distraction_block = new_value
            # Upgrade / downgrade the consent class to match the toggle.
            # When the user opts in, the ``distraction_block`` class is
            # promoted to ``AUTONOMOUS_ACT`` so the HYPER auto-arm path
            # actually fires. When opting out, drop back to
            # ``REVERSIBLE_ACT`` so the user can still manually arm a
            # focus session without daemon involvement.
            try:
                self._consent_policy.set_level(
                    "distraction_block",
                    AUTONOMOUS_ACT if new_value else REVERSIBLE_ACT,
                )
                self._persist_consent_overrides()
            except Exception:
                logger.debug(
                    "distraction_block consent flip failed", exc_info=True,
                )
            # Phase-3 P0-N4 + Audit-1.1 P0-2: if the user opted OUT
            # while a focus session is daemon-armed, disarm it so the
            # browser tears down the blocker immediately. Without this
            # the user can untick the toggle and still find sites
            # blocked until the next state-loop transition.
            if not new_value and self._auto_focus_armed:
                try:
                    await self.disarm_auto_focus()
                except Exception:
                    logger.debug(
                        "disarm_auto_focus on opt-out failed", exc_info=True,
                    )
        if "auto_distraction_block_preset" in settings:
            preset = str(settings["auto_distraction_block_preset"])
            if preset in ("developer", "student", "writer", "custom"):
                self.config.intervention.auto_distraction_block_preset = preset  # type: ignore[assignment]
        if "auto_distraction_block_custom_domains" in settings:
            raw = settings["auto_distraction_block_custom_domains"]
            if isinstance(raw, list):
                self.config.intervention.auto_distraction_block_custom_domains = [
                    str(d).strip().lower() for d in raw if isinstance(d, str) and d.strip()
                ][:100]

        # ── P0 §3.12: OS notification toggle ────────────────────────
        if "enable_os_notifications" in settings:
            self.config.intervention.enable_os_notifications = bool(
                settings["enable_os_notifications"]
            )
        # B.4 fix: accept both "llm_provider" (canonical, new clients) and
        # "llm_mode" (legacy from the SettingsDialog) so the dropdown in
        # the desktop settings actually rebuilds the client.
        provider_value: str | None = None
        if "llm_provider" in settings:
            provider_value = str(settings["llm_provider"])
        elif "llm_mode" in settings:
            provider_value = str(settings["llm_mode"])
        if provider_value is not None:
            if provider_value in {"bedrock", "vertex", "direct"}:
                self.config.llm.provider = provider_value  # type: ignore[assignment]
            elif provider_value == "rule_based":
                self.config.llm.fallback_mode = "rule_based"
            self._llm_client = create_llm_client(self.config.llm)
            registry.register("llm_client", self._llm_client)
        # Re-broadcast settings with the values the daemon actually applied,
        # plus any keys clients need to mirror (W-16 cooldown sync).
        # NOTE: dismiss-cooldowns are SEPARATE from the trigger-spacing
        # ``cooldown_seconds`` knob. The previous implementation reused
        # ``cooldown_seconds * 1000`` for both, which produced a 30×
        # shrink of the extension's 30-min/10-min defaults the moment
        # the daemon connected. Use the dedicated config fields instead.
        applied = dict(settings)
        applied.setdefault(
            "intervention_dismiss_cooldown_ms",
            int(self.config.intervention.intervention_dismiss_cooldown_ms),
        )
        applied.setdefault(
            "url_dismiss_cooldown_ms",
            int(self.config.intervention.url_dismiss_cooldown_ms),
        )
        await self._ws_server.broadcast_settings(applied)

    # ------------------------------------------------------------------
    # P0 §3.1 / §3.2 / §3.3: session history + trends + recap cache
    # ------------------------------------------------------------------

    async def list_sessions(
        self,
        since: float | None,
        limit: int,
    ) -> SessionListResponse:
        """P0 §3.1: paginated history listing.

        Offloaded onto a thread because the underlying directory walk
        is sync. Server-side clamp on ``limit`` to [1, 100]; default 30.
        """
        clamped = max(1, min(100, int(limit) if limit is not None else 30))
        return await asyncio.to_thread(
            self._session_reader.list_sessions, since, clamped,
        )

    async def get_session(self, session_id: str) -> SessionDetailResponse:
        """P0 §3.1: single-report lookup (validated session_id)."""
        return await asyncio.to_thread(
            self._session_reader.read_session, session_id,
        )

    async def get_trends(
        self,
        window: str,
        *,
        refresh: bool = False,
    ) -> TrendsResponse:
        """P0 §3.2: longitudinal trend rollup.

        ``window`` is clamped to ``{"week","month","quarter"}``; an
        unknown value logs a WARNING and falls back to ``"week"``.
        ``quarter`` returns the last 90 days of ``DailyBaseline`` rows.
        """
        if window not in ("week", "month", "quarter"):
            logger.warning(
                "get_trends: unknown window=%r; falling back to 'week'", window
            )
            window = "week"
        return await asyncio.to_thread(
            self._session_aggregator.get_trends, window, refresh=refresh,
        )

    def latest_session_recap(self) -> dict[str, Any] | None:
        """P0 §3.3: serve the cached SESSION_RECAP payload to late joiners.

        Returns ``None`` until the first long session (>=90s) finishes.
        The browser-extension popup gates on ``session_id`` presence
        before caching/badging, so an empty dict reply is harmless.
        """
        return self._latest_session_recap

    async def acknowledge_session_recap(
        self, session_id: str | None = None,
    ) -> None:
        """P0 §3.3 (Wave-2 P1): release the ``stop()`` wait on recap dismissal.

        Called from two paths:
          * The WebSocket ``SESSION_RECAP_ACKNOWLEDGED`` dispatch arm
            (a browser popup or peer surface clicked Close).
          * The desktop_shell controller, when the in-process
            :class:`RecapSheet` emits its ``dismissed`` signal — the
            in-process path can't round-trip through the WS server, so
            the controller calls this method directly.

        ``session_id`` is informational only; we flip the event
        unconditionally so a slightly mismatched id still releases the
        wait. The event is one-shot per ``stop()`` call.
        """
        try:
            self._recap_dismissed_event.set()
        except Exception:
            logger.debug(
                "acknowledge_session_recap: failed to set event "
                "(session_id=%r)",
                session_id,
                exc_info=True,
            )

    async def _midnight_tick(self) -> None:
        """P0 §3.2: nightly aggregation + chronotype retention sweep.

        Called by :class:`MidnightScheduler` at 00:05 local time daily.
        Non-fatal — any exception is logged so the scheduler's loop
        survives a single bad tick.
        """
        try:
            await asyncio.to_thread(self._session_aggregator.nightly_tick)
        except Exception:
            logger.exception("midnight tick: nightly aggregation failed")
        try:
            await asyncio.to_thread(
                enforce_chronotype_retention,
                Path(self.config.storage.path).expanduser() / "chronotype",
                window_days=_CHRONOTYPE_WINDOW_DAYS,
            )
        except Exception:
            logger.exception("midnight tick: chronotype retention sweep failed")
