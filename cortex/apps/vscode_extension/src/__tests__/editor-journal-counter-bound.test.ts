/**
 * The editor journal's `attempt_counters` must be bounded where it grows.
 *
 * Exact twin of the browser-extension defect: `_makeReceipt` inserted one key
 * per (authorization_id, action_id, phase) triple and bounded only each key's
 * *value* (<= 100), never the key count, while `validateEditorJournal`
 * rejected the whole journal above MAX_EDITOR_COUNTERS (2048). Counters were
 * reclaimed only for operations retired through `acknowledgeTransactionState`,
 * so orphans accumulated without limit. Past the cap the journal stopped
 * loading, and `_readJournal` throws rather than repairing — so every
 * subsequent editor apply and restore failed permanently, with no way back.
 *
 * The browser half was fixed in b689a72; that commit touched only the browser
 * extension, leaving this twin live.
 */

import * as vscode from "vscode";
import { EditorTransactionAdapter } from "../editor-transaction-adapter";

const BOOT_ID = "11111111-1111-4111-8111-111111111111";
const JOURNAL_KEY = "cortex.interventionTransactionJournal.v1";
const MAX_EDITOR_COUNTERS = 2_048;

function memento(seed: Record<string, unknown> = {}): vscode.Memento {
    const values = new Map<string, unknown>(Object.entries(seed));
    return {
        keys: () => [...values.keys()],
        get: ((key: string, fallback?: unknown) =>
            values.has(key) ? values.get(key) : fallback) as vscode.Memento["get"],
        update: async (key: string, value: unknown) => {
            values.set(key, value);
        },
    };
}

function journalWithCounters(count: number) {
    const attempt_counters: Record<string, number> = {};
    for (let i = 0; i < count; i += 1) {
        attempt_counters[`auth-${i}:action-${i}:apply`] = 1;
    }
    return {
        schema_version: "1",
        consumed_authorizations: {},
        operations: {},
        attempt_counters,
        receipt_outbox: [],
    };
}

function adapterOver(journal: unknown) {
    const store = memento({ [JOURNAL_KEY]: journal });
    const adapter = new EditorTransactionAdapter(
        store, BOOT_ID, () => undefined, () => true,
    );
    return { adapter, store };
}

describe("editor journal attempt_counters", () => {
    it("repairs an over-cap journal instead of refusing to load it", async () => {
        // Exactly the state a long-running profile reached before the write
        // side was bounded: thousands of orphaned counters, no live operation.
        const { adapter, store } = adapterOver(
            journalWithCounters(MAX_EDITOR_COUNTERS + 500),
        );

        // The defect was that this threw "Cortex editor transaction journal is
        // corrupt", and kept throwing — every apply and restore dead for good.
        // Loading must succeed; the repaired map is written back on the next
        // journal write, not by this read-only path.
        await expect(adapter.flushPendingReceipts()).resolves.toBeUndefined();
        expect(store.get(JOURNAL_KEY)).toBeDefined();
    });

    it("still refuses an absurd counter map as corrupt", async () => {
        // The repair path must not become an unbounded-work vector.
        const { adapter } = adapterOver(
            journalWithCounters(MAX_EDITOR_COUNTERS * 8 + 1),
        );
        await expect(adapter.flushPendingReceipts()).rejects.toThrow(/corrupt/);
    });
});
