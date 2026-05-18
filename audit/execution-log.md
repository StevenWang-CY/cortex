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

(none yet — all original Ledger entries remain open)
