import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import type { InterventionReceiptBatch } from "../generated/cortex_schemas";
import { EditorTransactionAdapter } from "../editor-transaction-adapter";
import { canonicalJson, sha256Hex } from "../intervention-transaction";

const BOOT_ID = "11111111-1111-4111-8111-111111111111";
const INSTANCE_ID = `vscode_${BOOT_ID}`;

function selection(line: number, character = 0) {
    const point = { line, character };
    return { anchor: point, active: point };
}

function editor(filePath: string, line: number, version = 1) {
    return {
        document: {
            uri: vscode.Uri.file(filePath),
            version,
        },
        selection: selection(line),
    };
}

function memento(): vscode.Memento {
    const values = new Map<string, unknown>();
    return {
        keys: () => [...values.keys()],
        get: ((key: string, fallback?: unknown) =>
            values.has(key) ? values.get(key) : fallback) as vscode.Memento["get"],
        update: async (key: string, value: unknown) => {
            values.set(key, value);
        },
    };
}

function exactCommand(targetPath: string) {
    const now = Date.now();
    const action = {
        action_id: "resume-exact",
        ordinal: 0,
        executor: "editor",
        capability: "resume_last_active_file",
        parameters_json: canonicalJson({
            suggested_action: {
                action_id: "resume-exact",
                action_type: "resume_last_active_file",
                target: `${targetPath}:4`,
            },
        }),
        reverse_capability: "restore_active_file",
        workspace_mutation: true,
        required_consent_level: 3,
        source: "suggested_action",
    };
    const canonical = canonicalJson({
        actions: [action],
        intervention_id: "intervention-exact-editor",
        schema_version: "1",
    });
    const digest = sha256Hex(canonical);
    return {
        manifest: {
            schema_version: "1",
            intervention_id: "intervention-exact-editor",
            canonical_json: canonical,
            manifest_sha256: digest,
            action_count: 1,
            created_at_unix_ms: now - 1_000,
            created_at_mono_ns: 1_000_000,
            expires_at_unix_ms: now + 299_000,
            ttl_ms: 300_000,
            boot_id: "22222222-2222-4222-8222-222222222222",
        },
        authorization: {
            authorization_id: "authz-exact-editor",
            authorization_request_id: "request-exact-editor",
            intervention_id: "intervention-exact-editor",
            manifest_sha256: digest,
            authorized_action_ids: ["resume-exact"],
            consent_revision: 3,
            authorization_kind: "user_confirmed",
            source_surface: "vscode",
            source_client_id: INSTANCE_ID,
            requester_boot_id: BOOT_ID,
            issued_at_unix_ms: now - 100,
            issued_at_mono_ns: 2_000_000,
            expires_at_unix_ms: now + 29_900,
            ttl_ms: 30_000,
            boot_id: "22222222-2222-4222-8222-222222222222",
            nonce: "a".repeat(32),
        },
        actions: [action],
    };
}

describe("EditorTransactionAdapter", () => {
    let workspacePath: string;
    let priorPath: string;
    let targetPath: string;
    let active: ReturnType<typeof editor>;
    let sent: InterventionReceiptBatch[];

    beforeEach(() => {
        workspacePath = fs.mkdtempSync(path.join(os.tmpdir(), "cortex-editor-test-"));
        priorPath = path.join(workspacePath, "prior.ts");
        targetPath = path.join(workspacePath, "target.ts");
        fs.writeFileSync(priorPath, "const prior = true;\n");
        fs.writeFileSync(targetPath, "\n\n\nconst target = true;\n");
        active = editor(priorPath, 0);
        sent = [];
        Object.defineProperty(vscode.window, "activeTextEditor", {
            configurable: true,
            get: () => active,
        });
        jest.mocked(vscode.workspace.getWorkspaceFolder).mockImplementation(
            () => ({
                uri: vscode.Uri.file(workspacePath),
                name: "test",
                index: 0,
            }),
        );
        jest.mocked(vscode.window.showTextDocument).mockImplementation(
            async (uri: vscode.Uri, options?: vscode.TextDocumentShowOptions) => {
                const line = options?.selection?.end.line ?? 0;
                active = editor(uri.fsPath, line, 2);
                return active as unknown as vscode.TextEditor;
            },
        );
    });

    afterEach(() => {
        fs.rmSync(workspacePath, { recursive: true, force: true });
        jest.clearAllMocks();
    });

    it("persists intent before apply, rejects replay, and restores exact prior focus", async () => {
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );
        const command = exactCommand(targetPath);

        await adapter.handleApply(command);
        expect(vscode.window.showTextDocument).toHaveBeenCalledTimes(1);
        expect(active.document.uri.fsPath).toBe(fs.realpathSync(targetPath));
        expect(sent[0].receipts[0]).toMatchObject({
            status: "succeeded",
            verification: "verified",
            attempt: 1,
            idempotency_key: "authz-exact-editor:resume-exact:apply:1",
        });

        await adapter.handleApply(command);
        expect(vscode.window.showTextDocument).toHaveBeenCalledTimes(1);
        expect(sent[1].receipts[0]).toMatchObject({
            status: "already_complete",
            attempt: 2,
            idempotency_key: "authz-exact-editor:resume-exact:apply:2",
        });

        const inverse = sent[0].receipts[0].inverse_payload_json;
        await adapter.handleRestore({
            restore_id: "restore-exact-editor",
            intervention_id: command.manifest.intervention_id,
            manifest_sha256: command.manifest.manifest_sha256,
            reason: "user_undo",
            requested_at_unix_ms: Date.now(),
            requested_at_mono_ns: 3_000_000,
            boot_id: command.manifest.boot_id,
            actions: [{
                action_id: "resume-exact",
                executor: "editor",
                reverse_capability: "restore_active_file",
                inverse_payload_json: inverse,
                original_authorization_id: "authz-exact-editor",
                owner_client_instance_id: INSTANCE_ID,
            }],
        });

        expect(vscode.window.showTextDocument).toHaveBeenCalledTimes(2);
        expect(active.document.uri.fsPath).toBe(fs.realpathSync(priorPath));
        expect(sent[2].receipts[0]).toMatchObject({
            phase: "restore",
            status: "succeeded",
            verification: "verified",
        });
    });

    it("rejects out-of-workspace targets before invoking an editor capability", async () => {
        jest.mocked(vscode.workspace.getWorkspaceFolder).mockReturnValue(undefined);
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );
        await adapter.handleApply(exactCommand(targetPath));

        expect(vscode.window.showTextDocument).not.toHaveBeenCalled();
        expect(sent[0].receipts[0]).toMatchObject({
            status: "failed",
            error_code: "editor_capability_failed",
        });
    });

    it("rejects apply before mutation when no reversible prior editor exists", async () => {
        active = undefined as unknown as ReturnType<typeof editor>;
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );

        await adapter.handleApply(exactCommand(targetPath));

        expect(vscode.window.showTextDocument).not.toHaveBeenCalled();
        expect(sent[0].receipts[0]).toMatchObject({
            status: "failed",
            error_code: "editor_capability_failed",
            retryable: false,
        });
    });

    it("restores the exact prior cursor when Cortex focused another line in the same file", async () => {
        active = editor(targetPath, 0);
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );
        const command = exactCommand(targetPath);
        await adapter.handleApply(command);
        expect(active.selection.active.line).toBe(3);

        await adapter.handleRestore({
            restore_id: "restore-same-file",
            intervention_id: command.manifest.intervention_id,
            manifest_sha256: command.manifest.manifest_sha256,
            reason: "user_undo",
            requested_at_unix_ms: Date.now(),
            requested_at_mono_ns: 3_000_000,
            boot_id: command.manifest.boot_id,
            actions: [{
                action_id: "resume-exact",
                executor: "editor",
                reverse_capability: "restore_active_file",
                inverse_payload_json: sent[0].receipts[0].inverse_payload_json,
                original_authorization_id: "authz-exact-editor",
                owner_client_instance_id: INSTANCE_ID,
            }],
        });

        expect(active.document.uri.fsPath).toBe(fs.realpathSync(targetPath));
        expect(active.selection.active.line).toBe(0);
        expect(sent[1].receipts[0]).toMatchObject({
            status: "succeeded",
            verification: "verified",
        });
    });

    it("verifies user supersession when every editor was closed before restore", async () => {
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );
        const command = exactCommand(targetPath);
        await adapter.handleApply(command);
        const inverse = sent[0].receipts[0].inverse_payload_json;

        active = undefined as unknown as ReturnType<typeof editor>;
        await adapter.handleRestore({
            restore_id: "restore-after-editor-close",
            intervention_id: command.manifest.intervention_id,
            manifest_sha256: command.manifest.manifest_sha256,
            reason: "user_undo",
            requested_at_unix_ms: Date.now(),
            requested_at_mono_ns: 3_000_000,
            boot_id: command.manifest.boot_id,
            actions: [{
                action_id: "resume-exact",
                executor: "editor",
                reverse_capability: "restore_active_file",
                inverse_payload_json: inverse,
                original_authorization_id: "authz-exact-editor",
                owner_client_instance_id: INSTANCE_ID,
            }],
        });

        expect(vscode.window.showTextDocument).toHaveBeenCalledTimes(1);
        expect(sent[1].receipts[0]).toMatchObject({
            phase: "restore",
            status: "already_complete",
            verification: "verified",
        });
        expect(sent[1].receipts[0].after_fingerprint).toMatch(/^[0-9a-f]{64}$/);
    });

    it("records an already-active exact file position as a verified no-op", async () => {
        active = editor(targetPath, 3);
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );

        await adapter.handleApply(exactCommand(targetPath));

        expect(vscode.window.showTextDocument).not.toHaveBeenCalled();
        expect(sent[0].receipts[0]).toMatchObject({
            status: "already_complete",
            verification: "verified",
        });
        expect(JSON.parse(
            String(sent[0].receipts[0].inverse_payload_json),
        )).toMatchObject({
            noEffect: true,
            targetPath: fs.realpathSync(targetPath),
            targetLine: 4,
        });
    });

    it("compensates a wrong-line editor result before reporting apply failure", async () => {
        jest.mocked(vscode.window.showTextDocument).mockImplementation(
            async (uri: vscode.Uri, options?: vscode.TextDocumentShowOptions) => {
                const requestedLine = options?.selection?.end.line ?? 0;
                const line = uri.fsPath === fs.realpathSync(targetPath)
                    ? requestedLine - 1
                    : requestedLine;
                active = editor(uri.fsPath, Math.max(0, line), 2);
                return active as unknown as vscode.TextEditor;
            },
        );
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );

        await adapter.handleApply(exactCommand(targetPath));

        expect(vscode.window.showTextDocument).toHaveBeenCalledTimes(2);
        expect(active.document.uri.fsPath).toBe(fs.realpathSync(priorPath));
        expect(sent[0].receipts[0]).toMatchObject({
            status: "failed",
            error_code: "editor_capability_failed",
            retryable: false,
        });
        expect(sent[0].receipts[0].inverse_payload_json)
            .not.toContain("cortexEffectMayExist");
    });

    it("marks the effect indeterminate when exact local compensation also fails", async () => {
        jest.mocked(vscode.window.showTextDocument).mockImplementation(
            async (uri: vscode.Uri, options?: vscode.TextDocumentShowOptions) => {
                const requestedLine = options?.selection?.end.line ?? 0;
                if (uri.fsPath === fs.realpathSync(targetPath)) {
                    active = editor(uri.fsPath, Math.max(0, requestedLine - 1), 2);
                } else {
                    active = editor(uri.fsPath, requestedLine + 1, 2);
                }
                return active as unknown as vscode.TextEditor;
            },
        );
        const state = memento();
        const adapter = new EditorTransactionAdapter(
            state,
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );

        await adapter.handleApply(exactCommand(targetPath));

        expect(sent[0].receipts[0]).toMatchObject({
            status: "failed",
            error_code: "editor_effect_indeterminate",
            retryable: true,
        });
        expect(JSON.parse(String(sent[0].receipts[0].inverse_payload_json)))
            .toMatchObject({ cortexEffectMayExist: true });
        const journal = state.get<{
            operations: Record<string, { state: string }>;
        }>("cortex.interventionTransactionJournal.v1");
        expect(journal?.operations["intervention-exact-editor:resume-exact"]?.state)
            .toBe("applying");
    });

    it("does not mutate when local execution mode is suggestion-only", async () => {
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => false,
        );
        await adapter.handleApply(exactCommand(targetPath));
        expect(vscode.window.showTextDocument).not.toHaveBeenCalled();
        expect(sent[0].receipts[0]).toMatchObject({
            status: "failed",
            error_code: "execution_mode_denied",
        });
    });

    it("rejects a corrupt attempt counter before invoking VS Code", async () => {
        const state = memento();
        await state.update("cortex.interventionTransactionJournal.v1", {
            schema_version: "1",
            consumed_authorizations: {},
            operations: {},
            attempt_counters: { "forged:counter:apply": 101 },
            receipt_outbox: [],
        });
        const adapter = new EditorTransactionAdapter(
            state,
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );

        await adapter.handleApply(exactCommand(targetPath));

        expect(vscode.window.showTextDocument).not.toHaveBeenCalled();
        expect(sent).toEqual([]);
    });

    it("proves no effect for compensation that arrived after a lost apply send", async () => {
        const adapter = new EditorTransactionAdapter(
            memento(),
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );
        const command = exactCommand(targetPath);

        const handled = await adapter.handleRestore({
            restore_id: "restore-never-delivered-editor",
            intervention_id: command.manifest.intervention_id,
            manifest_sha256: command.manifest.manifest_sha256,
            reason: "partial_compensation",
            requested_at_unix_ms: Date.now(),
            requested_at_mono_ns: 3_000_000,
            boot_id: command.manifest.boot_id,
            actions: [{
                action_id: "resume-exact",
                executor: "editor",
                reverse_capability: "restore_active_file",
                inverse_payload_json: "{}",
                original_authorization_id: "authz-exact-editor",
                inverse_receipt_id: null,
                owner_client_instance_id: INSTANCE_ID,
            }],
        });

        expect(handled).toBe(true);
        expect(vscode.window.showTextDocument).not.toHaveBeenCalled();
        expect(sent[0].receipts[0]).toMatchObject({
            phase: "compensate",
            status: "already_complete",
            verification: "verified",
            error_code: undefined,
        });
        expect(JSON.parse(String(sent[0].receipts[0].inverse_payload_json)))
            .toEqual({ noEffect: true });
    });

    it("durably replays unacknowledged receipts and removes only terminal acknowledgements", async () => {
        const state = memento();
        const adapter = new EditorTransactionAdapter(
            state,
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );
        const command = exactCommand(targetPath);
        await adapter.handleApply(command);
        const originalReceiptId = sent[0].receipts[0].receipt_id;

        const restarted = new EditorTransactionAdapter(
            state,
            BOOT_ID,
            (batch) => sent.push(batch),
            () => true,
        );
        await restarted.flushPendingReceipts();
        expect(sent).toHaveLength(2);
        expect(sent[1].receipts[0].receipt_id).toBe(originalReceiptId);

        await restarted.acknowledgeTransactionState({
            authorization_id: command.authorization.authorization_id,
            state: "applied",
        });
        await restarted.flushPendingReceipts();
        expect(sent).toHaveLength(2);
    });
});
