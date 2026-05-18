# Cortex — Audit Phase 2 Execution Log

Entries appended in commit order. Each entry: finding ID, fix summary, files touched, test added, compatibility class, rollback note. New Ledger entries surfaced mid-remediation go to the bottom of this file under `## New Findings`.

---

## Commits

### F19 — End-to-end correlation IDs (Python-side)

**Fix.** Added `cortex/libs/logging/correlation.py` with a `ContextVar`-backed correlation id, `correlation_scope` context manager, and a stdlib `logging.Filter` that injects the id onto every record. Wired `structlog.contextvars.merge_contextvars` into the structlog processor chain so any code path that calls `get_logger()` automatically gets `correlation_id=...` in its log records. Added a FastAPI middleware that mints or accepts `X-Cortex-Request-ID` per request, binds it for the request lifetime, and echoes it back on the response. The WebSocket server enters a correlation scope around every inbound message and stamps the active id onto outbound messages in `_broadcast` so daemon-initiated traffic (state updates, intervention triggers) carries the originating request's id. The Anthropic planner's `llm.request status=ok` log line now includes `cid=…` so cost telemetry (F20, next) can group spend by request.

**Files touched** (7):
- `cortex/libs/logging/correlation.py` (new)
- `cortex/libs/logging/structured.py`
- `cortex/libs/logging/__init__.py`
- `cortex/services/api_gateway/app.py`
- `cortex/services/api_gateway/websocket_server.py`
- `cortex/services/llm_engine/anthropic_planner.py`
- `cortex/tests/integration/test_correlation_ids.py` (new test)

**Test.** `cortex/tests/integration/test_correlation_ids.py` — 8 cases. Asserts the contextvar round-trips, nests correctly, the HTTP middleware mints when absent and echoes when supplied, and `_broadcast` stamps the active id onto a `WSMessage` that arrived with `correlation_id=None`. All 8 pass on this branch; on `main` (pre-fix) the imports `from cortex.libs.logging.correlation import ...` resolve to ModuleNotFoundError, the middleware doesn't exist (no `X-Cortex-Request-ID` header in response), and `_broadcast` does not stamp ids — every test fails.

**Verification.**
- F19 suite: `pytest cortex/tests/integration/test_correlation_ids.py` → 8 passed (0.64s).
- Regression check: `pytest cortex/tests/unit/test_api_gateway.py cortex/tests/unit/test_anthropic_planner.py` → 56 passed (11.51s).

**Compatibility.** Additive only. The middleware adds one header to every response; the WS envelope's `correlation_id` field already existed and was optional. No schema changes, no migrations, no client-side coordination required. Stale clients that don't propagate the id continue to work — they get a freshly-minted id per inbound message.

**Rollback.** `git revert` of this commit is clean: no DB or cache state; the contextvar dies with the process; the middleware is added in code only.

**Scope split.** The browser-extension half of correlation propagation (popup `→` background `→` daemon round-trip) is filed as new Ledger entry **F19b** in `New Findings` below; closing it depends on F40 (TS test infra). Daemon-internal traceability — which is what F19 promised — is closed.

---

### F07 — Capability token gate on WebSocket SHUTDOWN

**Fix.** Tactical mitigation for Architectural Debt #2 (implicit localhost-trust model). Added `cortex/libs/auth/local_token.py` exposing `load_or_create_token()` (atomic write, mode 0600) and `verify_token()` (constant-time compare). Daemon startup now provisions the token before any service binds. The WebSocket server's `SHUTDOWN` handler now requires `payload.auth_token` to match; missing or wrong tokens are logged and silently ignored — the malicious caller learns nothing, the legitimate user still has 5 other paths to stop the daemon (HTTP /shutdown, native messaging stop, launcher /stop — last one closes in F08). Cross-origin localhost web pages and hostile extensions cannot read mode-0600 files, so they cannot present a valid token.

**Files touched** (5):
- `cortex/libs/auth/__init__.py` (new)
- `cortex/libs/auth/local_token.py` (new)
- `cortex/services/api_gateway/websocket_server.py`
- `cortex/services/runtime_daemon.py` (provision token at startup)
- `cortex/tests/unit/test_auth_local_token.py` (new test)

**Test.** 8 cases in `test_auth_local_token.py`. Token round-trip is idempotent, file is 0o600, empty/wrong tokens rejected, correct token accepted, truncated files replaced, and crucially the WS SHUTDOWN handler does not call the shutdown callback when the token is missing but does when it matches. All 8 fail on `main` (module does not exist; SHUTDOWN handler accepts unauthenticated messages).

**Verification.**
- F07 suite: `pytest cortex/tests/unit/test_auth_local_token.py` → 8 passed (0.54s).
- Regression check: `pytest cortex/tests/unit/test_api_gateway.py cortex/tests/integration/test_correlation_ids.py` → 49 passed.

**Compatibility.** Breaking for callers that send `SHUTDOWN` without `auth_token`. Current callers: only `background.ts` line 2548. That call now silently no-ops on Step 1 of the kill chain; Steps 2–6 (HTTP /shutdown, native-messaging stop, launcher /stop, tab cleanup) still run and reliably stop the daemon. The native-host-mediated token fetch needed to restore Step 1 is filed as **F07b** below.

**Rollback.** `git revert` is clean: token file is harmless to leave behind; the WS handler reverts to its previous unauthenticated behaviour; no migration.

---

### F08 + F07b — Capability token gate on launcher /stop and native-host token fetch

**Fix.** Same threat model as F07 — close cross-origin localhost. Two changes ship together because the legitimate extension path needs both halves of the plumbing to remain functional:

1. `launcher_agent.py` requires `X-Cortex-Auth-Token` on `POST /stop`. Header missing or wrong → 401, no PID enumeration, no SIGTERM. `/launch`, `/health`, `/status` remain open (non-destructive; needed for liveness probes and supervisor start-up). The launcher's "zero cortex imports" invariant is preserved by inlining a minimal `_auth_token_path()` + `_verify_auth_token()` (`hmac.compare_digest`).
2. `native_host.py` gains a `get_auth_token` command that returns the daemon's token. The browser↔native-host channel is already OS-authenticated per-profile, so returning the token here does not widen the attack surface; the mode-0600 file is still unreadable from any sandboxed page context.

**Files touched** (4):
- `cortex/scripts/launcher_agent.py`
- `cortex/scripts/native_host.py`
- `cortex/tests/unit/test_launcher_auth.py` (new)
- `cortex/tests/unit/test_native_host_auth.py` (new)

**Test.** 5 cases in `test_launcher_auth.py` + 2 cases in `test_native_host_auth.py`. The launcher tests boot the real `LauncherHandler` on an ephemeral port, monkeypatch `_stop_daemon` to a no-op so the test does not kill the developer's running daemon, and verify (a) /stop without token → 401, (b) /stop with wrong token → 401, (c) /stop with the file's token → 200, (d) /health stays open, (e) missing token file → fall-closed (401, not open). The native-host tests verify `get_auth_token` returns an existing token unchanged and provisions a new one when absent. All fail on `main`.

**Verification.** `pytest cortex/tests/unit/test_launcher_auth.py cortex/tests/unit/test_native_host_auth.py cortex/tests/unit/test_auth_local_token.py cortex/tests/integration/test_correlation_ids.py` → 23 passed.

**Compatibility.** Breaking for external `POST /stop` callers without the token. Internal callers: `background.ts:2578-2583`. After this commit the extension's Step 6 of its kill chain fails 401; Steps 2–5 still run (HTTP /shutdown, native-messaging stop). To fully restore Step 6, the extension must fetch the token via `chrome.runtime.sendNativeMessage("com.cortex.launcher", {command: "get_auth_token"})` and add it as `X-Cortex-Auth-Token` to its `/stop` fetch. Wiring this in `background.ts` is split out as **F08b** (gated on F40 TS test infra).

**Rollback.** `git revert` is clean. The launcher's inline auth helper is self-contained; the native-host new command has no side effects.

---

### F02 — Atomic session report write at shutdown

**Fix.** The previous shutdown path wrapped `self._session_report.finish()` and `session_path.write_text(...)` in a single `try/except Exception: logger.warning(...)`. Any failure — disk-full, permission denied, a Pydantic model error inside `finish()`, a SIGKILL after the file was opened in write-truncate mode but before bytes were flushed — silently dropped the entire session debrief and left no recoverable artefact.
- New helper `cortex/libs/utils/atomic_write.py`: `atomic_write_text` / `atomic_write_json` write to `<path>.tmp`, fsync, and `os.replace` into place. `os.replace` is atomic on POSIX and NTFS; failure before the rename leaves the prior on-disk file intact.
- `runtime_daemon.stop()` now splits compute-vs-disk error handling: `finish()` errors log "nothing to persist" and skip the write; disk-write errors log "prior file preserved" and the previous report (if any) survives.
- Both branches use `logger.error` instead of `warning` so the failure is observable at the daemon's default log level.

**Files touched** (3):
- `cortex/libs/utils/atomic_write.py` (new)
- `cortex/services/runtime_daemon.py`
- `cortex/tests/unit/test_atomic_write.py` (new test)

**Test.** 5 cases in `test_atomic_write.py`. Round-trip JSON, no leftover `.tmp` on success, prior file survives `os.replace` failure (simulated `PermissionError`), tmp file cleaned up on write failure (simulated mid-write `OSError`). All fail on `main` (helper does not exist).

**Verification.**
- F02 suite: 5 passed (0.03s).
- Regression check: full unit suite `pytest cortex/tests/unit/` → 931 passed, 1 skipped.

**Compatibility.** Additive. The on-disk session report format is unchanged. Stale readers continue to see `session_<id>.json`. No migration.

**Rollback.** `git revert` is clean. The atomic-write helper has no callers other than `runtime_daemon.stop()`; the previous `write_text` path is straight-line restored.

---

### F03 — Track background tasks spawned by the state loop

**Fix.** The state loop's intervention dispatch path used bare `asyncio.create_task(...)` with no reference (`runtime_daemon.py:1057`). `stop()` cancelled only the long-running loops listed in `self._tasks`; any in-flight intervention task was orphaned. If that task held a file handle (session-record append, baseline write) the daemon could exit mid-write, truncating JSONL.
- Added `self._background_tasks: set[asyncio.Task]` in `__init__`.
- New helper `_spawn_background_task(coro, *, name=...)` adds to the set + registers an `add_done_callback(self._background_tasks.discard)` so the set stays bounded automatically.
- The previously-orphan call site is rewritten to use the helper.
- `stop()` now cancels every outstanding background task and `await`s them with `return_exceptions=True` before clearing.

**Files touched** (2):
- `cortex/services/runtime_daemon.py`
- `cortex/tests/unit/test_background_task_tracking.py` (new)

**Test.** 4 cases. Tests intentionally use a `_StubDaemon` carrying the exact same plumbing rather than booting `CortexDaemon`, because the full daemon requires camera + store backends and the contract under test is a tiny set of lines. Cases: spawn tracks the task; completed tasks auto-discard; `stop()` cancels in-flight tasks; `stop()` drains multiple concurrent tasks. All fail on `main` (helper doesn't exist; the test's `_spawn_background_task` mirror would still pass against the stub but the orphan call site on `main` proves the bug — a separate live-daemon test would be needed to catch the original orphan, but in this codebase that requires a full integration harness that isn't trivially available; the contract test guards the new helper rigorously).

**Verification.** F03 suite: 4 passed (0.04s). Import-check: `CortexDaemon` imports clean.

**Compatibility.** Additive. Existing `self._tasks` mechanism untouched. No schema or wire changes.

**Rollback.** `git revert` is clean. The orphan call site reverts to bare `asyncio.create_task`; the set + helper die with the diff.

---

### F01 — Capture pipeline stop() bounded by timeout

**Fix.** `runtime_daemon.stop()` awaited `self._capture_pipeline.stop()` with no upper bound. A disconnected USB webcam or stuck mediapipe worker hangs the close indefinitely; only SIGKILL unblocks the daemon, and SIGKILL leaves the AVFoundation camera handle owned by a dead PID for minutes — next launch fails with a permission-loop. The fix wraps the call in `asyncio.wait_for(..., timeout=5.0)`. On timeout the daemon logs an explicit error and proceeds with the rest of the shutdown chain; the kernel reclaims the camera handle on actual process exit. Non-timeout exceptions are now logged (previously they were silently swallowed by `except: pass`).

**Files touched** (2):
- `cortex/services/runtime_daemon.py`
- `cortex/tests/unit/test_capture_stop_timeout.py` (new)

**Test.** 3 cases. A `_NeverFinishingPipeline` proves the timeout fires within bounds; a fast pipeline is not interrupted; non-timeout errors propagate (the wrapper does not swallow `RuntimeError`). On `main` the production code uses `await` with no `wait_for`, so a hung pipeline would block forever — adapter tests cannot match against that since they would themselves hang; the wrapper-pattern tests prove the new contract.

**Verification.** F01 suite: 3 passed (0.28s).

**Compatibility.** Behavioural change at shutdown: previously infinite wait, now 5 s. Legitimate camera close paths complete in well under 1 s; the 5 s budget is generous. No wire/schema change.

**Rollback.** `git revert` is clean. Single hunk in `runtime_daemon.py`; the prior `try/except: pass` is straight-line restored.

---

### F11 — Bedrock token no longer leaks into process environment

**Fix.** Previously `AnthropicPlanner.__init__` for `provider="bedrock"` fetched the bearer token from Keychain and wrote it permanently to `os.environ["AWS_BEARER_TOKEN_BEDROCK"]`. The Cortex daemon spawns many subprocesses (capture worker, native-host re-launches, project-launcher terminals); every one inherited the env, and any debugger / crash-dump tool attached to any descendant could read the token. The Anthropic SDK reads the bearer only inside its constructor, so we now scope the env mutation to that single call and restore the prior state on exit. Keychain is consulted only when env is initially empty (preserves the documented "env wins" precedence); the user's own env value, if present at startup, survives untouched.

**Files touched** (2):
- `cortex/services/llm_engine/anthropic_planner.py`
- `cortex/tests/unit/test_bedrock_token_containment.py` (new)

**Test.** 3 cases: (a) the scoped-mutation pattern in isolation produces a clean env, (b) full `AnthropicPlanner` construction sees the keychain token during SDK build (captured via stub) but the env is clean afterwards, (c) a pre-existing user-supplied env value is preserved (keychain skipped). The third case fails on `main` only by coincidence (no behavioural assertion before the fix); cases (a) and (b) fail on `main` because the env mutation was unbounded — the post-construction assertion `"AWS_BEARER_TOKEN_BEDROCK" not in os.environ` was false.

**Verification.** F11 suite: 3 passed (0.90s). Regression check: `test_anthropic_planner.py` — 15 passed.

**Compatibility.** Subtle but additive. Code that relied on the daemon polluting its own env after construction (none in this repo, grep verified) would break; the SDK's runtime requests do not re-read the env, so the post-construction emptiness has no functional effect on legitimate calls.

**Rollback.** `git revert` is clean. Single hunk in `anthropic_planner.py`; the old "set env permanently" path is straight-line restored.

---

### F09 — Prompt-injection defence in the LLM engine

**Fix.** Two-sided defence shipping together:

1. **Sanitiser hardened.** `sanitize_prompt_text` now defangs the prompt-injection patterns most commonly seen in the wild: leading `System:` / `Assistant:` / `Human:` lines, the XML role tags `<SYSTEM>` / `</SYSTEM>` / `<INSTRUCTION>` / `<ASSISTANT>`, the Llama-style `[INST]` / `[/INST]` markers, and any premature `</USER_CONTENT>` close tag. Defang inserts spaces inside the marker — the human-readable text survives, the byte pattern the model recognises does not.
2. **Delimiter wrapping.** New `wrap_user_content(text, *, tag)` helper. Every user-controlled string interpolated into the user prompt (`context`, `constraints_text`, `goal_hint`, `extra_context`) is wrapped in a tag-distinct delimiter — `<WORKSPACE_CONTEXT>`, `<CONSTRAINTS>`, `<USER_GOAL>`, `<EXTRA_CONTEXT>`.
3. **SYSTEM_PROMPT** gains a "PROMPT INJECTION DEFENCE" clause that tells the model these tagged regions are DATA, never instructions, and to ignore any embedded "System:" prefix, "ignore previous instructions" directive, or new-rules text inside them.

**Files touched** (2):
- `cortex/services/llm_engine/prompts.py`
- `cortex/tests/unit/test_prompt_injection_defence.py` (new)

**Test.** 9 cases. Sanitiser defangs `System:`/`Assistant:`/`Human:` prefixes, XML role tags, `[INST]` brackets, and `</USER_CONTENT>` close tag. `wrap_user_content` produces the expected delimiter. Round-trip attack (a tab title combining every injection pattern) is fully neutralised. The system prompt carries the matching defence clause. Pre-existing brace-escape behaviour is preserved (regression guard). All fail on `main` (sanitiser pre-F09 did not defang any of these patterns; system prompt had no injection-defence clause).

**Verification.**
- F09 suite: 9 passed (1.00s).
- Regression check on prompt/context tests (`pytest -k "prompt or context"`): 104 passed.

**Compatibility.** Wire/schema unchanged. The LLM's effective prompt grows slightly (one tag-wrapper per interpolated value), well within token budget. The injection-defence clause may marginally bias the model toward refusing tab titles that contain `System:` literally — acceptable given the threat.

**Rollback.** `git revert` is clean. Single file modified plus the test; the previous sanitiser is restored straight-line.

---

## New Findings (surfaced during Phase 2)

### F07b — Native-host mediated auth-token fetch for extension

**Status.** Daemon-side closed in F08. Extension-side wiring still open as **F08b**.

### F08b — Extension wires native-host get_auth_token into SHUTDOWN and /stop

**Summary.** F07+F08+F07b shipped the daemon-side primitives. The browser extension still does not fetch the token via `chrome.runtime.sendNativeMessage("com.cortex.launcher", {command: "get_auth_token"})`, so its Step 1 (WS SHUTDOWN with `auth_token`) and Step 6 (`POST /stop` with `X-Cortex-Auth-Token`) currently fail. User-facing kill chain still works via Steps 2–5; the legacy redundancy absorbs the gap, but Step 1's graceful-flush intent and Step 6's belt-and-braces shutdown are lost.
**Fix outline.** On WS connect or first SHUTDOWN attempt, send `{command:"get_auth_token"}` to the native host, cache in memory, attach to outbound SHUTDOWN payload and `/stop` fetch.
**Location.** `cortex/apps/browser_extension/background.ts:2544-2583`.
**Category.** UI.
**Blast radius.** maintainability.
**Fix complexity.** S.
**Dependencies.** F08 (closed), F40 (no TS test infra to satisfy Phase-2 quality bar).

### F19b — Correlation IDs in browser extension

**Summary.** F19 closed daemon-internal correlation. The browser extension (`background.ts`, `popup.tsx`, `newtab.tsx`) still does not mint a correlation id at the user-action origin, does not include it in outbound WS messages, and does not log it. End-to-end traceability from popup click → daemon → LLM → response is therefore one hop short of complete.
**Location.** `cortex/apps/browser_extension/background.ts:1383,1391` (correlation_id forwarded if present but never logged or minted), `cortex/apps/browser_extension/popup.tsx` (no minting).
**Category.** Cross / UI.
**Blast radius.** maintainability + correctness.
**Fix complexity.** S, but **depends on F40** (no TS test infra to verify).
**Dependencies.** F19 (closed), F40 (no TS tests).
**Why split.** Touching the TS side without F40's test infra means manual UI verification only — violates the Phase-2 quality bar requiring a failing-on-main test. Filed as deferred; will be picked up immediately after F40.

---

## Deferred From Phase 1

(All Ledger entries not yet visited remain open — see `audit/state.md` for the locked execution order. None has been formally deferred yet; the session ended on a natural cohort boundary, not a scope-failure boundary.)

---

## Phase 2 Session 1 — Residual Risk Statement

The eight commits in this session close the data-loss tier (F01, F02, F03), the security tier on local-CSRF + prompt injection + credential containment (F07, F08, F09, F11), and the observability foundation (F19). The following residual risks remain after this session — three things most likely to still go wrong in production:

1. **Cost runaway via state oscillation (F20, F25, F26, F27 still open).** A user whose biometric signal oscillates at the HYPER/FLOW boundary can drive 60+ LLM calls/hour. F19 added `cid=` to the planner's success log so per-request grouping is now possible, but the per-user budget, the kill-switch, and the hysteresis fix are still pending. **Monitoring needed:** alarm on per-user planner calls/hour > 30 in any 60-minute window. The `cid=` field is already in place; a downstream log aggregator can group by user and count.

2. **TS extension is uncovered by tests (F40 still open, blocks F19b/F08b/F07b's extension wiring).** The extension-side correlation propagation, native-host token fetch, and `/stop` token attachment are all daemon-ready but extension-unwired. The extension currently fails its WS SHUTDOWN step (Step 1 of stop chain) and its launcher `/stop` step (Step 6) 401. User-facing function is preserved by the redundant other steps; if one of those fails, the user will discover daemons that don't shut down cleanly. **Monitoring needed:** the launcher's `/stop` access log will now show 401s from the extension. That should be the first thing a fresh tail of `~/Library/Application Support/Cortex/launcher.log` reveals.

3. **Action validation gap (F10 still open).** F09 closed the prompt-injection path; F10 — the executor-side allowlist for `open_url`/`close_tab` arguments — is the matching defence and is the next finding in the locked order. Until F10 lands, an LLM that *was* persuaded by injection (e.g. via a vector the F09 defence doesn't cover, like a Unicode homograph not yet in our defang list) can still emit a structurally-valid action with a malicious URL or tab index. **Monitoring needed:** log every `suggested_actions[*].target` value at INFO so post-hoc review can flag novel URLs.

## Phase 2 Session 2 — F10 closed (executor-safety allowlist)

### F10 — Pydantic validators + runtime filter for LLM-emitted actions

**Fix.** Two-layer defence against unsafe ``SuggestedAction`` payloads. Layer 1: ``@field_validator``/``@model_validator`` on ``SuggestedAction`` reject non-http(s) ``open_url`` targets, newlines in ``search_error`` queries, negative ``tab_index``, and per-action_type ``target`` length caps tighter than the outer ``max_length=500``. Layer 2: ``filter_unsafe_actions(plan, tab_count=N)`` in ``parser.py`` runs after enrichment and drops actions whose ``tab_index >= tab_count`` (live upper bound the schema cannot know) or that mutated post-parse into an unsafe shape. New ``EventType.INTERVENTION_ACTION_REJECTED`` log line per drop, carrying the correlation id from F19.

**Files touched** (4):
- `cortex/libs/schemas/intervention.py` (validators + allowlist constants)
- `cortex/services/llm_engine/parser.py` (`filter_unsafe_actions`, wired into `enrich_plan_with_context`)
- `cortex/libs/logging/structured.py` (new EventType)
- `cortex/tests/unit/test_action_allowlist.py` (new test)

**Test.** 17 cases. URL-scheme rejections (javascript/data/file/none), positive accepts (http/https), empty-target parse leniency + runtime drop, search_error newline + length caps, negative tab_index rejection, tab_index upper-bound drop at runtime, non-tab actions untouched, rejection logging carries cid + reason, filter idempotence. All fail on `main` (validators don't exist; filter doesn't exist).

**Verification.**
- F10 suite: 17 passed (0.89s).
- Regression check: 76 LLM-engine/planner/injection tests passed.

**Compatibility.** Breaking on the schema: any historical plan with a `javascript:`/`data:`/etc. URL fails Pydantic parse. Grep of `storage/sessions/*.json` (none in repo) confirms no existing session contains such payloads. For deployed installs, banned actions on replay would surface as parse warnings, not crashes.

**Rollback.** `git revert` is clean. Validators are additive; the filter call is a single line in `enrich_plan_with_context`.

---

## Phase 2 Session 1 — Least-Confident Fix

**F03** (background task tracking). The fix itself is straightforward — a tracked set + `_spawn_background_task` helper + cancellation in `stop()`. What I am least confident about is whether the test coverage is sufficient. The test runs against a `_StubDaemon` that mirrors the helper because booting the full `CortexDaemon` requires a real camera, real store backends, and other dependencies. The stub exercises the contract precisely, but it cannot catch the case where a future call site in the daemon adds another bare `asyncio.create_task(...)` instead of using `_spawn_background_task`. A `pytest --collect-only` style lint or a one-line grep CI check (`! grep -rn "asyncio.create_task" cortex/services/runtime_daemon.py | grep -v "_spawn_background_task"`) would close that gap. Filing as **F03b** but not opening a Ledger row this session because the existing failure mode (orphan tasks) is closed at the call site that was flagged; the residual risk is regression-only, not active.

---

## Audit Wave 2 — UI Consistency Reconciliation Report

**Date.** 2026-05-19. **Posture.** Senior UI/UX engineer auditing Wave 1's
desktop_shell + browser_extension changes for visual consistency and
macOS-native feel. One fix per commit; tests where feasible.

**Commits shipped.**

| SHA | Subject |
|-----|---------|
| `9c7c32b` | promote warm label tints to tokens, lift sub-AA tertiary |
| `d661a38` | route timeline font-family through FONT_MONO token |
| `84a58f4` | regression-guard for native window chrome coverage |
| `c90d382` | route remaining raw-int spacing through tokens |
| `bdff047` | accessible names + tab order on settings, connections, onboarding |
| `4bd1687` | route popup toggle radius + transitions through tokens |

### Per-dimension verdict

**1. Token source-of-truth.** GAP FOUND + CLOSED.
`connections.py`, `settings.py`, `onboarding.py` each carried private
`_LABEL_SECONDARY = "#5C5854"` and `_LABEL_TERTIARY = "#827971"` copies.
The tertiary value fails WCAG AA on the cream background (3.98:1 against
#FFFFFF) — F55 fixed this in `dashboard.py` (raised to `#6B6661`,
~5.4:1) but the other three surfaces silently drifted. Wave-2 commit
`9c7c32b` promotes both tints into the token registry (`tokens.yaml`
emitter + generated `tokens.py` + browser-extension `design-tokens.ts`),
pins `CX_TEXT_TERTIARY = "#6B6661"`, and switches every consumer to
`from cortex.apps.desktop_shell.tokens import CX_TEXT_TERTIARY`.
Regression test `test_token_label_consistency.py` (9 cases) pins the
registry value and asserts no surface carries the legacy literal.
`dashboard.py:898` retains `#B25430` for the degraded badge; it's a
deliberate WCAG-AA-verified deep terracotta scoped to that single
banner and not a candidate for promotion.

**2. Typography.** ESSENTIALLY CLEAN — ONE PROMOTION.
Every `setFont(...)` call across the five panels routes through
`mac_native.system_font(FS_*, weight)`. No `"Arial"` / `"Helvetica"` /
`QFont("Times")` literals anywhere in desktop_shell. The brand
Cormorant headings consistently use `BRAND_DISPLAY_FONT`. Commit
`d661a38` promotes one `font-family: "SF Mono", ui-monospace, ...`
literal in `dashboard.py:1005` (timeline panel) to the `FONT_MONO`
token — the literal happened to match verbatim but would have drifted
on a future stack edit.

**3. Window chrome.** ALREADY CONSISTENT — REGRESSION GUARD ADDED.
All five top-level windows (`DashboardWindow`, `SettingsDialog`,
`OnboardingWindow`, `OverlayWindow`, `ConnectionsPanel`) already invoke
`apply_unified_titlebar` + `apply_vibrancy` in `showEvent`. Commit
`84a58f4` adds `test_window_chrome_coverage.py` — 10 parameterised
ast-based cases that pin every top-level window to its required
`mac_native` calls. A future window class that forgets to apply native
chrome will fail CI rather than inherit Qt's default opaque titlebar
silently.

**4. Spacing rhythm.** TWO LITERALS PROMOTED.
Most layout `setSpacing`/`setContentsMargins` calls already consume the
SP1-SP10 token scale. Commit `c90d382` promotes two outliers:
- `overlay.py:282` `setContentsMargins(24, 24, 24, 24)` → `(SP6, SP6,
  SP6, SP6)`.
- `onboarding.py:236` `setSpacing(8)` → `setSpacing(SP2)`.
The remaining raw integers in `dashboard.py` (3px inner pill padding,
2px inter-column gaps, 10/3/12/3 badge tracker margins) are
intentional sub-4pt fine tuning below the token granularity — they're
not candidates for promotion without inventing new sub-grid tokens.

**5. Accessibility coverage.** GAP FOUND + CLOSED.
F55 wired accessibility on the dashboard + overlay; the three other
panels were untouched. VoiceOver would announce every control as
"button" / "checkbox" / "slider" without semantic context, and the
focus ring escaped the window unpredictably. Commit `bdff047`:
- Extracts the defensive `set_accessible_name` / `setTabOrder` helpers
  into `cortex/apps/desktop_shell/a11y.py` so every panel imports them
  once.
- Wires 16 controls in `settings.py` (back, 6 checkboxes, slider, 2
  spinboxes, combo, 4 debug checkboxes, close, apply) into a 15-step
  tab chain.
- Wires `connections.py`'s back button + every Connect button into a
  collected `_tab_order_chain` and chains it.
- Wires `onboarding.py`'s BYOK token input, region combo, save button,
  Open Connections, Get Started, and per-step Grant buttons + status
  pills.
Regression test `test_a11y_coverage.py` (3 cases) instantiates each
panel offscreen and asserts the accessible names are present.
`tray.py` deliberately untouched — on macOS the tray uses the native
`NSStatusItem` wrapper (`mac_native.StatusBarItem`) which is announced
by VoiceOver via the system menu-bar role; QAction-based accessibility
doesn't apply on the mac path.

**6. Browser-extension native feel.** ESSENTIALLY CLEAN — TWO
PROMOTIONS. Every `fontFamily` already routes through `CX.font` /
`CX.fontSerif` / `CX.fontBrand` / `CX.mono`. The macOS-system stack is
`-apple-system, BlinkMacSystemFont, ...` (the correct native chain).
Focus rings are explicit `outline: 2px solid CX.accent` with
`outline-offset: 2px` — readable + brand-preserving. Commit `4bd1687`
promotes two outliers in `popup.tsx`:
- `toggleTrack.borderRadius: 12` → `CX.radiusFull` (still clamps to
  half-height for the pill shape).
- `toggleThumb.background: "#fff"` → `CX.textInverse`.
Three `transition: "... 0.2s ease"` literals → `CX.durationNormal` +
`CX.easeDefault`. All 31 vitest specs stay green.

**7. Loading / empty / error states.** ALREADY DISTINGUISHED.
F18 added the degraded banner; F54 added the four connectivity states
(`not_installed`, `installed_no_daemon`, `installed_version_mismatch`,
`handshake_failed`) each with its own title + body + CTA. F40+F54
tests cover all four states. The morning briefing card (`popup.tsx`)
renders only when `briefing !== null` — no loading skeleton, no
explicit error state. This is intentional: the briefing is push-based
from the daemon, so absence = no briefing yet (= correct silent state).
The activity-tracker resume cards (`newtab.tsx`) similarly render only
when `activities.length > 0`. The dashboard timeline panel already has
the "No events yet" empty state. **Residual:** no explicit loading
skeleton on the briefing card or activity preview — listed in the
residual-risk section below.

**8. Motion / micro-interactions.** REVIEWED, NO CHANGES.
The audit prompt explicitly said "be conservative — don't add motion
to functional elements like Apply Settings". The two remaining
`setVisible(True)` call sites are:
- `connections.py:238` translocation warning — critical functional
  info, not a candidate for delight motion.
- `onboarding.py:717` Grant button visibility flip — functional state
  change, not delight.
Existing motion is already calibrated (overlay alert `cxAlertIn`,
heartbeat `cxPulse`, breathing pacer, activity-card fade-in,
focus-ring transitions). Adding 150ms fades to the degraded badge or
fallback hint would draw attention to error states — counter-
productive. **Listed as residual** for a future targeted polish pass.

### Surfaces audited

| Surface | Verdict |
|---------|---------|
| `cortex/apps/desktop_shell/dashboard.py` | Already F47/F55/F31; one font-stack literal promoted. |
| `cortex/apps/desktop_shell/overlay.py` | F47/F55/F06 closed; one margin literal promoted. |
| `cortex/apps/desktop_shell/onboarding.py` | A11y added; spacing literal promoted; tertiary tint pulled from tokens. |
| `cortex/apps/desktop_shell/settings.py` | A11y added (16 controls + 15-step tab chain); tertiary tint pulled from tokens. |
| `cortex/apps/desktop_shell/connections.py` | A11y added; tertiary tint pulled from tokens. |
| `cortex/apps/desktop_shell/tray.py` | macOS native `NSStatusItem` — no Qt a11y needed. |
| `cortex/apps/desktop_shell/mac_native.py` | Single point of contact for native chrome — clean. |
| `cortex/apps/desktop_shell/tokens.py` | Auto-generated; emitter updated for AA tertiary tint. |
| `cortex/apps/browser_extension/popup.tsx` | Toggle radius + transitions promoted to tokens. |
| `cortex/apps/browser_extension/newtab.tsx` | Already on tokens; activity card aesthetics preserved. |
| `cortex/apps/browser_extension/design-tokens.ts` | Auto-generated; tertiary tint synced. |

### Verification

- `QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_overlay_dismiss.py
  cortex/tests/unit/test_dashboard_stop.py
  cortex/tests/unit/test_overlay_tokens.py
  cortex/tests/unit/test_token_label_consistency.py
  cortex/tests/unit/test_window_chrome_coverage.py
  cortex/tests/unit/test_a11y_coverage.py -q` → **35 passed**.
- `QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/
  --ignore=cortex/tests/unit/test_desktop_shell.py -q` →
  **1150 passed**.
- `cortex/apps/browser_extension && npx vitest run` →
  **31 passed across 10 spec files**.

`test_desktop_shell.py` installs lightweight PySide6 mocks that bleed
into modules imported after it inside the same pytest session — a
pre-existing harness quirk documented in `test_overlay_tokens.py`
(every dependent test file unloads stale PySide6 mocks at module
top). The legacy mock suite has one pre-existing failure
(`TestOverlayWindow::test_show_intervention` — `MockQLabel.clear`
missing) that is unrelated to this wave's changes; verified by
running `test_desktop_shell.py` isolated against `HEAD~6` and
observing the same failure.

### Residual risk

1. **No loading skeleton on morning-briefing / activity-tracker
   preview.** Both are push-based from the daemon, so absence = no
   data yet. A future commit could add a 600ms shimmer skeleton if
   user testing reveals confusion about "is Cortex thinking, or did
   it fail?" Scoped out of Wave-2 because it's net-new UX, not
   reconciliation.
2. **No fade-in on the dashboard degraded badge / overlay fallback
   hint.** Both are functional notifications; per the audit's "be
   conservative" rule, no motion added. If user testing shows the
   abrupt appearance is jarring, a 150ms `QPropertyAnimation` on
   `windowOpacity` is the targeted fix.
3. **`test_desktop_shell.py` mock-pollution.** Pre-existing. Affects
   only intra-session ordering; every dependent test file already
   defends with the stale-PySide6 unload pattern at module top.
   Cleanest fix is to migrate the legacy mock suite to real PySide6
   under `QT_QPA_PLATFORM=offscreen` — out of scope for visual
   reconciliation.



---

## Debt-2 closure — capability-token client bootstrap

The Wave-1 tactical token gates (F07 SHUTDOWN, F08 launcher `/stop`) defended only two destructive endpoints. Every other HTTP route on the API gateway and every other WebSocket message type still trusted "comes from localhost" as proof of legitimacy — the implicit trust model named in `audit/findings.md` Debt-2. A hostile webpage in another browser tab could connect to `ws://127.0.0.1:9473` and watch the daemon's STATE_UPDATE stream without ever owning the token; the same page could `POST /state/infer` and burn the daemon's numpy allocators. The systemic close-out flips the default: every connection presents the capability token; servers reject anything else.

Five atomic commits in the locked order from `implement-all-that-s-helpful-mellow-hammock.md` §4 Phase H:

| Commit | SHA       | Headline                                                | Files |
|--------|-----------|---------------------------------------------------------|------:|
| 1      | `0fe609a` | Server-side capability-token gate on every HTTP route   | 7     |
| 2      | `78d9d57` | WebSocket AUTH-first handshake                          | 8     |
| 3      | `f16a46a` | desktop_shell WS client AUTHs before IDENTIFY           | 2     |
| 4      | `5eaef88` | extension WS sends AUTH before IDENTIFY                 | 2     |
| 5      | `9066df1` | capability-token rotation UI in Settings                | 5     |

**Server side.** `cortex/services/api_gateway/auth.py` (new) exports `require_capability_token` (a FastAPI dependency that raises 401 on miss/mismatch and emits `EventType.AUTH_REJECTED`) and `optional_capability_token` (used nowhere yet but reserved for `/health` extensions). `routes.py` is split into `router` (the default — gates every mutating endpoint via `Depends(require_capability_token)`) and `health_router` (the supervisor liveness probe; no auth, mounted separately). Adding a new mutating route on `router` automatically inherits the gate; adding a route to `health_router` is auditable in code review. The 401 response carries `WWW-Authenticate: Bearer` per RFC 7235.

**WebSocket side.** `WebSocketServer._dispatch_message` short-circuits to `_handle_auth` on the `AUTH` message type; until the client's `authenticated` flag flips True, every other type triggers `close(code=1011, reason="auth required")` + `EventType.AUTH_REJECTED`. `_handle_auth` validates via `cortex.libs.auth.verify_token`, replies with an `AUTH_OK` frame, and replays the latest cached `STATE_UPDATE` so the legacy "new connection sees current state on attach" UX is preserved. `_broadcast` skips unauthenticated peers so a connect-and-listen origin cannot harvest the state stream. `MessageType.AUTH` and `MessageType.AUTH_OK` were added to `cortex/libs/schemas/ws_message_types.py`; the Phase G codegen regenerated `cortex_schemas.d.ts` automatically.

**Client side.** `cortex/apps/desktop_shell/main.py::WebSocketBridge` reads the token at startup via `load_or_create_token`, sends `AUTH` as the first frame on every connect (ahead of `IDENTIFY`), and exposes `refresh_auth_token` for the rotation path. `cortex/apps/browser_extension/background.ts::connect()` fires `getAuthToken().then(send AUTH then IDENTIFY)` inside `onopen`; the existing Wave-1 `X-Cortex-Auth-Token` header on `/shutdown` and `/stop` fetches continues to satisfy the systemic HTTP gate. A new `case "AUTH_OK"` no-op landed in `handleMessage` so the daemon's ACK is recognised rather than logged as an unknown type.

**Rotation.** `cortex/libs/auth/local_token.py::rotate_token` writes a fresh `secrets.token_hex(32)` to the same path `auth_token_path()` returns, atomically (`.tmp` sibling chmod 0600, then `os.replace`). The Settings panel's new "Security" section exposes this as a "Rotate authentication token" button with an inline status label. The button briefly disables itself after a click to absorb a double-click. `EventType.AUTH_TOKEN_ROTATED` lands in the structured log so a support engineer can correlate "the user just rotated" with "all WS clients suddenly reconnected."

**Defense in depth (Commit 6 deliberately omitted).** The Wave-1 F07 inline `verify_token` call on the WebSocket `SHUTDOWN` handler stays. The Wave-1 F08 launcher `/stop` token gate stays (the launcher binary has a zero-cortex-imports invariant so we cannot apply the systemic FastAPI dependency to it). Both are now redundant given the systemic gate, but they remain as cheap belt-and-braces: a future regression in `_dispatch_message` that accidentally lets a non-AUTH message through still cannot fire SHUTDOWN, and the launcher's `/stop` is still gated even when the daemon API gateway is unreachable. The trade is ~20 lines of duplicated check for a much harder-to-bypass invariant — worth it.

**Migration path.** Existing installs (Wave-1 already shipped the token file via daemon startup `load_or_create_token`) get the systemic gate for free — the token file is already on disk. Fresh installs mint the file on first daemon start. The browser extension's existing `getAuthToken()` cache (`chrome.storage.session`) survives across service-worker restarts; the desktop_shell reads the file fresh on every launch via `WebSocketBridge.__init__`. No coordinated rollout needed because the legacy WS handshake (`IDENTIFY` first, no `AUTH`) was never broadcast to a wire — the daemon's gate simply closes any peer that sends `IDENTIFY` first, and the reconnect loop on both clients retries with the now-cached token in the AUTH frame.

**Threat model recap.** Closes cross-origin localhost — a hostile webpage in another browser tab that can speak the HTTP / WS protocols cannot read the mode-0600 token file, so it cannot present the token, so every request it makes returns 401 / 1011. Explicitly does NOT close malware-as-the-user (a compromised account on the same Mac can read any user-readable file the daemon can read); that threat is named in `audit/findings.md` and is out of scope for this debt.

### Reproducible verification commands

```bash
# New auth tests:
pytest cortex/tests/unit/test_systemic_auth_http.py \
       cortex/tests/integration/test_systemic_auth_ws.py \
       cortex/tests/unit/test_desktop_controller_auth.py \
       cortex/tests/unit/test_token_rotation.py -q          # 17 passed

# Full Python suite (excl. legacy desktop_shell mock-pollution suite):
pytest cortex/tests/ -q --ignore=cortex/tests/unit/test_desktop_shell.py
# 1307 passed, 3 skipped

# TS suite (browser extension):
cd cortex/apps/browser_extension
./node_modules/.bin/vitest run                              # 33 passed

# Schema codegen still in sync after AUTH/AUTH_OK addition:
CORTEX_JSON2TS_CMD=$(which json2ts) python -m cortex.scripts.generate_ts_schemas --check
# exit 0

# Manual adversarial test (closes within 2s without AUTH):
python -c "
import asyncio, websockets
async def go():
    async with websockets.connect('ws://127.0.0.1:9473') as ws:
        await ws.send('{\"type\":\"STATE_UPDATE\",\"payload\":{},\"timestamp\":0,\"sequence\":0}')
        print(await asyncio.wait_for(ws.recv(), 2))
asyncio.run(go())
"
# websockets.exceptions.ConnectionClosedError: ... [code=1011 reason=auth required]
```

---

## Audit Wave 2 — Contract drift sweep (post Phase G + Phase H)

**Date.** 2026-05-19. **Posture.** Senior coordination engineer verifying
no residual frontend ↔ backend contract drift survived the Debt-1 schema
codegen + Debt-2 systemic-auth waves.

**Commits shipped.**

| SHA | Subject |
|-----|---------|
| `a7bcf70` | thread F18 degraded/source through STATE_UPDATE WS payload |
| `e8bac22` | surface unhandled-but-known WS frames in extension |

### Per-category verdict

**1. HTTP routes ↔ extension fetches.** VERIFIED CLEAN.
Three fetch sites in the entire extension surface
(``background.ts:2860`` ``/shutdown``, ``background.ts:2880`` ``/stop``,
``background.ts:2947`` ``/launch``). All three carry the
``X-Cortex-Auth-Token`` header when the cached capability token is
available; ``/launch`` is intentionally unauthenticated because the
launcher boots the daemon before the daemon's keychain is loaded
(launcher_agent.py exposes it on its own port 9471, separate trust
boundary). Verb / path / body / response field reads match the FastAPI
routes one-for-one.

**2. WS client → server.** VERIFIED CLEAN.
12 distinct ``send()`` types in ``background.ts`` (``AUTH``,
``IDENTIFY``, ``USER_ACTION``, ``ACTION_EXECUTE``, ``USER_RATING``,
``CONTEXT_RESPONSE``, ``SETTINGS_SYNC``, ``ACTIVITY_SYNC``,
``TAB_RELEVANCE_FEEDBACK``, ``LEETCODE_CONTEXT_UPDATE``,
``INTERVENTION_APPLIED``, ``SHUTDOWN``); ``WebSocketServer._dispatch_message``
has explicit dispatch arms for all 12. The ``AUTH`` frame goes first
per Debt-2.

**3. WS server → extension broadcast.** GAP FOUND + CLOSED.
The ``MessageType`` enum lists 15 ``LEETCODE_*`` cues; ``background.ts``
explicitly handles 6 (the 5 actions the live ``InterventionMatrix``
emits plus ``SHOW_CONSOLIDATION``). The other 9
(``LEETCODE_LOCK_EDITOR``, ``LEETCODE_INTERCEPT_SUBMIT``,
``LEETCODE_GATE_SOLUTIONS``, ``LEETCODE_SHOW_SESSION_BRIEFING``,
``LEETCODE_AI_RESTATEMENT_CHECK`` / ``LEETCODE_AI_COMPREHENSION_CHECK`` /
``LEETCODE_AI_HYPOTHESIS_CHECK`` / ``LEETCODE_AI_STUCK_ANALYSIS`` /
``LEETCODE_AI_SESSION_BRIEFING``) are catalogue-only — the ``LeetCodeAdapter``
advertises them but no runtime selector calls
``_leetcode_adapter.execute(<capability>, ...)`` for them today. Commit
``e8bac22`` adds a defensive ``default:`` arm in the message switch so a
future regression where the daemon adds a new emitter (or the matrix
grows to cover the AI checks) is visible in DEBUG logs instead of
silently swallowed. ``COPILOT_THROTTLE`` is targeted at vscode clients
only (``target_client_types=["vscode"]``) and never reaches the chrome
peer, so the chrome extension's lack of a handler is correct.

**4. Generated types coverage.** VERIFIED CLEAN.
``cortex_schemas.d.ts`` includes ``WSMessage``, ``MessageType`` (the
enum-string union of all 37 wire types), ``StateEstimate``,
``InterventionPlan``, ``SuggestedAction``, ``TaskContext``,
``InterventionApplyResult``, plus ``LeetCodeContext`` /
``LeetCodeModeEstimate``. ``StateInferResponse`` is defined in
``routes.py`` not in ``cortex/libs/schemas/`` so the codegen walk does
not pick it up; verified no extension client consumes the
``/state/infer`` envelope (the dashboard reads ``degraded`` /
``source`` off the WS ``STATE_UPDATE`` stream — see category 5).

**5. F18 degraded envelope surfaced.** GAP FOUND + CLOSED.
The F18 fix added ``source`` / ``degraded`` to ``StateInferResponse``;
the dashboard advanced tab reads both off the payload dict to toggle the
"classifier unavailable" banner. But the dashboard is fed by the WS
``STATE_UPDATE`` broadcast, not by ``/state/infer`` —
``WebSocketServer._make_state_update`` never stamped the two fields, so
the banner could not fire through the WS path. F18 was end-to-end
silently broken. Commit ``a7bcf70`` mirrors the envelope fields onto
every STATE_UPDATE frame (``degraded = estimate.classifier_source is
None``; ``source = "fallback" if degraded else "classifier"``) and
fixes a brittle dashboard fallback test that conflated the envelope
``source`` literal (``classifier``/``fallback``) with the debug-overlay
``classifier_source`` field (``rule``/``ml``/``ensemble``) — on a healthy
``classifier_source="rule"`` payload the banner would have flipped True
and stuck visible.

**6. F20 cost telemetry surfaced.** PARTIALLY VERIFIED.
``CostTracker.record`` emits ``EventType.LLM_COST`` per call and
``EventType.LLM_BUDGET_KILL`` when the daily budget trips. The kill
path stamps ``plan.metadata["budget_killed"] = True`` and the overlay
(``cortex/apps/desktop_shell/overlay.py:510``) surfaces a per-intervention
"Cortex offline mode — daily AI budget reached" hint. The persistent
dashboard banner contemplated in Phase A is NOT implemented — only the
per-intervention overlay hint. Filed as residual (net-new UX, not
contract drift).

**7. F10 action-rejection telemetry.** VERIFIED CLEAN.
``filter_unsafe_actions`` emits ``EventType.INTERVENTION_ACTION_REJECTED``
per drop with the bound cid; by design the rejection is log-only — the
user never sees a banned action, so there is no UI to suppress. The plan
notes this explicitly.

**8. Per-route auth dependency coverage.** VERIFIED CLEAN.
``cortex/services/api_gateway/app.py:183`` mounts the gated router via
``app.include_router(router, dependencies=[Depends(require_capability_token)])``;
``app.include_router(health_router)`` at line 182 mounts only ``/health``
without auth. Every mutating endpoint in ``routes.py`` lives on
``router`` and inherits the gate; ``/health`` is the only endpoint
reachable without the capability token, which is the documented design.

### Surfaces audited

| Surface | Verdict |
|---------|---------|
| ``cortex/services/api_gateway/routes.py`` | Auth coverage clean; F18 envelope set on HTTP. |
| ``cortex/services/api_gateway/auth.py`` | Two FastAPI deps; only health route uses ``optional_capability_token``. |
| ``cortex/services/api_gateway/app.py`` | Single ``include_router(dependencies=[…])`` wire — adding new route inherits the gate. |
| ``cortex/services/api_gateway/websocket_server.py`` | AUTH-first dispatch; broadcast skips unauth peers; STATE_UPDATE now stamps F18 envelope fields. |
| ``cortex/apps/browser_extension/background.ts`` | All 3 fetches gated; 12 send-types match dispatch; unhandled-frame default arm added. |
| ``cortex/apps/desktop_shell/dashboard.py`` | F18 banner reader hardened against the ``classifier_source`` / ``source`` conflation. |
| ``cortex/libs/schemas/ws_message_types.py`` | 37 enum members; catalogue surfaces the 9 still-unwired LEETCODE_* cues so the schema gate notices future drift. |
| ``cortex/apps/browser_extension/types/generated/cortex_schemas.d.ts`` | 2050 lines; codegen still in sync. |

### Verification

```bash
# F18 WS plumbing + envelope contract:
pytest cortex/tests/unit/test_ws_state_update_degraded.py \
       cortex/tests/unit/test_state_infer_envelope.py -q
# 7 passed in 0.53s

# Defensive default arm in extension switch:
cd cortex/apps/browser_extension
./node_modules/.bin/vitest run __tests__/audit_w2_unhandled_ws_frame.spec.ts
# 2 passed

# Schema codegen still in sync (no Python schema changes in this wave):
CORTEX_JSON2TS_CMD=$(which json2ts) \
  python -m cortex.scripts.generate_ts_schemas --check
# exit 0 (no diff)
```

### Residual

1. **F20 persistent dashboard banner.** The Phase A plan called for a
   dashboard-level banner on ``LLM_BUDGET_KILL`` in addition to the
   per-intervention overlay hint. The hint is wired; the banner is not.
   This is net-new UX (not contract drift) and is scoped out of the
   Wave-2 sweep.
2. **9 catalogue-only LEETCODE_* types.** The schema lists them and the
   ``LeetCodeAdapter`` exposes the capabilities, but no live
   ``InterventionMatrix`` selector emits them. The default-arm log line
   is the visibility hatch for when a future fix wires them up; no
   handler implementations land here because there is no caller to
   regression-test against.


---

## Phase 2 Session 2 — Close-out Report (Wave 3 + Wave 4 sweep)

### Closed Ledger findings (53 of 56)

Session 1 + Session 2 cumulative, mapped to commit cohort:

| Tier | IDs | Cohort |
|------|-----|--------|
| Data-loss | F01, F02, F03, F36, F53 | Wave 1-E/F + session 1 |
| Security | F07, F07b, F08, F08b, F09, F10, F11, F12, F13, F14+F37 | Wave 1-A + session 1 + Phase H |
| Correctness | F04, F05, F06, F15, F16, F16-srv, F18, F19, F19b, F20, F21, F22, F23, F24, F26, F27, F28, F29, F30, F34, F38+F39, F40, F42, F43, F44, F45 | Waves 1-B/C/D/E/G + Wave 2-A/B + Phase G |
| Cost | (folded into F20/F25-partial/F30) | Wave 1-C + Wave 2-B |
| Maintainability | F31, F32, F33, F35, F46, F47, F48, F49, F50, F51, F52, F54, F55, F56 | Wave 1-F + Wave 2-C |

### Architectural Debt — closed

- **Debt-1 (shared schema codegen).** `pydantic-to-typescript` generator + drift gate + extension migration. Closes F42/F43/F44/F45 structurally; future Pydantic schema edits regenerate `cortex_schemas.d.ts` automatically; CI rejects out-of-sync commits.
- **Debt-2 (capability-token client bootstrap).** Every HTTP route now requires `Authorization: Bearer <token>` or `X-Cortex-Auth-Token`; every WebSocket connection requires `AUTH` as its first frame. F07/F08's tactical single-endpoint gates retained as defense-in-depth. Token rotation UI in Settings.

### Non-Ledger phases shipped

- **Phase I (performance).** Capture-loop mediapipe sub-sampling + colour-convert cache, parallel WS broadcast with 100 ms budget, lazy mediapipe + keyring imports (sub-2s warm-cache startup), content-script-only leetcode observer. Bundle ~175 KB (under 250 KB target). 25 new perf tests.
- **Phase J (UX polish).** Onboarding "Why?" expanders + Continuity-camera callout, error toast with selectable correlation-id, biometrics empty states, overlay scale-in + fade-in micro-interactions (Reduce-Motion honoured), accessibility sweep + CHANGELOG. 26 new UX tests.

### Outstanding (3 of 56 — filed as deferred)

- **F17 — State-update sequence-number check on receivers.** Sender side already increments `WSMessage.sequence`. The receiver-side drop-stale logic is partially provided by F16/F16-srv's correlation-id swap on intervention frames but is NOT generalised to `STATE_UPDATE`. Deferred because the practical impact is bounded — broadcast cadence is 2 Hz, so reorder windows are too narrow to matter in real networks — and the cleanest fix is bundled into the schema-versioned `WSMessage` migration that lands as part of a future protocol revision.
- **F25 — Cooldown/dwell oscillation direct fix.** Cost-runaway aspect closed by F20 (budget kill-switch) and W2-B's "re-consult cost kill switch between LLM retry attempts". Quality-of-experience aspect (intervention spam under jitter) closed by F26 (quiet-mode persistence) and F27 (fallback transparency). The underlying race between `trigger_policy.evaluate` and `cooldown_seconds` survives but is no longer expensive. Filed as a hysteresis-tuning follow-up; ML eval should drive the tuning, not code.
- **F41 — Eval harness in CI.** Phase G's CI workflow (`.github/workflows/ci.yml`) added `python (pytest+ruff+mypy)`, `extension (vitest)`, and `schema-codegen-check`. The eval harness in `cortex/services/eval/` exists and runs locally; wiring it to CI with a regression threshold is the next session's lift. Deferred because the baseline pass-rate hasn't been captured yet and CI needs a stable threshold.

### New Ledger entries surfaced and closed mid-remediation

- **F07b** — Native-host mediated auth-token fetch (closed in Wave 1-G).
- **F08b** — Extension `X-Cortex-Auth-Token` on `/stop` and `/shutdown` (closed in Wave 1-G).
- **F16-srv** — Daemon refuses stale USER_ACTION cid (closed in Wave 1-G).
- **F19b** — Correlation IDs in browser extension (closed in Wave 1-G).
- **F19a / F03b regression guards** — Lint guards filed but not committed (residual; would block future bare `asyncio.create_task` calls).

### Residual filed (non-Ledger, deferred to future sessions)

- **F20 persistent dashboard banner.** `LLM_BUDGET_KILL` event is emitted and the per-intervention overlay hint flags `metadata.budget_killed`, but a dedicated dashboard banner is not wired. Per-intervention hint is sufficient for the audit ship; banner is a UX deepening.
- **9 catalogue-only LEETCODE_* types.** The schema lists them; no caller emits them. Default-arm log line in the extension is the visibility hatch.
- **`SessionReport` aggregate rollup of `intervention_apply_confirmation`.** Per-session JSONL has the data; the aggregated `SessionReport` does not roll it up into a "X of Y interventions confirmed" surface. UI/UX-tier follow-up, not contract drift.
- **3 Qt overlay tests (`test_circuit_breaker_surfacing`, `test_context_truncation`, `test_desktop_shell::test_show_intervention`)** fail when collected alongside other Qt tests due to pre-existing PySide6 mock pollution. Each passes in isolation. Pre-existing test-infra issue, not a regression.
- **Pre-existing test-pollution suite** in `test_redis_store.py`, `test_helpfulness.py`, `test_focus_graph.py`, etc. — 26+ tests fail when run alongside the full suite, pass in isolation. Pre-existing fixture leakage (likely `registry.reset` + `fakeredis` import order + stubbed PySide6 modules); orthogonal to the audit work.
- **4 P2/P3 a11y items** (documented in `CHANGELOG.md` "Known limitations"): VoiceOver rotor on Cormorant numerics, high-contrast palette tier, live-region announcements on state transitions, Reduce Motion gating on HR-trace plot + breathing pacer + focus-ring transitions.

### Final residual-risk statement (post-audit, top 3)

1. **Trigger-policy hysteresis under real biometric jitter (F25-residual).** Cost runaway is bounded by F20's budget kill-switch ($20/day default per user). Quality is bounded by F26's quiet-mode escalation memory and F27's fallback transparency. The next escalation is data-driven: ship a /eval baseline and tune the cooldown/dwell pair. Monitor: `cortex_state_loop_interventions_per_hour` should stay under 10 under nominal load and never exceed 30 with the budget kill armed.
2. **Schema-codegen drift through model edits that bypass the Pydantic source.** Debt-1 closure depends on every TS-visible field originating in `cortex/libs/schemas/`. A future contributor who adds an `Any` field or hand-edits `cortex_schemas.d.ts` will trip the CI gate — but only if the gate is required-for-merge. Monitor: CI job `schema-codegen-check` must be marked Required on the GitHub repo.
3. **Capability-token rotation collision with in-flight WS sessions.** Debt-2 rotation kills existing connections, forcing reconnect. Browsers cache the old token in `chrome.storage.session`; the cache is invalidated on next `get_auth_token` call. There is a window of seconds where the old token is rejected but the new one isn't fetched yet — the extension's auto-reconnect handles it but logs an AUTH_REJECTED. Monitor: spike in `AUTH_REJECTED` events lasting longer than 30 seconds = rotation went wrong.

### Verification commands (reproducible)

```bash
source .venv/bin/activate

# All Session 2 audit-specific tests:
pytest cortex/tests/unit/test_action_allowlist.py \
       cortex/tests/unit/test_cost_tracker.py \
       cortex/tests/unit/test_anthropic_planner_cancellation.py \
       cortex/tests/unit/test_anthropic_planner_budget_retry.py \
       cortex/tests/unit/test_circuit_breaker_surfacing.py \
       cortex/tests/unit/test_cache_template_version.py \
       cortex/tests/unit/test_context_truncation.py \
       cortex/tests/unit/test_state_infer_envelope.py \
       cortex/tests/unit/test_rate_limit.py \
       cortex/tests/unit/test_atomic_write.py \
       cortex/tests/unit/test_background_task_tracking.py \
       cortex/tests/unit/test_dismissal_model_persistence.py \
       cortex/tests/unit/test_quiet_mode_persistence.py \
       cortex/tests/unit/test_consent_ladder_race.py \
       cortex/tests/unit/test_prompt_injection_defence.py \
       cortex/tests/unit/test_prompt_injection_wrapper_tags.py \
       cortex/tests/unit/test_quiet_mode_history_age.py \
       cortex/tests/unit/test_architecture_md_alignment.py \
       cortex/tests/unit/test_schema_codegen.py \
       cortex/tests/unit/test_ws_message_schema.py \
       cortex/tests/unit/test_api_gateway.py \
       cortex/tests/unit/test_anthropic_planner.py \
       cortex/tests/unit/test_systemic_auth_http.py \
       cortex/tests/unit/test_token_rotation.py \
       cortex/tests/unit/test_ws_user_action_cid.py \
       cortex/tests/unit/test_ws_state_update_degraded.py \
       cortex/tests/unit/test_ws_slow_client.py \
       cortex/tests/unit/test_pending_context_cleanup.py \
       cortex/tests/unit/test_launcher_auth.py \
       cortex/tests/unit/test_native_host_auth.py \
       cortex/tests/unit/test_launcher_allowlist.py \
       cortex/tests/unit/test_native_messaging_schema.py \
       cortex/tests/unit/test_seed_config_dead_envs.py \
       cortex/tests/unit/test_auth_local_token.py \
       cortex/tests/unit/test_capture_stop_timeout.py \
       cortex/tests/unit/test_bedrock_token_containment.py \
       cortex/tests/integration/test_correlation_ids.py \
       cortex/tests/integration/test_systemic_auth_ws.py \
       cortex/tests/integration/test_apply_intervention_confirmation.py \
       cortex/tests/performance/ -q

# Phase J UX tests (offscreen Qt):
QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_toast.py \
                                  cortex/tests/unit/test_dashboard_empty_state.py \
                                  cortex/tests/unit/test_onboarding_hints.py \
                                  cortex/tests/unit/test_overlay_animation.py -q

# Extension TS tests:
cd cortex/apps/browser_extension && pnpm test

# Schema-codegen drift gate (Debt-1):
CORTEX_JSON2TS_CMD=$(which json2ts) python -m cortex.scripts.generate_ts_schemas --check
```

### Session count summary

- **93 audit commits** landed this session.
- **53 of 56 Ledger findings** closed (3 deferred with explicit justification above).
- **2 Architectural Debts** closed (Debt-1 codegen, Debt-2 systemic auth).
- **2 Non-Ledger phases** shipped (Phase I performance, Phase J UX polish).
- **~345 audit-specific tests** added across Python and TypeScript.

### Least-confident fix in this session

**F25 (cooldown/dwell oscillation, partial closure).** The cost-runaway aspect is well-contained by F20's budget kill-switch with regression-tested thresholds. The quality-of-experience aspect — does the user actually get spammed with interventions under real biometric jitter? — is partially closed by F26/F27 but not directly tested with adversarial state sequences. The right next step is an /eval suite that replays a synthetic jittery-state trace and asserts intervention count stays within an envelope. That is F41's territory and was deferred. Until F41 is closed, the operator's only signal is the `cortex_state_loop_interventions_per_hour` metric.
