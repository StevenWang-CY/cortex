# Architecture

## Overview

Cortex is an in-process supervisor (`cortex/services/runtime_daemon.py`) orchestrating a 5-layer sensing-to-action loop exposed via FastAPI (`:9472`) and WebSocket (`:9473`), with an optional sidecar launcher agent on `:9471` that the browser extension uses to start/stop the daemon.

## Ports

| Port | Service | Bound by | Notes |
|------|---------|----------|-------|
| 9471 | Launcher agent (HTTP) | `cortex/scripts/launcher_agent.py` | Sidecar that the extension and the desktop shell call to spawn or kill the daemon. `/stop` is capability-token gated (audit F08); `/health` is open for liveness probes. |
| 9472 | FastAPI HTTP API | `cortex/services/api_gateway/app.py` | Mutating endpoints (`/shutdown`, `/apply_intervention`, `/state/infer`, `/llm/plan`) carry the per-request correlation id from F19 in `X-Cortex-Request-ID`. |
| 9473 | WebSocket | `cortex/services/api_gateway/websocket_server.py` | Real-time STATE_UPDATE / INTERVENTION_TRIGGER stream. `SHUTDOWN` payloads require the local capability token (audit F07). |

All three bind to `127.0.0.1` only.

## Layered Design

```
L1 Bio/Telemetry Extraction
  capture_service + physio_engine + kinematics_engine + telemetry_engine
        │
        ▼
L2 State Engine
  feature_fusion + rule_scorer + smoother + detectors + stress_integral
        │
        ▼
L3 Trigger/Policy
  trigger_policy + eval/amip (or legacy bandit modes)
        │
        ▼
L4 LLM Planning
  llm_engine (Anthropic SDK over AWS Bedrock / GCP Vertex / direct Anthropic
  API, with a deterministic rule-based fallback) + parser + planner validation
        │
        ▼
L5 Intervention Execution
  consent ladder + executor + restore + helpfulness logging
```

## Core Data Contracts

- `PhysioFeatures` now includes expanded HRV + SQI fields.
- `KinematicFeatures` includes `perclos_60s`, blink duration, EAR variance.
- `TelemetryFeatures` includes correction/scroll-back rates.
- `StateEstimate` includes calibrated probabilities and classifier metadata.
- `InterventionPlan` includes non-fatal `plan_warnings`.

All additions are backward-compatible (additive fields only).

## L1 Details

- rPPG backends: POS/CHROM/green with optional ONNX TSCAN.
- ROI extraction includes adaptive patch weighting and motion penalties.
- Pulse estimator computes expanded HRV metrics and composite SQI.
- Physiology is SQI-gated before publication.
- Respiration is dual-path (BVP + motion proxy fusion).

## L2 Details

- Rule scorer uses personalized baseline distributions where present.
- FLOW rule aligns with near-baseline engagement signatures and long dwell.
- Smoother outputs calibrated probabilities while retaining hysteresis behavior.
- Stress integral tracks standardized HRV deficit and supports recovery credit.
- Optional per-user logistic model exists in `state_engine/ml_classifier.py`.

## L3 Details

- Trigger policy adds receptivity gates and learned dismissal suppression.
- Dwell defaults are evidence-updated: HYPER 30s, HYPO 60s, FLOW 120s.
- AMIP policy (`services/eval/amip.py`) performs contextual Thompson sampling with:
  - temperature softmax,
  - deterministic safety floor,
  - propensity logging,
  - write-ahead decision logging.
- Nightly causal report generation: `services/eval/causal_report.py`.

## L4/L5 Safety

- Prompt inputs are sanitized before interpolation.
- LLM output is parsed as structured JSON and verified against schema.
- Invalid actions are dropped in-place (graceful degradation).
- Causal explanations are checked against observed context metrics.
- Destructive-looking actions undergo a self-critique filter before execution.
- Consent is recency-aware and applied consistently, including LeetCode actions.

## Durable Learning Artifacts

- Policy log WAL: `storage/policy_log/YYYY-MM-DD.jsonl`
- Causal report: `storage/reports/causal_YYYY-MM-DD.md`
- Helpfulness records include `decision_id`, `policy_arm`, and `propensity`.

## Repository Map

- `cortex/services/capture_service/*`: camera selection (incl. Continuity Camera filtering), frame capture.
- `cortex/services/physio_engine/*`: rPPG, SQI, pulse, respiration, ROI.
- `cortex/services/kinematics_engine/*`: blink/EAR/PERCLOS, head pose, posture from face landmarks.
- `cortex/services/telemetry_engine/*`: keyboard / mouse / scroll variability + correction/scroll-back rates.
- `cortex/services/state_engine/*`: scoring, smoothing, trigger, detectors, ML classifier, persisted dismissal model (F21) and quiet-mode escalation memory (F26).
- `cortex/services/context_engine/*`: workspace context fusion (editor / terminal / browser tab snapshots) used to build the prompt envelope.
- `cortex/services/eval/*`: legacy bandit + AMIP + causal report + replay.
- `cortex/services/llm_engine/*`: backend clients, prompt construction (with F09 prompt-injection wrappers and F29 truncation telemetry), parsing, parser-side action allowlist (F10), cost tracker (F20).
- `cortex/services/intervention_engine/*`: planner, executor, restore, LeetCode interventions.
- `cortex/services/consent/*`: policy + ladder.
- `cortex/services/session_report/*`: per-session aggregate report (state breakdown, flow streaks, golden hour, comparison stats).
- `cortex/services/api_gateway/*`: FastAPI app, WebSocket server, route handlers (`/state/infer`, `/apply_intervention`, `/llm/plan`, `/shutdown`).
- `cortex/services/launcher/*`: project-config-driven workspace launcher (with the F12 shell-injection allowlist).
- `cortex/services/janitor/*`: retention sweep (`storage/sessions/`, `storage/logs/`, `storage/policy_log/`).
- `cortex/services/activity_tracker/*`: browser tab/activity feed forwarded by the extension.
- `cortex/services/handover/*`, `cortex/services/throttle/*`: ancillary helpers for hand-off between modes and rate-limiting helpers used by the API gateway.
- `cortex/scripts/launcher_agent.py`, `cortex/scripts/native_host.py`: out-of-process glue (port 9471 launcher and Chrome native messaging host) — described under "Ports" above.
- `cortex/apps/desktop_shell/*`: PySide6 macOS UI (dashboard, overlay, settings, tray, onboarding).
- `cortex/apps/browser_extension/*`: Plasmo / React MV3 extension.
