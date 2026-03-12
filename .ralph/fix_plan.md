# Cortex — Implementation Plan

> Generated from `.ralph/specs/cortex.md`. Phases ordered by dependency.

## Phase 1: Foundation — Project Scaffold, Schemas, Config

- [x] Create full repository directory structure (cortex/, apps/, services/, libs/, storage/, scripts/, tests/, docs/) with __init__.py files
- [x] Create pyproject.toml with all Python dependencies (fastapi, uvicorn, pydantic, opencv-python, mediapipe, scipy, numpy, pynput, PySide6, websockets, structlog, httpx, onnxruntime, pyobjc)
- [x] Create .env.example with SSH config, ports, model name, and all configurable settings
- [x] Implement libs/config/settings.py — global CortexConfig with LLMConfig, CaptureConfig, StateConfig, InterventionConfig using Pydantic BaseSettings + YAML loading
- [x] Create libs/config/defaults.yaml with all default values (thresholds, weights, timeouts, FPS, ROI landmarks)
- [x] Implement libs/schemas/features.py — FrameMeta, PhysioFeatures, KinematicFeatures, TelemetryFeatures, FeatureVector (all Pydantic models per spec)
- [x] Implement libs/schemas/state.py — StateEstimate, UserBaselines, StateTransition models
- [x] Implement libs/schemas/context.py — TaskContext, EditorContext, TerminalContext, BrowserContext models
- [x] Implement libs/schemas/intervention.py — InterventionPlan, SimplificationConstraints, WorkspaceSnapshot, InterventionOutcome models
- [x] Implement libs/logging/structured.py — structlog JSON logger setup with event classes (state_transition, intervention_triggered, feature_vector, error)
- [x] Implement libs/utils/platform.py — OS detection, platform-specific path resolution, permission checks
- [x] Implement libs/utils/async_helpers.py — async queue wrapper, timeout helpers, graceful shutdown utilities
- [x] Create tests/fixtures/sample_features.json with pre-computed feature vectors for FLOW, HYPO, HYPER, RECOVERY states
- [x] Create tests/fixtures/sample_context.json with example editor, terminal, browser context objects
- [x] Create tests/fixtures/sample_llm_response.json with valid + malformed LLM response examples
- [x] Set up pytest configuration in pyproject.toml with coverage settings, test paths, markers

## Phase 2: Signal Processing Library

- [x] Implement libs/signal/filters.py — Butterworth bandpass filter (4th-order, 0.7–3.5 Hz), configurable order and cutoffs, scipy.signal butter/sosfilt
- [x] Implement libs/signal/peak_detection.py — BVP peak detection for IBI series, Welch PSD with 0.1 Hz resolution, dominant frequency extraction
- [x] Implement libs/signal/windowing.py — sliding window manager (10s window, 1s stride), overlap-add windowing, circular buffer for real-time streaming
- [x] Write tests/unit/test_filters.py — test bandpass on known sinusoids, verify attenuation outside passband, edge cases (DC, Nyquist)
- [x] Write tests/unit/test_pulse_window.py — test POS algorithm on synthetic PPG signal (known frequency), verify HR estimation within ±2 BPM

## Phase 3: Capture Service — Webcam + Face Tracking

- [x] Implement services/capture_service/webcam.py — threaded OpenCV VideoCapture, stable FPS targeting, frame timestamping (monotonic clock), async queue publishing
- [x] Implement services/capture_service/face_tracker.py — MediaPipe FaceLandmarker Tasks API, face bounding box + confidence, landmark normalization, face lost/reacquire hysteresis (5-frame tolerance)
- [x] Implement services/capture_service/quality.py — frame quality scoring: brightness (mean pixel intensity, flag < 50 lux), blur (Laplacian variance), motion (inter-frame landmark jitter, discard > 5px at nose tip), composite quality gate
- [x] Integrate capture pipeline: webcam → face_tracker → quality gate → publish FrameMeta + landmarks to internal async queue
- [x] Implement adaptive frame skip when processing falls behind (drop oldest frames, maintain real-time)
- [x] Write test for capture_service with mock webcam (pre-recorded frames), verify FPS stability and quality gating

## Phase 4: Physio Engine — rPPG Heart Rate & HRV

- [x] Implement services/physio_engine/roi_extractor.py — extract RGB traces from forehead ROI (landmarks 10,67,69,104,108,151,299,337,338), left cheek (50,101,116-121), right cheek (mirrored), spatial averaging per ROI, dynamic ROI selection (highest SNR)
- [x] Implement services/physio_engine/rppg.py — POS algorithm: temporal normalization (divide by running mean), chrominance projection (S1/S2 axes), adaptive ratio combination, overlap-add windowing for continuous BVP signal
- [x] Implement CHROM algorithm as fallback in rppg.py — chrominance-based method for better cross-skin-tone performance
- [x] Implement green-channel baseline method in rppg.py — simplest reference implementation
- [x] Implement services/physio_engine/pulse_estimator.py — consume 10s BVP windows, apply Butterworth bandpass (0.7-3.5 Hz), Welch PSD peak detection → instantaneous HR (BPM), IBI series extraction → RMSSD computation, HR delta (5s gradient)
- [x] Implement services/physio_engine/quality_scorer.py — signal quality from SNR (peak power / noise floor ratio), confidence scoring, quality-based algorithm switching (POS → CHROM → green-channel)
- [x] Write tests/unit/test_roi_extraction.py — verify correct landmark selection, spatial averaging, ROI size validation
- [x] Write integration test: synthetic frames with known PPG modulation → verify extracted HR within ±5 BPM

## Phase 5: Kinematics Engine — Blink, Head Pose, Posture

- [x] Implement services/kinematics_engine/blink_detector.py — Eye Aspect Ratio (EAR) from FaceMesh landmarks, blink detection (EAR < 0.21 for ≥ 3 frames, recovery > 0.25), blink rate (rolling 60s window), blink suppression score, blink rate delta from baseline
- [x] Implement services/kinematics_engine/head_pose.py — head pitch/yaw/roll from FaceMesh landmarks using solvePnP, head movement jitter detection, freeze detection (no movement for extended period)
- [x] Implement services/kinematics_engine/posture.py — MediaPipe Pose landmarks 11/12 (shoulders), shoulder drop ratio (vs calibrated neutral, normalized by torso length), forward lean angle (shoulder-ear line vs vertical), slump score (composite 0-1), posture collapse detection (drop > 15% + lean > 20°)
- [x] Write tests/unit/test_kinematics_engine.py — test EAR computation, blink event detection, blink rate, head pose estimation, jitter/freeze detection, posture analysis (face-only and full pose)

## Phase 6: Telemetry Engine — Mouse, Keyboard, Window Tracking

- [x] Implement services/telemetry_engine/input_hooks.py — pynput mouse listener (position, clicks, scroll at 60Hz → downsample to 10Hz), keyboard listener (inter-keystroke intervals, backspace tracking), platform-specific permission handling (macOS accessibility), graceful degradation on PermissionError
- [x] Implement services/telemetry_engine/window_tracker.py — active window detection (macOS: pyobjc, Linux: python-xlib, Windows: ctypes), window switch event logging, app name + window title extraction, platform abstraction interface
- [x] Implement services/telemetry_engine/feature_aggregator.py — consume raw events over configurable window (default 15s), compute all derived features: mouse_velocity_mean/variance, mouse_jerk_score, click_burst_score, click_frequency, keyboard_burst_score, keystroke_interval_variance, backspace_density, inactivity_seconds, window_switch_rate, scroll_reversal_score, output TelemetryFeatures
- [x] Write tests/unit/test_telemetry_features.py — test feature computation from synthetic event sequences (known mouse paths, keystroke patterns)

## Phase 7: State Engine — Feature Fusion & Classification

- [x] Implement services/state_engine/feature_fusion.py — consume PhysioFeatures + KinematicFeatures + TelemetryFeatures, produce unified 12-dim FeatureVector every 500ms, handle missing channels (None values) with confidence weighting, per-channel quality tracking
- [x] Implement services/state_engine/rule_scorer.py — hyper_score with configurable weights (w1-w7 per spec), hypo_score, flow_score, recovery_score, all sub-score functions (score_pulse_elevation, score_hrv_drop, score_blink_suppression, score_posture, score_mouse_thrash, score_window_switch, score_workspace_complexity), user baseline comparison, normalization
- [x] Implement services/state_engine/smoother.py — EMA (α=0.3) over score history, hysteresis (entry: 0.85, exit: 0.70), dwell time enforcement (HYPER: 8s, HYPO: 15s), rolling buffer, StateEstimate output
- [x] Implement services/state_engine/trigger_policy.py — intervention trigger logic (HYPER + confidence > 0.85 + complexity > 0.6 + signal quality + cooldown + dismissal check), cooldown (60s), dismissal tracking (3 in 5 min → 30-min quiet), adaptive threshold (+0.05 per dismissal for 1 hour), dwell check (8s)
- [x] Write tests/unit/test_state_scoring.py — test score computation, hysteresis, state transitions, cooldown, dismissal escalation

## Phase 8: API Gateway & WebSocket Server

- [x] Implement services/api_gateway/app.py — FastAPI setup, CORS, lifespan events, service dependency injection
- [x] Implement services/api_gateway/routes.py — all REST endpoints per spec (capture, features, state, context, llm, intervention, status, health)
- [x] Implement services/api_gateway/websocket_server.py — WebSocket on ws://localhost:9473, STATE_UPDATE broadcast (500ms), INTERVENTION_TRIGGER dispatch, USER_ACTION reception, client management
- [x] Write integration test for API gateway — test all endpoints with fixtures, verify WebSocket message flow

## Phase 9: Context Engine — Workspace Adapters

- [x] Implement services/context_engine/app_classifier.py — active workspace mode detection (coding_debugging, reading_docs, browsing, terminal_errors)
- [x] Implement services/context_engine/editor_adapter.py — VS Code extension WebSocket communication, request/receive EditorContext, graceful fallback when extension unavailable
- [x] Implement services/context_engine/browser_adapter.py — Chrome extension WebSocket communication, request/receive BrowserContext (active tab, all tabs, content excerpt ≤ 2000 tokens, tab type classification), graceful fallback
- [x] Implement services/context_engine/terminal_adapter.py — capture recent N terminal lines, detect error blocks (stack traces), condense errors, identify root-cause region, output TerminalContext
- [x] Implement context assembly: gather from all adapters → TaskContext with complexity_score

## Phase 10: LLM Engine — Remote Qwen-3-8B Client

- [x] Implement services/llm_engine/client.py — abstract LLMClient protocol, async generate_intervention_plan interface
- [x] Implement services/llm_engine/remote_qwen.py — SSH tunnel management, OpenAI-compatible API via httpx, LLMConfig, timeout (10s), retry (2x), error handling
- [x] Implement services/llm_engine/local_ollama.py — local Ollama REST fallback
- [x] Implement services/llm_engine/prompts.py — all prompt templates (debug_error_summary, code_focus_reduction, browser_tab_reduction, micro_step_planner, calm_overlay_writer), system prompt with JSON schema, context injection assembly, prompt selection by mode
- [x] Implement services/llm_engine/parser.py — fault-tolerant JSON parsing (missing braces, trailing commas, unescaped quotes), Pydantic validation, 2-retry before fallback
- [x] Implement services/llm_engine/cache.py — LRU cache by context hash, configurable TTL (5 min)
- [x] Write tests/unit/test_llm_engine.py — valid JSON, malformed variants, invalid input, fallback, prompts, cache, client tests (59 tests)

## Phase 11: Intervention Engine — Trigger, Execute, Restore

- [x] Implement services/intervention_engine/trigger.py — trigger evaluation, level selection (overlay > 0.70, simplified > 0.85, guided > 0.95), cooldown, dismissal tracking
- [x] Implement services/intervention_engine/snapshot.py — capture pre-intervention WorkspaceSnapshot (fold state, tab visibility, overlay presence, intervention_id)
- [x] Implement services/intervention_engine/planner.py — validate InterventionPlan (no destructive actions, headline < 15 words, 1-3 steps), map hide_targets to adapter commands
- [x] Implement services/intervention_engine/executor.py — apply intervention: fold commands to VS Code, tab hide/dim to Chrome, desktop overlay, track mutations
- [x] Implement services/intervention_engine/restore.py — restore from snapshot, auto-timeout (5 min), recovery detection (FLOW > 0.70 for 15s), log outcome
- [x] Write tests/unit/test_intervention_engine.py — mock adapters, full cycle test (58 tests)

## Phase 12: VS Code Extension

- [ ] Initialize VS Code extension project: package.json, tsconfig.json, extension.ts entry point with activation events and command registration
- [ ] Implement src/ws-client.ts — WebSocket to ws://localhost:9473, handle STATE_UPDATE/INTERVENTION_TRIGGER, send USER_ACTION, auto-reconnect
- [ ] Implement src/context-provider.ts — cortex.getActiveFile, cortex.getDiagnostics, cortex.getSymbolAtCursor, read visible code
- [ ] Implement src/fold-controller.ts — cortex.foldExcept, cortex.unfoldAll, cortex.restoreFoldState, save/restore fold snapshots
- [ ] Implement src/panel-provider.ts — Cortex side panel webview: headline, micro-steps checklist, summary, dismiss button, breathing pacer (4-7-8)
- [ ] Write tests/integration/test_vscode_fold_restore.py — test fold/restore round-trip

## Phase 13: Chrome Extension

- [ ] Initialize Plasmo project: manifest.json (Manifest V3, activeTab + scripting), pnpm deps (React, Plasmo)
- [ ] Implement background.ts — service worker, WebSocket to daemon, dispatch content script injection
- [ ] Implement content.tsx — DOM text extraction (TreeWalker, ≤ 2000 tokens), Shadow DOM UI, focus overlay (dim rgba(0,0,0,0.7)), dismiss button
- [ ] Implement popup.tsx — state indicator, HR estimate, sensitivity toggles, quiet mode, connection status
- [ ] Implement tab management — collect titles/URLs, type classification, temporary hide/group, restore
- [ ] Write tests/integration/test_browser_hide_restore.py — test tab hide/restore round-trip

## Phase 14: Desktop Shell — PySide6 Control Panel & Overlay

- [x] Implement apps/desktop_shell/main.py — PySide6 app entry, system tray, main window
- [x] Implement apps/desktop_shell/tray.py — tray icon with state color, context menu (dashboard, pause/resume, settings, quit)
- [x] Implement apps/desktop_shell/dashboard.py — live state indicator, confidence bar, signal quality, HR trace plot, session timeline
- [x] Implement apps/desktop_shell/overlay.py — transparent always-on-top intervention window, LLM content rendering, calming palette, breathing pacer (4-7-8), dismiss (Escape/click), auto-fade
- [x] Implement apps/desktop_shell/settings.py — webcam toggle, intervention toggle, sensitivity slider (1-5), cooldown, quiet mode, LLM backend selector, debug toggles

## Phase 15: Scripts & Developer Tools

- [x] Implement scripts/run_dev.py — start all services (capture, physio, kinematics, telemetry, state, context, api_gateway), multiprocessing + asyncio, graceful shutdown
- [x] Implement scripts/run_capture.py — standalone webcam test with annotated frame display, FPS counter, quality metrics
- [x] Implement scripts/setup_ssh_tunnel.sh — SSH tunnel to gwhiz1, health check, auto-reconnect
- [x] Implement scripts/run_llm_server.py — start/verify vLLM on gwhiz1, test with sample request
- [x] Implement scripts/calibrate.py — 2-min baseline capture, compute personal baselines (HR, HRV, blink, posture), save profile
- [x] Implement scripts/replay_session.py — load JSONL session, replay state transitions, visualize features
- [x] Implement scripts/seed_config.py — generate default config files, create storage dirs, init SQLite schema

## Phase 16: Integration & End-to-End Testing

- [x] Write tests/integration/test_capture_to_state.py — mock webcam → face → physio + kinematics → fusion → state classification
- [x] Write tests/integration/test_context_to_llm.py — mock context → prompt → LLM (mock server) → parse → validate InterventionPlan
- [x] End-to-end integration test: HYPER → context → LLM → intervention → recovery → restore (< 12s with mocks)
- [x] Privacy verification: assert no frames saved, no biometrics in LLM requests, Chrome only activeTab + scripting
- [x] Performance benchmarks: frame processing < 50ms, fusion < 10ms, classification < 5ms, signal-to-state < 200ms

## Phase 17: Documentation & Polish

- [x] Write docs/setup.md — installation guide, gwhiz1 SSH setup, extension install
- [x] Write docs/architecture.md — system overview, layer diagram, data flow
- [x] Write docs/apis.md — REST and WebSocket API docs with examples
- [x] Write docs/calibration.md — calibration process, baselines, recalibration
- [x] Write docs/adapters.md — how to add new workspace adapters
- [x] Write project README.md — overview, quick start, architecture, demo, tech stack, privacy
- [x] Create .env.example with all configurable values documented

## Completed
- [x] Project initialization
- [x] Ralph framework setup
- [x] Complete specification written (cortex.md)
- [x] Phase 1: Foundation — Project Scaffold, Schemas, Config (all 16 items)
- [x] Phase 3: Capture Service — Webcam + Face Tracking (all 6 items)
- [x] Phase 4: Physio Engine — rPPG Heart Rate & HRV (all 8 items)
- [x] Phase 5: Kinematics Engine — Blink, Head Pose, Posture (all 4 items)
- [x] Phase 6: Telemetry Engine — Mouse, Keyboard, Window Tracking (all 4 items)
- [x] Phase 7: State Engine — Feature Fusion & Classification (all 5 items)
- [x] Phase 8: API Gateway & WebSocket Server (all 4 items)
- [x] Phase 9: Context Engine — Workspace Adapters (all 5 items)
- [x] Phase 10: LLM Engine — Remote Qwen-3-8B Client (all 7 items)
- [x] Phase 11: Intervention Engine — Trigger, Execute, Restore (all 6 items)
- [x] Phase 15: Scripts & Developer Tools (all 7 items)
- [x] Phase 16: Integration & End-to-End Testing (all 5 items)
- [x] Phase 14: Desktop Shell — PySide6 Control Panel & Overlay (all 5 items)
- [x] Phase 17: Documentation & Polish (all 7 items)

## Notes
- Each task should be completable in one Ralph loop
- Tasks within a phase are ordered by dependency
- Phase 1 MUST complete before any other phase
- Phases 2-6 can partially overlap (signal lib needed by phase 4)
- Phase 7 depends on phases 4-6 (needs all feature sources)
- Phase 8 can start after phase 1 (independent of sensing)
- Phase 9 depends on phase 8 (needs API gateway)
- Phase 10 can start after phase 1 (independent, needs only schemas)
- Phase 11 depends on phases 7, 9, 10
- Phases 12-14 can proceed in parallel after phase 8
- Phase 15 depends on phases 3-7 (needs services to exist)
- Phase 16 depends on all previous phases
- Phase 17 can start incrementally alongside other phases
- macOS is the primary development target
- Remote GPU (gwhiz1) is for LLM inference only — all sensing is local
- Privacy is non-negotiable: no video saved, no biometrics sent to LLM
