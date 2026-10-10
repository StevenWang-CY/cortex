# Model card: no-inference safety fallback

| Field | Value |
| --- | --- |
| Registry name | `no-inference` |
| Version | `safety-null-v1` |
| Feature schema | `support-features-v2.2.0` |
| Kind | safety null |
| Production eligible | yes |
| Configuration | `state.inference_mode: safety_null` |

## Purpose

This is the operational rollback target for support inference. It disables
interpretation without disabling capture diagnostics, connection health,
history access, explicit user commands, or manual workspace controls.
Its implementation identity is the feature-catalog digest; it does not share
the scored model's eleven-component implementation digest. See the
[current implementation record](../implementation-2026-10.md) for the active
scored model and its continuity/provenance changes.

For every input it returns:

- status `insufficient_evidence`;
- state `UNKNOWN` / support state `unknown`;
- all support scores zero;
- evidence coverage zero;
- no probabilities;
- an exclusion stating that the safety-null rollback is active.

It cannot propose an automatic state-driven intervention. It does not restore
the legacy logistic classifier or default to `FLOW`.

## When to activate

Activate on suspected rule regression, feature-schema drift, unexplained
missingness/coverage changes, unsafe UI interpretation, model-card mismatch,
or an incident where inference cannot be trusted. The rollback is deliberately
coarse and reversible; diagnose offline before reactivating a scored model.

## Verification

CI constructs the safety-null entry through the same `SupportInferenceEngine`
used by production and asserts that strong synthetic evidence still yields
only insufficient-evidence `UNKNOWN`. Model registry activation rejects unknown
or non-production-eligible versions.
