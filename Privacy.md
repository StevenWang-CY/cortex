# Privacy

## Data Boundary

Cortex keeps biometric processing local by design.

- Webcam frames, landmarks, pulse/HRV, blink, posture, and SQI are processed in-memory.
- Biometric signals are not sent to LLM providers.
- Intervention LLM calls only include workspace context (tabs, editor/terminal context, complexity/state labels).

## What Is Stored Locally

- Baselines and calibration artifacts (`storage/baselines/`)
- Session and evaluation artifacts (`storage/sessions/`, `storage/policy_log/`, `storage/reports/`)
- Optional learning metadata (tab relevance, helpfulness, consent state)

Redis is optional; in-memory fallback is available.

## Prompt/LLM Safety Guarantees

0.2.0 adds prompt hardening:

- User and learned strings are sanitized before prompt interpolation.
- Control characters and suspicious prompt-injection markers are stripped.
- Lengths are bounded and braces escaped to prevent template breakouts.
- LLM plans are schema-validated and unsafe actions are degraded/dropped before execution.

## Microrandomization / AMIP Policy Logging

When `eval.policy=amip`, Cortex logs decision tuples for causal evaluation:

- context features (numeric)
- action propensity distribution
- chosen arm
- linked reward

These records are written locally to `storage/policy_log/YYYY-MM-DD.jsonl` and summarized in local causal reports.

If you do not want exploration-style policy learning, set:

```bash
CORTEX_EVAL__POLICY=greedy
```

(Or `uniform` for research-style randomized mode.)

## Consent and Autonomy

All impactful workspace mutations are consent-gated. Consent escalation is recency-aware and reversible; high-impact actions (including LeetCode lockout-style interventions) are not executed autonomously without earned trust.
