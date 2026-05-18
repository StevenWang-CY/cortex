# Audit State Pointer

**Phase:** 2 (remediation closed; 53 of 56 Ledger findings + 2 Architectural Debts + Phase I + Phase J shipped)
**Next finding to address:** none — Ledger substantially closed. See "Outstanding" below.
**Last finding closed:** Phase J (UX polish) — final close-out commit `1e3416c`.
**Last commit:** `merge: Phase J — user-facing polish` (`1e3416c`).

## Resume protocol on fresh invocation

1. Read `audit/findings.md` — authoritative Ledger (Phase 1).
2. Read `audit/execution-log.md` — full commit-by-commit log + "Phase 2 Session 2 — Close-out Report" at the bottom.
3. Read this file — pointer + outstanding list.
4. If a deferred item is to be picked up, dispatch with the same per-finding atomic-commit conventions used in Session 2.

## Outstanding (3 of 56, deferred with justification)

| ID  | Summary | Why deferred | When to pick up |
|-----|---------|--------------|-----------------|
| F17 | State-update sequence-number check on receivers (per-frame drop-stale) | Sender already increments `WSMessage.sequence`. Practical impact bounded — broadcast cadence is 2 Hz, reorder windows are too narrow at real network speeds. Cleanest fix bundles with future protocol revision. | When a measurable reorder incident lands in support tickets, OR alongside the next `WSMessage` schema version bump. |
| F25 | Cooldown/dwell oscillation direct fix | Cost-runaway aspect closed (F20 + W2-B retry recheck). Quality aspect partially closed (F26 quiet-mode persistence + F27 fallback transparency). Direct hysteresis-tuning should be data-driven — needs F41 eval baseline first. | After F41 lands. Tune cooldown / dwell pair from /eval pass-rate. |
| F41 | Eval harness in CI with regression threshold | The harness exists and runs locally; baseline pass-rate not yet captured. CI needs a stable threshold. | Next session. Record baseline → wire workflow → set 3% regression gate. |

## New Ledger entries surfaced and resolved this session

- **F07b** — Native-host `get_auth_token` (Wave 1-G, closed).
- **F08b** — Extension `X-Cortex-Auth-Token` header on launcher/daemon HTTP (Wave 1-G, closed).
- **F16-srv** — Daemon refuses stale USER_ACTION cid (Wave 1-G, closed).
- **F19b** — Correlation IDs in browser extension (Wave 1-G, closed).

## Residual filed (non-Ledger, deferred)

- F20 persistent dashboard banner (per-intervention hint sufficient; dashboard banner is a deepening).
- 9 catalogue-only LEETCODE_* WS types (default-arm log line is the visibility hatch).
- `SessionReport` aggregate rollup of `intervention_apply_confirmation` events.
- 3 Qt overlay tests fail under PySide6 mock pollution (pre-existing test-infra issue; pass in isolation).
- Pre-existing test pollution suite (`test_redis_store`, `test_helpfulness`, etc.) — orthogonal to audit work.
- 4 P2/P3 a11y items documented in `CHANGELOG.md` "Known limitations".

## Session inventory (commits since the Session 1 close at `0b14653`)

- **93 commits** landed on `main` this session.
- **~345 audit-specific tests** added across Python (pytest) and TypeScript (vitest).
- **2 Architectural Debts** closed structurally (Debt-1 codegen, Debt-2 systemic auth).
- **2 Non-Ledger phases** shipped (Phase I performance, Phase J UX polish).
- **0 commits pushed** — all work is on local `main`; user should review before pushing to the `cortex` remote.

## Verification

See "Verification commands (reproducible)" section in `audit/execution-log.md` for the full battery. TL;DR:

```bash
pytest cortex/tests/unit/  # 1275 pass (modulo pre-existing test-pollution suite)
QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_toast.py \
    cortex/tests/unit/test_dashboard_empty_state.py \
    cortex/tests/unit/test_onboarding_hints.py \
    cortex/tests/unit/test_overlay_animation.py
cd cortex/apps/browser_extension && pnpm test  # 35 pass across 12 specs
CORTEX_JSON2TS_CMD=$(which json2ts) python -m cortex.scripts.generate_ts_schemas --check
```

## Final residual-risk statement (post-audit, top 3)

1. **Trigger-policy hysteresis under real biometric jitter (F25-residual).** Cost runaway bounded by F20's budget kill-switch ($20/day default). Quality bounded by F26 + F27. The next escalation is data-driven via F41's eval baseline. Monitor: `cortex_state_loop_interventions_per_hour` should stay <10 nominal, <30 with budget kill armed.
2. **Schema-codegen drift via Pydantic source bypass.** Debt-1 closure depends on every TS-visible field originating in `cortex/libs/schemas/`. CI gate `schema-codegen-check` must be marked Required on the GitHub repo to enforce.
3. **Capability-token rotation collision with in-flight WS sessions.** Debt-2 rotation kills existing connections; the extension's auto-reconnect handles it but logs AUTH_REJECTED during the transition window. Monitor: a sustained spike in AUTH_REJECTED beyond 30s = rotation went wrong.

## Least-confident fix this session

**F25 partial closure.** Cost-runaway side is well-contained and regression-tested. The quality-of-experience side (intervention spam under jitter) is partially closed by F26/F27 but not directly tested with adversarial state sequences. The right next step is an /eval suite that replays a synthetic jittery-state trace and asserts intervention count stays within an envelope. That is F41's territory and was deferred.
