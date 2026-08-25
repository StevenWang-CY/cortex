import { beforeEach, describe, expect, it, vi } from "vitest";

import { canonicalJson, sha256Hex } from "../lib/intervention-transaction";
import { getLatestSocket } from "../test/mocks/websocket";

const BROWSER_INSTANCE_ID = "browser_test_instance_0001";

async function exactCommand() {
    const now = Date.now();
    const suggestedAction = {
        action_id: "open-boundary-1",
        action_type: "open_url",
        target: "https://example.com/reference",
        label: "Open the exact reference",
        reason: "Keep relevant material nearby",
        category: "recommended",
        reversible: true,
        metadata: {},
    };
    const action = {
        action_id: suggestedAction.action_id,
        ordinal: 0,
        executor: "browser",
        capability: "open_url",
        parameters_json: canonicalJson({ suggested_action: suggestedAction }),
        reverse_capability: "close_created_tab",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
    };
    const canonical = canonicalJson({
        actions: [action],
        intervention_id: "intervention-boundary",
        schema_version: "1",
    });
    const manifest = {
        schema_version: "1",
        intervention_id: "intervention-boundary",
        canonical_json: canonical,
        manifest_sha256: await sha256Hex(canonical),
        action_count: 1,
        created_at_unix_ms: now - 1_000,
        created_at_mono_ns: 1_000_000,
        expires_at_unix_ms: now + 299_000,
        ttl_ms: 300_000,
        boot_id: "22222222-2222-4222-8222-222222222222",
    };
    return {
        manifest,
        authorization: {
            authorization_id: "authz-boundary",
            authorization_request_id: "desktop-request",
            intervention_id: manifest.intervention_id,
            manifest_sha256: manifest.manifest_sha256,
            authorized_action_ids: [action.action_id],
            consent_revision: 2,
            authorization_kind: "user_confirmed",
            source_surface: "desktop",
            source_client_id: "desktop-client",
            requester_boot_id: "33333333-3333-4333-8333-333333333333",
            issued_at_unix_ms: now - 100,
            issued_at_mono_ns: 2_000_000,
            expires_at_unix_ms: now + 29_900,
            ttl_ms: 30_000,
            boot_id: "22222222-2222-4222-8222-222222222222",
            nonce: "b".repeat(32),
        },
        actions: [action],
    };
}

async function suggestedCommand(
    capability: "open_url" | "search_error" | "highlight_tab",
    suggestedAction: Record<string, unknown>,
) {
    const now = Date.now();
    const actionId = String(suggestedAction.action_id);
    const action = {
        action_id: actionId,
        ordinal: 0,
        executor: "browser",
        capability,
        parameters_json: canonicalJson({ suggested_action: suggestedAction }),
        reverse_capability: capability === "highlight_tab"
            ? "restore_active_tab"
            : "close_created_tab",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
    };
    const canonical = canonicalJson({
        actions: [action],
        intervention_id: "intervention-boundary",
        schema_version: "1",
    });
    const manifest = {
        schema_version: "1",
        intervention_id: "intervention-boundary",
        canonical_json: canonical,
        manifest_sha256: await sha256Hex(canonical),
        action_count: 1,
        created_at_unix_ms: now - 1_000,
        created_at_mono_ns: 1_000_000,
        expires_at_unix_ms: now + 299_000,
        ttl_ms: 300_000,
        boot_id: "22222222-2222-4222-8222-222222222222",
    };
    return {
        manifest,
        authorization: {
            authorization_id: `authz-${capability}`,
            authorization_request_id: "desktop-request",
            intervention_id: manifest.intervention_id,
            manifest_sha256: manifest.manifest_sha256,
            authorized_action_ids: [actionId],
            consent_revision: 2,
            authorization_kind: "user_confirmed",
            source_surface: "desktop",
            source_client_id: "desktop-client",
            requester_boot_id: "33333333-3333-4333-8333-333333333333",
            issued_at_unix_ms: now - 100,
            issued_at_mono_ns: 2_000_000,
            expires_at_unix_ms: now + 29_900,
            ttl_ms: 30_000,
            boot_id: "22222222-2222-4222-8222-222222222222",
            nonce: "c".repeat(32),
        },
        actions: [action],
    };
}

function restoreCommand(
    command: Awaited<ReturnType<typeof suggestedCommand>>,
    inversePayloadJson: string,
    restoreId: string,
) {
    const action = command.actions[0];
    return {
        restore_id: restoreId,
        intervention_id: command.manifest.intervention_id,
        manifest_sha256: command.manifest.manifest_sha256,
        reason: "user_undo",
        requested_at_unix_ms: Date.now(),
        requested_at_mono_ns: 3_000_000,
        boot_id: command.manifest.boot_id,
        actions: [{
            action_id: action.action_id,
            executor: "browser",
            reverse_capability: action.reverse_capability,
            inverse_payload_json: inversePayloadJson,
            original_authorization_id: command.authorization.authorization_id,
            owner_client_instance_id: BROWSER_INSTANCE_ID,
        }],
    };
}

async function bootAuthorizedBackground(): Promise<NonNullable<ReturnType<typeof getLatestSocket>>> {
    vi.resetModules();
    await globalThis.__cortexChrome.storage.local.set({
        cortex_client_instance_id_v1: BROWSER_INSTANCE_ID,
    });
    await import("../background");
    await new Promise((resolve) => setTimeout(resolve, 0));
    const socket = getLatestSocket();
    if (!socket) throw new Error("background WebSocket did not start");
    socket.__deliver({
        type: "INTERVENTION_TRIGGER",
        payload: {
            intervention_id: "intervention-boundary",
            level: "simplified_workspace",
            execution_mode: "authorized",
            headline: "Simplify this workspace",
            suggested_actions: [],
            ui_plan: { show_overlay: false, dim_background: false },
        },
        sequence: 1,
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    return socket;
}

function sentFrames(socket: NonNullable<ReturnType<typeof getLatestSocket>>) {
    return socket.sent.map((raw) => JSON.parse(raw) as {
        type: string;
        payload: Record<string, unknown>;
    });
}

describe("browser exact apply boundary", () => {
    beforeEach(() => {
        globalThis.__cortexChrome.tabs.create.mockResolvedValue({
            id: 9,
            url: "https://example.com/reference",
        });
        globalThis.__cortexChrome.tabs.get.mockResolvedValue({
            id: 9,
            url: "https://example.com/reference",
        });
    });

    it("executes a verified command once and returns typed idempotent receipts", async () => {
        const socket = await bootAuthorizedBackground();
        const command = await exactCommand();

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: command,
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));
        expect(globalThis.__cortexChrome.tabs.create).toHaveBeenCalledTimes(1);

        const firstReceipt = sentFrames(socket).find(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        );
        expect(firstReceipt?.payload).toMatchObject({
            intervention_id: "intervention-boundary",
            authorization_id: "authz-boundary",
        });
        expect(firstReceipt?.payload.receipts).toEqual([
            expect.objectContaining({
                attempt: 1,
                idempotency_key: "authz-boundary:open-boundary-1:apply:1",
            }),
        ]);
        const journalAfterApply = globalThis.__cortexChrome.storage.local
            .__peek().cortex_intervention_transaction_journal_v1 as {
                receipt_outbox: unknown[];
            };
        expect(journalAfterApply.receipt_outbox).toHaveLength(1);

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: command,
            sequence: 3,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));
        expect(globalThis.__cortexChrome.tabs.create).toHaveBeenCalledTimes(1);

        const receipts = sentFrames(socket).filter(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        );
        expect(receipts).toHaveLength(2);
        expect(receipts[1]?.payload.receipts).toEqual([
            expect.objectContaining({
                status: "already_complete",
                attempt: 2,
                idempotency_key: "authz-boundary:open-boundary-1:apply:2",
            }),
        ]);

        socket.__deliver({
            type: "INTERVENTION_TRANSACTION_STATE",
            payload: {
                intervention_id: "intervention-boundary",
                authorization_id: "authz-boundary",
                state: "applied",
            },
            sequence: 4,
        });
        await new Promise((resolve) => setTimeout(resolve, 20));
        const acknowledged = globalThis.__cortexChrome.storage.local
            .__peek().cortex_intervention_transaction_journal_v1 as {
                receipt_outbox: unknown[];
            };
        expect(acknowledged.receipt_outbox).toEqual([]);
    });

    it("checkpoints a uniquely staged tab before navigating to the authorized URL", async () => {
        let stagingUrl = "";
        globalThis.__cortexChrome.tabs.create.mockImplementation(
            async (properties: { url?: string }) => {
                stagingUrl = String(properties.url ?? "");
                return { id: 9, url: stagingUrl };
            },
        );
        globalThis.__cortexChrome.tabs.update.mockImplementation(
            async (tabId: number, properties: { url?: string }) => {
                const journal = globalThis.__cortexChrome.storage.local
                    .__peek().cortex_intervention_transaction_journal_v1 as {
                        operations: Record<string, { inverse_payload_json: string }>;
                    };
                expect(JSON.parse(
                    journal.operations["intervention-boundary:open-boundary-1"]
                        ?.inverse_payload_json ?? "{}",
                )).toMatchObject({ tabId: 9, stagingUrl });
                expect(tabId).toBe(9);
                expect(properties).toEqual({ url: "https://example.com/reference" });
                return { id: 9, url: properties.url };
            },
        );

        const socket = await bootAuthorizedBackground();
        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: await exactCommand(),
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));

        expect(stagingUrl).toMatch(
            /^about:blank#cortex-created-tab=[0-9a-f-]{36}$/i,
        );
        expect(globalThis.__cortexChrome.tabs.update).toHaveBeenCalledTimes(1);
        const receipt = sentFrames(socket).find(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        );
        expect(receipt?.payload.receipts).toEqual([
            expect.objectContaining({ status: "succeeded", verification: "verified" }),
        ]);
    });

    it("recovers a staged tab after a worker crash before the tab-id checkpoint", async () => {
        const socket = await bootAuthorizedBackground();
        const command = await suggestedCommand("open_url", {
            action_id: "open-crash-recovery",
            action_type: "open_url",
            target: "https://example.com/recovered",
            label: "Open exact recovery reference",
            reason: "Exercise crash recovery",
            category: "recommended",
            reversible: true,
            metadata: {},
        });
        const stagingUrl =
            "about:blank#cortex-created-tab=12345678-1234-4234-8234-123456789abc";
        const inverse = canonicalJson({
            createdAfterUnixMs: Date.now() - 10,
            stagingUrl,
            url: "https://example.com/recovered",
        });
        await globalThis.__cortexChrome.storage.local.set({
            cortex_intervention_transaction_journal_v1: {
                schema_version: "1",
                consumed_authorizations: {
                    [command.authorization.authorization_id]: {
                        manifest_sha256: command.manifest.manifest_sha256,
                        nonce: command.authorization.nonce,
                        consumed_at_unix_ms: Date.now() - 10,
                    },
                },
                operations: {
                    "intervention-boundary:open-crash-recovery": {
                        intervention_id: command.manifest.intervention_id,
                        manifest_sha256: command.manifest.manifest_sha256,
                        action_id: "open-crash-recovery",
                        authorization_id: command.authorization.authorization_id,
                        capability: "open_url",
                        state: "applying",
                        inverse_payload_json: inverse,
                        restore_progress_json: null,
                        after_fingerprint: null,
                        updated_at_unix_ms: Date.now() - 10,
                    },
                },
                attempt_counters: {},
                receipt_outbox: [],
            },
        });
        let stagedTabExists = true;
        globalThis.__cortexChrome.tabs.query.mockImplementation(async () =>
            stagedTabExists ? [{ id: 77, pendingUrl: stagingUrl }] : [],
        );
        globalThis.__cortexChrome.tabs.get.mockImplementation(async (tabId: number) => {
            if (tabId === 77 && stagedTabExists) {
                return { id: 77, pendingUrl: stagingUrl };
            }
            throw new Error("tab absent");
        });
        globalThis.__cortexChrome.tabs.remove.mockImplementation(async (tabId: number) => {
            expect(tabId).toBe(77);
            stagedTabExists = false;
        });

        socket.__deliver({
            type: "INTERVENTION_RESTORE",
            payload: {
                ...restoreCommand(command, "{}", "restore-crash-recovery"),
                reason: "partial_compensation",
            },
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 40));

        expect(globalThis.__cortexChrome.tabs.remove).toHaveBeenCalledWith(77);
        const receipt = sentFrames(socket).find(
            (frame) => frame.type === "INTERVENTION_RECEIPT"
                && frame.payload.authorization_id === "restore-crash-recovery",
        );
        expect(receipt?.payload.receipts).toEqual([
            expect.objectContaining({
                phase: "compensate",
                status: "succeeded",
                verification: "verified",
            }),
        ]);
    });

    it("records an already-active highlight as a verified no-op", async () => {
        globalThis.__cortexChrome.tabs.query.mockResolvedValue([{
            id: 42,
            active: true,
            currentWindow: true,
            url: "https://example.com/current",
            title: "Current tab",
            index: 0,
        }]);
        const socket = await bootAuthorizedBackground();
        const command = await suggestedCommand("highlight_tab", {
            action_id: "highlight-current",
            action_type: "highlight_tab",
            tab_index: 0,
            target: "",
            label: "Keep this tab active",
            reason: "It is already the exact target",
            category: "recommended",
            reversible: true,
            metadata: {},
        });

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: command,
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));

        expect(globalThis.__cortexChrome.tabs.update).not.toHaveBeenCalled();
        const batch = sentFrames(socket).find(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        );
        const receipt = (
            batch?.payload.receipts as Array<Record<string, unknown>>
        )[0];
        expect(receipt).toMatchObject({
            status: "already_complete",
            verification: "verified",
        });
        expect(JSON.parse(String(receipt.inverse_payload_json))).toMatchObject({
            noEffect: true,
            priorActiveTabId: 42,
            targetTabId: 42,
        });
    });

    it("reports an indeterminate effect and preserves its inverse when verification fails", async () => {
        globalThis.__cortexChrome.tabs.get.mockResolvedValue({
            id: 9,
            url: "https://user-navigated.example/",
        });
        const socket = await bootAuthorizedBackground();
        const command = await exactCommand();

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: command,
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));

        const receipt = sentFrames(socket).find(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        );
        const actionReceipt = (receipt?.payload.receipts as Array<Record<string, unknown>>)[0];
        expect(actionReceipt).toMatchObject({
            status: "failed",
            verification: "failed",
            error_code: "postcondition_unverified",
            retryable: true,
        });
        expect(JSON.parse(String(actionReceipt.inverse_payload_json))).toMatchObject({
            tabId: 9,
            url: "https://example.com/reference",
            cortexEffectMayExist: true,
        });

        const journal = globalThis.__cortexChrome.storage.local
            .__peek().cortex_intervention_transaction_journal_v1 as {
                operations: Record<string, {
                    state: string;
                    inverse_payload_json: string;
                }>;
            };
        expect(journal.operations["intervention-boundary:open-boundary-1"]?.state)
            .toBe("applying");
        expect(JSON.parse(
            journal.operations["intervention-boundary:open-boundary-1"]
                ?.inverse_payload_json ?? "{}",
        )).toMatchObject({ cortexEffectMayExist: true });
    });

    it("rejects command tampering before any Chrome capability call", async () => {
        const socket = await bootAuthorizedBackground();
        const command = await exactCommand();
        command.actions[0] = {
            ...command.actions[0],
            capability: "search_error",
        };

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: command,
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 20));

        expect(globalThis.__cortexChrome.tabs.create).not.toHaveBeenCalled();
        expect(sentFrames(socket).some(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        )).toBe(false);
    });

    it("rejects destructive and ownership-unsafe tab capabilities before Chrome mutation", async () => {
        const socket = await bootAuthorizedBackground();
        const command = await exactCommand();
        const unsupported = {
            ...command.actions[0],
            capability: "bookmark_and_close",
            reverse_capability: "reopen_from_bookmark",
        };
        const canonical = canonicalJson({
            actions: [unsupported],
            intervention_id: command.manifest.intervention_id,
            schema_version: "1",
        });
        command.actions = [unsupported];
        command.manifest = {
            ...command.manifest,
            canonical_json: canonical,
            manifest_sha256: await sha256Hex(canonical),
        };
        command.authorization = {
            ...command.authorization,
            manifest_sha256: command.manifest.manifest_sha256,
        };

        socket.__deliver({ type: "INTERVENTION_APPLY", payload: command, sequence: 2 });
        await new Promise((resolve) => setTimeout(resolve, 30));

        expect(globalThis.__cortexChrome.tabs.create).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.tabs.remove).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.tabs.group).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.bookmarks.create).not.toHaveBeenCalled();
        expect(sentFrames(socket).some(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        )).toBe(false);
    });

    it("does not misreport a failed created-tab removal as already restored", async () => {
        const targetUrl = "https://example.com/reference";
        globalThis.__cortexChrome.tabs.create.mockResolvedValue({ id: 9, url: targetUrl });
        globalThis.__cortexChrome.tabs.get.mockResolvedValue({ id: 9, url: targetUrl });
        const socket = await bootAuthorizedBackground();
        const command = await suggestedCommand("open_url", {
            action_id: "open-action",
            action_type: "open_url",
            target: targetUrl,
            label: "Open reference",
            reason: "Provide context",
            category: "recommended",
            reversible: true,
            metadata: {},
        });
        socket.__deliver({ type: "INTERVENTION_APPLY", payload: command, sequence: 2 });
        await new Promise((resolve) => setTimeout(resolve, 30));
        const applyReceipt = sentFrames(socket)
            .filter((frame) => frame.type === "INTERVENTION_RECEIPT")[0]
            ?.payload.receipts as Array<Record<string, unknown>>;
        expect(applyReceipt[0]).toMatchObject({ status: "succeeded" });

        globalThis.__cortexChrome.tabs.remove.mockRejectedValue(
            new Error("Chrome refused removal"),
        );
        socket.__deliver({
            type: "INTERVENTION_RESTORE",
            payload: restoreCommand(
                command,
                String(applyReceipt[0].inverse_payload_json),
                "restore-open-failure",
            ),
            sequence: 3,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));

        const restoreReceipt = sentFrames(socket)
            .filter((frame) => frame.type === "INTERVENTION_RECEIPT")[1]
            ?.payload.receipts as Array<Record<string, unknown>>;
        expect(restoreReceipt[0]).toMatchObject({
            status: "failed",
            verification: "failed",
            error_code: "restore_failed",
            retryable: true,
        });
    });

    it("restores a Cortex-created tab idempotently without touching later state", async () => {
        const targetUrl = "https://example.com/idempotent";
        let createdExists = false;
        globalThis.__cortexChrome.tabs.create.mockImplementation(async () => {
            createdExists = true;
            return { id: 9, url: targetUrl };
        });
        globalThis.__cortexChrome.tabs.get.mockImplementation(async (tabId: number) => {
            if (tabId === 9 && createdExists) return { id: 9, url: targetUrl };
            throw new Error("tab absent");
        });
        globalThis.__cortexChrome.tabs.remove.mockImplementation(async () => {
            createdExists = false;
        });

        const socket = await bootAuthorizedBackground();
        const command = await suggestedCommand("open_url", {
            action_id: "open-idempotent-action",
            action_type: "open_url",
            target: targetUrl,
            label: "Open exact reference",
            reason: "Provide context",
            category: "recommended",
            reversible: true,
            metadata: {},
        });
        socket.__deliver({ type: "INTERVENTION_APPLY", payload: command, sequence: 2 });
        await new Promise((resolve) => setTimeout(resolve, 30));
        const applyReceipt = sentFrames(socket)
            .filter((frame) => frame.type === "INTERVENTION_RECEIPT")[0]
            ?.payload.receipts as Array<Record<string, unknown>>;
        expect(applyReceipt[0]).toMatchObject({ status: "succeeded" });
        const inverse = String(applyReceipt[0].inverse_payload_json);

        socket.__deliver({
            type: "INTERVENTION_RESTORE",
            payload: restoreCommand(command, inverse, "restore-open-first"),
            sequence: 3,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));
        socket.__deliver({
            type: "INTERVENTION_RESTORE",
            payload: restoreCommand(command, inverse, "restore-open-retry"),
            sequence: 4,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));

        expect(globalThis.__cortexChrome.tabs.remove).toHaveBeenCalledTimes(1);
        const receipts = sentFrames(socket)
            .filter((frame) => frame.type === "INTERVENTION_RECEIPT");
        expect((receipts[1]?.payload.receipts as Array<Record<string, unknown>>)[0])
            .toMatchObject({ status: "succeeded", verification: "verified" });
        expect((receipts[2]?.payload.receipts as Array<Record<string, unknown>>)[0])
            .toMatchObject({ status: "already_complete", verification: "verified" });
    });

    it("returns a failed receipt when local suggestion-only mode denies apply", async () => {
        const socket = await bootAuthorizedBackground();
        socket.__deliver({
            type: "SETTINGS_SYNC",
            payload: { execution_mode: "suggest_only" },
            sequence: 1,
        });
        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: await exactCommand(),
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 20));

        expect(globalThis.__cortexChrome.tabs.create).not.toHaveBeenCalled();
        const receipt = sentFrames(socket).find(
            (frame) => frame.type === "INTERVENTION_RECEIPT",
        );
        expect(receipt?.payload.receipts).toEqual([
            expect.objectContaining({
                status: "failed",
                error_code: "execution_mode_denied",
            }),
        ]);
    });

    it("proves no effect when compensation follows an apply write that never arrived", async () => {
        const socket = await bootAuthorizedBackground();
        const command = await suggestedCommand("open_url", {
            action_id: "open-never-delivered",
            action_type: "open_url",
            target: "https://example.com/reference",
            label: "Open the exact reference",
            reason: "Keep the relevant material nearby",
            reversible: true,
            metadata: {},
        });
        const restore = {
            ...restoreCommand(command, "{}", "restore-never-delivered-browser"),
            reason: "partial_compensation",
        };

        socket.__deliver({
            type: "INTERVENTION_RESTORE",
            payload: restore,
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));

        expect(globalThis.__cortexChrome.tabs.create).not.toHaveBeenCalled();
        const batch = sentFrames(socket).find(
            (frame) => frame.type === "INTERVENTION_RECEIPT"
                && frame.payload.authorization_id === "restore-never-delivered-browser",
        );
        expect(batch?.payload.receipts).toEqual([
            expect.objectContaining({
                phase: "compensate",
                status: "already_complete",
                verification: "verified",
            }),
        ]);
        const receipt = (batch?.payload.receipts as Array<Record<string, unknown>>)[0];
        expect(receipt).not.toHaveProperty("error_code");
        expect(JSON.parse(String(receipt.inverse_payload_json))).toEqual({
            noEffect: true,
        });
    });

    it("fails closed when the durable transaction journal is corrupt", async () => {
        const socket = await bootAuthorizedBackground();
        await globalThis.__cortexChrome.storage.local.set({
            cortex_intervention_transaction_journal_v1: {
                schema_version: "unexpected",
            },
        });

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: await exactCommand(),
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 20));

        expect(globalThis.__cortexChrome.tabs.create).not.toHaveBeenCalled();
    });

    it("rejects invalid receipt counters before a workspace effect", async () => {
        const socket = await bootAuthorizedBackground();
        await globalThis.__cortexChrome.storage.local.set({
            cortex_intervention_transaction_journal_v1: {
                schema_version: "1",
                consumed_authorizations: {},
                operations: {},
                attempt_counters: { "forged:counter:apply": 101 },
                receipt_outbox: [],
            },
        });

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: await exactCommand(),
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 20));

        expect(globalThis.__cortexChrome.tabs.create).not.toHaveBeenCalled();
    });

    it("preflights a full valid receipt outbox before a workspace effect", async () => {
        const socket = await bootAuthorizedBackground();
        const now = Date.now();
        const receiptOutbox = Array.from({ length: 256 }, (_, index) => {
            const authorizationId = `queued-auth-${index}`;
            const receipt = {
                receipt_id: `queued-receipt-${index}`,
                intervention_id: `queued-intervention-${index}`,
                authorization_id: authorizationId,
                manifest_sha256: "a".repeat(64),
                action_id: `queued-action-${index}`,
                phase: "apply",
                attempt: 1,
                idempotency_key: `${authorizationId}:queued-action-${index}:apply:1`,
                status: "failed",
                started_at_unix_ms: now,
                ended_at_unix_ms: now,
                started_at_mono_ns: 1,
                ended_at_mono_ns: 1,
                duration_ms: 0,
                boot_id: "22222222-2222-4222-8222-222222222222",
                inverse_payload_json: "{}",
                verification: "failed",
                verification_detail: "queued",
                after_fingerprint: null,
                error_code: "queued",
                error_message: "queued",
                retryable: true,
                source_client_type: null,
                source_client_id: null,
            };
            return {
                intervention_id: receipt.intervention_id,
                manifest_sha256: receipt.manifest_sha256,
                authorization_id: authorizationId,
                receipts: [receipt],
            };
        });
        await globalThis.__cortexChrome.storage.local.set({
            cortex_intervention_transaction_journal_v1: {
                schema_version: "1",
                consumed_authorizations: {},
                operations: {},
                attempt_counters: {},
                receipt_outbox: receiptOutbox,
            },
        });

        socket.__deliver({
            type: "INTERVENTION_APPLY",
            payload: await exactCommand(),
            sequence: 2,
        });
        await new Promise((resolve) => setTimeout(resolve, 30));

        expect(globalThis.__cortexChrome.tabs.create).not.toHaveBeenCalled();
    });

    it("cannot authorize an action whose displayed copy differs from the manifest", async () => {
        vi.resetModules();
        await import("../background");
        await new Promise((resolve) => setTimeout(resolve, 0));
        const socket = getLatestSocket();
        if (!socket) throw new Error("background WebSocket did not start");
        const displayed = {
            action_id: "open-presentation",
            action_type: "open_url",
            target: "https://example.com/reference",
            label: "Open this harmless reference",
            reason: "Provide exact context",
            category: "recommended",
            reversible: true,
            group_id: null,
            metadata: {},
            catalog_id: null,
        };
        const command = await suggestedCommand("open_url", {
            ...displayed,
            label: "Open a different displayed reference",
        });
        socket.__deliver({
            type: "INTERVENTION_TRIGGER",
            payload: {
                intervention_id: "intervention-boundary",
                intervention_type: "overlay_only",
                execution_mode: "authorized",
                headline: "Review this exact action",
                suggested_actions: [displayed],
                action_manifest: command.manifest,
                ui_plan: { show_overlay: true },
            },
            sequence: 1,
        });
        await new Promise((resolve) => setTimeout(resolve, 0));

        const listener = globalThis.__cortexChrome.runtime.onMessage.addListener
            .mock.calls[0][0] as (
                message: Record<string, unknown>,
                sender: unknown,
                respond: (value: unknown) => void,
            ) => boolean | void;
        let response: unknown;
        listener(
            {
                type: "EXECUTE_ACTION",
                intervention_id: "intervention-boundary",
                action: displayed,
            },
            undefined,
            (value) => { response = value; },
        );
        await new Promise((resolve) => setTimeout(resolve, 10));

        expect(response).toMatchObject({
            success: false,
            message: expect.stringContaining("immutable manifest"),
        });
        expect(sentFrames(socket).some(
            (frame) => frame.type === "INTERVENTION_AUTHORIZE",
        )).toBe(false);
    });
});
