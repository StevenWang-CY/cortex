"""
Runtime daemon for Cortex.

This module wires the sensing, state, context, LLM, intervention, REST, and
WebSocket layers into one in-process supervisor so the app can run as a real
product instead of a collection of disconnected test surfaces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn

from cortex.libs.config.settings import CortexConfig, get_config
from cortex.libs.schemas.features import KinematicFeatures, PhysioFeatures
from cortex.libs.schemas.intervention import InterventionPlan
from cortex.libs.schemas.state import UserBaselines
from cortex.services.api_gateway.app import create_app, registry
from cortex.services.api_gateway.websocket_server import WebSocketServer
from cortex.services.capture_service.pipeline import CapturePipeline, PipelineOutput
from cortex.services.context_engine import BrowserAdapter, ContextAssembler, EditorAdapter, TerminalAdapter
from cortex.services.intervention_engine.executor import InterventionExecutor
from cortex.services.intervention_engine.planner import prepare_plan
from cortex.services.intervention_engine.restore import RestoreManager
from cortex.services.intervention_engine.snapshot import capture_snapshot
from cortex.services.kinematics_engine.blink_detector import BlinkDetector
from cortex.services.kinematics_engine.head_pose import HeadPoseEstimator
from cortex.services.kinematics_engine.posture import PostureAnalyzer
from cortex.services.llm_engine import create_llm_client
from cortex.services.physio_engine.pulse_estimator import PulseEstimator
from cortex.services.physio_engine.roi_extractor import RoiExtractor
from cortex.services.physio_engine.rppg import extract_bvp
from cortex.services.state_engine import FeatureFusion, RuleScorer, ScoreSmoother
from cortex.services.state_engine.trigger_policy import TriggerPolicy
from cortex.services.telemetry_engine.feature_aggregator import FeatureAggregator
from cortex.services.telemetry_engine.input_hooks import InputHooks
from cortex.services.telemetry_engine.window_tracker import WindowTracker

# v2.0 imports
from cortex.libs.store import RedisStore, InMemoryStore
from cortex.services.consent.ladder import ConsentLadder
from cortex.services.consent.policy import ConsentPolicy
from cortex.services.eval.bandit import ContextualBandit
from cortex.services.eval.helpfulness import HelpfulnessTracker
from cortex.services.handover.briefing import MorningBriefing
from cortex.services.handover.detector import ShutdownDetector
from cortex.services.handover.snapshot import HandoverSnapshot
from cortex.services.state_engine.longitudinal import LongitudinalTracker
from cortex.services.state_engine.rabbit_hole import RabbitHoleDetector
from cortex.services.state_engine.stress_integral import StressIntegralTracker
from cortex.services.state_engine.zombie_detector import ZombieReadingDetector
from cortex.services.throttle.copilot_throttle import CopilotThrottle

logger = logging.getLogger(__name__)


class _PassiveWorkspaceAdapter:
    """Non-destructive adapter used for mutation tracking and API compatibility."""

    async def execute(self, action: str, params: dict[str, Any]) -> bool:
        return True


class SessionRecorder:
    """Append-only JSONL recorder for states, plans, and outcomes."""

    def __init__(self, storage_root: str) -> None:
        root = Path(storage_root)
        root.mkdir(parents=True, exist_ok=True)
        session_dir = root / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        self._path = session_dir / f"session_{int(time.time())}.jsonl"

    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "type": event_type,
            "timestamp": time.time(),
            "payload": payload,
        }
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


class CortexDaemon:
    """In-process supervisor for the full Cortex runtime."""

    def __init__(self, config: CortexConfig | None = None) -> None:
        self.config = config or get_config()
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._uvicorn_server: uvicorn.Server | None = None

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
        self._pulse_estimator = PulseEstimator(fs=float(self.config.capture.fps))
        self._blink_detector = BlinkDetector(
            blink_config=self.config.signal.blink,
            landmarks_config=self.config.landmarks,
        )
        self._head_pose = HeadPoseEstimator(
            frame_width=self.config.capture.width,
            frame_height=self.config.capture.height,
        )
        self._posture = PostureAnalyzer(self.config.signal.posture)
        self._feature_fusion = FeatureFusion()
        self._scorer = RuleScorer(config=self.config.state, baselines=self._load_baselines())
        self._smoother = ScoreSmoother(self.config.state)
        self._trigger_policy = TriggerPolicy(self.config.intervention)
        self._llm_client = create_llm_client(self.config.llm)
        self._executor = InterventionExecutor()
        self._restore_manager = RestoreManager(
            self._executor,
            timeout_seconds=float(self.config.intervention.timeout_minutes * 60),
        )
        for adapter_name in ("browser", "editor", "overlay", "terminal"):
            self._executor.register_adapter(adapter_name, _PassiveWorkspaceAdapter())

        self._ws_server = WebSocketServer(self.config.api)
        self._ws_server.set_user_action_callback(self._handle_user_action)
        self._ws_server.set_settings_callback(self.apply_settings)

        self._rgb_history: deque[np.ndarray] = deque(
            maxlen=max(1, self.config.signal.rppg.window_seconds * self.config.capture.fps)
        )
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
            head_pitch=None,
            head_yaw=None,
            head_roll=None,
            slump_score=None,
            forward_lean_score=None,
            shoulder_drop_ratio=None,
            confidence=0.0,
        )
        self._last_physio_update = 0.0
        self._active_intervention_id: str | None = None
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

        # --- v2.0 services ---
        # Store (Redis with in-memory fallback)
        self._store: RedisStore | InMemoryStore
        if self.config.redis.enabled:
            try:
                self._store = RedisStore(
                    host=self.config.redis.host,
                    port=self.config.redis.port,
                    db=self.config.redis.db,
                    key_prefix=self.config.redis.key_prefix,
                )
            except Exception:
                logger.warning("Redis unavailable, falling back to in-memory store")
                self._store = InMemoryStore()
        else:
            self._store = InMemoryStore()

        # Stress integral tracker (biological pomodoros)
        self._stress_tracker = StressIntegralTracker(
            hrv_baseline=self._load_baselines().hrv_baseline,
        )

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

        # Helpfulness tracker
        self._helpfulness = HelpfulnessTracker(store=self._store)

        # Contextual bandit
        self._bandit = ContextualBandit(store=self._store)

        # Copilot throttle
        self._copilot_throttle = CopilotThrottle(ws_server=self._ws_server)

        # Track previous state for copilot throttle transitions
        self._prev_state: str = "FLOW"

    async def start(self) -> None:
        """Start the runtime and block until shutdown."""
        self._register_services()
        self._input_hooks.start()
        self._window_tracker.start()
        try:
            await self._capture_pipeline.start()
            self._capture_available = True
        except Exception:
            logger.exception("Capture pipeline failed to start; continuing in telemetry-first mode")
            self._capture_available = False
        await self._ws_server.start()
        self._start_api_server()

        self._tasks = [
            asyncio.create_task(self._capture_loop(), name="cortex-capture-loop"),
            asyncio.create_task(self._telemetry_loop(), name="cortex-telemetry-loop"),
            asyncio.create_task(self._state_loop(), name="cortex-state-loop"),
            asyncio.create_task(self._context_loop(), name="cortex-context-loop"),
            asyncio.create_task(self._longitudinal_loop(), name="cortex-longitudinal-loop"),
        ]

        # v2.0: Check for morning briefing on startup
        await self._check_morning_briefing()

        logger.info("Cortex daemon started (v2.0)")
        await self._shutdown.wait()

    async def stop(self) -> None:
        """Gracefully stop all runtime services."""
        self._shutdown.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._capture_available and self._capture_pipeline.is_running:
            await self._capture_pipeline.stop()
        self._input_hooks.stop()
        self._window_tracker.stop()
        await self._ws_server.stop()
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
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
            "copilot_throttle": self._copilot_throttle,
        }.items():
            registry.register(name, service)
        registry.healthy = True

    def _start_api_server(self) -> None:
        app = create_app(config=self.config.api, cortex_config=self.config)
        config = uvicorn.Config(
            app,
            host=self.config.api.host,
            port=self.config.api.port,
            log_level="info",
            access_log=False,
            loop="asyncio",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._tasks.append(asyncio.create_task(self._uvicorn_server.serve(), name="cortex-api"))

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
            return UserBaselines.model_validate_json(baseline_path.read_text())
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
                output = await self._capture_pipeline.get_output(timeout=0.5)
                if output is not None:
                    await self._process_capture_output(output)
        except asyncio.CancelledError:
            pass

    async def _process_capture_output(self, output: PipelineOutput) -> None:
        registry.register("latest_frame_meta", output.frame_meta)
        if output.landmarks_px is None:
            return

        roi_frame = self._roi_extractor.extract(output.frame, output.landmarks_px, output.frame_meta.timestamp)
        combined_rgb = roi_frame.combined_rgb()
        if combined_rgb is not None:
            self._rgb_history.append(combined_rgb)

        stride_seconds = self.config.signal.rppg.stride_seconds
        if len(self._rgb_history) >= self._rgb_history.maxlen and (
            output.frame_meta.timestamp - self._last_physio_update
        ) >= stride_seconds:
            rgb_window = np.array(self._rgb_history, dtype=np.float64)
            bvp = extract_bvp(rgb_window, fs=float(self.config.capture.fps))
            self._pulse_estimator.process_window(bvp, timestamp=output.frame_meta.timestamp)
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
            head_pitch=pose.pitch,
            head_yaw=pose.yaw,
            head_roll=pose.roll,
            slump_score=posture.slump_score,
            forward_lean_score=posture.forward_lean_score,
            shoulder_drop_ratio=posture.shoulder_drop_ratio,
            confidence=output.frame_meta.face_confidence,
        )
        registry.register("latest_kinematics", self._latest_kinematics)
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
            pass

    async def _context_loop(self) -> None:
        """Build context every 5s — separate from fast state loop to avoid blocking."""
        try:
            while True:
                try:
                    context = await self._context_engine.build_context()
                    self._latest_context = context
                    registry.register("latest_task_context", context)
                    self._terminal_adapter.set_running_command(
                        context.terminal_context.running_command if context.terminal_context else None
                    )
                except Exception:
                    logger.exception("Context loop error")
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass

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
                    estimate = self._smoother.update(scores, quality, timestamp=timestamp)

                    # v2.0: Update stress integral
                    if vector.hrv_rmssd is not None:
                        self._stress_tracker.update(vector.hrv_rmssd, timestamp)
                        estimate.stress_integral = self._stress_tracker.current_load

                    # v2.0: Feed longitudinal tracker per-sample data
                    self._longitudinal.accumulate(
                        hr=vector.hr,
                        hrv=vector.hrv_rmssd,
                        resp=vector.respiration_rate,
                        state=estimate.state,
                    )

                    registry.register("latest_state_estimate", estimate)
                    self._recorder.append("state_estimate", estimate.model_dump(mode="json"))
                    biometrics = {
                        "heart_rate": vector.hr,
                        "hrv_rmssd": vector.hrv_rmssd,
                        "hr_delta": vector.hr_delta,
                        "blink_rate": vector.blink_rate,
                        "forward_lean": vector.forward_lean_angle,
                        "respiration_rate": vector.respiration_rate,
                        "thrashing_score": vector.thrashing_score,
                        "stress_integral": self._stress_tracker.current_load,
                    }
                    await self._ws_server.broadcast_state(estimate, biometrics)

                    # v2.0: Copilot throttle on state transitions
                    if estimate.state != self._prev_state:
                        await self._copilot_throttle.on_state_change(
                            estimate.state, estimate.confidence,
                        )
                        self._prev_state = estimate.state

                    context = self._latest_context
                    if context is not None:
                        decision = self._trigger_policy.evaluate(
                            estimate,
                            context_complexity=context.complexity_score,
                            current_time=timestamp,
                        )
                        registry.register("latest_trigger_decision", decision)
                        await self._handle_restore_updates(estimate, timestamp)

                        # v2.0: Check zombie reading
                        active_app = self._current_app_name()
                        telemetry = registry.get("latest_telemetry")
                        kinematics = self._latest_kinematics
                        if self._zombie_detector.update(
                            state=estimate.state,
                            active_app=active_app,
                            mouse_velocity=telemetry.mouse_velocity_mean if telemetry else 0.0,
                            blink_rate=kinematics.blink_rate,
                            blink_baseline=self._scorer.baselines.blink_rate_baseline,
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

                        # v2.0: Check stress integral break
                        if self._stress_tracker.should_break():
                            logger.info("Stress integral threshold — biological break")
                            await self._trigger_special_intervention(
                                context, estimate, template_name="breathing_overlay",
                                ws_type="BREATHING_OVERLAY",
                            )
                            self._stress_tracker.reset()

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
                            # v2.0: Consult bandit for intervention type
                            bandit_features = np.array(
                                self._build_bandit_features(estimate, context),
                                dtype=np.float64,
                            )
                            selected_arm = self._bandit.select_arm(bandit_features)
                            template_name = self._arm_to_template(selected_arm)

                            await self._trigger_intervention(
                                context, estimate, template_name=template_name,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("State loop iteration error")

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _trigger_intervention(
        self, context: Any, estimate: Any, *, template_name: str | None = None,
    ) -> None:
        plan = await self._llm_client.generate_intervention_plan(
            context, estimate, template_name=template_name,
        )
        validation, commands = prepare_plan(plan)
        if not validation.is_valid:
            logger.warning("Rejected intervention plan %s: %s", plan.intervention_id, validation.errors)
            return

        # v2.0: Check consent ladder
        consent_level_map = {
            "observe": 0, "suggest": 1, "preview": 2,
            "reversible_act": 3, "autonomous_act": 4,
        }
        requested_level = consent_level_map.get(plan.consent_level, 2)
        consent = await self._consent_ladder.check(
            action_type=plan.level, requested_level=requested_level,
        )
        if not consent.allowed:
            logger.info("Consent ladder blocked intervention %s (level=%s)", plan.intervention_id, plan.consent_level)
            return

        snapshot = capture_snapshot(context, intervention_id=plan.intervention_id)
        await self._executor.apply(plan, commands)
        self._restore_manager.start_intervention(plan.intervention_id, snapshot)
        self._trigger_policy.record_intervention()
        self._active_intervention_id = plan.intervention_id
        registry.register(f"workspace_snapshot:{plan.intervention_id}", snapshot)
        self._recorder.append("intervention_plan", plan.model_dump(mode="json"))
        await self._ws_server.send_intervention(plan)

        # v2.0: Start helpfulness tracking
        self._helpfulness.start_tracking(
            intervention_id=plan.intervention_id,
            intervention_type=plan.level,
            state=estimate.state,
            confidence=estimate.confidence,
        )

    async def _trigger_special_intervention(
        self,
        context: Any,
        estimate: Any,
        *,
        template_name: str,
        ws_type: str = "INTERVENTION_TRIGGER",
    ) -> None:
        """Trigger a special v2.0 intervention (breathing, active recall, rabbit hole)."""
        if self._active_intervention_id is not None:
            return  # Don't stack interventions

        try:
            plan = await self._llm_client.generate_intervention_plan(
                context, estimate, template_name=template_name,
            )
            self._active_intervention_id = plan.intervention_id
            self._recorder.append("intervention_plan", plan.model_dump(mode="json"))
            await self._ws_server.send_message(ws_type, plan.model_dump(mode="json"))
        except Exception:
            logger.exception("Failed to trigger special intervention (%s)", template_name)

    async def _handle_restore_updates(self, estimate: Any, timestamp: float) -> None:
        outcomes = await self._restore_manager.update(estimate, current_time=timestamp)
        for outcome in outcomes:
            self._active_intervention_id = None
            self._recorder.append("intervention_outcome", outcome.model_dump(mode="json"))
            await self._ws_server.send_restore(
                outcome.intervention_id,
                user_action=outcome.user_action,
            )

    async def _handle_user_action(self, payload: dict[str, Any]) -> None:
        # Log suggested action executions from the Chrome extension
        if payload.get("action_id") and payload.get("action_type"):
            self._recorder.append("action_executed", {
                "intervention_id": payload.get("intervention_id"),
                "action_id": payload.get("action_id"),
                "action_type": payload.get("action_type"),
                "result": payload.get("result"),
            })
            return

        # v2.0: Handle user ratings
        if payload.get("type") == "USER_RATING":
            iid = str(payload.get("intervention_id", ""))
            rating = str(payload.get("rating", ""))
            if iid and rating:
                self._helpfulness.record_rating(iid, rating)
                self._recorder.append("helpfulness", {
                    "intervention_id": iid,
                    "user_rating": rating,
                })
            return

        intervention_id = str(payload.get("intervention_id", ""))
        action = str(payload.get("action", "dismissed"))
        if not intervention_id:
            return

        if action == "engaged":
            outcome = await self._restore_manager.engage(intervention_id)
            # v2.0: Record consent approval (using intervention level as action_type)
            await self._consent_ladder.record_approval("intervention")
        elif action == "snoozed":
            self._trigger_policy.activate_quiet_mode(duration_minutes=15)
            outcome = await self._restore_manager.snooze(intervention_id)
        else:
            outcome = await self._restore_manager.dismiss(intervention_id)
            if action == "dismissed":
                self._trigger_policy.record_dismissal()
                # v2.0: Record consent rejection
                await self._consent_ladder.record_rejection("intervention")

        if outcome is None:
            return

        self._active_intervention_id = None
        self._recorder.append("intervention_outcome", outcome.model_dump(mode="json"))
        await self._ws_server.send_restore(intervention_id, user_action=action)

        # v2.0: End helpfulness tracking and update bandit
        context = self._latest_context
        state_estimate = registry.get("latest_state_estimate")
        if state_estimate:
            reward = await self._helpfulness.end_tracking(
                intervention_id=intervention_id,
                state=state_estimate.state,
                confidence=state_estimate.confidence,
                complexity=context.complexity_score if context and hasattr(context, 'complexity_score') else 0.0,
            )
            if reward is not None:
                self._recorder.append("helpfulness", {
                    "intervention_id": intervention_id,
                    "reward_signal": reward,
                })
                # Update bandit with reward
                if context:
                    features = self._build_bandit_features(state_estimate, context)
                    bandit_features = np.array(features, dtype=np.float64)
                    # Find arm index from template — use 0 as default
                    self._bandit.update(bandit_features, 0, reward)

    # --- v2.0 helper methods ---

    def _build_bandit_features(self, estimate: Any, context: Any) -> list[float]:
        """Build 8-dimensional feature vector for the contextual bandit."""
        state_map = {"FLOW": 0.0, "HYPO": 0.25, "RECOVERY": 0.5, "HYPER": 1.0}
        import datetime as dt
        hour = dt.datetime.now().hour
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
            snapshot = HandoverSnapshot(storage_root=self.config.storage.path)
            snapshot.capture(
                context=context,
                window_tracker=self._window_tracker,
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
            pass

    async def apply_settings(self, settings: dict[str, Any]) -> None:
        """Apply user-facing settings live when possible."""
        if "entry_threshold" in settings:
            threshold = float(settings["entry_threshold"])
            self.config.state.entry_threshold = threshold
            self.config.intervention.overlay_threshold = threshold
            self._trigger_policy = TriggerPolicy(self.config.intervention)
            registry.register("trigger_policy", self._trigger_policy)
        if "cooldown_seconds" in settings:
            self.config.intervention.cooldown_seconds = int(settings["cooldown_seconds"])
            self._trigger_policy = TriggerPolicy(self.config.intervention)
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
        if "quiet_mode" in settings:
            if bool(settings["quiet_mode"]):
                self._trigger_policy.activate_quiet_mode(
                    duration_minutes=int(settings.get("quiet_duration_minutes", 15))
                )
            else:
                self._trigger_policy.clear_quiet_mode()
        if "llm_mode" in settings:
            mode = str(settings["llm_mode"])
            self.config.llm.mode = "rule_based" if mode == "rule_based" else mode
            self._llm_client = create_llm_client(self.config.llm)
            registry.register("llm_client", self._llm_client)
        await self._ws_server.broadcast_settings(settings)
