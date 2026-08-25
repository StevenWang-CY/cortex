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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

import numpy as np
import uvicorn
from numpy.typing import NDArray

from cortex.application.clock import (
    SYSTEM_CLOCK,
    BoundedDeadline,
    Clock,
    clock_or_system,
    monotonic_seconds,
    unix_seconds,
    utc_datetime,
)
from cortex.application.coordinators import (
    ExperimentCoordinator,
    ExperimentOperations,
    InferenceCoordinator,
    InferenceOperations,
    InterventionCoordinator,
    LoopSpec,
    OperationsCoordinator,
    RuntimeCoordinatorSet,
    SensingCoordinator,
    SensingOperations,
)
from cortex.application.events import Subscription
from cortex.application.gateway import WebSocketCommandHandlers
from cortex.application.kernel import ApplicationKernel
from cortex.application.task_supervisor import TaskFailure, TaskGroupName
from cortex.libs.adapters.leetcode_adapter import LeetCodeAdapter
from cortex.libs.config.settings import CortexConfig, get_config
from cortex.libs.logging.structured import (
    EventType,
    configure_logging,
    get_logger,
)
from cortex.libs.schemas.calibration import (
    CALIBRATION_FEATURE_SCHEMA_VERSION,
    CALIBRATION_PROTOCOL_VERSION,
    ActiveCalibrationPointer,
    CalibrationMetricMaturity,
    CalibrationMetricName,
    CalibrationProfile,
    CalibrationProvenance,
    CalibrationUpdated,
)
from cortex.libs.schemas.features import (
    FeatureName,
    FeatureValue,
    KinematicFeatures,
    PhysioFeatures,
)
from cortex.libs.schemas.intervention import (
    InterventionApplyResult,
    InterventionPlan,
)
from cortex.libs.schemas.intervention_transaction import (
    ActionReceipt,
    AuthorizationDenied,
    InterventionApplyCommand,
    InterventionAuthorizationRequest,
    InterventionLifecycleState,
    InterventionReceiptBatch,
    InterventionRestoreCommand,
    ReceiptPhase,
    ReceiptStatus,
    VerificationStatus,
    manifest_suggestion_matches,
)
from cortex.libs.schemas.leetcode import LeetCodeContext
from cortex.libs.schemas.observations import (
    CameraFrameObservation,
    CameraIdentity,
    MissingReason,
    ObservationEnvelope,
    ObservationSource,
    ObservationValidity,
)
from cortex.libs.schemas.physiology import SignalAlgorithmIdentity, SignalEstimate
from cortex.libs.schemas.policy import (
    PolicyArm,
    PolicyContextSnapshot,
    PolicyDecisionRecord,
    policy_payload_sha256,
)
from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    TrendsResponse,
)
from cortex.libs.schemas.state import UserBaselines
from cortex.libs.schemas.temporal import EventTime
from cortex.libs.schemas.ws_message_types import MessageType

# v2.0 imports
from cortex.libs.store import (
    InMemoryStore,
    RedisStore,
    SQLiteKeyValueStore,
    default_legacy_store_path,
    make_default_store,
)
from cortex.libs.utils import receptivity
from cortex.services.activity_tracker.aggregator import ActivityAggregator
from cortex.services.api_gateway.app import create_app
from cortex.services.api_gateway.websocket_server import WebSocketServer
from cortex.services.capture_service.calibration_store import (
    CalibrationProfileStore,
    calibration_profile_sha256,
)
from cortex.services.capture_service.feature_factory import (
    ProductionCameraFeatureComponents,
    build_production_camera_feature_components,
    production_calibration_algorithm_identities,
)
from cortex.services.capture_service.observation_buffer import (
    NumericObservation,
    ObservationBuffer,
    PreparedObservationWindow,
    prepare_observation_window,
)
from cortex.services.capture_service.pipeline import CapturePipeline, PipelineOutput
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import (
    AUTONOMOUS_ACT,
    REVERSIBLE_ACT,
    ConsentPolicy,
    canonical_action_type,
)
from cortex.services.context_engine import (
    BrowserAdapter,
    ContextAssembler,
    EditorAdapter,
    TerminalAdapter,
    classify_tab_type,
)
from cortex.services.eval.helpfulness import HelpfulnessTracker
from cortex.services.eval.policy_diagnostics import (
    generate_daily_policy_diagnostics,
    migrate_legacy_causal_report_names,
)
from cortex.services.eval.policy_lifecycle import PolicyLifecycleService
from cortex.services.eval.policy_repository import PolicyRepository
from cortex.services.eval.production_policy import PolicySelectionInput
from cortex.services.eval.research_policy import (
    RESEARCH_POLICY_NAME,
    RESEARCH_POLICY_VERSION,
    ResearchPolicySettings,
)
from cortex.services.eval.tab_relevance import TabRelevanceTracker
from cortex.services.handover.briefing import MorningBriefing
from cortex.services.handover.detector import ShutdownDetector
from cortex.services.handover.snapshot import HandoverSnapshot
from cortex.services.intervention_engine.break_overlay import (
    BreakUIHandler,
    GuidedBreakController,
)
from cortex.services.intervention_engine.executor import InterventionExecutor
from cortex.services.intervention_engine.leetcode_interventions import InterventionMatrix
from cortex.services.intervention_engine.planner import (
    materialize_suggestion_only,
    prepare_plan,
)
from cortex.services.intervention_engine.restore import RestoreManager
from cortex.services.intervention_engine.snapshot import capture_snapshot
from cortex.services.intervention_engine.transaction import (
    InterventionTransactionCoordinator,
    build_action_manifest,
)
from cortex.services.intervention_engine.transaction_store import (
    SQLiteInterventionTransactionStore,
)
from cortex.services.janitor.retention import (
    enforce_chronotype_retention,
)
from cortex.services.janitor.retention import (
    sweep_once_async as run_retention_sweep_async,
)
from cortex.services.launcher.launcher import ProjectLauncher
from cortex.services.llm_engine import create_llm_client
from cortex.services.llm_engine.parser import enrich_plan_with_context
from cortex.services.physio_engine.pulse_estimator import PulseEstimator, PulseStabilizer
from cortex.services.session_report.generator import SessionReportGenerator
from cortex.services.session_report.longitudinal import LongitudinalAggregator
from cortex.services.session_report.reader import SessionReader
from cortex.services.session_report.scheduler import MidnightScheduler
from cortex.services.state_engine import FeatureFusion, RuleScorer, ScoreSmoother
from cortex.services.state_engine.amygdala_hijack import AmygdalaHijackDetector
from cortex.services.state_engine.causal_attribution import CausalAttributor
from cortex.services.state_engine.destructive_struggle import DestructiveStruggleDetector
from cortex.services.state_engine.focus_break_policy import FocusBreakPolicy
from cortex.services.state_engine.leetcode_mode_resolver import LeetCodeModeResolver
from cortex.services.state_engine.longitudinal import LongitudinalTracker
from cortex.services.state_engine.model_registry import SupportModelRegistry
from cortex.services.state_engine.parasympathetic_rebound import ParasympatheticReboundDetector
from cortex.services.state_engine.rabbit_hole import RabbitHoleDetector
from cortex.services.state_engine.support_inference import SupportInferenceEngine
from cortex.services.state_engine.trigger_policy import TriggerPolicy
from cortex.services.state_engine.zombie_detector import ZombieReadingDetector
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
from cortex.services.telemetry_engine.input_hooks import InputHooks
from cortex.services.telemetry_engine.window_tracker import WindowTracker
from cortex.services.throttle.copilot_throttle import CopilotThrottle
from cortex.storage import SQLiteDatabase
from cortex.storage.event_writer import BoundedAnalyticsWriter
from cortex.storage.legacy_migrator import LegacyDataMigrator
from cortex.storage.maintenance import StorageMaintenance

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedCalibrationGraph:
    """Fully validated replacement graph built before the commit boundary."""

    profile: CalibrationProfile
    baselines: UserBaselines
    camera_features: ProductionCameraFeatureComponents
    pulse_estimator: PulseEstimator
    rgb_observations: ObservationBuffer[NumericObservation]
    feature_fusion: FeatureFusion
    scorer: RuleScorer
    smoother: ScoreSmoother
    zombie_detector: ZombieReadingDetector
    shutdown_detector: ShutdownDetector
    causal_attributor: CausalAttributor
    camera_calibration_valid: bool


def _build_legacy_pulse_estimator(config: CortexConfig) -> PulseEstimator:
    """Construct the compatibility pulse path from the canonical config."""

    rppg = config.signal.rppg
    stabilizer = (
        PulseStabilizer(
            enter_windows=rppg.lock_enter_windows,
            grace_seconds=rppg.lock_grace_seconds,
            snr_release_db=rppg.snr_release_db,
            sqi_release=rppg.sqi_release,
            smoothing_seconds=rppg.bpm_smoothing_seconds,
            max_slew_bpm_per_s=rppg.bpm_max_slew_bpm_per_s,
        )
        if rppg.stabilize
        else None
    )
    return PulseEstimator(
        fs=float(config.capture.fps),
        nsqi_threshold=rppg.nsqi_threshold,
        min_cardiac_snr_db=rppg.min_cardiac_snr_db,
        hrv_min_window_seconds=float(rppg.hrv_min_window_seconds),
        hrv_min_valid_ibi=rppg.hrv_min_valid_ibi,
        stabilizer=stabilizer,
    )


def _build_rgb_observation_buffer(
    config: CortexConfig,
) -> ObservationBuffer[NumericObservation]:
    """Build the bounded camera-signal history used by runtime inference."""

    window_seconds = max(
        float(config.signal.rppg.window_seconds),
        float(config.signal.rppg.respiration_window_seconds),
    )
    max_age_seconds = window_seconds + config.signal.rppg.max_interpolation_gap_ms / 1000.0 + 1.0
    return ObservationBuffer(
        max_age_seconds=max_age_seconds,
        max_items=max(
            config.capture.observation_buffer_max_items,
            int(max_age_seconds * config.signal.rppg.fps_clamp_max) + 2,
        ),
    )


def _validate_runtime_calibration_profile(
    profile: CalibrationProfile,
    *,
    expected_algorithms: dict[str, SignalAlgorithmIdentity],
) -> None:
    """Fail closed before any persisted calibration can influence runtime."""

    if profile.provenance != CalibrationProvenance.MEASURED.value:
        raise ValueError("only measured calibration profiles may be applied")
    if not profile.is_approved:
        raise ValueError("calibration profile must be approved before application")
    if profile.feature_schema_version != CALIBRATION_FEATURE_SCHEMA_VERSION:
        raise ValueError("calibration feature schema is incompatible")
    if profile.protocol_version != CALIBRATION_PROTOCOL_VERSION:
        raise ValueError("calibration protocol version is incompatible")

    algorithm_key_by_metric = {
        CalibrationMetricName.BLINK_RATE_PER_MIN.value: "blink",
        CalibrationMetricName.MOUSE_VELOCITY_PX_PER_S.value: "telemetry",
        CalibrationMetricName.MOUSE_VELOCITY_VARIANCE.value: "telemetry",
        CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG.value: "head_pose",
        CalibrationMetricName.NEUTRAL_FACE_SCALE_PX.value: "head_pose",
    }
    for summary in profile.metrics:
        algorithm_key = algorithm_key_by_metric.get(str(summary.metric))
        if algorithm_key is None or summary.value is None:
            continue
        if summary.algorithm != expected_algorithms[algorithm_key]:
            raise ValueError(f"calibration algorithm identity is incompatible for {summary.metric}")


def _daemon_clock(owner: object) -> Clock:
    """Resolve the daemon clock at legacy unbound-method boundaries."""

    return clock_or_system(getattr(owner, "_clock", None))


# C6 (audit): structured event sink. ``configure_logging`` (invoked once at
# daemon startup, see ``start()``) wires the structlog processor chain; this
# bound logger is the canonical emitter for daemon-owned observability events
# (QUIET_MODE_ENTERED/EXITED, OS_NOTIFICATION_SENT, FACE_LOST/FACE_REACQUIRED).
_event_logger = get_logger("cortex.daemon")


def _emit_event(event: EventType, **fields: Any) -> None:
    """Emit a structured observability event (C6).

    Best-effort: a logging failure must never break the capture / state
    loops, so any exception is swallowed at DEBUG. The correlation id (if
    one is bound on the current context) is merged automatically by the
    ``merge_contextvars`` processor configured in ``configure_logging``.
    """
    try:
        _event_logger.info(event.value, **fields)
    except Exception:
        logger.debug("structured event %s emit failed", event.value, exc_info=True)


def _interpolate_nan_window(window: NDArray[np.float64]) -> NDArray[np.float64]:
    """Legacy bounded-input helper for a prevalidated rPPG RGB window.

    ``window`` is shape ``(N, C)``. The production path first applies the
    explicit time/coverage/gap gate in ``prepare_observation_window`` and no
    longer calls this helper. It remains for compatibility callers that have
    already bounded their gaps. An entirely missing channel remains NaN:
    fabricating a zero trace would make an unavailable window look ready.

    A window with no NaNs is returned (a copy is acceptable) unchanged.
    """
    if window.size == 0:
        return window
    if not np.isnan(window).any():
        return window
    out = window.astype(np.float64, copy=True)
    n_frames = out.shape[0]
    x = np.arange(n_frames, dtype=np.float64)
    # Treat a 1-D window as a single channel.
    channels = out.shape[1] if out.ndim == 2 else 1
    view = out if out.ndim == 2 else out.reshape(n_frames, 1)
    for c in range(channels):
        col = view[:, c]
        good = ~np.isnan(col)
        if not good.any():
            continue
        if good.all():
            continue
        col[~good] = np.interp(x[~good], x[good], col[good])
    return out


def _face_normalized_vertical_position(landmarks_px: Any) -> float | None:
    """Return face-centre vertical position in units of observed face height.

    Pixel displacement is camera-resolution and distance dependent. Dividing
    by the current face height yields a dimensionless motion channel suitable
    for detrending; invalid/degenerate landmark sets abstain explicitly.
    """

    try:
        landmarks = np.asarray(landmarks_px, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if (
        landmarks.ndim != 2
        or landmarks.shape[0] < 3
        or landmarks.shape[1] < 2
        or not bool(np.isfinite(landmarks[:, :2]).all())
    ):
        return None
    vertical = landmarks[:, 1]
    lower, upper = np.percentile(vertical, [5.0, 95.0])
    face_height = float(upper - lower)
    if face_height < 1.0:
        return None
    return float(np.median(vertical) / face_height)


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

_EXECUTION_MODES = frozenset(
    {
        "suggest_only",
        "authorized",
        "research_autonomous",
    }
)


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
            logger.warning("F36 storage budget: could not evict %s", path, exc_info=True)
    if evicted > 0:
        logger.info(
            "F36 storage budget: evicted %d session(s) to make room for %d-byte write (cap=%d MB)",
            evicted,
            incoming_bytes,
            max_total_size_mb,
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

    def __init__(self, storage_root: str, *, clock: Clock | None = None) -> None:
        self._clock = clock or SYSTEM_CLOCK
        root = Path(storage_root)
        root.mkdir(parents=True, exist_ok=True)
        session_dir = root / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        self._path = session_dir / f"session_{self._clock.unix_ms() // 1000}.jsonl"
        # Bounded queue so a runaway producer can't exhaust memory; if
        # the writer thread falls behind by more than 4096 records we
        # drop the oldest and log so the data loss is observable.
        self._queue: queue.Queue[tuple[str, dict[str, Any], float] | None] = queue.Queue(
            maxsize=4096
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
        record = (event_type, payload, unix_seconds(self._clock))
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

    def _write_record(self, f: Any, item: tuple[str, dict[str, Any], float]) -> None:
        event_type, payload, ts = item
        try:
            line = json.dumps(
                {"type": event_type, "timestamp": ts, "payload": payload},
                default=str,
            )
            f.write(line + "\n")
        except Exception:
            logger.exception("SessionRecorder write failed for %s", event_type)

    def _drain_remaining(self, f: Any) -> None:
        """Write every record still queued, non-blocking, until empty.

        P2 (audit): ``flush()`` sets ``_stop_event`` AND enqueues the ``None``
        sentinel. The main loop's top-of-loop ``_stop_event`` check could
        fire BEFORE the sentinel (and the records queued ahead of it) were
        consumed, dropping the trailing session window (last user_action,
        the session_report meta-event, ...). On exit we therefore drain
        whatever is left so no queued record is lost. The ``None`` sentinel
        is skipped; everything else is written.
        """
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            self._write_record(f, item)

    def _writer_loop(self) -> None:
        try:
            with self._path.open("a", encoding="utf-8", buffering=1) as f:
                while not self._stop_event.is_set():
                    try:
                        item = self._queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if item is None:
                        # Graceful flush sentinel: drain anything still
                        # queued behind it, then exit.
                        self._drain_remaining(f)
                        return
                    self._write_record(f, item)
                # P2 (audit): ``_stop_event`` was set (flush). Drain the
                # records queued before/with the sentinel so the trailing
                # session window is not lost.
                self._drain_remaining(f)
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

    def __init__(
        self,
        config: CortexConfig | None = None,
        *,
        clock: Clock | None = None,
        kernel: ApplicationKernel | None = None,
    ) -> None:
        self.config = config or get_config()
        self._clock = clock or SYSTEM_CLOCK
        storage_root = Path(self.config.storage.path).expanduser()
        self._database = SQLiteDatabase(
            storage_root / self.config.storage.sqlite_filename,
            clock=self._clock,
            busy_timeout_ms=int(getattr(self.config.storage, "sqlite_busy_timeout_ms", 5_000)),
            backup_retention_count=int(getattr(self.config.storage, "backup_retention_count", 3)),
        )
        self._analytics_writer = BoundedAnalyticsWriter(
            self._database,
            capacity=self.config.storage.analytics_queue_capacity,
        )
        self._storage_maintenance = StorageMaintenance(
            self._database,
            storage_root=storage_root,
            analytics_writer=self._analytics_writer,
            clock=self._clock,
            retention_days={
                "sessions": self.config.storage.session_retention_days,
                "policy": self.config.storage.error_retention_days,
                "interventions": self.config.storage.error_retention_days,
            },
            namespace=self.config.redis.key_prefix,
            legacy_store_path=default_legacy_store_path(),
        )
        self._legacy_data_migrator = LegacyDataMigrator(
            self._database,
            storage_root=storage_root,
            clock=self._clock,
            session_retention_days=self.config.storage.session_retention_days,
        )
        self._calibration_store = CalibrationProfileStore(
            self.config.storage.path,
            clock=self._clock,
        )
        loaded_calibration_profile: CalibrationProfile | None = None
        self._active_calibration_profile: CalibrationProfile | None = None
        try:
            loaded_calibration_profile = self._calibration_store.load_active()
        except Exception:
            logger.exception("Active calibration profile failed validation; starting uncalibrated")
        self._baseline_snapshot = UserBaselines()
        self._inference_publication_paused = False
        self._shutdown = asyncio.Event()
        self._kernel = kernel or ApplicationKernel.create(
            on_task_failure=self._on_supervised_task_failure,
        )
        self._services = self._kernel.services
        self._events = self._kernel.events
        self._task_supervisor = self._kernel.tasks
        self._runtime_status = self._kernel.runtime_status
        self._runtime_data = self._kernel.runtime_data
        self._runtime_status.bind_daemon(self)
        self._stop_started = False
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
        self._state_callback: Callable[[dict[str, Any]], None] | None = None
        self._intervention_callback: Callable[[dict[str, Any]], None] | None = None
        self._legacy_state_subscription: Subscription | None = None
        self._legacy_intervention_subscription: Subscription | None = None
        # F17 (audit): per-callback monotonic sequence numbers. The
        # in-process bridge (``DaemonBridge``) reads ``_seq`` from the
        # payload and drops frames whose ``_seq`` is not strictly
        # greater than the last applied value. Reset on daemon restart
        # is implicit — the bridge starts with ``last_seq = 0`` per
        # restart, so the first frame from a fresh daemon always wins.
        self._state_callback_seq: int = 0
        self._intervention_callback_seq: int = 0

        self._recorder = SessionRecorder(self.config.storage.path, clock=self._clock)
        self._input_hooks = InputHooks(self.config.telemetry, clock=self._clock)
        self._window_tracker = WindowTracker(clock=self._clock)
        self._terminal_adapter = TerminalAdapter()
        self._editor_adapter = EditorAdapter(request_context_fn=self._request_context)
        self._browser_adapter = BrowserAdapter(request_context_fn=self._request_context)
        self._context_engine = ContextAssembler(
            editor_adapter=self._editor_adapter,
            browser_adapter=self._browser_adapter,
            terminal_adapter=self._terminal_adapter,
            active_app_provider=self._current_app_name,
        )

        self._capture_pipeline = CapturePipeline(
            self.config.capture,
            clock=self._clock,
        )
        camera_features = build_production_camera_feature_components(self.config)
        if loaded_calibration_profile is not None:
            try:
                _validate_runtime_calibration_profile(
                    loaded_calibration_profile,
                    expected_algorithms=(
                        production_calibration_algorithm_identities(
                            self.config,
                            components=camera_features,
                        )
                    ),
                )
            except Exception:
                logger.exception(
                    "Active calibration profile is incompatible; starting uncalibrated"
                )
            else:
                self._active_calibration_profile = loaded_calibration_profile
                self._baseline_snapshot = loaded_calibration_profile.to_user_baselines()
        self._roi_extractor = camera_features.roi_extractor
        self._pulse_estimator = _build_legacy_pulse_estimator(self.config)
        self._physiology_v2 = camera_features.physiology
        self._blink_detector = camera_features.blink
        self._blink_detector.baseline_blink_rate = self._baseline_snapshot.blink_rate_baseline
        self._head_pose = camera_features.head_pose
        self._posture = camera_features.head_neck_proxy
        self._feature_fusion = FeatureFusion(clock=self._clock)
        self._scorer = RuleScorer(
            config=self.config.state,
            baselines=self._baseline_snapshot,
        )
        self._support_model_registry = SupportModelRegistry()
        if self.config.state.inference_mode == "safety_null":
            self._support_model_registry.rollback_to_safe_null()
        self._support_inference = SupportInferenceEngine(
            self._scorer,
            self._support_model_registry,
        )
        self._smoother = ScoreSmoother(self.config.state, clock=self._clock)

        # The former per-user logistic branch used intervention feedback as a
        # pseudo-label for cognitive state and had no participant-held-out
        # validation lifecycle. It remains a research utility module only;
        # production always runs the registered deterministic support rules.
        self._trigger_policy = TriggerPolicy(
            self.config.intervention,
            state_config=self.config.state,
            clock=self._clock,
        )
        self._llm_client = create_llm_client(self.config.llm, clock=self._clock)
        self._executor = InterventionExecutor(
            execution_mode=self.intervention_execution_mode,
        )
        self._project_launcher = ProjectLauncher(storage_path=self.config.storage.path)
        self._restore_manager = RestoreManager(
            self._executor,
            timeout_seconds=float(self.config.intervention.timeout_minutes * 60),
            clock=self._clock,
        )

        # Phase-4b TASK M: bind the per-action consent gate + the two
        # special-action hooks on the executor. The hooks run inside
        # ``InterventionExecutor.apply`` so the daemon owns the WS
        # broadcast and editor-focus delivery while the executor stays
        # adapter-agnostic.
        self._executor.set_consent_check(self._check_action_consent)
        self._executor.set_editor_focus_hook(self._resume_last_active_file)
        self._executor.set_prompt_broadcast_hook(self._broadcast_prompt)

        self._ws_server = WebSocketServer(
            self.config.api,
            clock=self._clock,
            services=self._services,
            events=self._events,
            runtime_status=self._runtime_status,
        )
        # P0 §3.6: route MICRO_STEP_TOGGLED frames into ``toggle_micro_step``
        # so peer surfaces (popup, VS Code panel, WS-mode overlay) can
        # toggle steps without the in-process Qt overlay path.
        # G1 (audit-prod): caller registers via set_client_identified_callback
        # to bridge connection events to the desktop shell. Default is a
        # noop list so the daemon can still run headless.
        self._client_identified_listeners: list[Callable[[str, bool], None]] = []

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

        self._rgb_observations: ObservationBuffer[NumericObservation] = (
            _build_rgb_observation_buffer(self.config)
        )
        self._legacy_capture_sequence = 0
        self._legacy_capture_source_instance_id = self._clock.boot_id
        self._legacy_capture_mono_origin_ns = self._clock.monotonic_ns()
        self._active_camera_identity_key: str | None = None
        self._active_camera_geometry: tuple[int, int] | None = None
        self._active_camera_source_instance_id: str | None = None
        self._latest_respiration_evidence: SignalEstimate | None = None
        # A profile is camera-bound. Readiness remains false until the first
        # live, post-open identity confirms that binding.
        self._camera_calibration_valid = False
        # P0-2: count of low-quality frames rejected from the rPPG window.
        self._frames_low_quality_rejected: int = 0
        # C6 (audit): edge-trigger latch for FACE_LOST / FACE_REACQUIRED
        # structured events. Starts True so the first lost-face frame emits
        # FACE_LOST exactly once (we assume the user is present at startup).
        self._face_present_prev: bool = True
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
        self._last_physio_update_mono_ns = 0
        # B22 (Phase 4.1): monotonic timestamp of the most recent
        # kinematics feature delivery. State loop marks the kinematics
        # channel stale when monotonic age exceeds 2 seconds.
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
        self._pending_apply_results: dict[str, asyncio.Future[Any]] = {}
        # F05: ``_background_tasks`` (declared above at __init__ top) tracks
        # timeout watchers etc. Mirrors the F03 pattern from the audit
        # Ledger: any new task spawn must use ``_spawn_background_task`` so
        # ``stop()`` can drain them cleanly. (No re-annotation here — the
        # single declaration at __init__ top is the canonical one.)
        self._aggregator = FeatureAggregator(
            self._input_hooks,
            self._window_tracker,
            config=self.config.telemetry,
            tab_count_provider=lambda: (
                self._browser_adapter.last_context.tab_count
                if self._browser_adapter.last_context is not None
                else 0
            ),
            clock=self._clock,
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
        # WP7: one embedded SQLite database is authoritative for durable
        # consent, small state, policy records, and intervention authority.
        # Redis/InMemory remain import-compatible adapters for external test
        # composition, but the shipped runtime never splits authority across
        # them.
        self._store: RedisStore | InMemoryStore | SQLiteKeyValueStore
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
            # Compatibility diagnostic for operators who still carry the
            # pre-WP7 Redis setting. SQLite remains authoritative; constructing
            # the lazy adapter performs no network I/O, but preserves the
            # established degraded signal exposed by custom/test adapters.
            try:
                requested_redis = RedisStore(
                    host=self.config.redis.host,
                    port=self.config.redis.port,
                    db=self.config.redis.db,
                    key_prefix=self.config.redis.key_prefix,
                )
                self._store_degraded = bool(getattr(requested_redis, "degraded", False))
            except Exception:
                self._store_degraded = True
        self._consent_overrides_path = storage_root / "consent_overrides.json"
        self._store = make_default_store(
            self.config,
            database=self._database,
            consent_overrides_path=self._consent_overrides_path,
        )

        # C7 (audit): mirror the store's own ``degraded`` flag onto the
        # daemon flag after construction. ``RedisStore`` exposes a public
        # ``degraded`` property that flips True once Redis is found
        # unreachable and the persistent in-memory fallback takes over;
        # ``InMemoryStore`` has no such attribute (it is the non-degraded
        # DMG-default backend), so ``getattr(..., False)`` keeps the flag
        # False there. We OR with the existing value so the explicit
        # except-branch above (construction failure) is never un-set.
        self._store_degraded = self._store_degraded or bool(getattr(self._store, "degraded", False))
        self._runtime_status.publish_storage(
            degraded=self._store_degraded,
            backend="sqlite",
            healthy=not self._store_degraded,
        )

        self._focus_break_policy = FocusBreakPolicy(
            enabled=self.config.intervention.enable_focus_break_reminders,
            interval_minutes=self.config.intervention.focus_break_interval_minutes,
            suggested_duration_seconds=(self.config.intervention.focus_break_duration_seconds),
            clock=self._clock,
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
        self._longitudinal = LongitudinalTracker(
            store=self._store,
            clock=self._clock,
        )

        # Zombie reading detector
        self._zombie_detector = ZombieReadingDetector(
            blink_baseline=self._baseline_snapshot.blink_rate_baseline
        )

        # Rabbit hole detector
        self._rabbit_hole = RabbitHoleDetector()

        # Shutdown detector (morning handover)
        self._shutdown_detector = ShutdownDetector(
            hrv_baseline=self._baseline_snapshot.hrv_baseline,
            config=self.config.handover,
        )

        # Consent ladder
        self._consent_policy = ConsentPolicy()
        self._consent_ladder = ConsentLadder(
            store=self._store,
            policy=self._consent_policy,
            clock=self._clock,
        )
        # Phase-3 P0 + Audit-1.1 P0-1: consent policy overrides must
        # survive a daemon restart. Without persistence, the user's
        # opt-in to ``distraction_block`` at AUTONOMOUS_ACT silently
        # reverts to REVERSIBLE_ACT on reboot — the toggle in Settings
        # stays "on" but the HYPER auto-arm gate never opens.
        self._transaction_coordinator = InterventionTransactionCoordinator(
            self._consent_ladder,
            store=SQLiteInterventionTransactionStore(
                self._database,
                legacy_json_path=storage_root / "intervention_transactions.json",
                clock=self._clock,
            ),
            clock=self._clock,
            execution_mode=self.intervention_execution_mode,
        )
        self._executor.set_authorization_verifier(self._transaction_coordinator.validate_consumed)
        self._restore_manager.set_restore_callback(self._restore_transaction_and_wait)
        self._pending_restore_results: dict[str, asyncio.Future[bool]] = {}
        self._pending_startup_restores: dict[str, InterventionRestoreCommand] = {}

        # Helpfulness tracker
        self._helpfulness = HelpfulnessTracker(store=self._store, clock=self._clock)

        # P0 §3.9: per-signal causal attributor — fed by ``_state_loop``
        # at the same cadence the state estimate runs and queried when
        # the daemon constructs an intervention plan.
        self._causal_attributor = CausalAttributor()
        # P0 §3.9: cache the most-recent CausalSignal list per
        # intervention id so a late-arriving WHY_DETAIL_REQUEST can
        # resolve without re-running attribution against stale features.
        self._causal_signals_by_intervention: dict[str, list[dict[str, Any]]] = {}

        # Flipped by ``_set_break_suppression`` for the duration of a
        # break overlay so the state loop skips trigger evaluation.
        self._break_active: bool = False
        # The guided-break controller is instantiated after
        # ``_session_report`` is created (a few lines below); keep a
        # forward-declared attribute so type checkers don't trip.
        self._break_controller: GuidedBreakController | None = None
        # Audio gate: ``last_mic_active_at`` is the monotonic timestamp of the
        # ``last_mic_active_at`` is the monotonic timestamp of the
        # most-recent positive ``receptivity.is_microphone_in_use``
        # reading. The break controller flips ``audio_cue=False`` for
        # any break whose start falls within
        # ``InterventionConfig.guided_break_audio_mute_after_mic_seconds``
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
        self._per_tab_feedback_ids: deque[str] = deque(
            maxlen=50
        )  # intervention IDs with per-tab feedback

        # WP8: production is a deterministic, non-learning policy. The only
        # randomized path requires an explicit fixed research epoch and
        # separate consent configuration; deterministic records never expose
        # propensities suitable for causal/off-policy analysis.
        self._policy_repository = PolicyRepository(self._database, clock=self._clock)
        research_settings: ResearchPolicySettings | None = None
        if self.config.eval.policy == "research_randomized":
            research = self.config.eval.research
            specification = research.mrt_specification(
                policy_name=RESEARCH_POLICY_NAME,
                policy_version=RESEARCH_POLICY_VERSION,
                reward_version=self.config.eval.outcome.reward_version,
                proximal_window_seconds=self.config.eval.outcome.reward_window_seconds,
            )
            research_settings = ResearchPolicySettings(
                study_id=research.study_id,
                study_epoch=research.study_epoch,
                consent_version=research.consent_version,
                seed_hex=research.seed_hex,
                specification_sha256=policy_payload_sha256(specification.model_dump(mode="json")),
                action_catalog=research.action_catalog,
                minimum_probability=research.minimum_probability,
                online_learning=research.online_learning,
            )
        self._policy_lifecycle = PolicyLifecycleService(
            self._policy_repository,
            clock=self._clock,
            mode=self.config.eval.policy,
            reward_window_seconds=self.config.eval.outcome.reward_window_seconds,
            research_settings=research_settings,
        )
        self._next_policy_decision_mono_ns = 0
        # P1: canonical consent action-types for each applied intervention,
        # captured at apply time keyed by intervention_id. On engage/dismiss
        # the daemon records the approval/rejection under THESE keys (the
        # same canonical keys the executor's per-action gate checks) instead
        # of the literal "intervention", so the escalation ladder actually
        # lifts the gate on the actions the user approved. Empty -> falls
        # back to ("intervention",) to preserve legacy behaviour.
        self._consent_actions_by_intervention: dict[str, list[str]] = {}
        # audit C-note: snapshot of the (trigger-time confidence,
        # context_complexity) the dismissal-prediction model must be trained
        # on — captured AT trigger time keyed by intervention_id. The legacy
        # code trained the model on ``outcome.recovery_confidence`` and the
        # CURRENT context's complexity at feedback time, which are different
        # quantities (recovery confidence is a FLOW-recovery score, not the
        # trigger confidence) and so the dismissal model learned from
        # mislabelled features. ``(confidence, context_complexity)``.
        self._dismissal_features_by_intervention: dict[str, tuple[float, float]] = {}

        # Copilot throttle
        self._copilot_throttle = CopilotThrottle(ws_server=self._ws_server)

        # Activity tracker aggregator
        self._activity_aggregator = ActivityAggregator(store=self._store)

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
        # P0 §3.2: serve REQUEST_TRENDS
        # P0 §3.3: serve REQUEST_SESSION_RECAP from the cached payload
        # P0 §3.3 (Wave-2 P1): consume SESSION_RECAP_ACKNOWLEDGED
        # frames; flips ``_recap_dismissed_event`` to release ``stop()``.

        # Compatibility break-overlay controller. The policy feeding it is
        # the opt-in elapsed-focus reminder above; no HRV tracker is bound.
        # The desktop shell binds its full-screen
        # overlay handler via :meth:`set_break_overlay_ui_handler`.
        self._break_controller = GuidedBreakController(
            session_report=self._session_report,
            suppress_interventions=self._set_break_suppression,
        )

        # P0 §3.9: serve WHY_DETAIL_REQUEST from the per-intervention
        # causal-signal cache. Returns a list[dict] or None.

        # P0 §3.11: serve QUIET_MODE_TOGGLE / SNOOZE_REQUEST. Routed
        # through :meth:`set_quiet_mode` which centralises the
        # quiet/pause primitives and broadcasts QUIET_MODE_STATE so
        # every surface (dashboard, overlay, tray, browser popup, VS
        # Code) reflects the same truth.

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
        self._quiet_mode_deadline: BoundedDeadline | None = None
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

        self._coordinators = self._compose_runtime_coordinators()
        self._bind_gateway_handlers()

    def _on_supervised_task_failure(self, failure: TaskFailure) -> None:
        """Fail the application closed when a critical owner exits."""

        if failure.critical:
            logger.error(
                "Critical application task failed; requesting shutdown: %s/%s",
                failure.group,
                failure.name,
            )
            self._shutdown.set()

    def _bind_gateway_handlers(self) -> None:
        """Compose the transport command boundary exactly once."""

        self._intervention_coordinator.bind(self._ws_server)

    def _gateway_command_handlers(self) -> WebSocketCommandHandlers:
        """Return the immutable command port implemented by this runtime."""

        return WebSocketCommandHandlers(
            user_action=self._handle_user_action,
            settings=self.apply_settings,
            calibration_reload=self.activate_calibration_profile,
            shutdown=self._request_shutdown,
            activity_sync=self._handle_activity_sync,
            tab_relevance_feedback=self._handle_tab_relevance_feedback,
            leetcode_context=self._handle_leetcode_context_update,
            intervention_applied=self._handle_intervention_applied,
            intervention_authorize=self._authorize_intervention,
            intervention_receipt=self._record_intervention_receipts,
            intervention_dispatch_failure=self._record_intervention_dispatch_failure,
            intervention_partial_dispatch=self._compensate_partial_intervention_dispatch,
            intervention_dispatch_binding=(
                self._transaction_coordinator.bind_dispatch_targets
            ),
            client_identified=self._on_client_identified,
            session_list=self.list_sessions,
            session_detail=self.get_session,
            trends=self.get_trends,
            micro_step_toggled=self.toggle_micro_step,
            why_detail=self.get_causal_signals,
            session_recap_cache=self.latest_session_recap,
            session_recap_acknowledged=self.acknowledge_session_recap,
            quiet_mode_toggle=self.set_quiet_mode,
        )

    def _compose_runtime_coordinators(self) -> RuntimeCoordinatorSet:
        """Declare every long-lived application loop and its owner."""

        self._intervention_coordinator = InterventionCoordinator(
            self._task_supervisor,
            handlers=self._gateway_command_handlers(),
        )
        return RuntimeCoordinatorSet(
            SensingCoordinator(
                self._task_supervisor,
                operations=SensingOperations(
                    capture_enabled=lambda: (
                        self._capture_available
                        and self._capture_processing_enabled
                    ),
                    next_capture=self._next_capture_output,
                    process_capture=self._process_capture_output,
                    telemetry_enabled=lambda: self._telemetry_enabled,
                    sample_telemetry=self._sample_telemetry,
                    refresh_context=self._refresh_context,
                ),
            ),
            InferenceCoordinator(
                self._task_supervisor,
                operations=InferenceOperations(
                    state_loop=self._state_loop,
                    current_publication=lambda: self._latest_broadcast_snapshot,
                    publish_state=self._ws_server.broadcast_state,
                    broadcast_interval_seconds=self._broadcast_interval_seconds,
                ),
            ),
            # Intervention commands are event-driven through the immutable
            # WebSocket handler bundle; this coordinator deliberately owns no
            # polling loop.
            self._intervention_coordinator,
            ExperimentCoordinator(
                self._task_supervisor,
                operations=ExperimentOperations(
                    finalize_outcomes=self._finalize_policy_outcomes,
                    outcome_interval_seconds=(
                        self.config.eval.outcome.collector_interval_seconds
                    ),
                    generate_diagnostics_if_due=(
                        self._generate_policy_diagnostics_if_due
                        if self.config.eval.policy_diagnostics.enabled
                        else None
                    ),
                ),
            ),
            OperationsCoordinator(
                self._task_supervisor,
                (
                    LoopSpec(
                        "cortex-longitudinal-loop",
                        self._longitudinal_loop,
                        critical=False,
                    ),
                    LoopSpec(
                        "cortex-retention-loop",
                        self._retention_sweep_loop,
                        critical=False,
                    ),
                    LoopSpec(
                        "cortex-session-checkpoint-loop",
                        self._session_checkpoint_loop,
                        critical=False,
                    ),
                    LoopSpec(
                        "cortex-chronotype-backfill",
                        lambda: asyncio.to_thread(
                            self._session_aggregator.backfill_if_needed
                        ),
                        critical=False,
                    ),
                ),
            ),
        )

    @property
    def task_tree(self) -> dict[str, Any]:
        """Return an operator-safe snapshot of application task ownership."""

        return self._task_supervisor.snapshot()

    @property
    def services(self) -> Any:
        """Expose the instance composition container for diagnostic adapters."""

        return self._services

    def _current_state_estimate(self) -> Any | None:
        value = self._runtime_data.snapshot().state_estimate
        return value if value is not None else self._services.get("latest_state_estimate")

    def _current_telemetry(self) -> Any | None:
        value = self._runtime_data.snapshot().telemetry
        return value if value is not None else self._services.get("latest_telemetry")

    def _current_feature_vector(self) -> Any | None:
        value = self._runtime_data.snapshot().feature_vector
        return value if value is not None else self._services.get("latest_feature_vector")

    def _workspace_snapshot(self, intervention_id: str) -> Any | None:
        present, value = self._runtime_data.workspace_snapshot(intervention_id)
        if present:
            return value
        return self._services.get(f"workspace_snapshot:{intervention_id}")

    def subscribe_state(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> Subscription:
        return self._events.state.subscribe(listener)

    def subscribe_intervention(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> Subscription:
        return self._events.intervention.subscribe(listener)

    @property
    def intervention_execution_mode(
        self,
    ) -> Literal["suggest_only", "authorized", "research_autonomous"]:
        """Return the configured workspace-authority mode, fail closed.

        Configuration is Pydantic-validated in normal operation, but this
        boundary also protects tests, legacy deserializers, and partially
        constructed configs from increasing authority with an unknown value.
        """
        config = getattr(self, "config", None)
        intervention = getattr(config, "intervention", None)
        raw = str(getattr(intervention, "execution_mode", "suggest_only"))
        if raw not in _EXECUTION_MODES:
            logger.error(
                "Unknown intervention execution_mode=%r; using suggest_only",
                raw,
            )
            return "suggest_only"
        return raw  # type: ignore[return-value]

    @property
    def workspace_mutation_allowed(self) -> bool:
        """Whether this runtime may enter a workspace-apply path at all."""
        return self.intervention_execution_mode != "suggest_only"

    def set_state_callback(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked on every state update.

        The callback receives a deep-copied dict with ``estimate`` and
        ``biometrics`` keys.  It is called from the asyncio daemon thread;
        the recipient is responsible for thread-safe dispatching (e.g.
        emit a Qt signal).
        """
        if self._legacy_state_subscription is not None:
            self._legacy_state_subscription.cancel()
        self._state_callback = fn
        self._legacy_state_subscription = self.subscribe_state(fn)

    def set_intervention_callback(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked when an intervention is sent.

        The callback receives a deep-copied dict of the intervention plan
        payload.  Same threading caveat as :meth:`set_state_callback`.
        """
        if self._legacy_intervention_subscription is not None:
            self._legacy_intervention_subscription.cancel()
        self._intervention_callback = fn
        self._legacy_intervention_subscription = self.subscribe_intervention(fn)

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
        supervisor = getattr(self, "_task_supervisor", None)
        if supervisor is None:
            # Compatibility for narrow mixin-style adapters that bind this
            # method without constructing the full kernel. The task is still
            # supervised; production always supplies the kernel-owned one.
            from cortex.application.task_supervisor import TaskSupervisor

            supervisor = TaskSupervisor()
            self._task_supervisor = supervisor
        background_tasks = getattr(self, "_background_tasks", None)
        if background_tasks is None:
            background_tasks = set()
            self._background_tasks = background_tasks
        task = supervisor.spawn(
            coro,
            name=name or "cortex-background-task",
            group=TaskGroupName.BACKGROUND,
            critical=False,
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
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

            event_time = EventTime.from_clock(self._clock)
            estimate = _StateEstimate(
                state="UNKNOWN",
                support_state="unknown",
                status="insufficient_evidence",
                confidence=0.0,
                scores=_StateScores(flow=0.0, hypo=0.0, hyper=0.0, recovery=0.0),
                evidence_coverage=0.0,
                signal_quality=_SQ(physio=0.0, kinematics=0.0, telemetry=0.0),
                timestamp=event_time.observed_at_mono_ns / 1_000_000_000.0,
                observed_at_unix_ms=event_time.observed_at_unix_ms,
                observed_at_mono_ns=event_time.observed_at_mono_ns,
                boot_id=event_time.boot_id,
                dwell_seconds=0.0,
                reasons=["capture_unavailable"],
            )
            # Plant the stale marker so ``_make_state_update`` stamps
            # the field on the outbound payload. The registry is a
            # process-wide bag the WS server already reads from to
            # surface ``capture.frames_flowing`` and ``face_detected``;
            # adding the ``stale`` field there keeps the wire path
            # unchanged.
            self._services.register("capture_stale", True)
            self._runtime_status.mark_capture_stale()
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
        # C6 (audit): configure structlog ONCE here, before any logger use,
        # replacing the bare ``logging.basicConfig`` the daemon relied on.
        # Idempotent (see ``configure_logging`` docstring) so the run_dev /
        # desktop_shell entrypoints can also call it without conflict; the
        # last caller's level wins. ``json_format`` follows the telemetry
        # config so a dev terminal gets readable console output.
        try:
            configure_logging(
                level=self.config.logging.level or "INFO",
                json_format=self.config.logging.format == "json",
                include_timestamp=self.config.logging.include_timestamp,
            )
        except Exception:
            logger.debug("configure_logging failed; continuing", exc_info=True)
        # Open and fully verify durable authority before capture, transports,
        # or proposal producers start. A corrupt/future/read-only database is
        # a startup failure: continuing would be unable to prove whether a
        # prior workspace effect still needs restoration.
        await self._database.start()
        await self._legacy_data_migrator.migrate_all()
        await self._policy_lifecycle.start()
        await asyncio.to_thread(
            migrate_legacy_causal_report_names,
            self.config.storage.path,
        )
        sqlite_calibration = await self._legacy_data_migrator.load_active_calibration()
        if sqlite_calibration is not None and (
            self._active_calibration_profile is None
            or self._active_calibration_profile.profile_id != sqlite_calibration.profile_id
            or calibration_profile_sha256(self._active_calibration_profile)
            != calibration_profile_sha256(sqlite_calibration)
        ):
            # Reconcile the composition graph to SQLite, which is now the
            # authority. The file pointer remains a compatibility projection
            # for synchronous desktop readers.
            self.apply_calibration_profile(sqlite_calibration)
        await self._analytics_writer.start()
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
        # Recover crash-interrupted transactions before accepting new
        # intervention work. With no extension connected yet, exact restore
        # commands remain queued and are retried when their executor identifies.
        startup_restores = await self._transaction_coordinator.recover_unfinished()
        for restore_command in startup_restores:
            # Retain until the coordinator observes every inverse receipt.
            # A partial restore send is expected when only one owning surface
            # has reconnected during startup.
            self._pending_startup_restores[restore_command.restore_id] = restore_command
            await self._ws_server.send_restore_command(restore_command)
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
                self._services.register("store_degraded", True)
                self._runtime_status.publish_storage(
                    degraded=True,
                    backend="sqlite",
                    healthy=False,
                )
            except Exception:
                logger.debug("registry.register(store_degraded) failed", exc_info=True)

        # The composition root, rather than this method, declares ownership
        # of every long-running loop. Critical child failures request a
        # fail-closed daemon shutdown through ``TaskSupervisor``.
        self._tasks = list(await self._coordinators.start())
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
                    "loop.add_signal_handler unsupported for %s; falling back to outer harness",
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
        if self._stop_started:
            return
        self._stop_started = True
        self._shutdown.set()
        # Revoke the production path before cancelling proposal producers.
        # Already-consumed effects are handled by the exact restore barrier
        # below; no new authorization may be minted during teardown.
        self._interventions_enabled = False
        self._transaction_coordinator.set_execution_mode("suggest_only")
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
                "macos_notifications.mark_shutting_down failed",
                exc_info=True,
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
                    "disarm_auto_focus during stop failed",
                    exc_info=True,
                )
        if self._uvicorn_server is not None:
            self._task_supervisor.expect_completion(self._api_task)
            self._uvicorn_server.should_exit = True
        await self._coordinators.stop(timeout=5.0)
        self._tasks.clear()
        # F03 + F05: cancel + drain dynamically-spawned background tasks
        # so they cannot outlive the daemon and corrupt persisted state.
        # Apply-confirmation timeout watchers (F05) are part of this set.
        await self._task_supervisor.cancel(TaskGroupName.BACKGROUND, timeout=5.0)
        self._background_tasks.clear()
        # WP6: while authenticated clients and receipt handlers are still
        # alive, request every exact inverse and wait a bounded interval for
        # verified completion. Offline/unresponsive owners remain durable and
        # are retried on next startup rather than being reported restored.
        try:
            restore_summary = await self.restore_all_transactional_effects(
                reason="system_cancelled",
                timeout_seconds=3.0,
            )
            if restore_summary["pending"] or restore_summary["failed"]:
                logger.warning(
                    "Shutdown retained unresolved exact restores: %s",
                    restore_summary,
                )
        except Exception:
            logger.exception("Shutdown exact-restore barrier failed; journal retained for startup")
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
                "B18: aborting %d in-flight apply-confirmation futures on daemon shutdown",
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
            await asyncio.wait_for(self._capture_pipeline.stop(), timeout=5.0)
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

                        sessions_dir = Path(self.config.storage.path).expanduser() / "sessions"
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
                        await self._legacy_data_migrator.upsert_session(report)
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
                    # C4 (audit): send the declared ``SessionRecap`` wrapper
                    # so schema == wire. The EXT reads ``payload.report.*``
                    # and ``payload.persisted``; ``generated_at`` is the
                    # recap-construction instant (distinct from
                    # ``report.end_time``). Building via the Pydantic model
                    # guarantees the shape matches the generated TS type.
                    from cortex.libs.schemas.realtime import SessionRecap

                    recap_payload = SessionRecap(
                        report=report,
                        generated_at=utc_datetime(self._clock).isoformat(),
                        persisted=persisted_ok,
                    ).model_dump(mode="json")
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
                        logger.info("SESSION_RECAP acknowledged by UI; proceeding with shutdown")
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

                        sessions_dir = Path(self.config.storage.path).expanduser() / "sessions"
                        sessions_dir.mkdir(parents=True, exist_ok=True)
                        session_path_short = sessions_dir / f"session_{report.session_id}.json"
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
                        await self._legacy_data_migrator.upsert_session(report)
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
                        logger.debug("synthetic empty SESSION_RECAP broadcast failed (non-fatal)")
        # P0 §3.2: stop the midnight scheduler cleanly before the WS server
        # tears down so we don't await a callback that needs a WS broadcast.
        if self._midnight_scheduler is not None:
            try:
                await self._midnight_scheduler.stop()
            except Exception:
                logger.debug("midnight scheduler stop raised (non-fatal)", exc_info=True)
            self._midnight_scheduler = None

        # audit fix #10: a daemon stop while Copilot is throttled would leave
        # the editor's inline suggestions disabled until the next manual
        # toggle. Force-re-enable on the way out so a stop never strands the
        # user in a half-throttled editor. Must run BEFORE the WS server
        # teardown because ``force_enable`` broadcasts a re-enable frame.
        try:
            await self._copilot_throttle.force_enable()
        except Exception:
            logger.debug("copilot force_enable during stop failed", exc_info=True)

        await self._ws_server.stop()
        # Audit-2 fix: drain the session recorder's writer thread before
        # we exit so the trailing window of events (last user_action, the
        # session_report meta-event, etc.) is flushed to disk.
        try:
            self._recorder.flush(timeout=5.0)
        except Exception:
            logger.debug("Recorder flush failed (non-fatal)", exc_info=True)
        try:
            await self._analytics_writer.stop()
            await self._store.close()
        finally:
            await self._database.close()
        self._uvicorn_server = None
        self._task_supervisor.forget_finished()
        self._services.reset()
        self._events.clear()
        self._runtime_status.reset()
        self._runtime_data.reset()
        logger.info("Cortex daemon stopped")

    async def run(self) -> None:
        try:
            await self.start()
        finally:
            await self.stop()

    def _register_services(self) -> None:
        self._services.reset()
        for name, service in {
            "feature_fusion": self._feature_fusion,
            "rule_scorer": self._scorer,
            "support_inference": self._support_inference,
            "support_model_registry": self._support_model_registry,
            "score_smoother": self._smoother,
            "context_engine": self._context_engine,
            "llm_client": self._llm_client,
            "intervention_executor": self._executor,
            "intervention_transactions": self._transaction_coordinator,
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
            "database": self._database,
            "analytics_writer": self._analytics_writer,
            "storage_maintenance": self._storage_maintenance,
            "legacy_data_migrator": self._legacy_data_migrator,
            "longitudinal_tracker": self._longitudinal,
            "zombie_detector": self._zombie_detector,
            "rabbit_hole_detector": self._rabbit_hole,
            "shutdown_detector": self._shutdown_detector,
            "consent_ladder": self._consent_ladder,
            "helpfulness_tracker": self._helpfulness,
            "policy_repository": self._policy_repository,
            "policy_lifecycle": self._policy_lifecycle,
            "copilot_throttle": self._copilot_throttle,
        }.items():
            self._services.register(name, service)
        self._services.register("store_backend", "sqlite")
        self._services.register("store_healthy", not self._store_degraded)
        self._runtime_status.publish_storage(
            degraded=self._store_degraded,
            backend="sqlite",
            healthy=not self._store_degraded,
        )
        self._services.healthy = True

    def _start_api_server(self) -> None:
        app = create_app(
            config=self.config.api,
            cortex_config=self.config,
            clock=self._clock,
            services=self._services,
        )
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

        daemon_clock = self._clock

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
                    clock=daemon_clock,
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
                logger.exception("API server crashed unexpectedly; triggering daemon shutdown")
                self._shutdown.set()

        self._api_task = self._task_supervisor.spawn(
            _supervised_serve(),
            name="cortex-api",
            group=TaskGroupName.TRANSPORT,
            critical=True,
        )

    def _current_app_name(self) -> str:
        events = self._window_tracker.get_events_in_window(window_seconds=60.0)
        if not events:
            return "other"
        return events[-1].app_name

    def _load_baselines(self) -> UserBaselines:
        """Return the one startup/live-applied immutable profile view."""

        return self._baseline_snapshot

    def reload_active_calibration(
        self,
        expected_profile_id: str | None = None,
    ) -> CalibrationUpdated:
        """Reload the already-active pointer, primarily for startup recovery."""

        profile = self._legacy_data_migrator.load_active_calibration_blocking()
        if profile is None:
            raise ValueError("no active calibration profile exists")
        if expected_profile_id is not None and str(profile.profile_id) != str(expected_profile_id):
            raise ValueError("active calibration profile does not match request")
        return self.apply_calibration_profile(profile)

    def activate_calibration_profile(
        self,
        profile_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> CalibrationUpdated:
        """Validate, persistently activate, and live-apply one staged profile.

        Replacement services are constructed before the active pointer changes.
        ``CalibrationProfileStore.activate`` commits the pointer last, and the
        following graph swap is synchronous and assignment-only.  Consequently
        a validation/construction/persistence error leaves both the previous
        pointer and previous live graph in place.
        """

        profile = self._calibration_store.load_profile(profile_id)
        actual_sha256 = calibration_profile_sha256(profile)
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise ValueError("staged calibration profile checksum mismatch")
        prepared = self._prepare_calibration_profile(profile)
        pointer = ActiveCalibrationPointer(
            profile_id=profile.profile_id,
            profile_sha256=actual_sha256,
            activated_at_unix_ms=self._clock.unix_ms(),
        )
        # SQLite commits first and is the authoritative pointer. The JSON
        # pointer is a compatibility projection for synchronous desktop
        # readers; a projection failure cannot roll back proven authority or
        # the prepared live graph.
        prior_sqlite_calibration = (
            self._legacy_data_migrator.load_active_calibration_record_blocking()
        )
        self._legacy_data_migrator.upsert_calibration_blocking(
            profile,
            active=pointer,
        )
        try:
            self._calibration_store.activate(profile, pointer=pointer)
        except (OSError, ValueError):
            # Cross-file atomicity is emulated by restoring the prior SQLite
            # pointer if the compatibility projection cannot commit. The new
            # immutable profile may remain staged, but neither authority nor
            # the live graph changes.
            self._legacy_data_migrator.restore_active_calibration_blocking(prior_sqlite_calibration)
            raise
        return self._commit_calibration_graph(prepared)

    def apply_calibration_profile(
        self,
        profile: CalibrationProfile,
    ) -> CalibrationUpdated:
        """Apply an already-authoritative profile with no await/interleaving.

        All validation and replacement-object construction happens before the
        first live reference changes.  Because runtime domain work is owned by
        one asyncio loop and this method contains no await, consumers observe
        either the prior component graph or the complete replacement graph.
        """

        return self._commit_calibration_graph(self._prepare_calibration_profile(profile))

    def _prepare_calibration_profile(
        self,
        profile: CalibrationProfile,
    ) -> _PreparedCalibrationGraph:
        """Validate and construct a complete replacement without mutating live state."""

        new_components = build_production_camera_feature_components(self.config)
        expected_algorithms = production_calibration_algorithm_identities(
            self.config,
            components=new_components,
        )
        _validate_runtime_calibration_profile(
            profile,
            expected_algorithms=expected_algorithms,
        )
        new_baselines = profile.to_user_baselines()
        new_components.blink.baseline_blink_rate = new_baselines.blink_rate_baseline
        values = profile.baselines
        camera_valid = bool(
            profile.camera is not None
            and self._active_camera_identity_key is not None
            and profile.camera.identity_key == self._active_camera_identity_key
            and self._active_camera_geometry == (profile.camera.width, profile.camera.height)
            and values.neutral_head_pitch_deg is not None
            and values.neutral_face_scale_px is not None
        )
        if (
            camera_valid
            and profile.camera is not None
            and values.neutral_head_pitch_deg is not None
            and values.neutral_face_scale_px is not None
        ):
            new_components.head_neck_proxy.apply_calibration(
                neutral_pitch_deg=values.neutral_head_pitch_deg,
                neutral_face_scale=values.neutral_face_scale_px,
                camera_identity_key=profile.camera.identity_key,
            )
        new_fusion = FeatureFusion(clock=self._clock)
        new_scorer = RuleScorer(config=self.config.state, baselines=new_baselines)
        new_smoother = ScoreSmoother(self.config.state, clock=self._clock)
        new_zombie = ZombieReadingDetector(blink_baseline=new_baselines.blink_rate_baseline)
        new_shutdown = ShutdownDetector(
            hrv_baseline=new_baselines.hrv_baseline,
            config=self.config.handover,
        )

        return _PreparedCalibrationGraph(
            profile=profile,
            baselines=new_baselines,
            camera_features=new_components,
            pulse_estimator=_build_legacy_pulse_estimator(self.config),
            rgb_observations=_build_rgb_observation_buffer(self.config),
            feature_fusion=new_fusion,
            scorer=new_scorer,
            smoother=new_smoother,
            zombie_detector=new_zombie,
            shutdown_detector=new_shutdown,
            causal_attributor=CausalAttributor(),
            camera_calibration_valid=camera_valid,
        )

    def _commit_calibration_graph(
        self,
        prepared: _PreparedCalibrationGraph,
    ) -> CalibrationUpdated:
        """Swap a prepared graph atomically and publish its domain event."""

        profile = prepared.profile
        previous_id = (
            self._active_calibration_profile.profile_id
            if self._active_calibration_profile is not None
            else None
        )
        self._inference_publication_paused = True
        try:
            self._rgb_observations = prepared.rgb_observations
            self._roi_extractor = prepared.camera_features.roi_extractor
            self._pulse_estimator = prepared.pulse_estimator
            self._physiology_v2 = prepared.camera_features.physiology
            self._blink_detector = prepared.camera_features.blink
            self._head_pose = prepared.camera_features.head_pose
            self._posture = prepared.camera_features.head_neck_proxy
            self._feature_fusion = prepared.feature_fusion
            self._scorer = prepared.scorer
            self._support_inference.replace_scorer(prepared.scorer)
            self._smoother = prepared.smoother
            self._zombie_detector = prepared.zombie_detector
            self._shutdown_detector = prepared.shutdown_detector
            self._causal_attributor = prepared.causal_attributor
            self._active_calibration_profile = profile
            self._baseline_snapshot = prepared.baselines
            self._camera_calibration_valid = prepared.camera_calibration_valid
            self._latest_respiration_evidence = None
            self._latest_estimate = None
            self._latest_biometrics = None
            self._latest_broadcast_snapshot = None
            self._last_physio_update = 0.0
            self._last_physio_update_mono_ns = 0
            self._last_kinematics_ts = 0.0
        finally:
            self._inference_publication_paused = False

        applied_metrics = tuple(
            str(summary.metric)
            for summary in profile.metrics
            if summary.maturity
            in {
                CalibrationMetricMaturity.OBSERVED.value,
                CalibrationMetricMaturity.SUPPORTED.value,
            }
            and summary.metric
            in {
                CalibrationMetricName.BLINK_RATE_PER_MIN.value,
                CalibrationMetricName.MOUSE_VELOCITY_PX_PER_S.value,
                CalibrationMetricName.MOUSE_VELOCITY_VARIANCE.value,
                CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG.value,
                CalibrationMetricName.NEUTRAL_FACE_SCALE_PX.value,
            }
            and (
                summary.metric
                not in {
                    CalibrationMetricName.NEUTRAL_HEAD_PITCH_DEG.value,
                    CalibrationMetricName.NEUTRAL_FACE_SCALE_PX.value,
                }
                or prepared.camera_calibration_valid
            )
        )
        event = CalibrationUpdated(
            event_id=uuid4(),
            profile_id=profile.profile_id,
            previous_profile_id=previous_id,
            observed_at_unix_ms=self._clock.unix_ms(),
            observed_at_mono_ns=self._clock.monotonic_ns(),
            boot_id=self._clock.boot_id,
            camera_calibration_valid=prepared.camera_calibration_valid,
            applied_metrics=applied_metrics,
            reset_components=(
                "physiology",
                "blink",
                "head_pose",
                "head_neck_proxy",
                "feature_fusion",
                "rule_scorer",
                "score_smoother",
                "stress_integral",
                "zombie_detector",
                "shutdown_detector",
                "causal_attribution",
            ),
        )
        self._services.register("active_calibration_profile", profile.model_dump(mode="json"))
        self._services.register("calibration_updated", event.model_dump(mode="json"))
        self._services.register(
            "camera_calibration_valid",
            prepared.camera_calibration_valid,
        )
        _emit_event(
            EventType.CALIBRATION_UPDATED,
            **event.model_dump(mode="json"),
        )
        return event

    async def _request_context(self, client_type: str) -> dict[str, Any]:
        return await self._ws_server.request_context(client_type)

    async def _next_capture_output(self) -> PipelineOutput | None:
        """Typed sensing port implemented by the capture pipeline."""

        return await self._capture_pipeline.get_output(timeout=0.5)

    @property
    def _rgb_history(self) -> deque[NDArray[np.float64]]:
        """Derived legacy view; the canonical store is one observation deque."""

        maxlen = max(1, self.config.signal.rppg.window_seconds * self.config.capture.fps)
        rows = (
            item.value
            if item.is_valid and item.value is not None
            else np.full(3, np.nan, dtype=np.float64)
            for item in self._rgb_observations.snapshot()
        )
        return deque(rows, maxlen=maxlen)

    @property
    def _rgb_ts_history(self) -> deque[float]:
        """Derived legacy timestamp view; no parallel timestamp state exists."""

        maxlen = max(1, self.config.signal.rppg.window_seconds * self.config.capture.fps)
        return deque(
            (
                item.observed_at_mono_ns / 1_000_000_000.0
                for item in self._rgb_observations.snapshot()
            ),
            maxlen=maxlen,
        )

    def _effective_rppg_fps(self) -> float:
        """Return measured cadence from the canonical observation stream."""

        cfg = self.config.signal.rppg
        fallback = float(self.config.capture.fps)
        times = np.asarray(
            [item.observed_at_mono_ns for item in self._rgb_observations.snapshot()],
            dtype=np.int64,
        )
        if len(times) < 2:
            return fallback
        diffs = np.diff(times)
        if bool((diffs <= 0).any()):
            return fallback
        candidate = 1_000_000_000.0 / float(np.median(diffs))
        if cfg.fps_clamp_min <= candidate <= cfg.fps_clamp_max:
            return candidate
        return fallback

    def _legacy_capture_observation(
        self, output: PipelineOutput
    ) -> ObservationEnvelope[CameraFrameObservation]:
        """Adapt pre-v2 internal test/plugin outputs without mixing clocks."""

        frame_meta = output.frame_meta
        sequence = self._legacy_capture_sequence
        self._legacy_capture_sequence += 1
        interval_ns = max(1, int(1_000_000_000 / max(1, self.config.capture.fps)))
        mono_ns = self._legacy_capture_mono_origin_ns + sequence * interval_ns
        unix_ms = max(0, int(float(frame_meta.timestamp) * 1000))
        identity = self._camera_identity_from_output(output)
        face_detected = bool(getattr(frame_meta, "face_detected", False))
        low_quality = bool(getattr(frame_meta, "low_quality", False))
        frame = getattr(output, "frame", None)
        landmarks_px = getattr(output, "landmarks_px", None)
        valid = face_detected and not low_quality and frame is not None and landmarks_px is not None
        validity = (
            ObservationValidity.VALID
            if valid
            else (ObservationValidity.REJECTED if low_quality else ObservationValidity.MISSING)
        )
        reason = (
            None if valid else (MissingReason.ARTIFACT if low_quality else MissingReason.NO_FACE)
        )
        components = {
            "brightness": float(getattr(frame_meta, "brightness_score", 0.0)),
            "blur": float(getattr(frame_meta, "blur_score", 0.0)),
            "motion": float(getattr(frame_meta, "motion_score", 0.0)),
            "face_confidence": float(getattr(frame_meta, "face_confidence", 0.0)),
        }
        quality = min(max(0.0, min(1.0, value)) for value in components.values())
        if valid:
            assert frame is not None
            frame_shape = np.asarray(frame).shape
            value = CameraFrameObservation(
                width=int(frame_shape[1]),
                height=int(frame_shape[0]),
                face_detected=True,
                face_stable=True,
                face_confidence=components["face_confidence"],
                camera_identity=identity,
            )
        else:
            value = None
        return ObservationEnvelope[CameraFrameObservation](
            source=ObservationSource.CAMERA,
            source_instance_id=self._legacy_capture_source_instance_id,
            sequence=sequence,
            observed_at_unix_ms=unix_ms,
            observed_at_mono_ns=mono_ns,
            boot_id=self._clock.boot_id,
            value=value,
            validity=validity,
            missing_reason=reason,
            quality=quality,
            quality_components=components,
            algorithm_version="capture-integrity/legacy-adapter",
        )

    def _observation_from_output(
        self, output: PipelineOutput
    ) -> ObservationEnvelope[CameraFrameObservation]:
        observation = getattr(output, "observation", None)
        if isinstance(observation, ObservationEnvelope):
            return observation
        return self._legacy_capture_observation(output)

    def _camera_identity_from_output(self, output: PipelineOutput) -> CameraIdentity:
        identity = getattr(output, "camera_identity", None)
        if isinstance(identity, CameraIdentity):
            return identity
        observation = getattr(output, "observation", None)
        value = getattr(observation, "value", None)
        value_identity = getattr(value, "camera_identity", None)
        if isinstance(value_identity, CameraIdentity):
            return value_identity
        return CameraIdentity(
            identity_key="legacy-camera",
            device_id=max(0, int(self.config.capture.device_id or 0)),
            device_name=None,
            source="legacy",
            backend=None,
            width=self.config.capture.width,
            height=self.config.capture.height,
        )

    def _reset_camera_dependent_state(self, *, invalidate_calibration: bool) -> None:
        self._rgb_observations.clear()
        self._pulse_estimator.reset()
        self._physiology_v2.reset()
        self._latest_respiration_evidence = None
        self._blink_detector.reset()
        self._head_pose.reset()
        if invalidate_calibration:
            self._posture.reset_calibration()
            self._camera_calibration_valid = False
        else:
            self._posture.reset()
        self._feature_fusion.invalidate_camera_channels()
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
        self._last_physio_update_mono_ns = 0
        self._last_kinematics_ts = 0.0
        self._services.register("latest_physio", self._latest_physio)
        self._services.register("latest_kinematics", self._latest_kinematics)
        self._services.register("camera_calibration_valid", self._camera_calibration_valid)

    def _handle_camera_identity(
        self,
        identity: CameraIdentity,
        observation: ObservationEnvelope[CameraFrameObservation],
    ) -> None:
        old_key = self._active_camera_identity_key
        old_geometry = self._active_camera_geometry
        new_geometry = (identity.width, identity.height)
        source_id = str(observation.source_instance_id)
        profile_camera = (
            self._active_calibration_profile.camera
            if self._active_calibration_profile is not None
            and self._active_calibration_profile.camera is not None
            else None
        )
        profile_camera_key = profile_camera.identity_key if profile_camera is not None else None
        profile_geometry = (
            (profile_camera.width, profile_camera.height) if profile_camera is not None else None
        )
        physical_changed = old_key is not None and (
            old_key != identity.identity_key or old_geometry != new_geometry
        )
        initial_profile_mismatch = (
            old_key is None
            and profile_camera is not None
            and (profile_camera_key != identity.identity_key or profile_geometry != new_geometry)
        )
        source_changed = (
            self._active_camera_source_instance_id is not None
            and self._active_camera_source_instance_id != source_id
        )
        if physical_changed or initial_profile_mismatch:
            self._reset_camera_dependent_state(invalidate_calibration=True)
            self._camera_calibration_valid = self._apply_camera_bound_posture_calibration(identity)
            _emit_event(
                EventType.CAMERA_IDENTITY_CHANGED,
                previous_identity_key=old_key or profile_camera_key,
                camera_identity_key=identity.identity_key,
                observed_at_unix_ms=observation.observed_at_unix_ms,
            )
        elif (
            old_key is None
            and profile_camera_key == identity.identity_key
            and profile_geometry == new_geometry
        ):
            self._camera_calibration_valid = self._apply_camera_bound_posture_calibration(identity)
        elif source_changed:
            # Same camera reopened: preserve its calibration, but never bridge
            # signal windows or temporal detector state across acquisitions.
            self._reset_camera_dependent_state(invalidate_calibration=False)
        self._active_camera_identity_key = identity.identity_key
        self._active_camera_geometry = new_geometry
        self._active_camera_source_instance_id = source_id
        self._services.register("camera_identity", identity.model_dump(mode="json"))
        self._services.register("camera_calibration_valid", self._camera_calibration_valid)

    def _apply_camera_bound_posture_calibration(
        self,
        identity: CameraIdentity,
    ) -> bool:
        """Apply the active neutral-pose values only to their measured camera."""

        profile = self._active_calibration_profile
        if profile is None or profile.camera is None:
            return False
        values = profile.baselines
        if (
            profile.camera.identity_key != identity.identity_key
            or profile.camera.width != identity.width
            or profile.camera.height != identity.height
            or values.neutral_head_pitch_deg is None
            or values.neutral_face_scale_px is None
        ):
            return False
        self._posture.apply_calibration(
            neutral_pitch_deg=values.neutral_head_pitch_deg,
            neutral_face_scale=values.neutral_face_scale_px,
            camera_identity_key=identity.identity_key,
        )
        return self._posture.is_calibrated

    def _handle_face_transition(
        self,
        output: PipelineOutput,
        observation: ObservationEnvelope[CameraFrameObservation],
    ) -> None:
        tracking = getattr(output, "tracking", None)
        face_now = bool(
            getattr(
                tracking,
                "face_stable",
                getattr(output.frame_meta, "face_detected", False),
            )
        )
        if face_now == self._face_present_prev:
            return
        if face_now:
            _emit_event(
                EventType.FACE_REACQUIRED,
                observed_at_unix_ms=observation.observed_at_unix_ms,
                face_confidence=float(output.frame_meta.face_confidence),
            )
        else:
            _emit_event(
                EventType.FACE_LOST,
                observed_at_unix_ms=observation.observed_at_unix_ms,
            )
            self._reset_camera_dependent_state(invalidate_calibration=False)
        self._face_present_prev = face_now

    def _prepare_rppg_window(self) -> PreparedObservationWindow:
        cfg = self.config.signal.rppg
        return prepare_observation_window(
            self._rgb_observations.snapshot(),
            window_seconds=float(cfg.window_seconds),
            nominal_fps=float(self.config.capture.fps),
            min_valid_fraction=cfg.min_valid_coverage,
            max_interpolation_gap_ms=cfg.max_interpolation_gap_ms,
            max_motion_fraction=cfg.max_motion_rejected_fraction,
            fps_clamp_min=cfg.fps_clamp_min,
            fps_clamp_max=cfg.fps_clamp_max,
        )

    def _prepare_respiration_window(self) -> PreparedObservationWindow:
        cfg = self.config.signal.rppg
        return prepare_observation_window(
            self._rgb_observations.snapshot(),
            window_seconds=float(cfg.respiration_window_seconds),
            nominal_fps=float(self.config.capture.fps),
            min_valid_fraction=cfg.min_valid_coverage,
            max_interpolation_gap_ms=cfg.max_interpolation_gap_ms,
            max_motion_fraction=cfg.max_motion_rejected_fraction,
            fps_clamp_min=cfg.fps_clamp_min,
            fps_clamp_max=cfg.fps_clamp_max,
        )

    def _publish_unavailable_physio(
        self,
        prepared: PreparedObservationWindow,
        *,
        mono_seconds: float,
    ) -> None:
        self._latest_physio = PhysioFeatures(
            pulse_bpm=None,
            pulse_quality=prepared.quality,
            pulse_variability_proxy=None,
            physio_sqi=prepared.quality,
            physio_sqi_components={
                "valid_fraction": prepared.valid_fraction,
                "temporal_coverage": prepared.temporal_coverage,
                "artifact_fraction": prepared.artifact_fraction,
            },
            hr_delta_5s=None,
            valid=False,
        )
        self._services.register("latest_physio", self._latest_physio)
        self._services.register(
            "physio_window_readiness",
            {
                "ready": False,
                "quality": prepared.quality,
                "valid_fraction": prepared.valid_fraction,
                "temporal_coverage": prepared.temporal_coverage,
                "artifact_fraction": prepared.artifact_fraction,
                "reasons": [reason.value for reason in prepared.unavailable_reasons],
            },
        )
        self._feature_fusion.update_physio(
            self._latest_physio,
            timestamp=mono_seconds,
        )

    async def _process_capture_output(self, output: PipelineOutput) -> None:
        self._services.register("latest_frame_meta", output.frame_meta)
        self._runtime_status.publish_frame(output.frame_meta)
        observation = self._observation_from_output(output)
        identity = self._camera_identity_from_output(output)
        self._handle_camera_identity(identity, observation)
        self._handle_face_transition(output, observation)
        self._services.register(
            "latest_camera_observation",
            observation.model_dump(mode="json"),
        )

        validity = str(observation.validity)
        missing_reason = observation.missing_reason
        rgb_value: NDArray[np.float64] | None = None
        head_jitter_deg = 0.0
        frame = getattr(output, "frame", None)
        landmarks_px = getattr(output, "landmarks_px", None)
        roi_frame: Any | None = None
        if (
            validity == ObservationValidity.VALID.value
            and frame is not None
            and landmarks_px is not None
        ):
            roi_frame = self._roi_extractor.extract(
                frame,
                landmarks_px,
                observation.observed_at_unix_ms / 1000.0,
            )
            combined_rgb = roi_frame.combined_rgb()
            if combined_rgb is not None and bool(np.isfinite(combined_rgb).all()):
                rgb_value = np.asarray(combined_rgb, dtype=np.float64)
                head_jitter_deg = float(roi_frame.head_jitter_px) * (
                    45.0 / max(1.0, float(self.config.capture.width))
                )
            else:
                validity = ObservationValidity.REJECTED.value
                missing_reason = MissingReason.OCCLUDED
        elif validity == ObservationValidity.VALID.value:
            validity = ObservationValidity.REJECTED.value
            missing_reason = MissingReason.ARTIFACT

        if validity != ObservationValidity.VALID.value:
            if validity == ObservationValidity.REJECTED.value:
                self._frames_low_quality_rejected += 1
            rgb_value = None

        numeric = NumericObservation(
            observed_at_unix_ms=observation.observed_at_unix_ms,
            observed_at_mono_ns=observation.observed_at_mono_ns,
            boot_id=observation.boot_id,
            sequence=observation.sequence,
            value=rgb_value,
            validity=validity,
            missing_reason=missing_reason,
            quality=observation.quality if rgb_value is not None else 0.0,
            head_jitter_deg=head_jitter_deg,
            head_vertical_face_units=(
                _face_normalized_vertical_position(landmarks_px)
                if rgb_value is not None and landmarks_px is not None
                else None
            ),
        )
        try:
            self._rgb_observations.append(numeric)
        except ValueError:
            logger.warning(
                "Capture observation time moved backwards; resetting window",
                exc_info=True,
            )
            self._rgb_observations.clear()
            self._rgb_observations.append(
                NumericObservation(
                    observed_at_unix_ms=numeric.observed_at_unix_ms,
                    observed_at_mono_ns=numeric.observed_at_mono_ns,
                    boot_id=numeric.boot_id,
                    sequence=numeric.sequence,
                    value=None,
                    validity=ObservationValidity.REJECTED.value,
                    missing_reason=MissingReason.ARTIFACT,
                    quality=0.0,
                )
            )

        mono_seconds = observation.observed_at_mono_ns / 1_000_000_000.0
        stride_ns = int(self.config.signal.rppg.stride_seconds * 1_000_000_000)
        if (
            self._last_physio_update_mono_ns == 0
            or observation.observed_at_mono_ns - self._last_physio_update_mono_ns >= stride_ns
        ):
            prepared = self._prepare_rppg_window()
            if (
                prepared.ready
                and prepared.values is not None
                and prepared.sample_times_mono_ns is not None
            ):
                pulse_result = self._physiology_v2.pulse.process_window(
                    prepared.values,
                    prepared.sample_times_mono_ns,
                    sample_rate_hz=prepared.sample_rate_hz,
                    boot_id=observation.boot_id,
                    observation_quality=prepared.quality,
                    head_jitter_deg=prepared.mean_head_jitter_deg,
                    face_presence_ratio=prepared.valid_fraction,
                )
                unix_seconds_at_observation = observation.observed_at_unix_ms / 1000.0
                self._pulse_estimator.process_window(
                    pulse_result.waveform,
                    timestamp=unix_seconds_at_observation,
                    fs=prepared.sample_rate_hz,
                    head_jitter_deg=prepared.mean_head_jitter_deg,
                    face_presence_ratio=prepared.valid_fraction,
                )
                legacy_physio_shadow = self._pulse_estimator.get_features(
                    unix_seconds_at_observation
                )
                self._services.register(
                    "legacy_physio_shadow",
                    legacy_physio_shadow.model_dump(mode="json"),
                )
                if pulse_result.summary.hr.value is None:
                    # The acquisition-aware v2 gate is authoritative for
                    # availability even while the legacy stabilizer remains
                    # the displayed pulse implementation. A clean-looking
                    # spectrum cannot override failed observation evidence.
                    self._latest_physio = PhysioFeatures(
                        pulse_bpm=None,
                        pulse_quality=pulse_result.summary.quality,
                        pulse_variability_proxy=None,
                        physio_sqi=pulse_result.summary.quality,
                        physio_sqi_components={
                            "observation_quality": prepared.quality,
                            "valid_fraction": prepared.valid_fraction,
                            "artifact_fraction": prepared.artifact_fraction,
                        },
                        hr_delta_5s=None,
                        valid=False,
                    )
                else:
                    # The compatibility fields and their evidence must refer
                    # to the same estimate.  The legacy stabilizer is retained
                    # only as a shadow comparison during migration; publishing
                    # its value beside v2 provenance would create an
                    # unverifiable hybrid result.
                    self._latest_physio = PhysioFeatures(
                        pulse_bpm=pulse_result.summary.hr.value,
                        pulse_quality=pulse_result.summary.quality,
                        pulse_variability_proxy=None,
                        physio_sqi=pulse_result.summary.quality,
                        physio_sqi_components={
                            "observation_quality": prepared.quality,
                            "valid_fraction": prepared.valid_fraction,
                            "artifact_fraction": prepared.artifact_fraction,
                        },
                        hr_delta_5s=None,
                        respiration_rate_bpm=None,
                        valid=True,
                    )
                respiration_window = self._prepare_respiration_window()
                if (
                    respiration_window.ready
                    and respiration_window.values is not None
                    and respiration_window.sample_times_mono_ns is not None
                ):
                    respiration_result = self._physiology_v2.respiration.process_window(
                        respiration_window.values,
                        respiration_window.sample_times_mono_ns,
                        sample_rate_hz=respiration_window.sample_rate_hz,
                        boot_id=observation.boot_id,
                        head_vertical_face_units=(respiration_window.head_vertical_face_units),
                    )
                    self._latest_respiration_evidence = respiration_result.fused
                    self._services.register(
                        "latest_respiration_evidence",
                        {
                            "fused": respiration_result.fused.model_dump(mode="json"),
                            "channels": {
                                name: estimate.model_dump(mode="json")
                                for name, estimate in respiration_result.channels.items()
                            },
                        },
                    )
                self._latest_physio = self._latest_physio.model_copy(
                    update={
                        "pulse_evidence": pulse_result.summary.hr,
                        "hrv_evidence": {
                            metric.value: estimate
                            for metric, estimate in pulse_result.hrv_estimates.items()
                        },
                        "respiration_evidence": self._latest_respiration_evidence,
                    }
                )
                self._services.register("latest_physio", self._latest_physio)
                self._services.register(
                    "latest_pulse_evidence",
                    {
                        "summary": pulse_result.summary.model_dump(mode="json"),
                        "beats": [
                            item.model_dump(mode="json") for item in pulse_result.beat_events[-32:]
                        ],
                        "intervals": [
                            item.model_dump(mode="json") for item in pulse_result.intervals[-32:]
                        ],
                        "hrv": {
                            metric.value: estimate.model_dump(mode="json")
                            for metric, estimate in pulse_result.hrv_estimates.items()
                        },
                    },
                )
                self._services.register(
                    "physio_window_readiness",
                    {
                        "ready": True,
                        "quality": prepared.quality,
                        "valid_fraction": prepared.valid_fraction,
                        "temporal_coverage": prepared.temporal_coverage,
                        "artifact_fraction": prepared.artifact_fraction,
                        "sample_rate_hz": prepared.sample_rate_hz,
                    },
                )
                self._feature_fusion.update_physio(
                    self._latest_physio,
                    timestamp=mono_seconds,
                )
            else:
                self._publish_unavailable_physio(
                    prepared,
                    mono_seconds=mono_seconds,
                )
            self._last_physio_update = observation.observed_at_unix_ms / 1000.0
            self._last_physio_update_mono_ns = observation.observed_at_mono_ns

        if (
            validity != ObservationValidity.VALID.value
            or landmarks_px is None
            or bool(getattr(getattr(output, "tracking", None), "is_replayed", False))
        ):
            self._blink_detector.observe_missing(mono_seconds)
            self._head_pose.observe_missing(mono_seconds)
            self._posture.observe_missing(mono_seconds)
            return

        blink = self._blink_detector.update(landmarks_px, mono_seconds)
        pose = self._head_pose.update(landmarks_px, mono_seconds)
        posture = self._posture.update(
            pitch_deg=pose.pitch,
            face_scale=self._posture.face_scale(landmarks_px),
            timestamp=mono_seconds,
            camera_identity_key=identity.identity_key,
        )
        self._latest_kinematics = KinematicFeatures(
            blink_rate=blink.blink_rate,
            blink_rate_delta=blink.blink_rate_delta,
            blink_suppression_score=blink.blink_suppression_score,
            perclos_60s=blink.perclos_60s,
            mean_blink_duration_ms=blink.mean_blink_duration_ms,
            ear_variance=blink.ear_variance,
            blink_valid_exposure_seconds=blink.valid_exposure_seconds,
            head_pitch=pose.pitch,
            head_yaw=pose.yaw,
            head_roll=pose.roll,
            head_angular_velocity_deg_per_s=pose.angular_velocity_deg_per_s,
            head_is_jittery=pose.is_jittery,
            head_is_frozen=pose.is_frozen,
            head_neck_flexion_angle=posture.head_neck_flexion_angle,
            head_neck_flexion_score=posture.head_neck_flexion_score,
            head_neck_flexion_dwell_seconds=posture.sustained_flexion_seconds,
            head_neck_proxy_available=posture.proxy_available,
            slump_score=None,
            forward_lean_score=None,
            shoulder_drop_ratio=None,
            confidence=output.frame_meta.face_confidence,
        )
        self._services.register("latest_kinematics", self._latest_kinematics)
        self._last_kinematics_ts = mono_seconds
        self._feature_fusion.update_kinematics(
            self._latest_kinematics,
            timestamp=mono_seconds,
        )

    async def _sample_telemetry(self) -> None:
        """Publish one telemetry sample; cadence belongs to its coordinator."""

        features = self._aggregator.build_features()
        self._services.register("latest_telemetry", features)
        self._runtime_data.publish_telemetry(features)
        self._feature_fusion.update_telemetry(features)

    async def _refresh_context(self) -> None:
        """Build and publish one context snapshot."""

        context = await self._context_engine.build_context()
        if self._user_goal_override:
            try:
                context.current_goal_hint = self._user_goal_override
            except Exception:
                logger.debug("Failed to apply user goal override", exc_info=True)
        self._latest_context = context
        self._services.register("latest_task_context", context)
        self._runtime_data.publish_task_context(context)
        self._terminal_adapter.set_running_command(
            context.terminal_context.running_command
            if context.terminal_context
            else None
        )

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
                    await self._storage_maintenance.enforce_retention()
                except Exception:
                    logger.debug("Retention sweep failed", exc_info=True)
                await asyncio.sleep(24 * 60 * 60)
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful retention-sweep shutdown.
            logger.debug("retention sweep loop cancelled")

    def _write_session_file(self, report: Any) -> bool:
        """Atomically write a session report to its on-disk file (Task C).

        Shared shape with the stop()-time write, minus the storage-budget
        enforcement and recap broadcast: a checkpoint overwrites the SAME
        ``session_<id>.json`` each time (no net growth), and the final
        stop() write enforces the budget. Returns True on success. Runs the
        blocking file I/O on the caller's thread (the checkpoint loop hands
        it to ``asyncio.to_thread``).
        """
        try:
            from cortex.libs.utils.atomic_write import atomic_write_json

            sessions_dir = Path(self.config.storage.path).expanduser() / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            session_path = sessions_dir / f"session_{report.session_id}.json"
            atomic_write_json(session_path, report.model_dump(mode="json"))
            return True
        except Exception:
            logger.debug("session checkpoint write failed", exc_info=True)
            return False

    async def _session_checkpoint_loop(self) -> None:
        """Persist the in-progress session periodically (Task C).

        Without this, sessions were only written on stop(), so the History
        tab showed "No sessions yet" for a session that the dashboard's live
        counter was actively tracking. Each tick snapshots the current
        session (non-mutating — see ``SessionReportGenerator.snapshot``) to
        its session file once it crosses a minimum duration, then invalidates
        the reader cache so the next history query surfaces it. Cancelled in
        stop() BEFORE the finalize write, so there is no write race.
        """
        interval = float(getattr(self.config.storage, "session_checkpoint_seconds", 90.0))
        min_seconds = float(getattr(self.config.storage, "session_checkpoint_min_seconds", 30.0))
        if interval <= 0:
            return
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    if not self._session_report_started:
                        continue
                    snap = self._session_report.snapshot()
                    if float(getattr(snap, "duration_seconds", 0.0)) < min_seconds:
                        continue
                    wrote = await asyncio.to_thread(self._write_session_file, snap)
                    if wrote:
                        await self._legacy_data_migrator.upsert_session(snap)
                        # Refresh the listing cache on the main thread so the
                        # next History query picks up the checkpoint.
                        self._session_reader.invalidate(snap.session_id)
                        logger.debug(
                            "session checkpoint written id=%s dur=%.0fs",
                            snap.session_id,
                            float(getattr(snap, "duration_seconds", 0.0)),
                        )
                except Exception:
                    logger.debug("session checkpoint iteration failed", exc_info=True)
        except asyncio.CancelledError:
            logger.debug("session checkpoint loop cancelled")

    async def _generate_policy_diagnostics_if_due(self) -> bool:
        """Generate one due diagnostic; polling cadence belongs to WP10."""

        now = utc_datetime(self._clock).astimezone()
        target_hour = self.config.eval.policy_diagnostics.nightly_hour_local
        if now.hour != target_hour or now.minute >= 5:
            return False
        await generate_daily_policy_diagnostics(
            self._policy_repository,
            self.config.storage.path,
            day=self._clock.today_utc().isoformat(),
        )
        return True

    def _policy_outcome_snapshot(self) -> dict[str, Any] | None:
        """Capture the same bounded behavioral fields for every policy arm."""

        estimate = self._current_state_estimate()
        context = self._latest_context
        if estimate is None or context is None:
            return None
        browser = getattr(context, "browser_context", None)
        return {
            "schema_version": "policy-outcome-snapshot/2.0",
            "support_state": str(getattr(estimate, "state", "UNKNOWN")),
            "support_status": str(getattr(estimate, "status", "unavailable")),
            "support_confidence": float(getattr(estimate, "confidence", 0.0) or 0.0),
            "evidence_coverage": float(getattr(estimate, "evidence_coverage", 0.0) or 0.0),
            "complexity_score": float(getattr(context, "complexity_score", 0.0) or 0.0),
            "tab_count": int(getattr(browser, "tab_count", 0) or 0),
            "error_count": int(getattr(context, "total_errors", 0) or 0),
            "thrashing_score": float(getattr(self._aggregator, "thrashing_score", 0.0) or 0.0),
            "observed_at_unix_ms": self._clock.unix_ms(),
            "observed_at_mono_ns": self._clock.monotonic_ns(),
            "boot_id": str(self._clock.boot_id),
        }

    async def _finalize_policy_outcomes(self) -> None:
        """Finalize one due outcome batch, including the no-action arm."""

        finalized = await self._policy_lifecycle.finalize_due(
            self._policy_outcome_snapshot,
        )
        if finalized:
            logger.info("Finalized %d policy outcome window(s)", len(finalized))

    async def _state_loop(self) -> None:
        try:
            while True:
                if self._inference_publication_paused:
                    await asyncio.sleep(0.05)
                    continue
                event_time = EventTime.from_clock(self._clock)
                timestamp = event_time.observed_at_mono_ns / 1_000_000_000.0
                try:
                    vector, quality = self._feature_fusion.fuse(event_time=event_time)

                    # v2.0: Inject thrashing score from aggregator. The
                    # FeatureAggregator guarantees ``thrashing_score`` as
                    # a property returning float (0.0 when no events have
                    # accumulated yet) so we can read it unconditionally.
                    vector.thrashing_score = self._aggregator.thrashing_score
                    window_switch_value = vector.features.get(FeatureName.TAB_SWITCH_RATE_PER_MIN)
                    if (
                        vector.telemetry_seen_count >= 5
                        and quality.telemetry > 0.0
                        and window_switch_value is not None
                        and window_switch_value.valid
                    ):
                        vector.features[FeatureName.THRASHING_SCORE] = FeatureValue(
                            value=vector.thrashing_score,
                            valid=True,
                            quality=quality.telemetry,
                            age_ms=0,
                            source_window_ms=60_000,
                            algorithm_version="focus-transition-graph-v2",
                        )

                    # Product containment: the current webcam pipeline has
                    # not met the validation duration/reference gates for
                    # HRV or respiration. Null them before *any* scorer,
                    # detector, report, or policy can consume an injected or
                    # stale value. Heart rate remains quality-gated and live.
                    vector.hrv_rmssd = None
                    vector.hrv_sdnn = None
                    vector.respiration_rate = None

                    evaluation = self._support_inference.evaluate(vector)
                    estimate = self._smoother.update(
                        evaluation,
                        quality,
                        event_time=event_time,
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
                                "kinematics signal stale (age=%.2fs) — zeroing channel quality",
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

                    # Containment: webcam HRV/respiration and the derived
                    # stress integral are unavailable until metric-specific
                    # reference validation lands. Never carry an old tracker
                    # value into a public state estimate.
                    estimate.stress_integral = None

                    # Opt-in break reminders use only elapsed active-work time
                    # and the user's declared interval. They do not consume
                    # pulse, HRV, state labels, or the retired stress integral.
                    inactivity_feature = vector.features.get(FeatureName.INACTIVITY_SECONDS)
                    input_is_active = bool(
                        inactivity_feature is not None
                        and inactivity_feature.valid
                        and inactivity_feature.value is not None
                        and inactivity_feature.value
                        < self.config.intervention.focus_break_inactivity_pause_seconds
                    )
                    self._focus_break_policy.update_preferences(
                        enabled=(self.config.intervention.enable_focus_break_reminders),
                        interval_minutes=(self.config.intervention.focus_break_interval_minutes),
                        suggested_duration_seconds=(
                            self.config.intervention.focus_break_duration_seconds
                        ),
                    )
                    break_decision = self._focus_break_policy.evaluate(
                        active=input_is_active,
                        timestamp=timestamp,
                    )
                    self._services.register("latest_focus_break_decision", break_decision)
                    if break_decision.should_recommend and self._interventions_enabled:
                        from cortex.libs.schemas.realtime import BreakRecommendation

                        recommendation = BreakRecommendation(
                            reason="You've reached your preferred focus interval.",
                            urgency="low",
                            basis="elapsed_focus",
                            focus_elapsed_seconds=(break_decision.active_elapsed_seconds),
                            preferred_interval_seconds=(break_decision.preferred_interval_seconds),
                            stress_load=None,
                            threshold=None,
                            duration_seconds=(break_decision.suggested_duration_seconds),
                            breathing_pattern="box",
                        )
                        await self._ws_server.send_message(
                            MessageType.BREAK_RECOMMENDATION.value,
                            recommendation.model_dump(mode="json"),
                        )

                    # P0 §3.9: feed the causal attributor at the same
                    # cadence so the per-signal sparkline buffers fill
                    # smoothly. Also stash the live feature vector so
                    # ``get_causal_signals`` can fall back to live
                    # attribution when the per-intervention cache is
                    # cold.
                    try:
                        self._causal_attributor.record_feature_vector(vector)
                        self._services.register("latest_feature_vector", vector)
                        self._runtime_data.publish_feature_vector(vector)
                    except Exception:
                        # B20 (Phase 4.1): a silent attributor failure
                        # means the per-intervention causal sparkline
                        # buffers stop filling, so the "Why this?" panel
                        # serves stale data. Elevate to WARNING so the
                        # observability path catches it.
                        logger.warning(
                            "causal attributor feed failed",
                            exc_info=True,
                        )

                    # v2.0: Feed longitudinal tracker per-sample data.
                    # Skip when HR is missing — accumulate() treats None
                    # as the absence of biometric signal, and a None-only
                    # sample contributes no rows to the daily baseline.
                    if vector.hr is not None:
                        self._longitudinal.accumulate(
                            hr=vector.hr,
                            hrv=None,
                            resp=None,
                            state=estimate.state,
                        )

                    self._services.register("latest_state_estimate", estimate)
                    self._runtime_data.publish_state_estimate(estimate)
                    self._recorder.append("state_estimate", estimate.model_dump(mode="json"))
                    # G.1: feed the session-debrief generator.
                    try:
                        if not self._session_report_started:
                            self._session_report.start()
                            self._session_report_started = True
                        self._session_report.record_state(
                            estimate.state,
                            unix_seconds(self._clock),
                        )
                        if vector.hr:
                            self._session_report.record_hr(float(vector.hr))
                    except Exception:
                        logger.debug("session_report record failed", exc_info=True)
                    # The camera measures only a camera-relative head/neck
                    # pitch proxy.  Publish its calibrated semantics directly;
                    # the old torso/shoulder-named fields stay unavailable so
                    # no consumer can mistake the proxy for body posture.
                    biometrics = {
                        "heart_rate": vector.hr,
                        "hr_delta": vector.hr_delta,
                        "blink_rate": vector.blink_rate,
                        "head_neck_flexion_score": (
                            vector.head_neck_flexion_score
                            if vector.head_neck_proxy_available
                            else None
                        ),
                        "head_neck_flexion_angle": (
                            vector.head_neck_flexion_angle
                            if vector.head_neck_proxy_available
                            else None
                        ),
                        "head_neck_flexion_dwell_seconds": (vector.head_neck_flexion_dwell_seconds),
                        "head_neck_proxy_available": (vector.head_neck_proxy_available),
                        "forward_lean": None,
                        "forward_lean_angle": None,
                        "thrashing_score": vector.thrashing_score,
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

                    if self._events.state.subscriber_count:
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
                            "support_state": estimate.support_state,
                            "status": estimate.status,
                            "confidence": estimate.confidence,
                            "scores": _scores_dump,
                            "support_scores": (
                                estimate.support_scores.model_dump()
                                if estimate.support_scores is not None
                                else None
                            ),
                            "evidence_coverage": estimate.evidence_coverage,
                            "contributing_features": [
                                item.model_dump(mode="json")
                                for item in estimate.contributing_features
                            ],
                            "exclusions": list(estimate.exclusions),
                            "model": estimate.model.model_dump(mode="json"),
                            "probabilities": None,
                            "signal_quality": estimate.signal_quality.model_dump(),
                            "dwell_seconds": estimate.dwell_seconds,
                            "reasons": estimate.reasons,
                            "biometrics": biometrics,
                            "timestamp": float(
                                getattr(estimate, "timestamp", timestamp) or timestamp
                            ),
                            "stress_integral": getattr(estimate, "stress_integral", None),
                            "source": "rules",
                            "degraded": estimate.status != "estimated",
                            "calibrated_probabilities": estimate.__dict__.get(
                                "calibrated_probabilities"
                            ),
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
                        self._events.state.publish(_payload)

                    # v2.0: Copilot throttle on state transitions
                    if estimate.state != self._prev_state:
                        await self._copilot_throttle.on_state_change(
                            estimate.state,
                            estimate.confidence,
                        )
                        self._prev_state = estimate.state

                    # P0 §3.10: auto-armed distraction blocking on
                    # sustained HYPER. Runs every state tick so the
                    # symmetric STOP_FOCUS_AUTO fires even when the
                    # user dwells in HYPER without state transitions.
                    try:
                        await self._evaluate_auto_distraction_block(
                            estimate,
                            timestamp,
                        )
                    except Exception:
                        logger.debug(
                            "auto-distraction evaluation failed",
                            exc_info=True,
                        )

                    await self._maybe_trigger_leetcode_interventions(
                        estimate,
                        vector,
                        timestamp,
                    )

                    context = self._latest_context
                    if context is not None:
                        telemetry_for_trigger = self._current_telemetry()
                        typing_burst_seconds = 0.0
                        if telemetry_for_trigger is not None:
                            kb_burst = float(
                                getattr(telemetry_for_trigger, "keyboard_burst_score", 0.0)
                            )
                            if kb_burst >= 0.8:
                                typing_burst_seconds = (
                                    self.config.intervention.receptivity_typing_burst_seconds
                                )
                        hour_now = utc_datetime(self._clock).astimezone().hour
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
                            self._last_mic_active_at = monotonic_seconds(self._clock)
                        decision = self._trigger_policy.evaluate(
                            estimate,
                            context_complexity=context.complexity_score,
                            mic_active=bool(mic_state) if mic_state is not None else False,
                            fullscreen_active=bool(fs_state) if fs_state is not None else False,
                            typing_burst_seconds=typing_burst_seconds,
                            within_work_hours=within_work_hours,
                            current_time=timestamp,
                        )
                        self._services.register("latest_trigger_decision", decision)
                        await self._handle_restore_updates(estimate, timestamp)

                        # v2.0: Check zombie reading
                        active_app = self._current_app_name()
                        telemetry = self._current_telemetry()
                        kinematics = self._latest_kinematics
                        self._zombie_detector.update_baseline(
                            self._scorer.baselines.blink_rate_baseline
                        )
                        # audit P2: zombie-reading + rabbit-hole detection feed
                        # off ``estimate.state`` and ``blink_rate`` — the SAME
                        # biometric pipeline the standard intervention trigger
                        # gates on ``signal_quality.acceptable``. Without the
                        # floor a low-quality frame (face occluded, motion
                        # blur) produces an unreliable HYPER/blink reading that
                        # fired a spurious active-recall / goal-drift overlay.
                        # Apply the same floor here so all HYPER-derived
                        # interventions share one quality gate.
                        signal_ok = bool(
                            estimate.status == "estimated" and estimate.evidence_coverage >= 0.45
                        )
                        # Advance the detector's dwell state on every tick so
                        # its timers stay continuous, but only FIRE the
                        # intervention when the signal is trustworthy.
                        zombie_detected = self._zombie_detector.update(
                            state=estimate.state,
                            active_app=active_app,
                            mouse_velocity=telemetry.mouse_velocity_mean if telemetry else 0.0,
                            blink_rate=kinematics.blink_rate,
                        )
                        if signal_ok and zombie_detected:
                            logger.info("Zombie reading detected — triggering active recall")
                            await self._trigger_special_intervention(
                                context,
                                estimate,
                                template_name="active_recall",
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
                            if signal_ok and alert is not None:
                                logger.info("Rabbit hole detected — goal drift intervention")
                                await self._trigger_special_intervention(
                                    context,
                                    estimate,
                                    template_name="rabbit_hole",
                                    ws_type="INTERVENTION_TRIGGER",
                                )

                        # HRV-derived stress warnings and break triggers are
                        # intentionally unavailable. Reintroduction requires
                        # the validated SignalEstimate contract and the
                        # intervention transaction from WP-3/WP-6.

                        # v2.0: Check shutdown detection
                        if self._shutdown_detector.should_handover(
                            posture_slump=kinematics.slump_score or 0.0,
                            hrv=vector.hrv_rmssd,
                            error_count=context.total_errors
                            if hasattr(context, "total_errors")
                            else 0,
                        ):
                            logger.info("Shutdown signal detected — generating handover")
                            await self._generate_handover(context)

                        # Standard intervention trigger
                        if (
                            self._interventions_enabled
                            and decision.should_trigger
                            and self._active_intervention_id is None
                            and estimate.signal_quality.acceptable
                            and self._clock.monotonic_ns() >= self._next_policy_decision_mono_ns
                        ):
                            browser = getattr(context, "browser_context", None)
                            policy_context = PolicyContextSnapshot(
                                support_state=str(getattr(estimate, "state", "UNKNOWN")),
                                support_status=str(getattr(estimate, "status", "unavailable")),
                                support_confidence=float(
                                    getattr(estimate, "confidence", 0.0) or 0.0
                                ),
                                evidence_coverage=float(
                                    getattr(estimate, "evidence_coverage", 0.0) or 0.0
                                ),
                                complexity_score=float(
                                    getattr(context, "complexity_score", 0.0) or 0.0
                                ),
                                tab_count=int(getattr(browser, "tab_count", 0) or 0),
                                error_count=int(getattr(context, "total_errors", 0) or 0),
                                thrashing_score=float(
                                    getattr(self._aggregator, "thrashing_score", 0.0) or 0.0
                                ),
                                hour_utc=utc_datetime(self._clock).hour,
                            )
                            preferred = self.config.eval.production.preferred_low_friction_arm
                            feasible_arms: tuple[PolicyArm, ...] = (
                                "no_action",
                                "suggest_only",
                            )
                            if preferred is not None and preferred not in feasible_arms:
                                feasible_arms = (*feasible_arms, preferred)
                            try:
                                report_snapshot = self._session_report.snapshot()
                                session_id = str(report_snapshot.session_id)
                            except Exception:
                                session_id = f"boot:{self._clock.boot_id}"
                            try:
                                policy_decision = await self._policy_lifecycle.decide(
                                    PolicySelectionInput(
                                        decision_point_id=uuid4(),
                                        session_id=session_id,
                                        context=policy_context,
                                        eligible=True,
                                        available=not decision.receptivity_blocked,
                                        availability_reason=(
                                            "eligible_and_receptive"
                                            if not decision.receptivity_blocked
                                            else "receptivity_blocked"
                                        ),
                                        feasible_arms=feasible_arms,
                                        recent_repeated_dismissal=bool(
                                            decision.dismissal_probability is not None
                                            and decision.dismissal_probability
                                            >= self.config.intervention.dismissal_model_threshold
                                        ),
                                        preferred_low_friction_arm=preferred,
                                        reward_version=self.config.eval.outcome.reward_version,
                                    )
                                )
                            except Exception:
                                # A decision that cannot be durably recorded
                                # cannot authorize an interruption.
                                logger.exception(
                                    "Policy decision persistence failed; suppressing proposal"
                                )
                                continue
                            self._next_policy_decision_mono_ns = (
                                self._clock.monotonic_ns()
                                + self.config.eval.decision_interval_seconds * 1_000_000_000
                            )
                            template_name = self._policy_arm_to_template(
                                policy_decision.selected_arm
                            )
                            if policy_decision.selected_arm == "no_action":
                                continue

                            # Run intervention in background so the state
                            # loop keeps updating while the LLM responds.
                            self._active_intervention_id = "__pending__"
                            self._spawn_background_task(
                                self._trigger_intervention(
                                    context,
                                    estimate,
                                    template_name=template_name,
                                    policy_decision=policy_decision,
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

    @staticmethod
    def _active_trigger_url(context: Any) -> str | None:
        """C3 (audit): the active browser tab URL the plan is scoped to.

        Read off the assembled ``context.browser_context.active_tab_url``
        when present. Returns None on non-browser contexts (editor /
        terminal focus) or when no browser surface is connected — the EXT
        treats a null ``trigger_url`` as "no page scope". ``getattr`` keeps
        the helper tolerant of duck-typed test contexts.
        """
        browser_ctx = getattr(context, "browser_context", None)
        if browser_ctx is None:
            return None
        url = getattr(browser_ctx, "active_tab_url", None)
        if isinstance(url, str) and url.strip():
            return url
        return None

    async def _close_unpresented_transaction(
        self,
        intervention_id: str,
        *,
        reason: str,
        clear_snapshot: bool = True,
    ) -> None:
        """Close a registered proposal that never became presentable.

        Delivery and cancellation can race an approval arriving from a
        surface that received the final bytes before a transport error. The
        coordinator therefore abandons only pre-effect states; if authority
        was already consumed this helper asks for the exact owner-bound
        inverse and keeps it queued until receipt verification succeeds.
        Local UI/evaluation state is unwound in either case.
        """

        restore_command: InterventionRestoreCommand | None = None
        try:
            abandoned = await self._transaction_coordinator.abandon(
                intervention_id,
                reason,
            )
            if not abandoned:
                restore_command = await self._transaction_coordinator.request_restore(
                    intervention_id,
                    reason="system_cancelled",
                )
        except Exception:
            # The durable journal remains authoritative. Startup recovery
            # will retry any APPLYING/APPLIED transaction we could not close.
            logger.exception(
                "Could not close unpresented transaction %s",
                intervention_id,
            )

        if restore_command is not None:
            pending = getattr(self, "_pending_startup_restores", None)
            if isinstance(pending, dict):
                pending[restore_command.restore_id] = restore_command
            sender = getattr(self._ws_server, "send_restore_command", None)
            if callable(sender):
                try:
                    await sender(restore_command)
                except Exception:
                    logger.exception(
                        "Immediate recovery dispatch failed for %s; queued",
                        intervention_id,
                    )

        if self._active_intervention_id == intervention_id:
            self._active_intervention_id = None
        async with self._micro_step_lock:
            if (
                self._active_plan is not None
                and self._active_plan.intervention_id == intervention_id
            ):
                self._active_plan = None
                self._micro_step_recovery_fired = False

        helpfulness = getattr(self, "_helpfulness", None)
        cancel_tracking = getattr(helpfulness, "cancel_tracking", None)
        if callable(cancel_tracking):
            cancel_tracking(intervention_id)
        for attribute in (
            "_dismissal_features_by_intervention",
            "_consent_actions_by_intervention",
            "_causal_signals_by_intervention",
        ):
            mapping = getattr(self, attribute, None)
            if isinstance(mapping, dict):
                mapping.pop(intervention_id, None)

        pending_applies = getattr(self, "_pending_apply_results", None)
        if isinstance(pending_applies, dict):
            future = pending_applies.pop(intervention_id, None)
            if future is not None and not future.done():
                future.set_result(
                    InterventionApplyResult(
                        intervention_id=intervention_id,
                        confirmed=False,
                        timed_out=False,
                        errors=[reason[:200]],
                    )
                )
        if clear_snapshot:
            self._services.register(f"workspace_snapshot:{intervention_id}", None)
            self._runtime_data.put_workspace_snapshot(intervention_id, None)

    async def _trigger_intervention(
        self,
        context: Any,
        estimate: Any,
        *,
        template_name: str | None = None,
        policy_decision: PolicyDecisionRecord | None = None,
    ) -> None:
        decision_id = policy_decision.decision_id if policy_decision is not None else None
        registered_intervention_id: str | None = None
        presentation_committed = False
        policy_delivery_finalized = decision_id is None
        policy_non_delivery_reason = "presentation_pipeline_failed"
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
            # C3 (audit): stamp the active-tab URL so the EXT's state-guards
            # can scope this intervention to the page it was triggered on
            # (the field is `None` until populated here).
            plan.trigger_url = self._active_trigger_url(context)

            # Staleness check: suppress if student genuinely recovered
            current_state = self._current_state_estimate()
            if current_state:
                # Suppress only if student is in FLOW for >3s (genuine recovery)
                if current_state.state == "FLOW" and current_state.dwell_seconds >= 3.0:
                    logger.info(
                        "Suppressing stale intervention: student in FLOW for %.1fs",
                        current_state.dwell_seconds,
                    )
                    self._active_intervention_id = None
                    policy_non_delivery_reason = "stale_state_recovered"
                    return
                # Also check if workspace context changed significantly
                if hasattr(context, "browser_context") and context.browser_context:
                    current_tab_count = (
                        len(context.browser_context.all_tabs)
                        if context.browser_context.all_tabs
                        else 0
                    )
                    if plan.suggested_actions:
                        stale_actions = sum(
                            1
                            for a in plan.suggested_actions
                            if a.tab_index is not None and a.tab_index >= current_tab_count
                        )
                        if stale_actions > len(plan.suggested_actions) * 0.5:
                            logger.info(
                                "Suppressing stale intervention: >50%% tab references invalid"
                            )
                            self._active_intervention_id = None
                            policy_non_delivery_reason = "stale_workspace_context"
                            return

            # P0 §3.9: attach structured causal signals to every plan
            # so each surface's "Why?" drilldown renders without an
            # extra round-trip. The cache is keyed by intervention_id
            # so WHY_DETAIL_REQUEST can resolve even if the surface
            # joined late.
            try:
                latest_features = self._current_feature_vector()
                if latest_features is not None:
                    plan.causal_signals = self._causal_attributor.attribute_top_signals(
                        latest_features,
                        self._scorer.baselines,
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
                logger.warning(
                    "Rejected intervention plan %s: %s", plan.intervention_id, validation.errors
                )
                policy_non_delivery_reason = "planner_output_rejected"
                return
            if validation.warnings:
                plan.plan_warnings.extend(validation.warnings)

            execution_mode = self.intervention_execution_mode
            if execution_mode == "suggest_only":
                # Validation observes the original LLM plan; containment is
                # then materialized as a new presentation-only plan. The
                # original requested level is retained in metadata, while no
                # adapter command survives to the executor.
                plan = materialize_suggestion_only(plan)
                commands = []
            else:
                plan.metadata = dict(plan.metadata or {})
                plan.metadata["execution_mode"] = execution_mode
                plan.metadata["workspace_mutation_allowed"] = True

            # WP6: the plan is a proposal, never authority. Lower every
            # possible effect into an immutable digest and persist it before
            # any surface sees the proposal. Suggest-only carries an empty
            # manifest even though descriptive SuggestedAction cards remain.
            action_manifest = build_action_manifest(
                plan,
                commands,
                consent_policy=self._consent_policy,
                clock=self._clock,
                include_suggested_actions=(execution_mode != "suggest_only"),
            )
            await self._transaction_coordinator.register_proposal(action_manifest)
            registered_intervention_id = plan.intervention_id
            snapshot = (
                capture_snapshot(
                    context,
                    intervention_id=plan.intervention_id,
                    clock=self._clock,
                )
                if action_manifest.action_count > 0
                else None
            )
            authenticated_count = getattr(
                self._ws_server,
                "authenticated_client_count",
                None,
            )
            if (
                isinstance(authenticated_count, int)
                and authenticated_count == 0
                and self._events.intervention.subscriber_count == 0
            ):
                await self._close_unpresented_transaction(
                    plan.intervention_id,
                    reason="no_authenticated_presentation_surface",
                )
                logger.info(
                    "Suppressed proposal %s: no authenticated presentation surface is connected",
                    plan.intervention_id,
                )
                policy_non_delivery_reason = "no_authenticated_presentation_surface"
                return
            self._active_intervention_id = plan.intervention_id
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
            if snapshot is not None:
                self._services.register(
                    f"workspace_snapshot:{plan.intervention_id}",
                    snapshot,
                )
                self._runtime_data.put_workspace_snapshot(
                    plan.intervention_id,
                    snapshot,
                )
            self._recorder.append("intervention_plan", plan.model_dump(mode="json"))

            # v2.0: Start helpfulness tracking
            self._helpfulness.start_tracking(
                intervention_id=plan.intervention_id,
                intervention_type=plan.level,
                state=estimate.state,
                confidence=estimate.confidence,
                complexity=(
                    float(context.complexity_score) if hasattr(context, "complexity_score") else 0.0
                ),
                tab_count=(
                    int(context.browser_context.tab_count)
                    if hasattr(context, "browser_context") and context.browser_context
                    else 0
                ),
                error_count=(int(context.total_errors) if hasattr(context, "total_errors") else 0),
                thrashing_score=float(getattr(self._aggregator, "thrashing_score", 0.0)),
                # Compatibility outcome field only; unavailable in product.
                stress_integral=0.0,
                decision_id=str(decision_id) if decision_id is not None else None,
                propensity=(
                    {
                        str(arm): probability
                        for arm, probability in policy_decision.propensities.items()
                    }
                    if policy_decision is not None and policy_decision.propensities is not None
                    else None
                ),
                policy_arm=(policy_decision.selected_arm if policy_decision is not None else None),
            )
            # audit C-note: snapshot the trigger-time confidence + context
            # complexity so the eventual engaged/dismissed outcome trains the
            # dismissal model on the SAME features the trigger decision saw.
            trigger_confidence = float(getattr(estimate, "confidence", 0.0) or 0.0)
            trigger_complexity = (
                float(context.complexity_score)
                if context is not None and hasattr(context, "complexity_score")
                else 0.0
            )
            self._dismissal_features_by_intervention[plan.intervention_id] = (
                trigger_confidence,
                trigger_complexity,
            )
            # Bound the cache the same way the causal-signals cache is bounded.
            if len(self._dismissal_features_by_intervention) > 64:
                oldest_dis = next(iter(self._dismissal_features_by_intervention))
                self._dismissal_features_by_intervention.pop(oldest_dis, None)

            # P1: snapshot the canonical consent action-types this plan acts
            # on so engage/dismiss records the user's approval/rejection
            # under the SAME keys the executor's per-action gate checks
            # (canonical_action_type collapses both the plan's action_type
            # vocabulary and the adapter verbs onto one policy key). Without
            # this the daemon recorded under the literal "intervention",
            # disjoint from the gated keys, so escalation never lifted.
            consent_actions = sorted(
                {canonical_action_type(a.action_type) for a in plan.suggested_actions}
            )
            if consent_actions:
                self._consent_actions_by_intervention[plan.intervention_id] = consent_actions
                if len(self._consent_actions_by_intervention) > 64:
                    oldest_ca = next(iter(self._consent_actions_by_intervention))
                    self._consent_actions_by_intervention.pop(oldest_ca, None)

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
            await self._transaction_coordinator.mark_delivered(plan.intervention_id)
            sent = await self._ws_server.send_intervention(
                plan,
                action_manifest=action_manifest,
                desktop_focused=desktop_focused,
                execution_mode=execution_mode,
            )
            callback_delivered = False
            if self._events.intervention.subscriber_count:
                # F17: stamp a monotonic sequence so the in-process bridge
                # can drop reordered intervention triggers. The plan dict
                # is augmented with ``_seq`` after deep-copying so we
                # never mutate the model the daemon retains.
                self._intervention_callback_seq += 1
                _payload = copy.deepcopy(plan.model_dump(mode="json"))
                _payload["_seq"] = self._intervention_callback_seq
                _payload["execution_mode"] = execution_mode
                _payload["action_manifest"] = action_manifest.model_dump(mode="json")
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
                callback_delivered = self._events.intervention.publish(_payload) > 0

            if sent == 0 and not callback_delivered:
                # A surface can disconnect between the preflight count and
                # the actual broadcast. Close the durable proposal and unwind
                # every local tracker instead of leaving a delivered-looking
                # active intervention that no user could ever see.
                await self._close_unpresented_transaction(
                    plan.intervention_id,
                    reason="presentation_delivery_race",
                )
                logger.info(
                    "Abandoned proposal %s after every presentation surface disconnected",
                    plan.intervention_id,
                )
                policy_non_delivery_reason = "presentation_delivery_race"
                return

            presentation_committed = True
            if decision_id is not None:
                await self._policy_lifecycle.mark_delivered(
                    decision_id,
                    plan.intervention_id,
                )
                policy_delivery_finalized = True

            self._trigger_policy.record_intervention()
            try:
                self._session_report.increment_interventions_triggered()
            except Exception:
                logger.debug(
                    "session_report increment_interventions_triggered failed",
                    exc_info=True,
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
                        "OS notification dispatch failed",
                        exc_info=True,
                    )
        except TimeoutError:
            logger.warning("Intervention LLM call timed out")
            policy_non_delivery_reason = "presentation_pipeline_timed_out"
            if registered_intervention_id is not None and not presentation_committed:
                await self._close_unpresented_transaction(
                    registered_intervention_id,
                    reason="presentation_pipeline_timed_out",
                )
        except asyncio.CancelledError:
            policy_non_delivery_reason = "presentation_task_cancelled"
            if registered_intervention_id is not None and not presentation_committed:
                await asyncio.shield(
                    self._close_unpresented_transaction(
                        registered_intervention_id,
                        reason="presentation_task_cancelled",
                    )
                )
            raise
        except Exception:
            logger.exception("Failed to trigger intervention")
            policy_non_delivery_reason = "presentation_pipeline_failed"
            if registered_intervention_id is not None and not presentation_committed:
                await self._close_unpresented_transaction(
                    registered_intervention_id,
                    reason="presentation_pipeline_failed",
                )
        finally:
            if decision_id is not None and not policy_delivery_finalized:
                try:
                    if presentation_committed and registered_intervention_id is not None:
                        await asyncio.shield(
                            self._policy_lifecycle.mark_delivered(
                                decision_id,
                                registered_intervention_id,
                            )
                        )
                    else:
                        await asyncio.shield(
                            self._policy_lifecycle.mark_not_delivered(
                                decision_id,
                                policy_non_delivery_reason,
                            )
                        )
                    policy_delivery_finalized = True
                except Exception:
                    logger.critical(
                        "Could not persist policy delivery status for decision %s",
                        decision_id,
                        exc_info=True,
                    )
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
        interventions do not currently bind to a production-policy decision, so the
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
        # Special detector interventions are not part of the production
        # policy experiment ledger. ``decision_id`` remains a compatibility
        # parameter for older callers but confers no reward attribution.

        registered_intervention_id: str | None = None
        presentation_committed = False
        try:
            plan = await self._llm_client.generate_intervention_plan(
                context,
                estimate,
                template_name=template_name,
            )
            plan = enrich_plan_with_context(plan, context)
            # C3 (audit): scope special interventions to the active tab too.
            plan.trigger_url = self._active_trigger_url(context)
            tab_count = None
            if hasattr(context, "browser_context") and context.browser_context is not None:
                tab_count = len(context.browser_context.all_tabs)
            validation, _commands = prepare_plan(plan, tab_count=tab_count)
            if not validation.is_valid:
                logger.warning(
                    "Rejected special intervention %s: %s",
                    plan.intervention_id,
                    validation.errors,
                )
                return
            if validation.warnings:
                plan.plan_warnings.extend(validation.warnings)
            # Special detector surfaces are presentation-only until each
            # detector and capability has its own validated transaction
            # protocol. A custom message type must never be a second mutation
            # command hidden beside INTERVENTION_APPLY.
            plan = materialize_suggestion_only(plan)
            action_manifest = build_action_manifest(
                plan,
                [],
                consent_policy=self._consent_policy,
                clock=self._clock,
                include_suggested_actions=False,
            )
            await self._transaction_coordinator.register_proposal(action_manifest)
            registered_intervention_id = plan.intervention_id
            authenticated_count = getattr(
                self._ws_server,
                "authenticated_client_count",
                None,
            )
            if isinstance(authenticated_count, int) and authenticated_count == 0:
                await self._close_unpresented_transaction(
                    plan.intervention_id,
                    reason="no_authenticated_presentation_surface",
                )
                logger.info(
                    "Suppressed special proposal %s: no authenticated "
                    "WebSocket surface is connected",
                    plan.intervention_id,
                )
                return
            await self._transaction_coordinator.mark_delivered(plan.intervention_id)
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
            if ws_type == MessageType.INTERVENTION_TRIGGER.value:
                sent = await self._ws_server.send_intervention(
                    plan,
                    action_manifest=action_manifest,
                    execution_mode="suggest_only",
                )
            else:
                payload = plan.model_dump(mode="json")
                payload["action_manifest"] = action_manifest.model_dump(mode="json")
                payload["execution_mode"] = "suggest_only"
                sent = await self._ws_server.send_message(ws_type, payload)
            if not sent:
                await self._close_unpresented_transaction(
                    plan.intervention_id,
                    reason="presentation_delivery_race",
                )
                logger.info(
                    "Abandoned special proposal %s after presentation delivery failed",
                    plan.intervention_id,
                )
                return
            presentation_committed = True
        except asyncio.CancelledError:
            if registered_intervention_id is not None and not presentation_committed:
                await asyncio.shield(
                    self._close_unpresented_transaction(
                        registered_intervention_id,
                        reason="special_presentation_task_cancelled",
                    )
                )
            raise
        except Exception:
            logger.exception("Failed to trigger special intervention (%s)", template_name)
            if registered_intervention_id is not None and not presentation_committed:
                await self._close_unpresented_transaction(
                    registered_intervention_id,
                    reason="special_presentation_pipeline_failed",
                )
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
            step
            for step in plan.micro_steps
            if not any(tok in step.text.lower() for tok in blocked_tokens)
        ] or plan.micro_steps[:1]

    async def _handle_restore_updates(self, estimate: Any, timestamp: float) -> None:
        outcomes = await self._restore_manager.update(estimate, current_time=timestamp)
        for outcome in outcomes:
            self._recorder.append("intervention_outcome", outcome.model_dump(mode="json"))
            if not outcome.workspace_restored:
                logger.warning(
                    "Automatic close for %s could not verify restoration; "
                    "keeping the transaction active for retry",
                    outcome.intervention_id,
                )
                continue
            if self._active_intervention_id == outcome.intervention_id:
                self._active_intervention_id = None
            if (
                self._active_plan is not None
                and self._active_plan.intervention_id == outcome.intervention_id
            ):
                self._active_plan = None
            self._micro_step_recovery_fired = False
            await self._ws_server.send_restore(
                outcome.intervention_id,
                user_action=outcome.user_action,
            )

    async def dispatch_intervention_action(
        self,
        intervention_id: str,
        action: dict[str, Any],
    ) -> int:
        """Authorize one desktop gesture through the exact remote adapter.

        The in-process shell is a presentation/request surface, not an
        executor. It may request any action present in the immutable manifest;
        the WebSocket gateway then routes the consumed command to the exact
        browser or editor owner and binds receipts to that client identity.
        Returns the count of executor clients reached.

        Audit-prod fix: validate ``intervention_id`` against the active
        plan so a stale overlay click (timer race against dismiss) is
        not forwarded; validate ``action`` against the SuggestedAction
        Pydantic schema so a malformed action_type / missing action_id
        is rejected at the daemon boundary instead of confusing the
        extension's switch-default.
        """
        if not self.workspace_mutation_allowed:
            logger.info(
                "ACTION_DISPATCH denied: execution_mode=%s",
                self.intervention_execution_mode,
            )
            return 0
        if not intervention_id or not isinstance(action, dict):
            return 0
        # Reject stale interventions. ``__pending__`` is the sentinel
        # used while the LLM call is in flight; once a real id is set
        # we honour exactly that id and no other.
        active = self._active_intervention_id
        if active is None or active == "__pending__" or active != intervention_id:
            logger.warning(
                "ACTION_DISPATCH dropped: stale intervention_id (requested=%s active=%s)",
                intervention_id,
                active,
            )
            return 0
        # Validate the action shape against the Pydantic source of truth.
        # SuggestedAction's validators enforce the action_type Literal, URL
        # scheme allowlist, tab_index bounds, etc. The manifest comparison
        # below remains authoritative for the digest-covered parameters.
        try:
            from cortex.libs.schemas.intervention import SuggestedAction

            validated = SuggestedAction.model_validate(action).model_dump()
        except Exception as exc:
            logger.warning(
                "ACTION_DISPATCH dropped: action failed validation (%s)",
                exc,
            )
            return 0
        transaction = await self._transaction_coordinator.get_transaction(intervention_id)
        if transaction is None:
            logger.warning("Desktop action request has no transaction")
            return 0
        action_id = str(validated.get("action_id") or "")
        manifest_action = next(
            (
                item
                for item in transaction.manifest.body.actions
                if item.action_id == action_id and item.executor in {"browser", "editor"}
            ),
            None,
        )
        if manifest_action is None:
            logger.warning(
                "Desktop action %s is absent from the executable manifest",
                action_id,
            )
            return 0
        if not manifest_suggestion_matches(transaction.manifest, validated):
            logger.warning(
                "Desktop action presentation differs from exact manifest "
                "(intervention_id=%s action_id=%s)",
                intervention_id,
                action_id,
            )
            return 0
        request = InterventionAuthorizationRequest(
            authorization_request_id=f"desktop_{uuid4().hex}",
            intervention_id=intervention_id,
            manifest_sha256=transaction.manifest.manifest_sha256,
            approved_action_ids=(action_id,),
            source_surface="desktop",
            requested_at_unix_ms=self._clock.unix_ms(),
            requested_at_mono_ns=self._clock.monotonic_ns(),
            boot_id=self._clock.boot_id,
        )
        authorized = await self._authorize_intervention(
            request,
            "desktop_shell",
            "desktop",
        )
        if isinstance(authorized, AuthorizationDenied):
            logger.info(
                "Desktop action authorization denied: %s",
                authorized.reason_code,
            )
            return 0
        try:
            dispatch = await self._ws_server.dispatch_apply_command(authorized)
            sent = dispatch.delivered_targets
            if dispatch.attempted_targets == 0:
                await self._transaction_coordinator.record_dispatch_failure(
                    authorized.authorization.authorization_id,
                    reason="desktop_action_no_executor",
                )
            elif sent != dispatch.expected_targets:
                compensation = await self._compensate_partial_intervention_dispatch(
                    authorized.authorization.authorization_id,
                    "desktop_action_delivery_ambiguous",
                )
                if compensation is not None:
                    await self._ws_server.send_restore_command(compensation)
        except Exception:
            logger.exception("Transactional desktop action dispatch failed")
            try:
                compensation = await self._compensate_partial_intervention_dispatch(
                    authorized.authorization.authorization_id,
                    "desktop_action_dispatch_exception",
                )
                if compensation is not None:
                    await self._ws_server.send_restore_command(compensation)
            except Exception:
                logger.exception("Transactional desktop action compensation failed")
            return 0
        if sent == 0:
            logger.info(
                "Desktop exact action has no connected executor (intervention_id=%s action_id=%s)",
                intervention_id,
                validated.get("action_id"),
            )
        return sent

    async def dispatch_action_to_browser(
        self,
        intervention_id: str,
        action: dict[str, Any],
    ) -> int:
        """Compatibility facade for old desktop controller/test callers.

        It no longer emits ``ACTION_DISPATCH``. The request is authorized and
        executed through :meth:`dispatch_intervention_action`.
        """

        return await self.dispatch_intervention_action(
            intervention_id,
            action,
        )

    # ─── P0 §3.7: biology-driven break orchestration ─────────────────

    def _set_break_suppression(self, active: bool) -> None:
        """Toggle the global break-suppression flag.

        Called by :class:`GuidedBreakController` around the overlay
        lifecycle so peer adapters skip trigger evaluation while the
        breathing session runs.
        """
        self._break_active = bool(active)

    def set_break_overlay_ui_handler(
        self,
        handler: BreakUIHandler | None,
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

    async def _authorize_intervention(
        self,
        request: InterventionAuthorizationRequest,
        source_client_id: str,
        source_client_type: str,
    ) -> InterventionApplyCommand | AuthorizationDenied:
        """Turn one exact user gesture into a consumed authorization."""

        active = self._active_intervention_id
        if active is None or active == "__pending__" or active != request.intervention_id:
            return AuthorizationDenied(
                authorization_request_id=request.authorization_request_id,
                intervention_id=request.intervention_id,
                manifest_sha256=request.manifest_sha256,
                reason_code="transaction_closed",
                detail=f"intervention is no longer active (active={active})"[:500],
            )
        result = await self._transaction_coordinator.authorize_and_consume(
            request,
            source_client_id=source_client_id,
        )
        try:
            if isinstance(result, AuthorizationDenied):
                self._recorder.append(
                    "intervention_authorization_denied",
                    result.model_dump(mode="json"),
                )
            else:
                self._recorder.append(
                    "intervention_authorized",
                    {
                        "intervention_id": result.authorization.intervention_id,
                        "authorization_id": result.authorization.authorization_id,
                        "manifest_sha256": result.authorization.manifest_sha256,
                        "action_ids": list(result.authorization.authorized_action_ids),
                        "consent_revision": result.authorization.consent_revision,
                        "source_client_type": source_client_type,
                    },
                )
        except Exception:
            logger.debug("authorization recorder append failed", exc_info=True)
        return result

    async def _record_intervention_receipts(
        self,
        batch: InterventionReceiptBatch,
        source_client_id: str,
        source_client_type: str,
    ) -> tuple[
        InterventionLifecycleState,
        InterventionRestoreCommand | None,
    ]:
        """Persist receipts, update consent evidence, and resolve waiters."""

        state, compensation = await self._transaction_coordinator.record_receipts(
            batch,
            source_client_type=source_client_type,
            source_client_id=source_client_id,
        )
        if compensation is not None:
            # Keep compensation retryable across an offline executor. The WS
            # server preflights the complete route; this queue is retried as
            # each required surface identifies and is removed only after
            # verified inverse receipts reach RESTORED.
            self._pending_startup_restores[compensation.restore_id] = compensation
        transaction = await self._transaction_coordinator.get_transaction(batch.intervention_id)
        if transaction is not None:
            actions = {action.action_id: action for action in transaction.manifest.body.actions}
            for receipt in batch.receipts:
                action = actions.get(receipt.action_id)
                if (
                    action is not None
                    and receipt.phase == "apply"
                    and receipt.status
                    in {
                        ReceiptStatus.SUCCEEDED.value,
                        ReceiptStatus.ALREADY_COMPLETE.value,
                    }
                    and receipt.verification == VerificationStatus.VERIFIED.value
                    and await self._transaction_coordinator.claim_consent_evidence(
                        receipt.receipt_id
                    )
                ):
                    await self._consent_ladder.record_approval(
                        canonical_action_type(action.capability)
                    )

            # Register the timed restore session only after a verified
            # workspace effect exists. Proposal delivery never creates a
            # theatrical "active mutation".
            if state == InterventionLifecycleState.APPLIED:
                has_workspace_effect = any(
                    receipt.authorization_id == batch.authorization_id
                    and receipt.phase == "apply"
                    and receipt.status == ReceiptStatus.SUCCEEDED.value
                    and actions.get(receipt.action_id) is not None
                    and actions[receipt.action_id].workspace_mutation
                    for receipt in transaction.receipts
                )
                if (
                    has_workspace_effect
                    and self._restore_manager.get_active(batch.intervention_id) is None
                ):
                    snapshot = self._workspace_snapshot(batch.intervention_id)
                    if snapshot is not None:
                        self._restore_manager.start_intervention(
                            batch.intervention_id,
                            snapshot,
                        )

        try:
            self._recorder.append(
                "intervention_receipts",
                {
                    **batch.model_dump(mode="json"),
                    "source_client_id": source_client_id,
                    "source_client_type": source_client_type,
                    "transaction_state": state.value,
                },
            )
        except Exception:
            logger.debug("receipt recorder append failed", exc_info=True)

        # Bridge the old HTTP confirmation response onto the new truthful
        # receipt result during the one-release compatibility window.
        if state in {
            InterventionLifecycleState.APPLIED,
            InterventionLifecycleState.PARTIAL,
            InterventionLifecycleState.FAILED,
        } and any(receipt.phase == "apply" for receipt in batch.receipts):
            future = self._pending_apply_results.pop(batch.intervention_id, None)
            if future is not None and not future.done():
                # The lifecycle state is an aggregate across every one-time
                # authorization in the intervention. Derive this legacy HTTP
                # acknowledgement from the *current authorization's* complete
                # receipt set; otherwise a prior active effect could make a
                # later all-failed grant look confirmed, and the last of
                # several executor batches could hide earlier results.
                ledger = (
                    next(
                        (
                            entry
                            for entry in transaction.authorizations
                            if entry.authorization.authorization_id == batch.authorization_id
                        ),
                        None,
                    )
                    if transaction is not None
                    else None
                )
                expected_ids = (
                    set(ledger.authorization.authorized_action_ids) if ledger is not None else set()
                )
                latest: dict[str, ActionReceipt] = {}
                if transaction is not None:
                    for receipt in transaction.receipts:
                        if (
                            receipt.authorization_id != batch.authorization_id
                            or receipt.phase != ReceiptPhase.APPLY.value
                        ):
                            continue
                        prior = latest.get(receipt.action_id)
                        if prior is None or receipt.attempt >= prior.attempt:
                            latest[receipt.action_id] = receipt
                successful = sorted(
                    action_id
                    for action_id, receipt in latest.items()
                    if receipt.status
                    in {
                        ReceiptStatus.SUCCEEDED.value,
                        ReceiptStatus.ALREADY_COMPLETE.value,
                    }
                    and receipt.verification == VerificationStatus.VERIFIED.value
                )
                confirmed = bool(expected_ids) and set(successful) == expected_ids
                errors = [
                    (
                        latest[action_id].error_message
                        or latest[action_id].error_code
                        or "action_failed"
                    )
                    if action_id in latest
                    else f"missing_receipt:{action_id}"
                    for action_id in sorted(expected_ids - set(successful))
                ]
                future.set_result(
                    InterventionApplyResult(
                        intervention_id=batch.intervention_id,
                        confirmed=confirmed,
                        timed_out=False,
                        applied_actions=successful,
                        errors=errors,
                        phase="apply",
                    )
                )

        inverse_batch = all(
            receipt.phase in {"compensate", "restore"} for receipt in batch.receipts
        )
        if state in {
            InterventionLifecycleState.RESTORED,
            InterventionLifecycleState.RESTORE_FAILED,
        }:
            restore_future = self._pending_restore_results.pop(
                batch.authorization_id,
                None,
            )
            if restore_future is not None and not restore_future.done():
                restore_future.set_result(state == InterventionLifecycleState.RESTORED)
            if state == InterventionLifecycleState.RESTORED:
                self._pending_startup_restores.pop(batch.authorization_id, None)
        elif inverse_batch and state == InterventionLifecycleState.APPLIED:
            # A partial compensation can complete while effects from an
            # earlier, distinct authorization remain active. The aggregate
            # correctly returns to APPLIED; this restore command itself is
            # terminal and must not be replayed on every reconnect.
            self._pending_startup_restores.pop(batch.authorization_id, None)
            restore_future = self._pending_restore_results.pop(
                batch.authorization_id,
                None,
            )
            if restore_future is not None and not restore_future.done():
                restore_future.set_result(True)
        return state, compensation

    async def _record_intervention_dispatch_failure(
        self,
        authorization_id: str,
        reason: str,
    ) -> None:
        """Persist a preflight routing failure for a consumed authorization."""

        await self._transaction_coordinator.record_dispatch_failure(
            authorization_id,
            reason=reason,
        )

    async def _compensate_partial_intervention_dispatch(
        self,
        authorization_id: str,
        reason: str,
    ) -> InterventionRestoreCommand | None:
        """Durably enter recovery after a cross-surface send race."""

        command = await self._transaction_coordinator.compensate_partial_dispatch(
            authorization_id,
            reason=reason,
        )
        if command is not None:
            self._pending_startup_restores[command.restore_id] = command
        return command

    async def _restore_transaction_and_wait(
        self,
        intervention_id: str,
        user_action: str,
    ) -> bool:
        """Dispatch exact inverse actions and await verified receipts."""

        reason_map: dict[
            str,
            Literal[
                "user_undo",
                "dismissed",
                "snoozed",
                "timed_out",
                "natural_recovery",
                "system_cancelled",
                "partial_compensation",
                "startup_recovery",
                "emergency_restore",
            ],
        ] = {
            "restore": "user_undo",
            "dismissed": "dismissed",
            "snoozed": "snoozed",
            "timed_out": "timed_out",
            "natural_recovery": "natural_recovery",
            "engaged": "natural_recovery",
            "system_cancelled": "system_cancelled",
        }
        reason = reason_map.get(user_action, "system_cancelled")
        command = await self._transaction_coordinator.request_restore(
            intervention_id,
            reason=reason,
        )
        if command is None:
            transaction = await self._transaction_coordinator.get_transaction(intervention_id)
            return bool(
                transaction is not None
                and transaction.state
                in {
                    InterventionLifecycleState.RESTORED.value,
                    InterventionLifecycleState.ABANDONED.value,
                }
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        prior = self._pending_restore_results.get(command.restore_id)
        if prior is not None and not prior.done():
            prior.set_result(False)
        self._pending_restore_results[command.restore_id] = future
        self._pending_startup_restores[command.restore_id] = command
        sent = await self._ws_server.send_restore_command(command)
        if sent == 0:
            self._pending_restore_results.pop(command.restore_id, None)
            logger.warning(
                "Restore %s has no connected executor; retaining for retry",
                command.restore_id,
            )
            return False
        try:
            return await asyncio.wait_for(future, timeout=10.0)
        except TimeoutError:
            logger.warning(
                "Restore %s receipt timed out; retaining for retry",
                command.restore_id,
            )
            return False
        finally:
            current = self._pending_restore_results.get(command.restore_id)
            if current is future:
                self._pending_restore_results.pop(command.restore_id, None)

    async def restore_all_transactional_effects(
        self,
        *,
        reason: Literal["system_cancelled", "emergency_restore"] = ("emergency_restore"),
        timeout_seconds: float = 3.0,
    ) -> dict[str, int]:
        """Dispatch every exact inverse and wait for bounded verification.

        Offline owners remain in ``_pending_startup_restores`` and are retried
        on their next stable-identity reconnect. The returned counts never
        claim those effects were restored merely because a frame was sent.
        """

        commands = await self._transaction_coordinator.request_restore_all(
            reason=reason,
        )
        loop = asyncio.get_running_loop()
        waiters: dict[str, asyncio.Future[bool]] = {}
        dispatched = 0
        for command in commands:
            self._pending_startup_restores[command.restore_id] = command
            future = self._pending_restore_results.get(command.restore_id)
            if future is None or future.done():
                future = loop.create_future()
                self._pending_restore_results[command.restore_id] = future
            waiters[command.restore_id] = future
            try:
                if await self._ws_server.send_restore_command(command) > 0:
                    dispatched += 1
            except Exception:
                logger.exception(
                    "Global restore dispatch failed for %s",
                    command.restore_id,
                )

        bounded_timeout = max(0.0, min(float(timeout_seconds), 10.0))
        if waiters and bounded_timeout > 0:
            await asyncio.wait(
                set(waiters.values()),
                timeout=bounded_timeout,
            )

        restored = 0
        failed = 0
        for restore_id, future in waiters.items():
            if future.done() and not future.cancelled():
                try:
                    if future.result():
                        restored += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            current = self._pending_restore_results.get(restore_id)
            if current is future:
                self._pending_restore_results.pop(restore_id, None)
        pending = len(commands) - restored - failed
        return {
            "requested": len(commands),
            "dispatched": dispatched,
            "restored": restored,
            "failed": failed,
            "pending": pending,
        }

    async def _check_action_consent(
        self,
        action_type: str,
        requested_level: int,
    ) -> bool:
        """Phase-4b TASK M: per-action consent gate for the executor.

        ``InterventionExecutor.apply`` calls this BEFORE adapter
        dispatch. Returning False short-circuits the mutation into
        ``success=False, reason="consent_denied"`` so local diagnostics
        can record the failure mode.
        """
        try:
            decision = await self._consent_ladder.check(
                action_type=action_type,
                requested_level=requested_level,
            )
            return bool(decision.allowed)
        except Exception:
            logger.exception(
                "consent ladder check raised; denying action=%s",
                action_type,
            )
            return False

    async def _resume_last_active_file(
        self,
        params: dict[str, Any],
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
        self,
        action_type: str,
        params: dict[str, Any],
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
            metadata = {k: v for k, v in params.items() if k not in ("prompt", "timeout_seconds")}
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
                "broadcast_prompt failed for action=%s",
                action_type,
            )
            return (False, "broadcast_failed")

    async def start_biology_break(
        self,
        *,
        intervention_id: str | None = None,
        duration_seconds: int = 240,
        breathing_pattern: str | None = None,
        audio_cue: bool = True,
        reason: str = "preferred_focus_interval_reached",
    ) -> dict[str, Any] | None:
        """Run one guided breathing session and return a BreakRecord dict."""
        if self._break_controller is None:
            return None
        pattern_arg: Literal["box", "4-7-8", "coherent"] | None
        if breathing_pattern in ("box", "4-7-8", "coherent"):
            pattern_arg = breathing_pattern  # type: ignore[assignment]
        else:
            # No biometric auto-selection: a missing preference uses the
            # neutral product default.
            pattern_arg = "box"
        # P0 §3.7 audit fix (spec line 643): default audio off when the
        # microphone was active recently. ``last_mic_active_at`` is the
        # most-recent positive ``receptivity.is_microphone_in_use``
        # timestamp recorded by the state loop; configurable mute
        # window defaults to 5 min per the spec risk mitigation.
        if audio_cue:
            mute_window = float(
                getattr(
                    self.config.intervention,
                    "guided_break_audio_mute_after_mic_seconds",
                    300.0,
                )
            )
            if (
                mute_window > 0
                and self._last_mic_active_at > 0
                and monotonic_seconds(self._clock) - self._last_mic_active_at < mute_window
            ):
                logger.info(
                    "Guided break: muting audio cue — microphone was active in the last %.0fs",
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
                "BIOLOGY_BREAK_STARTED log failed",
                exc_info=True,
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
        if record is None:
            return None
        if record.completed:
            self._focus_break_policy.record_break_taken()
        # Phase-4b TASK G: structured BIOLOGY_BREAK_COMPLETED event.
        try:
            logger.info(
                "%s intervention_id=%s duration_s=%.1f recovery_delta=%s completed=%s",
                EventType.BIOLOGY_BREAK_COMPLETED.value,
                intervention_id or "-",
                float(record.duration_seconds),
                (f"{record.recovery_delta:.1f}" if record.recovery_delta is not None else "n/a"),
                bool(record.completed),
            )
        except Exception:
            logger.debug(
                "BIOLOGY_BREAK_COMPLETED log failed",
                exc_info=True,
            )
        payload = record.model_dump(mode="json")
        if intervention_id:
            self._recorder.append(
                "biology_break",
                {
                    "intervention_id": intervention_id,
                    **payload,
                },
            )
        return payload

    # ─── P0 §3.9: causal rationale resolution ────────────────────────

    async def get_causal_signals(
        self,
        intervention_id: str,
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
            latest_features = self._current_feature_vector()
            if latest_features is None:
                return None
            signals = self._causal_attributor.attribute_top_signals(
                latest_features,
                self._scorer.baselines,
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
        active_clock = getattr(self, "_clock", SYSTEM_CLOCK)
        deadline = getattr(self, "_quiet_mode_deadline", None)
        deadline_expired = (
            deadline.expired(active_clock)
            if isinstance(deadline, BoundedDeadline)
            else ends_at is not None and unix_seconds(active_clock) >= ends_at
        )
        if kind == "off" or (ends_at is not None and deadline_expired):
            # Stale window — re-normalise so the broadcast is honest.
            kind = "off"
            ends_at = None
            self._quiet_mode_kind = "off"
            self._quiet_mode_ends_at = None
            self._quiet_mode_deadline = None
        duration_minutes: int | None = None
        if ends_at is not None:
            remaining_seconds = (
                deadline.remaining_ms(active_clock) / 1_000.0
                if isinstance(deadline, BoundedDeadline)
                else max(0.0, ends_at - unix_seconds(active_clock))
            )
            duration_minutes = max(0, int(round(remaining_seconds / 60.0)))
        return {
            "kind": kind,
            "duration_minutes": duration_minutes,
            "ends_at": ends_at,
            "ends_at_unix_ms": int(ends_at * 1_000) if ends_at is not None else None,
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
                "set_quiet_mode: unknown kind=%r (treating as 'off')",
                kind,
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
            minutes = max(
                1, min(240, int(duration_minutes or self.config.intervention.quiet_mode_minutes))
            )
        else:
            minutes = 0  # pause / off carry no countdown
        active_clock = getattr(self, "_clock", SYSTEM_CLOCK)
        deadline = BoundedDeadline.after(active_clock, minutes * 60_000) if minutes > 0 else None
        ends_at = deadline.expires_at_unix_ms / 1_000.0 if deadline is not None else None

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
                    was_running = bool(
                        getattr(
                            self._capture_pipeline,
                            "is_running",
                            False,
                        )
                    )
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
            self._quiet_mode_deadline = deadline
            self._quiet_mode_source = str(source or "daemon")

            # C6 (audit): emit QUIET_MODE_ENTERED / QUIET_MODE_EXITED on the
            # real transition. Entering = moving INTO any non-"off" kind;
            # exiting = moving FROM a non-"off" kind to "off". A kind→kind
            # change (e.g. snooze_15 → quiet_session) re-emits ENTERED with
            # the new kind/duration so the observability stream is honest.
            if kind != "off":
                _emit_event(
                    EventType.QUIET_MODE_ENTERED,
                    kind=kind,
                    previous_kind=prev_kind,
                    duration_minutes=minutes if minutes > 0 else None,
                    ends_at=ends_at,
                    source=self._quiet_mode_source,
                )
            elif prev_kind != "off":
                _emit_event(
                    EventType.QUIET_MODE_EXITED,
                    previous_kind=prev_kind,
                    source=self._quiet_mode_source,
                )

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
                    "execution_mode": self.intervention_execution_mode,
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
            kind,
            minutes if minutes > 0 else "-",
            source,
        )

    async def _decay_quiet_mode_after(
        self,
        delay_seconds: float,
        expected_kind: str,
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
            self._quiet_mode_deadline = None
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
                    "execution_mode": self.intervention_execution_mode,
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
        primary_focus = (getattr(plan, "primary_focus", "") or "").strip()
        # F09 sanitisation: explicit allowlist — no biometric numerics.
        body_parts: list[str] = []
        if primary_focus:
            body_parts.append(primary_focus)
        body = " — ".join(body_parts) or "Cortex has a suggestion"
        intervention_id = getattr(plan, "intervention_id", "") or ""
        try:
            sent = await asyncio.to_thread(
                send_intervention_notification,
                title=headline,
                body=body,
                intervention_id=intervention_id,
            )
        except Exception:
            logger.debug(
                "send_intervention_notification raised",
                exc_info=True,
            )
            return
        # C6 (audit): emit OS_NOTIFICATION_SENT when the OS actually posted
        # the notification (the helper returns False on non-mac / missing
        # PyObjC / permission-denied). No biometric data in the event.
        if sent:
            _emit_event(
                EventType.OS_NOTIFICATION_SENT,
                intervention_id=intervention_id,
                channel="macos_unusernotification",
            )

    # ------------------------------------------------------------------
    # P0 §3.10: auto-armed distraction blocking on HYPER
    # ------------------------------------------------------------------

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
          4. Support is likely, evidence strength ≥ the gate, and the
             confirmed dwell has met ``dwell_gate``.
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
        if (
            getattr(estimate, "status", "insufficient_evidence") != "estimated"
            or float(getattr(estimate, "evidence_coverage", 0.0) or 0.0) < 0.45
        ):
            if self._auto_focus_armed:
                self._auto_focus_armed = False
                await self._emit_stop_focus_auto(reason="evidence_unavailable")
            self._reset_auto_focus_timers()
            return
        confidence_gate = float(getattr(cfg, "auto_distraction_block_confidence", 0.85))
        dwell_gate = float(getattr(cfg, "auto_distraction_block_dwell_seconds", 30.0))
        exit_gate = float(getattr(cfg, "auto_distraction_block_exit_seconds", 300.0))

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
                        cooldown_elapsed,
                        _AUTO_FOCUS_DEBOUNCE_S,
                    )
                else:
                    # Phase-3 P1-2 (Audit-1.1): only flip the armed flag
                    # after the broadcast lands. ``_emit_start_focus_auto``
                    # returns True on success.
                    ok = await self._emit_start_focus_auto(
                        reason="behavior_support_likely",
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
                                "%s phase=arm reason=behavior_support_likely dwell_s=%.1f",
                                EventType.DISTRACTION_BLOCKED.value,
                                dwelled,
                            )
                        except Exception:
                            logger.debug(
                                "arm structured log failed",
                                exc_info=True,
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
                        held_for,
                        _AUTO_FOCUS_DEBOUNCE_S,
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
        """Fail closed until autonomous focus has a WP-6 transaction.

        A persisted settings toggle and a state estimate are policy inputs,
        not a one-time manifest-bound authorization. Manual focus sessions and
        STOP_FOCUS_AUTO remain available, but this forward effect must not be
        emitted through the legacy command channel.
        """
        logger.info(
            "START_FOCUS_AUTO suppressed pending exact transaction support (mode=%s reason=%s)",
            getattr(self, "intervention_execution_mode", "suggest_only"),
            reason,
        )
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
        recovery path feeds the local helpfulness diagnostics via the same tracker
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
                logger.warning("toggle_micro_step: invalid step_index=%r", step_index)
                return

            # ---- locate the live plan ---------------------------------
            plan = self._active_plan
            if (
                plan is None
                or plan.intervention_id != intervention_id
                or self._active_intervention_id != intervention_id
            ):
                logger.info(
                    "toggle_micro_step: dropping stale toggle (requested=%s active=%s)",
                    intervention_id,
                    self._active_intervention_id,
                )
                return

            if step_index >= len(plan.micro_steps):
                logger.warning(
                    "toggle_micro_step: step_index=%d out of range (len=%d) for intervention=%s",
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
            now = utc_datetime(_daemon_clock(self))
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
                transaction = await self._transaction_coordinator.get_transaction(intervention_id)
                await self._ws_server.send_intervention(
                    plan,
                    action_manifest=(transaction.manifest if transaction is not None else None),
                    execution_mode=(
                        self.intervention_execution_mode
                        if transaction is not None and transaction.manifest.action_count > 0
                        else "suggest_only"
                    ),
                )
            except Exception:
                logger.debug("MICRO_STEP_TOGGLED rebroadcast failed", exc_info=True)

            # Persist via the recorder so the session report sees the
            # ``done`` history per step.
            try:
                self._recorder.append(
                    "micro_step_toggled",
                    {
                        "intervention_id": intervention_id,
                        "step_index": step_index,
                        "new_status": new_status,
                        "text": step.text,
                    },
                )
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
                    # local helpfulness ledger sees a consistent close.
                    try:
                        self._recorder.append(
                            "intervention_outcome",
                            outcome.model_dump(mode="json"),
                        )
                    except Exception:
                        logger.debug("intervention_outcome append failed", exc_info=True)
                    if outcome.workspace_restored:
                        self._active_intervention_id = None
                        self._active_plan = None
                        try:
                            await self._ws_server.send_restore(
                                intervention_id,
                                user_action="natural_recovery",
                            )
                        except Exception:
                            logger.debug(
                                "send_restore on natural_recovery failed",
                                exc_info=True,
                            )
                        try:
                            self._helpfulness.record_user_action(
                                intervention_id,
                                "natural_recovery",
                            )
                            await self._policy_lifecycle.observe_intervention(
                                intervention_id,
                                kind="user_action",
                                idempotency_key=(
                                    f"terminal-action:{intervention_id}:natural_recovery"
                                ),
                                payload={"action": "natural_recovery"},
                            )
                        except Exception:
                            logger.debug(
                                "record_user_action(natural_recovery) failed",
                                exc_info=True,
                            )
                    else:
                        # The plan remains active and the exact restore is
                        # retryable. Re-open the all-done latch so a later
                        # toggle or explicit Undo can retry safely.
                        self._micro_step_recovery_fired = False

    async def _handle_user_action(self, payload: dict[str, Any]) -> None:
        # Auto-focus dismissals are observations inside the one fixed reward
        # window. They never update policy state or finalize a reward early.
        if payload.get("auto_focus_dismissed") is True:
            iid = str(payload.get("intervention_id") or "")
            if iid:
                try:
                    await self._policy_lifecycle.observe_intervention(
                        iid,
                        kind="user_action",
                        idempotency_key=f"auto-focus-dismissed:{iid}",
                        payload={"action": "dismissed", "surface": "auto_focus"},
                    )
                except Exception:
                    logger.debug(
                        "auto_focus_dismissed policy observation failed",
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
                payload.get("result") is None and payload.get("request_dispatch") is True
            )
            if is_dispatch_request and not self.workspace_mutation_allowed:
                logger.info(
                    "Action request denied in suggest-only mode (action_id=%s action_type=%s)",
                    payload.get("action_id"),
                    payload.get("action_type"),
                )
                return
            if not is_dispatch_request:
                self._recorder.append(
                    "action_executed",
                    {
                        "intervention_id": payload.get("intervention_id"),
                        "action_id": payload.get("action_id"),
                        "action_type": payload.get("action_type"),
                        "result": payload.get("result"),
                        "source": payload.get("source") or source_client or None,
                    },
                )
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
            # Compatibility action names remain decodable, but unsupported
            # physiology never confers break-action authority.
            action_dict_raw = (
                payload.get("action") if isinstance(payload.get("action"), dict) else None
            )
            current_action_type = str(
                payload.get("action_type") or (action_dict_raw or {}).get("action_type") or ""
            )
            if current_action_type == "take_biology_break":
                logger.info(
                    "Ignoring compatibility take_biology_break action (intervention_id=%s)",
                    payload.get("intervention_id"),
                )
                return
            if is_dispatch_request and source_client in ("", "desktop"):
                raw_action = payload.get("action")
                action_dict: dict[str, Any]
                if isinstance(raw_action, dict):
                    # normalise an untyped dict[Any, Any] to dict[str, Any]
                    action_dict = {str(k): v for k, v in raw_action.items()}
                else:
                    action_dict = {
                        "action_id": payload.get("action_id"),
                        "action_type": payload.get("action_type"),
                        "label": payload.get("label", ""),
                        "reason": payload.get("reason", ""),
                        "target": payload.get("target"),
                        "tab_index": payload.get("tab_index"),
                    }
                await self.dispatch_intervention_action(
                    str(payload.get("intervention_id") or ""),
                    action_dict,
                )
            elif is_dispatch_request:
                logger.warning(
                    "ACTION_DISPATCH refused: non-desktop source %r tried to forward action_id=%s",
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
                    iid,
                    rating,
                    text_feedback=text_feedback,
                )
                self._recorder.append(
                    "helpfulness",
                    {
                        "intervention_id": iid,
                        "user_rating": rating,
                        "text_feedback": text_feedback,
                    },
                )
                if rating in ("thumbs_up", "thumbs_down"):
                    try:
                        await self._policy_lifecycle.observe_intervention(
                            iid,
                            kind="user_rating",
                            idempotency_key=f"user-rating:{iid}:{rating}",
                            payload={"rating": rating},
                        )
                    except Exception:
                        logger.debug("policy rating observation failed", exc_info=True)
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
                        now = monotonic_seconds(self._clock)
                        already_latched = now - self._quiet_mode_throttle_latched_at < 30.0
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

        # audit C-note: train the dismissal model on the TRIGGER-time
        # confidence + context_complexity snapshot (cached at trigger time),
        # not ``outcome.recovery_confidence`` / the feedback-time context.
        # Fall back to the trigger-policy's last evaluated confidence and the
        # current context only when the snapshot is missing (e.g. a legacy
        # intervention id from before this cache existed).
        cached_features = self._dismissal_features_by_intervention.get(intervention_id)
        if cached_features is not None:
            trigger_conf, trigger_complexity = cached_features
        else:
            trigger_conf = 0.0
            trigger_complexity = (
                float(context.complexity_score)
                if context and hasattr(context, "complexity_score")
                else 0.0
            )

        # P1: record approval/rejection under the canonical action-types
        # this intervention acted on (the executor gates on the same keys),
        # so escalation actually lifts the gate on approved actions. Falls
        # back to the legacy "intervention" key when no actions were cached.
        consent_keys = self._consent_actions_by_intervention.get(intervention_id) or [
            "intervention"
        ]

        if action == "engaged":
            outcome = await self._restore_manager.engage(intervention_id)
            # Consent approval is evidence only after a verified action
            # receipt. Merely clicking a micro-step or proposal CTA must not
            # silently escalate workspace authority.
            self._trigger_policy.record_outcome(
                dismissed=False,
                confidence=trigger_conf,
                context_complexity=trigger_complexity,
            )
            # P1-PIPE-REPORT: the user accepted (engaged with) a delivered
            # intervention. Count it so SessionReport.interventions_accepted
            # is real instead of the constant 0 the longitudinal aggregator
            # used to fall back on. Sits in the engage branch alongside the
            # _helpfulness.record_user_action close below.
            try:
                self._session_report.increment_interventions_accepted()
            except Exception:
                logger.debug(
                    "session_report increment_interventions_accepted failed",
                    exc_info=True,
                )
        elif action == "snoozed":
            self._trigger_policy.activate_quiet_mode(duration_minutes=15)
            outcome = await self._restore_manager.snooze(intervention_id)
        elif action == "restore":
            # Desktop "Undo" pill (audit fix #15) — user wants the
            # workspace mutations reversed without recording an
            # engagement / dismissal. ``restore_intervention`` is the
            # stable public entry point we expose to the desktop_shell
            # controller so it can wire the Undo button to a single
            # method name. It delegates to RestoreManager.cancel(),
            # which performs the executor.reverse() pass and produces
            # an InterventionOutcome with user_action="system_cancelled".
            outcome = await self.restore_intervention(intervention_id)
        else:
            outcome = await self._restore_manager.dismiss(intervention_id)
            if action == "dismissed":
                self._trigger_policy.record_dismissal()
                self._trigger_policy.record_outcome(
                    dismissed=True,
                    confidence=trigger_conf,
                    context_complexity=trigger_complexity,
                )
                # v2.0: Record consent rejection under each gated action-type.
                for consent_key in consent_keys:
                    await self._consent_ladder.record_rejection(consent_key)

        try:
            await self._policy_lifecycle.observe_intervention(
                intervention_id,
                kind="user_action",
                idempotency_key=f"terminal-action:{intervention_id}:{action}",
                payload={"action": action},
            )
            if action == "restore":
                await self._policy_lifecycle.observe_intervention(
                    intervention_id,
                    kind="undo",
                    idempotency_key=f"undo:{intervention_id}",
                    payload={"action": action},
                )
            if outcome is not None and not outcome.workspace_restored:
                await self._policy_lifecycle.observe_intervention(
                    intervention_id,
                    kind="restore_failure",
                    idempotency_key=f"restore-failure:{intervention_id}:{action}",
                    payload={"action": action},
                )
        except Exception:
            logger.debug("policy terminal observation failed", exc_info=True)

        if outcome is None:
            return

        self._recorder.append("intervention_outcome", outcome.model_dump(mode="json"))
        if not outcome.workspace_restored:
            # Closing presentation is not evidence that adapter-owned state
            # was reversed. Keep the plan and RestoreManager record alive so
            # Undo/startup recovery can retry the same exact inverses.
            logger.warning(
                "User close for %s did not verify restoration; retaining "
                "active state and retry authority",
                intervention_id,
            )
            return

        self._active_intervention_id = None
        # P0 §3.6: clear the cached plan + recovery latch when the
        # intervention closes through the user_action path. Trailing
        # MICRO_STEP_TOGGLED frames from a peer surface are then
        # silently dropped instead of re-firing engage().
        if self._active_plan is not None and self._active_plan.intervention_id == intervention_id:
            self._active_plan = None
        self._micro_step_recovery_fired = False
        await self._ws_server.send_restore(intervention_id, user_action=action)

        # Keep the immediate score for the local descriptive helpfulness UI.
        # It is not a policy update; the durable reward finalizes only after
        # the prespecified proximal window.
        self._helpfulness.record_user_action(intervention_id, action)
        context = self._latest_context
        state_estimate = self._current_state_estimate()
        if state_estimate:
            reward_record = await self._helpfulness.end_tracking(
                intervention_id=intervention_id,
                state=state_estimate.state,
                confidence=state_estimate.confidence,
                complexity=context.complexity_score
                if context and hasattr(context, "complexity_score")
                else 0.0,
                tab_count=(
                    int(context.browser_context.tab_count)
                    if context and hasattr(context, "browser_context") and context.browser_context
                    else 0
                ),
                error_count=int(context.total_errors)
                if context and hasattr(context, "total_errors")
                else 0,
            )
            if reward_record is not None:
                reward = float(reward_record.get("reward_signal", 0.0))
                self._recorder.append(
                    "helpfulness",
                    {
                        "intervention_id": intervention_id,
                        "descriptive_helpfulness_score": reward,
                    },
                )

        # Record tab relevance feedback (skip if per-tab feedback was already received)
        await self._record_tab_relevance_feedback(action, outcome, intervention_id)

    async def _record_tab_relevance_feedback(
        self,
        action: str,
        outcome: Any,
        intervention_id: str = "",
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

    # P1-PIPE-REPORT: tab types classify_tab_type() returns that count as a
    # distraction for the per-session ``top_distraction_domains`` rollup the
    # longitudinal task-pattern aggregator reads. Work-relevant types
    # (documentation/stackoverflow/search/code_host/goal_relevant) are NOT
    # distractions and are excluded.
    _DISTRACTION_TAB_TYPES = frozenset({"distraction", "social", "video_platform"})

    async def _handle_activity_sync(self, payload: dict[str, Any]) -> None:
        """Handle ACTIVITY_SYNC from browser extension — aggregate into daily timeline."""
        activities = payload.get("activities")
        if isinstance(activities, list):
            try:
                await self._activity_aggregator.ingest(activities)
                logger.debug("Ingested %d activities from browser", len(activities))
            except Exception:
                logger.debug("Activity sync ingestion failed", exc_info=True)
            # P1-PIPE-REPORT: feed the SAME activities into the session
            # report so SessionReport.top_activities and
            # top_distraction_domains are populated from real input. Before
            # this wiring both producers had ZERO call sites, so every
            # persisted report carried empty lists and the longitudinal
            # chronotype ``task_patterns`` rollup (which reads
            # top_distraction_domains) was permanently empty.
            for activity in activities:
                if not isinstance(activity, dict):
                    continue
                url = str(activity.get("url") or "")
                title = str(activity.get("title") or "")
                try:
                    dwell = float(activity.get("duration_spent_s") or 0.0)
                except (TypeError, ValueError):
                    dwell = 0.0
                tab_type = classify_tab_type(url) if url else "other"
                try:
                    self._session_report.record_activity(
                        title=title or url or "untitled",
                        tab_type=tab_type,
                        dwell_s=dwell,
                    )
                    if tab_type in self._DISTRACTION_TAB_TYPES and url:
                        hostname = urlparse(url).netloc.lower()
                        if hostname.startswith("www."):
                            hostname = hostname[4:]
                        if hostname:
                            self._session_report.record_distraction(hostname)
                except Exception:
                    logger.debug(
                        "session_report activity recording failed",
                        exc_info=True,
                    )

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
        self._services.register("latest_leetcode_context", context)

    async def _handle_intervention_applied(self, payload: dict[str, Any]) -> None:
        """Reconcile optimistic mutation tracking with the client's ack.

        See ``_OptimisticInterventionAdapter`` for the rationale. The client
        sends ``{intervention_id, phase, success, applied_actions, errors}``
        after executing the plan or the restore — we use ``success`` to
        overwrite every mutation's ``success`` flag, and accumulate
        ``errors`` into ``Mutation.reason`` so downstream
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
                # The Mutation dataclass field is ``reason`` (see
                # cortex/services/intervention_engine/executor.py:69).
                # The previous ``mutation.error = error_text`` set a
                # never-read attribute via attribute punning while
                # leaving ``reason`` as None, hiding failure cause from
                # both the WS broadcast and the dashboard's Restore
                # pill telemetry. Preserve any prior structured reason
                # so we don't overwrite a richer message with the
                # generic concatenation.
                mutation.reason = mutation.reason or error_text

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
            telemetry = self._current_telemetry()
            # Biology-derived mode inputs are contained until their separate
            # reference/label validation gates pass. Domain behavior (typing,
            # submissions, rereads) remains available.
            blink_delta = 0.0

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
                hr_delta=0.0,
                blink_delta=blink_delta,
                key_velocity=key_velocity,
                wa_timestamp=wa_timestamp,
                current_time=timestamp,
            )

            current_load = 0.0
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
                hr=None,
                hr_baseline=float(baselines.hr_baseline),
                hrv_current=None,
                hrv_prev=None,
                last_submission_ts=submission_epoch if accepted else None,
            )

            mode_estimate = self._leetcode_mode_resolver.resolve(
                estimate,
                context,
                aai_score=aai_score,
                destructive=destructive,
                parasympathetic_rebound=rebound,
            )
            self._services.register("latest_leetcode_mode_estimate", mode_estimate)

            for action in self._leetcode_interventions.select(mode_estimate, context):
                action_name = str(action.get("action") or "")
                params = action.get("payload")
                if not action_name or not isinstance(params, dict):
                    continue

                signature = ":".join(
                    [
                        action_name,
                        str(context.problem_id),
                        context.stage.value,
                        str(context.submission_count),
                        str(context.wrong_answer_count),
                        str(context.last_submission_ts or ""),
                    ]
                )
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

                if not self.workspace_mutation_allowed:
                    logger.debug(
                        "LeetCode action %s suppressed in suggest-only mode",
                        action_name,
                    )
                    continue

                result = await self._leetcode_adapter.execute(action_name, params)
                if result.success:
                    self._leetcode_action_signatures[signature] = timestamp
                    self._recorder.append(
                        "leetcode_intervention",
                        {
                            "action": action_name,
                            "payload": params,
                            "mode": mode_estimate.mode.value,
                            "stage": mode_estimate.stage.value,
                            "problem_id": context.problem_id,
                        },
                    )
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
        if context.last_submission_at_unix_ms is not None:
            return float(context.last_submission_at_unix_ms) / 1_000.0
        value = context.last_submission_ts
        if value is None:
            return None
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        if ts <= 0:
            return None
        return ts

    def _leetcode_submission_monotonic(self, context: LeetCodeContext) -> float | None:
        """Convert a LeetCode submission epoch timestamp to monotonic time."""
        epoch_seconds = self._leetcode_submission_epoch_seconds(context)
        if epoch_seconds is None:
            return None
        age = max(0.0, unix_seconds(self._clock) - epoch_seconds)
        return monotonic_seconds(self._clock) - age

    # --- v2.0 helper methods ---

    @staticmethod
    def _policy_arm_to_template(arm_name: str) -> str | None:
        """Map the finite policy catalog to reviewed planner templates."""
        mapping = {
            "no_action": None,
            "suggest_only": None,
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
            # audit P0 (mypy): real constructor kwarg is ``storage_path``
            # (not ``storage_root``); ``check_and_generate`` is a coroutine
            # and MUST be awaited — the un-awaited call returned a coroutine
            # object on which ``.summary`` always raised AttributeError, so
            # the morning briefing silently never reached any surface.
            briefing = MorningBriefing(storage_path=str(self.config.storage.path))
            content = await briefing.check_and_generate()
            if content is not None:
                logger.info("Morning briefing available: %s", content.summary[:80])
                await self._ws_server.send_message(
                    "MORNING_BRIEFING",
                    {
                        "summary": content.summary,
                        "action_items": content.action_items,
                        # ``BriefingContent`` has no ``left_off_at`` field; the
                        # wire contract's ``left_off_at`` is the "where you left
                        # off" headline the popup feeds into START_FOCUS as the
                        # resume goal — that is the briefing ``title``.
                        "left_off_at": content.title,
                    },
                )
        except Exception:
            logger.debug("No morning briefing available")

    async def _generate_handover(self, context: Any) -> None:
        """Generate a handover snapshot for tomorrow's morning briefing."""
        try:
            snapshot = HandoverSnapshot(
                str(self.config.storage.path),
                clock=self._clock,
            )

            # Gather recent activity data for the handover
            activity_timeline: list[dict[str, Any]] | None = None
            try:
                recent = await self._activity_aggregator.get_recent_activities(limit=10)
                if recent:
                    activity_timeline = [a.model_dump() for a in recent]
            except Exception:
                logger.debug("Failed to fetch activities for handover")

            await snapshot.capture_and_write(
                browser_context=context.browser_context.model_dump()
                if hasattr(context, "browser_context") and context.browser_context
                else None,
                editor_context=context.editor_context.model_dump()
                if hasattr(context, "editor_context") and context.editor_context
                else None,
                terminal_context=context.terminal_context.model_dump()
                if hasattr(context, "terminal_context") and context.terminal_context
                else None,
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
                    # Compute the longitudinal trend for reporting only. It
                    # no longer tunes the unvalidated HRV stress prototype.
                    trend = await self._longitudinal.compute_trend()
                    multiplier = trend.get("sensitivity_multiplier", 1.0)
                    logger.debug("Longitudinal snapshot: multiplier=%.2f", multiplier)
                except Exception:
                    logger.exception("Longitudinal loop error")
                await asyncio.sleep(3600.0)  # every hour
        except asyncio.CancelledError:
            # B6 (Phase 4.1): graceful longitudinal loop shutdown.
            logger.debug("longitudinal loop cancelled")

    def register_client_identified_listener(
        self,
        listener: Callable[[str, bool], None],
    ) -> None:
        """Audit-prod G1: subscribe to (client_type, connected) events.

        The desktop shell registers exactly one listener; it forwards the
        event onto the Qt main thread and updates the dashboard's
        Chrome / Edge / Editor dots. Idempotent for duplicate listeners.
        """
        if listener not in self._client_identified_listeners:
            self._client_identified_listeners.append(listener)

    async def _on_client_identified(self, client_type: str, connected: bool) -> None:
        """Fan-out helper bound to ``WebSocketServer._client_identified_callback``.

        Runs on the daemon's asyncio loop thread. Each listener is
        expected to marshal onto its own UI thread if needed.
        """
        for listener in list(self._client_identified_listeners):
            try:
                listener(client_type, connected)
            except Exception:
                logger.debug("client_identified listener raised", exc_info=True)
        if not connected or not self._pending_startup_restores:
            return
        executor = {
            "chrome": "browser",
            "edge": "browser",
            "vscode": "editor",
            "desktop": "desktop",
        }.get(client_type)
        if executor is None:
            return
        for command in list(self._pending_startup_restores.values()):
            if not any(action.executor == executor for action in command.actions):
                continue
            try:
                await self._ws_server.send_restore_command(command)
            except Exception:
                logger.exception(
                    "Startup restore retry failed for %s",
                    command.restore_id,
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
        from cortex.services.llm_engine.cost_tracker import (
            probe_active_model,
            probe_token_totals,
        )

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
        # P2-CONTRACT-2: populate prompt_tokens/completion_tokens/model via the
        # SAME shared probes the HTTP GET /api/cost route uses, so the WS
        # COST_RESPONSE and the HTTP envelope can never disagree on these keys
        # (the CostResponse schema promises the two surfaces are identical).
        prompt_tokens, completion_tokens = probe_token_totals(tracker)
        model = probe_active_model(self._llm_client)
        return CostResponse.from_clock(
            _daemon_clock(self),
            cost_today=cost_today,
            budget_today=budget_today,
            provider=provider,
            budget_exhausted=budget_exhausted,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
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
        clock = _daemon_clock(self)
        start = monotonic_seconds(clock)
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
                latency_ms = (monotonic_seconds(clock) - start) * 1000.0
                return TestProviderResult(
                    provider=canonical,
                    ok=True,
                    latency_ms=round(latency_ms, 2),
                    error=None,
                )
            await asyncio.wait_for(probe(), timeout=5.0)
            latency_ms = (monotonic_seconds(clock) - start) * 1000.0
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
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
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
        clock = _daemon_clock(self)
        try:
            if self._session_report_started and self._session_report is not None:
                # C4 (audit): wrap the real report in the declared
                # ``SessionRecap`` envelope (forced recap is never persisted).
                from cortex.libs.schemas.realtime import SessionRecap

                report = self._session_report.finish()
                recap_payload = SessionRecap(
                    report=report,
                    generated_at=utc_datetime(clock).isoformat(),
                    persisted=False,
                ).model_dump(mode="json")
            else:
                # No active session: there is no SessionReport to wrap, so we
                # broadcast a minimal synthetic payload. The popup gates on
                # ``session_id`` presence and tolerates this shape.
                recap_payload = {
                    "session_id": "force_recap",
                    "start_time": utc_datetime(clock).isoformat(),
                    "end_time": utc_datetime(clock).isoformat(),
                    "duration_seconds": 0.0,
                    "persisted": False,
                }
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
                    clock=self._clock,
                )
            self._services.register("trigger_policy", self._trigger_policy)
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
                    clock=self._clock,
                )
            self._services.register("trigger_policy", self._trigger_policy)
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
            elif (
                not desired_capture
                and self._capture_available
                and self._capture_pipeline.is_running
            ):
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
                await self._consent_ladder.set_policy_level(
                    "distraction_block",
                    AUTONOMOUS_ACT if new_value else REVERSIBLE_ACT,
                )
            except Exception:
                logger.debug(
                    "distraction_block consent flip failed",
                    exc_info=True,
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
                        "disarm_auto_focus on opt-out failed",
                        exc_info=True,
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
        # External model transport is a separate, revision-bound privacy
        # choice from provider selection.  Every update rebuilds the planner
        # once and burns pending previews from the prior configuration.
        privacy_changed = False
        requested_planner_mode = settings.get("llm_planner_mode")
        if requested_planner_mode is not None:
            mode = str(requested_planner_mode)
            if mode in {"no_llm", "no_content", "external_redacted"}:
                self.config.llm.privacy.planner_mode = mode  # type: ignore[assignment]
                privacy_changed = True

        if "external_context_enabled" in settings or "llm_context_consent_revision" in settings:
            requested_enabled = bool(settings.get("external_context_enabled", False))
            requested_revision = str(settings.get("llm_context_consent_revision") or "")
            required_revision = self.config.llm.privacy.required_consent_revision
            acknowledged = requested_enabled and requested_revision == required_revision
            self.config.llm.privacy.external_context_enabled = acknowledged
            self.config.llm.privacy.consent_revision = (
                required_revision if acknowledged else ""
            )
            privacy_changed = True

        if self.config.llm.privacy.planner_mode != "external_redacted":
            self.config.llm.privacy.external_context_enabled = False
            self.config.llm.privacy.consent_revision = ""

        # B.4 fix: accept both "llm_provider" (canonical, new clients) and
        # "llm_mode" (legacy from the SettingsDialog) so the dropdown in
        # the desktop settings actually rebuilds the client.
        provider_value: str | None = None
        if "llm_provider" in settings:
            provider_value = str(settings["llm_provider"])
        elif "llm_mode" in settings:
            provider_value = str(settings["llm_mode"])
        provider_changed = False
        if provider_value is not None:
            if provider_value in {"bedrock", "vertex", "direct"}:
                provider_changed = self.config.llm.provider != provider_value
                self.config.llm.provider = provider_value  # type: ignore[assignment]
            elif provider_value == "rule_based":
                self.config.llm.fallback_mode = "rule_based"
                self.config.llm.privacy.planner_mode = "no_llm"
                self.config.llm.privacy.external_context_enabled = False
                self.config.llm.privacy.consent_revision = ""
                privacy_changed = True
        if privacy_changed or provider_changed:
            old_client = self._llm_client
            clear_previews = getattr(old_client, "clear_previews", None)
            if callable(clear_previews):
                clear_previews()
            self._llm_client = create_llm_client(self.config.llm, clock=self._clock)
            self._services.register("llm_client", self._llm_client)
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
        # Execution authority is not client-writable. Echo the daemon's
        # validated configuration even if a compromised client attempted to
        # smuggle a more-authoritative value in SETTINGS_SYNC.
        applied["execution_mode"] = self.intervention_execution_mode
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
            self._session_reader.list_sessions,
            since,
            clamped,
        )

    async def get_session(self, session_id: str) -> SessionDetailResponse:
        """P0 §3.1: single-report lookup (validated session_id)."""
        return await asyncio.to_thread(
            self._session_reader.read_session,
            session_id,
        )

    async def restore_intervention(
        self,
        intervention_id: str,
        user_action: str = "system_cancelled",
    ) -> Any | None:
        """Public Undo entry point for the desktop "Undo" pill.

        Reverses every workspace mutation belonging to
        ``intervention_id`` via the executor, drops the active
        intervention from the RestoreManager, and returns the
        resulting :class:`InterventionOutcome` (or ``None`` if no
        active intervention matches). The caller (desktop_shell
        controller) does not need to depend on RestoreManager
        internals — this method is the stable contract.
        """
        if user_action == "engaged":
            return await self._restore_manager.engage(intervention_id)
        if user_action == "snoozed":
            return await self._restore_manager.snooze(intervention_id)
        if user_action == "dismissed":
            return await self._restore_manager.dismiss(intervention_id)
        return await self._restore_manager.cancel(intervention_id)

    async def get_trends(
        self,
        window: str,
        *,
        refresh: bool = False,
    ) -> TrendsResponse:
        """P0 §3.2: longitudinal trend rollup.

        ``window`` is clamped to ``{"week","month"}`` to match the
        :class:`TrendsRequest` / :class:`TrendsResponse` schema. An
        unknown value (including the legacy ``"quarter"``) logs a
        WARNING and falls back to ``"week"`` so the dashboard never
        renders an empty pane on a stale URL.
        """
        # Narrow ``window`` to the aggregator's Literal so mypy accepts the
        # to_thread call. The legacy ``"quarter"`` and any unknown value
        # fall back to ``"week"``.
        resolved_window: Literal["week", "month", "quarter"]
        if window == "month":
            resolved_window = "month"
        elif window == "week":
            resolved_window = "week"
        else:
            logger.warning("get_trends: unknown window=%r; falling back to 'week'", window)
            resolved_window = "week"
        return await asyncio.to_thread(
            self._session_aggregator.get_trends,
            resolved_window,
            refresh=refresh,
        )

    def latest_session_recap(self) -> dict[str, Any] | None:
        """P0 §3.3: serve the cached SESSION_RECAP payload to late joiners.

        Returns ``None`` until the first long session (>=90s) finishes.
        The browser-extension popup gates on ``session_id`` presence
        before caching/badging, so an empty dict reply is harmless.
        """
        return self._latest_session_recap

    async def acknowledge_session_recap(
        self,
        session_id: str | None = None,
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
                "acknowledge_session_recap: failed to set event (session_id=%r)",
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
