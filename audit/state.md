# Audit State Pointer

**Phase:** 2 (remediation in progress)
**Next finding to address:** F03 (track all asyncio.create_task in state loop)
**Last finding closed:** F02 (atomic session report write)
**Last commit:** `audit F02: atomic session report write at shutdown`
**Resume protocol on fresh invocation:**

1. Read `audit/findings.md` — authoritative Ledger.
2. Read `audit/execution-log.md` — what has already shipped.
3. Read this file — what is next.
4. Pick up from the finding ID below; do not re-diagnose.

## Execution order (locked at end of Phase 1)

Locked sequence — see findings.md §VIII for rationale.

```
F19  →  F07  →  F08  →  F02  →  F03  →  F38  →  F39  →  F53
 →  F01  →  F09  →  F10  →  F11  →  F12
 →  F20  →  F30  →  F25  →  F26  →  F18  →  F27
 →  F06  →  F16  →  F17  →  F22  →  F34
 → (then maintainability tier — F31, F32, F33, F35, F36, F46, F47, F48, F49, F50, F51, F52, F54, F55, F56)
 → (cross-cutting tier — F40, F41, F42–F45, requires Debt-1 design)
```

## Out of scope for Phase 2 (deferred — own design doc required)

- **Debt-1** (shared schema source of truth) — generator + codegen.
- **Debt-2** (capability-token trust model) — supersedes F07/F08 piecemeal patches; for Phase 2 we ship the localhost token gate as a tactical fix, **not** the full client-bootstrap rework.

## Pointer

Next: **F19** — end-to-end correlation IDs. Adds a `request_id` to `WSMessage`, threads it through `controller.py`, `routes.py`, `websocket_server.py`, `anthropic_planner.py`, and back into the popup/overlay error surfaces. Test: a single user click on the popup produces log lines from `popup.tsx`, `background.ts`, `native_host.py`, `routes.py`, `state_engine`, and `anthropic_planner.py` — all sharing one `request_id`.
