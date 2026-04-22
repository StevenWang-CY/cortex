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
  llm_engine (Azure/Ollama/remote/rule) + parser + planner validation
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

- `cortex/services/physio_engine/*`: rPPG, SQI, pulse, respiration, ROI.
- `cortex/services/state_engine/*`: scoring, smoothing, trigger, detectors, ML classifier.
- `cortex/services/eval/*`: legacy bandit + AMIP + causal report + replay.
- `cortex/services/llm_engine/*`: backend clients, prompt construction, parsing.
- `cortex/services/intervention_engine/*`: planner, executor, restore, LeetCode interventions.
- `cortex/services/consent/*`: policy + ladder.
