# Architecture

## Overview

Cortex is an in-process supervisor (`cortex/services/runtime_daemon.py`) orchestrating a 5-layer sensing-to-action loop exposed via FastAPI (`:9472`) and WebSocket (`:9473`).

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
  llm_engine (Anthropic SDK — Bedrock / Vertex / direct — with rule-based fallback) + parser + planner validation
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

### Capability Token (HTTP + WebSocket)

Every mutating HTTP route on `cortex/services/api_gateway/` is gated by a capability token via the `require_capability_token` FastAPI dependency. The token lives at `~/Library/Application Support/Cortex/auth.token` with mode 0600 and is regenerated on first daemon start. Clients send either `Authorization: Bearer <token>` (canonical) or the legacy `X-Cortex-Auth-Token` header — both validate against the same on-disk value, and missing/wrong tokens return `401` with `WWW-Authenticate: Bearer`. The WebSocket protocol mirrors this: a connection is held in `pending_auth` until it sends an `AUTH` frame carrying `payload.auth_token`; the server replies with `AUTH_OK`, and any other type before `AUTH` triggers `close(code=1011)`. The token can be rotated from the desktop shell settings UI.

## Durable Learning Artifacts

- Policy log WAL: `storage/policy_log/YYYY-MM-DD.jsonl`
- Causal report: `storage/reports/causal_YYYY-MM-DD.md`
- Helpfulness records include `decision_id`, `policy_arm`, and `propensity`.

## Repository Map

- `cortex/services/capture_service/*`: webcam capture, smart camera selection (skips Continuity Camera), MediaPipe face tracking, quality gating.
- `cortex/services/physio_engine/*`: rPPG, SQI, pulse, respiration, ROI.
- `cortex/services/kinematics_engine/*`: EAR blink detection, solvePnP head pose, shoulder posture.
- `cortex/services/telemetry_engine/*`: pynput input hooks, window tracker, focus transition graph aggregation.
- `cortex/services/state_engine/*`: scoring, smoothing, trigger, detectors, ML classifier, LeetCode mode resolver, stress integral, longitudinal tracker.
- `cortex/services/context_engine/*`: editor / browser / terminal adapters and app classifier.
- `cortex/services/eval/*`: legacy LinUCB bandit + AMIP policy + helpfulness tracker + tab-relevance EMA + causal report + policy replay.
- `cortex/services/llm_engine/*`: Anthropic SDK client (Bedrock / Vertex / direct), planner, prompt construction, parsing, cost tracker, cache.
- `cortex/services/intervention_engine/*`: planner, executor, restore, LeetCode interventions.
- `cortex/services/consent/*`: policy + ladder.
- `cortex/services/handover/*`: ShutdownDetector, HandoverSnapshot, MorningBriefing.
- `cortex/services/activity_tracker/*`: ActivityAggregator (daily timelines), ActivitySummarizer (LLM recaps).
- `cortex/services/session_report/*`: session report generation.
- `cortex/services/throttle/*`: CopilotThrottle (silences inline suggestions in HYPER).
- `cortex/services/launcher/*`: ProjectConfig (YAML profiles), ProjectLauncher.
- `cortex/services/janitor/*`: background cleanup of expired storage records and stale artifacts.
- `cortex/services/api_gateway/*`: FastAPI app, REST routes, capability-token dependency, WebSocket server (AUTH handshake + dispatch).
