/**
 * Apply feedback in the worker: one authorization per intervention even
 * when two surfaces click at once, an ``INTERVENTION_APPLIED`` broadcast
 * that carries the same outcome the requester receives (and precedes it),
 * no ``INTERVENTION_RESTORE`` faked locally, and no ``engaged`` sent on
 * apply (the daemon would restore the change seconds later).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { canonicalJson, sha256Hex } from "../lib/intervention-transaction";
import { getLatestSocket } from "../test/mocks/websocket";

type Listener = (
    message: Record<string, unknown>,
    sender: unknown,
    sendResponse: (response: unknown) => void,
) => unknown;

const SUGGESTED = {
    action_id: "open-apply-1",
    action_type: "open_url",
    target: "https://example.com/reference",
    label: "Open the reference",
    reason: "Keep it nearby",
    category: "recommended",
    reversible: true,
    metadata: {},
};

async function manifest() {
    const now = Date.now();
    const action = {
        action_id: SUGGESTED.action_id,
        ordinal: 0,
        executor: "browser",
        capability: "open_url",
        parameters_json: canonicalJson({ suggested_action: SUGGESTED }),
        reverse_capability: "close_created_tab",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
    };
    const canonical = canonicalJson({
        actions: [action],
        intervention_id: "intervention-apply",
        schema_version: "1",
    });
    return {
        schema_version: "1",
        intervention_id: "intervention-apply",
        canonical_json: canonical,
        manifest_sha256: await sha256Hex(canonical),
        action_count: 1,
        created_at_unix_ms: now - 1_000,
        created_at_mono_ns: 1_000_000,
        expires_at_unix_ms: now + 299_000,
        ttl_ms: 300_000,
        boot_id: "22222222-2222-4222-8222-222222222222",
    };
}

function frames(socket: NonNullable<ReturnType<typeof getLatestSocket>>) {
    return socket.sent.map((raw) => JSON.parse(raw) as { type: string; payload: Record<string, unknown> });
}

function broadcasts(): Array<Record<string, unknown>> {
    return globalThis.__cortexChrome.runtime.sendMessage.mock.calls
        .map((call) => call[0] as Record<string, unknown>);
}

async function boot() {
    vi.resetModules();
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
    const socket = getLatestSocket()!;
    if (socket.readyState === WebSocket.CONNECTING) socket.__open();
    await socket.__deliver({
        type: "INTERVENTION_TRIGGER",
        payload: {
            intervention_id: "intervention-apply",
            level: "simplified_workspace",
            execution_mode: "authorized",
            headline: "Open the reference",
            situation_summary: "One tab would help.",
            primary_focus: "reference",
            micro_steps: [{ text: "Open it", status: "pending" }],
            suggested_actions: [SUGGESTED],
            action_manifest: await manifest(),
            ui_plan: { show_overlay: false, dim_background: false },
        },
        sequence: 1,
    });
    await new Promise((r) => setTimeout(r, 0));
    const listener = globalThis.__cortexChrome.runtime.onMessage.addListener.mock
        .calls[0][0] as Listener;
    return { socket, listener };
}

describe("apply feedback (worker)", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("sends one INTERVENTION_AUTHORIZE for concurrent clicks and broadcasts the outcome before answering", async () => {
        const { socket, listener } = await boot();
        globalThis.__cortexChrome.runtime.sendMessage.mockClear();

        const responses: Array<{ raw: unknown; appliedBroadcastSeen: boolean }> = [];
        const respond = (raw: unknown) => responses.push({
            raw,
            appliedBroadcastSeen: broadcasts().some((m) => m.type === "INTERVENTION_APPLIED"),
        });
        listener({ type: "EXECUTE_ALL_RECOMMENDED", intervention_id: "intervention-apply" }, {}, respond);
        listener({ type: "EXECUTE_ALL_RECOMMENDED", intervention_id: "intervention-apply" }, {}, respond);
        for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));

        const authorize = frames(socket).filter((f) => f.type === "INTERVENTION_AUTHORIZE");
        expect(authorize).toHaveLength(1);
        const requestId = authorize[0].payload.authorization_request_id as string;

        await socket.__deliver({
            type: "INTERVENTION_TRANSACTION_STATE",
            payload: {
                intervention_id: "intervention-apply",
                authorization_request_id: requestId,
                authorization_id: "authz-apply",
                state: "applied",
                receipt_results: [{
                    action_id: SUGGESTED.action_id,
                    status: "succeeded",
                    detail: "Exact Cortex-created tab verified",
                    reversible: true,
                }],
            },
            sequence: 2,
        });
        for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));

        expect(responses).toHaveLength(2);
        for (const { raw, appliedBroadcastSeen } of responses) {
            const response = raw as { ok: boolean; outcome: { phase: string; applied: number } };
            expect(response.ok).toBe(true);
            expect(response.outcome).toMatchObject({ phase: "applied", applied: 1, total: 1 });
            expect(appliedBroadcastSeen).toBe(true);
        }
        const applied = broadcasts().filter((m) => m.type === "INTERVENTION_APPLIED");
        expect(applied).toHaveLength(1);
        expect(applied[0].outcome).toMatchObject({ phase: "applied" });
        expect(broadcasts().some((m) => m.type === "INTERVENTION_RESTORE")).toBe(false);
        expect(frames(socket).some((f) =>
            f.type === "USER_ACTION" && f.payload.action === "engaged")).toBe(false);
    });

    it("answers a stale intervention with a failed outcome and no fake restore", async () => {
        const { listener } = await boot();
        globalThis.__cortexChrome.runtime.sendMessage.mockClear();
        let raw: unknown;
        listener({ type: "EXECUTE_ALL_RECOMMENDED", intervention_id: "someone-else" }, {}, (r) => { raw = r; });
        for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0));
        const response = raw as { ok: boolean; outcome: { phase: string; reason: string | null } };
        expect(response.ok).toBe(false);
        expect(response.outcome.phase).toBe("failed");
        expect(response.outcome.reason).toBe("this suggestion has expired");
        expect(broadcasts().some((m) => m.type === "INTERVENTION_RESTORE")).toBe(false);
    });
});
