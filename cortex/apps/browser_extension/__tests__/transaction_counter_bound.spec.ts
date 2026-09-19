/**
 * `attempt_counters` must be bounded where it grows, and a journal already
 * over the cap must repair rather than brick.
 *
 * `makeActionReceipt` inserted a key per (authorization_id, action_id, phase)
 * triple and bounded only each key's *value* (<= 100), never the key count,
 * while `validateBrowserTransactionJournal` rejected the whole journal above
 * MAX_TRANSACTION_COUNTERS (2048). Counters were reclaimed only for
 * operations retired through `acknowledgeReceipts`, so orphans accumulated
 * without limit. Past 2048 the journal stopped loading — and since
 * `readTransactionJournal` throws rather than repairing, every intervention
 * apply and restore failed permanently, with no path back.
 *
 * Every sibling collection is bounded at its write site: `receipt_outbox` and
 * `operations` refuse to exceed their caps, `consumed_authorizations` trims to
 * 256. This pins `attempt_counters` to the same contract.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const MAX_TRANSACTION_COUNTERS = 2_048;

// The chrome fake is installed in the shared ``beforeEach``, and importing
// the worker touches ``chrome`` at module scope, so the import has to happen
// inside a test rather than at the top level.
let _trimAttemptCounters: typeof import("../background")._trimAttemptCounters;
let _validateBrowserTransactionJournal:
    typeof import("../background")._validateBrowserTransactionJournal;

beforeEach(async () => {
    vi.resetModules();
    const bg = await import("../background");
    _trimAttemptCounters = bg._trimAttemptCounters;
    _validateBrowserTransactionJournal = bg._validateBrowserTransactionJournal;
});

function operation(authorizationId: string, actionId: string) {
    return {
        intervention_id: `int-${actionId}`,
        action_id: actionId,
        manifest_sha256: "a".repeat(64),
        authorization_id: authorizationId,
        capability: "open_url",
        state: "applied",
        inverse_payload_json: "{}",
        after_fingerprint: null,
        updated_at_unix_ms: 1_800_000_000_000,
    };
}

function journalWith(counterCount: number, operations: Record<string, unknown>) {
    const attempt_counters: Record<string, number> = {};
    for (let i = 0; i < counterCount; i += 1) {
        attempt_counters[`auth-${i}:action-${i}:apply`] = 1;
    }
    return {
        schema_version: "1",
        consumed_authorizations: {},
        operations,
        attempt_counters,
        receipt_outbox: [],
    };
}

describe("attempt_counters is bounded", () => {
    it("drops orphaned counters before any live one", () => {
        const counters: Record<string, number> = {
            "auth-dead:action-dead:apply": 7,
            "auth-live:action-live:apply": 3,
        };
        const operations = {
            "int-action-live:action-live": operation("auth-live", "action-live"),
        } as unknown as Parameters<typeof _trimAttemptCounters>[1];

        _trimAttemptCounters(counters, operations, 1);

        expect(Object.keys(counters)).toEqual(["auth-live:action-live:apply"]);
        // The surviving counter keeps its tally — trimming must not hand a
        // live action a fresh retry allowance while its operation is running.
        expect(counters["auth-live:action-live:apply"]).toBe(3);
    });

    it("falls back to oldest-first once every counter is live", () => {
        const counters: Record<string, number> = {};
        const operations: Record<string, unknown> = {};
        for (let i = 0; i < 5; i += 1) {
            counters[`auth-${i}:action-${i}:apply`] = 1;
            operations[`int-action-${i}:action-${i}`] = operation(`auth-${i}`, `action-${i}`);
        }

        _trimAttemptCounters(
            counters,
            operations as unknown as Parameters<typeof _trimAttemptCounters>[1],
            2,
        );

        // Insertion order makes Object.keys a FIFO queue.
        expect(Object.keys(counters)).toEqual([
            "auth-3:action-3:apply",
            "auth-4:action-4:apply",
        ]);
    });

    it("keeps a restore counter, which is keyed by restore id not authorization id", () => {
        // An apply receipt is stamped with the authorization id; a RESTORE
        // receipt is stamped with the restore id (`authorizationId:
        // verified.command.restore_id`). Deriving expected keys from
        // `operation.authorization_id` therefore never matched a live restore
        // counter, so the trim treated it as an orphan and silently reset that
        // restore's retry budget. Liveness is matched on the action id, which
        // is stable across every phase.
        const counters: Record<string, number> = {
            "auth-live:action-live:apply": 2,
            "restore-abc:action-live:restore": 5,
            "restore-abc:action-live:compensate": 1,
            "auth-dead:action-dead:apply": 9,
        };
        const operations = {
            "int-action-live:action-live": operation("auth-live", "action-live"),
        } as unknown as Parameters<typeof _trimAttemptCounters>[1];

        // Limit 3: enough to force the orphan out, not enough to trigger the
        // oldest-first fallback on the three live counters.
        _trimAttemptCounters(counters, operations, 3);

        expect(Object.keys(counters).sort()).toEqual([
            "auth-live:action-live:apply",
            "restore-abc:action-live:compensate",
            "restore-abc:action-live:restore",
        ]);
        // Retry budgets survive intact — that is the point of keeping them.
        expect(counters["restore-abc:action-live:restore"]).toBe(5);
    });

    it("leaves a journal under the limit untouched", () => {
        const counters: Record<string, number> = { "a:b:apply": 4 };
        _trimAttemptCounters(
            counters,
            {} as unknown as Parameters<typeof _trimAttemptCounters>[1],
            MAX_TRANSACTION_COUNTERS,
        );
        expect(counters).toEqual({ "a:b:apply": 4 });
    });

    it("repairs an over-cap journal instead of rejecting it", () => {
        // Exactly the state a long-running profile reached before the write
        // side was bounded: thousands of orphaned counters, no live operation.
        const raw = journalWith(MAX_TRANSACTION_COUNTERS + 500, {});

        const journal = _validateBrowserTransactionJournal(raw);

        expect(Object.keys(journal.attempt_counters).length)
            .toBeLessThanOrEqual(MAX_TRANSACTION_COUNTERS);
        // The rest of the journal must survive the repair intact.
        expect(journal.schema_version).toBe("1");
        expect(journal.receipt_outbox).toEqual([]);
    });

    it("still rejects an absurd counter map as corrupt", () => {
        // The repair path must not become an unbounded-work vector.
        const raw = journalWith(MAX_TRANSACTION_COUNTERS * 8 + 1, {});
        expect(() => _validateBrowserTransactionJournal(raw)).toThrow(/corrupt/);
    });
});
