# How It Works

Cortex is a five-layer real-time loop that senses physiology/behavior, estimates cognitive state, decides whether to intervene, and executes one-click workspace actions.

## Pipeline

```
Webcam + Input Telemetry
        │
        ▼
L1 Bio-Extraction
  rPPG (POS/CHROM/green or optional ONNX TSCAN)
  Respiration (BVP + motion proxy fusion)
  Blink/EAR/PERCLOS + head pose + posture
        │
        ▼
SQI Gate (NSQI + SNR + motion + face-loss)
        │
        ▼
L2 State Engine
  Personalized rule scoring + optional ML classifier
  EMA smoothing + Schmitt hysteresis + dwell
  Stress integral + specialized detectors
        │
        ▼
L3 Trigger Policy
  Receptivity gate + adaptive threshold + dismissal model
  AMIP policy (default) / greedy / uniform
        │
        ▼
L4 LLM Engine
  Structured JSON output + schema validation
  Grounded causal explanation verifier
  Destructive-action self-critique
        │
        ▼
L5 Intervention Execution
  Consent ladder + preview/confirm/execute + undo
  Reward logging + policy WAL + causal reporting
```

## L1: Bio-Extraction

- rPPG backends: `pos` (default), `chrom`, `green`, `tscan` (ONNX, auto-fallback to POS on failure).
- Adaptive ROI fusion: forehead/cheeks weighted by luminance/chroma stability and head-jitter penalties.
- Composite SQI is computed and propagated as `physio_sqi` with components; low-quality windows are marked invalid.
- HRV metrics from rolling IBI buffer: `RMSSD`, `SDNN`, `pNN50`, `SD1`, `SD2`, `LF/HF` (Lomb-Scargle), sample entropy.
- HRV requires sustained window readiness (60s gate) before emission.
- Respiration uses BVP modulation plus motion proxy, confidence-weighted.
- Blink features include `perclos_60s`, mean blink duration, EAR variance, and personalized EAR threshold support.
- Telemetry adds correction rate and scroll-back rate alongside keystroke/mouse variability features.

## L2: State Engine

- FLOW signature was corrected to near-baseline HR/HRV bands plus behavioral stability.
- Thresholding is personalized via baseline distributions when available (fallbacks remain conservative).
- Confidence output is calibrated from probability normalization; Schmitt hysteresis is retained.
- Dwell defaults:
  - `HYPER = 30s`
  - `HYPO = 60s`
  - `FLOW = 120s`
- Stress integral now uses standardized HRV deficit and supports explicit recovery credit.
- Optional per-user logistic classifier (`ml_classifier.py`) is available for labeled-session calibration.

## L3: Trigger Policy + AMIP

- Receptivity gate suppresses interventions when:
  - mic/call active,
  - fullscreen active,
  - typing burst is active,
  - outside configured work hours.
- Dismissal predictor can suppress high-probability dismiss contexts after warm-up.
- Confidence threshold is adaptively bounded per user.
- AMIP (`eval.policy=amip`, default):
  - contextual Thompson sampling over fixed intervention arms,
  - temperature softmax,
  - deterministic safety floor,
  - propensity logging and write-ahead log before updates.
- Artifacts:
  - `storage/policy_log/YYYY-MM-DD.jsonl`
  - `storage/reports/causal_YYYY-MM-DD.md`

## L4: LLM Grounding/Safety

- Structured output is required (JSON mode + parser/schema validation).
- Invalid actions are dropped individually (graceful degradation), not full-plan hard-fail.
- Causal explanation is verified against observable context values; fallback text is injected if ungrounded.
- Prompt inputs are sanitized (control stripping, brace escaping, bounded length).

## L5: Execution + Consent

- Consent ladder remains 5 levels, now with recency/decay logic and rejection-aware escalation safeguards.
- LeetCode high-impact actions are consent-gated consistently (`required_consent_level` in payloads).
- Execution stays reversible via snapshot + undo stack.
- Helpfulness tracking stores decision metadata (`decision_id`, `policy_arm`, `propensity`) for off-policy analysis.

## Validation Harness

- AMIP regret smoke: `cortex/tests/eval/test_amip_regret.py`
- IPS unbiasedness: `cortex/tests/eval/test_ips_unbiased.py`
- Safety floor invariants: `cortex/tests/eval/test_safety_floor.py`
- ML calibration/Brier: `cortex/tests/state_engine/test_calibration.py`
- LLM graceful degradation: `cortex/tests/unit/test_llm_safety_refinements.py`
- Dataset-gated UBFC/PURE replay: `cortex/tests/physio/test_rppg_ubfc.py`
