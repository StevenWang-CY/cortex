# How It Works

Cortex is a five-layer local loop that observes available signals, forms an
abstaining workspace-support hypothesis, decides whether a suggestion is
eligible, and presents reversible workspace proposals.

## Pipeline

```
Webcam + Input Telemetry
        │
        ▼
L1 Observation extraction
  rPPG (POS/CHROM/green or optional ONNX TSCAN)
  Respiration (BVP + motion proxy fusion)
  Blink/EAR/PERCLOS + head pose + posture
        │
        ▼
SQI Gate (NSQI + SNR + motion + face-loss)
        │
        ▼
L2 State Engine
  Named, availability-aware deterministic support rules
  EMA smoothing + Schmitt hysteresis + dwell
  UNKNOWN while evidence is warming, missing, or ambiguous
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
- HRV and respiration prototypes remain research-only and are unavailable in
  production pending metric-specific reference validation.
- Blink features include `perclos_60s`, mean blink duration, EAR variance, and personalized EAR threshold support.
- Telemetry adds correction rate and scroll-back rate alongside keystroke/mouse variability features.

## L2: State Engine

- Production support rules use only behavior aggregates with named validity,
  quality, age, source exposure, and missing reasons.
- Fixed denominators ensure missing/lower-quality evidence cannot increase a
  score. Scores are evidence strengths, never normalized probabilities.
- Steady activity requires affirmative recent interaction. Quiet activity
  requires observed inactivity and another corroborating channel.
- Camera pulse, blink, and head/neck values are excluded from support scoring.
- The engine starts `UNKNOWN` and exposes `warming_up` or
  `insufficient_evidence` rather than defaulting to a state.
- Dwell defaults:
  - `HYPER = 30s`
  - `HYPO = 60s`
  - `FLOW = 120s`
- Recovery is temporal and can only follow a confirmed support-likely episode.
- No learned classifier or physiology-driven break policy ships. The optional
  break reminder is user-enabled and based only on elapsed active-work time.
- Model identity, feature-schema digest, contribution details, and a
  `safety_null` rollback are present on the production path.

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

- Cortex talks to Claude exclusively through the Anthropic SDK; transport is selected per-deployment via `CORTEX_LLM__PROVIDER` (`bedrock` default, `vertex`, or `direct`). When every transport is unavailable, the engine falls back to a deterministic rule-based plan (`CORTEX_LLM__FALLBACK_MODE=rule_based`, the default). See [Setup](Setup) for credentials.
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
- Evidence/missingness/replay/rollback gates:
  `cortex/tests/unit/test_evidence_aware_support.py`
- Participant-held-out research scaffolding:
  `cortex/services/state_engine/evaluation_protocol.py`
- LLM graceful degradation: `cortex/tests/unit/test_llm_safety_refinements.py`
- Dataset-gated UBFC/PURE replay: `cortex/tests/physio/test_rppg_ubfc.py`
