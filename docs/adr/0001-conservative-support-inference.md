# ADR 0001: Conservative support inference and claim boundary

- Status: Accepted
- Date: 2026-08-25

## Context

Camera and interaction proxies do not establish a medical, neurological, or
psychological state. Earlier names and physiological triggers exceeded the
available validation evidence.

## Decision

Production uses fixed, reviewable telemetry rules with explicit evidence
coverage and `unknown` abstention. Camera pulse, HRV, respiration, blink, and
pose are diagnostic-only until metric-specific reference and participant-held-
out gates pass. Outputs are support hypotheses, never probabilities or
diagnoses. Suggestions are non-mutating by default.

## Consequences

Lower availability is accepted when evidence is missing. Product copy and APIs
must preserve the limitation. Learned models require a new versioned model
card, preregistered protocol, independent review, and a later promotion ADR.

Evidence: [model card](../model-cards/deterministic-support-v2.md),
[study protocol](../research/support-inference-study-protocol.md).
