/** Durable, exact-capability adapter for VS Code workspace effects. */

import * as fs from "fs";
import * as path from "path";
import { randomUUID } from "crypto";
import * as vscode from "vscode";
import type {
    ActionReceipt,
    InterventionReceiptBatch,
    ManifestAction,
    RestoreAction,
} from "./generated/cortex_schemas";
import {
    canonicalJson,
    sha256Hex,
    verifyApplyCommand,
    verifyRestoreCommand,
} from "./intervention-transaction";

interface ConsumedAuthorization {
    manifest_sha256: string;
    nonce: string;
    consumed_at_unix_ms: number;
}

interface EditorOperation {
    intervention_id: string;
    manifest_sha256: string;
    action_id: string;
    authorization_id: string;
    capability: string;
    state: "applying" | "applied" | "failed" | "restored";
    inverse_payload_json: string;
    after_fingerprint: string | null;
    updated_at_unix_ms: number;
}

interface EditorJournal {
    schema_version: "1";
    consumed_authorizations: Record<string, ConsumedAuthorization>;
    operations: Record<string, EditorOperation>;
    attempt_counters: Record<string, number>;
    receipt_outbox: InterventionReceiptBatch[];
}

interface ResumeTarget {
    path: string;
    line: number;
}

interface ResumeInverse extends Record<string, unknown> {
    targetPath: string;
    targetLine: number;
    priorPath: string | null;
    priorSelection: {
        anchorLine: number;
        anchorCharacter: number;
        activeLine: number;
        activeCharacter: number;
    } | null;
    noEffect?: boolean;
}

type ReceiptSender = (batch: InterventionReceiptBatch) => void;
type MutationAllowed = () => boolean;

const JOURNAL_KEY = "cortex.interventionTransactionJournal.v1";
const MAX_EDITOR_OPERATIONS = 512;
const MAX_EDITOR_COUNTERS = 2_048;
const MAX_RECEIPT_OUTBOX = 256;
const MAX_INVERSE_JSON_BYTES = 65_536;

function emptyJournal(): EditorJournal {
    return {
        schema_version: "1",
        consumed_authorizations: {},
        operations: {},
        attempt_counters: {},
        receipt_outbox: [],
    };
}

function isEditorRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBoundedEditorString(value: unknown, maximum = 256): value is string {
    return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function isCanonicalEditorObject(value: unknown): value is string {
    if (
        typeof value !== "string"
        || value.length === 0
        || value.length > MAX_INVERSE_JSON_BYTES
    ) return false;
    try {
        const parsed = JSON.parse(value) as unknown;
        return isEditorRecord(parsed) && canonicalJson(parsed) === value;
    } catch {
        return false;
    }
}

function validateEditorReceiptBatch(value: unknown): InterventionReceiptBatch {
    if (!isEditorRecord(value)) throw new Error("editor receipt batch is malformed");
    const interventionId = value.intervention_id;
    const authorizationId = value.authorization_id;
    const manifestSha256 = value.manifest_sha256;
    if (
        !isBoundedEditorString(interventionId)
        || !isBoundedEditorString(authorizationId)
        || typeof manifestSha256 !== "string"
        || !/^[0-9a-f]{64}$/.test(manifestSha256)
        || !Array.isArray(value.receipts)
        || value.receipts.length < 1
        || value.receipts.length > 96
    ) throw new Error("editor receipt batch fields are invalid");
    for (const candidate of value.receipts) {
        if (!isEditorRecord(candidate)) throw new Error("editor receipt is malformed");
        const numericFields = [
            candidate.started_at_unix_ms,
            candidate.ended_at_unix_ms,
            candidate.started_at_mono_ns,
            candidate.ended_at_mono_ns,
            candidate.duration_ms,
        ];
        if (
            !isBoundedEditorString(candidate.receipt_id, 128)
            || candidate.intervention_id !== interventionId
            || candidate.authorization_id !== authorizationId
            || candidate.manifest_sha256 !== manifestSha256
            || !isBoundedEditorString(candidate.action_id, 128)
            || !new Set(["apply", "compensate", "restore"]).has(String(candidate.phase))
            || !Number.isInteger(candidate.attempt)
            || Number(candidate.attempt) < 1
            || Number(candidate.attempt) > 100
            || !isBoundedEditorString(candidate.idempotency_key, 512)
            || !new Set(["succeeded", "failed", "already_complete"]).has(String(candidate.status))
            || numericFields.some((item) => typeof item !== "number" || !Number.isFinite(item) || item < 0)
            || Number(candidate.ended_at_unix_ms) < Number(candidate.started_at_unix_ms)
            || Number(candidate.ended_at_mono_ns) < Number(candidate.started_at_mono_ns)
            || !isBoundedEditorString(candidate.boot_id, 128)
            || !new Set(["verified", "failed", "not_applicable"]).has(String(candidate.verification))
            || (
                candidate.inverse_payload_json !== null
                && candidate.inverse_payload_json !== undefined
                && !isCanonicalEditorObject(candidate.inverse_payload_json)
            )
            || (
                candidate.after_fingerprint !== null
                && candidate.after_fingerprint !== undefined
                && (
                    typeof candidate.after_fingerprint !== "string"
                    || !/^[0-9a-f]{64}$/.test(candidate.after_fingerprint)
                )
            )
            || (candidate.source_client_type !== null && candidate.source_client_type !== undefined)
            || (candidate.source_client_id !== null && candidate.source_client_id !== undefined)
        ) throw new Error("editor receipt fields are invalid");
    }
    return value as unknown as InterventionReceiptBatch;
}

function validateEditorJournal(raw: unknown): EditorJournal {
    if (!isEditorRecord(raw) || raw.schema_version !== "1") {
        throw new Error("Cortex editor transaction journal is corrupt");
    }
    const consumed = raw.consumed_authorizations;
    const operations = raw.operations;
    const counters = raw.attempt_counters ?? {};
    const outbox = raw.receipt_outbox ?? [];
    if (
        !isEditorRecord(consumed)
        || !isEditorRecord(operations)
        || !isEditorRecord(counters)
        || !Array.isArray(outbox)
        || Object.keys(consumed).length > 256
        || Object.keys(operations).length > MAX_EDITOR_OPERATIONS
        || Object.keys(counters).length > MAX_EDITOR_COUNTERS
        || outbox.length > MAX_RECEIPT_OUTBOX
    ) throw new Error("Cortex editor transaction journal is corrupt");

    const validatedConsumed: Record<string, ConsumedAuthorization> = {};
    for (const [authorizationId, candidate] of Object.entries(consumed)) {
        if (
            !isBoundedEditorString(authorizationId)
            || !isEditorRecord(candidate)
            || typeof candidate.manifest_sha256 !== "string"
            || !/^[0-9a-f]{64}$/.test(candidate.manifest_sha256)
            || !isBoundedEditorString(candidate.nonce, 256)
            || typeof candidate.consumed_at_unix_ms !== "number"
            || !Number.isFinite(candidate.consumed_at_unix_ms)
            || candidate.consumed_at_unix_ms < 0
        ) throw new Error("editor consumed authorization is invalid");
        validatedConsumed[authorizationId] = candidate as unknown as ConsumedAuthorization;
    }
    const validatedOperations: Record<string, EditorOperation> = {};
    for (const [key, candidate] of Object.entries(operations)) {
        if (
            !isBoundedEditorString(key, 300)
            || !isEditorRecord(candidate)
            || !isBoundedEditorString(candidate.intervention_id)
            || !isBoundedEditorString(candidate.action_id, 128)
            || key !== operationKey(candidate.intervention_id, candidate.action_id)
            || typeof candidate.manifest_sha256 !== "string"
            || !/^[0-9a-f]{64}$/.test(candidate.manifest_sha256)
            || !isBoundedEditorString(candidate.authorization_id)
            || candidate.capability !== "resume_last_active_file"
            || !new Set(["applying", "applied", "failed", "restored"]).has(String(candidate.state))
            || !isCanonicalEditorObject(candidate.inverse_payload_json)
            || (
                candidate.after_fingerprint !== null
                && (
                    typeof candidate.after_fingerprint !== "string"
                    || !/^[0-9a-f]{64}$/.test(candidate.after_fingerprint)
                )
            )
            || typeof candidate.updated_at_unix_ms !== "number"
            || !Number.isFinite(candidate.updated_at_unix_ms)
            || candidate.updated_at_unix_ms < 0
        ) throw new Error("editor operation journal entry is invalid");
        validatedOperations[key] = candidate as unknown as EditorOperation;
    }
    const validatedCounters: Record<string, number> = {};
    for (const [key, value] of Object.entries(counters)) {
        if (
            !isBoundedEditorString(key, 512)
            || !Number.isInteger(value)
            || Number(value) < 0
            || Number(value) > 100
        ) throw new Error("editor receipt counter is invalid");
        validatedCounters[key] = Number(value);
    }
    const validatedOutbox = outbox.map(validateEditorReceiptBatch);
    const receiptIds = new Set<string>();
    for (const batch of validatedOutbox) {
        for (const item of batch.receipts) {
            const receiptId = String(item.receipt_id);
            if (receiptIds.has(receiptId)) {
                throw new Error("duplicate editor receipt id in outbox");
            }
            receiptIds.add(receiptId);
        }
    }
    return {
        schema_version: "1",
        consumed_authorizations: validatedConsumed,
        operations: validatedOperations,
        attempt_counters: validatedCounters,
        receipt_outbox: validatedOutbox,
    };
}

function operationKey(interventionId: string, actionId: string): string {
    return `${interventionId}:${actionId}`;
}

function monotonicNowNs(): number {
    return Math.max(0, Math.round(performance.now() * 1_000_000));
}

function receipt(args: {
    bootId: string;
    interventionId: string;
    authorizationId: string;
    manifestSha256: string;
    actionId: string;
    phase: "apply" | "compensate" | "restore";
    attempt: number;
    status: "succeeded" | "failed" | "already_complete";
    startedWallMs: number;
    startedMonoNs: number;
    inverse: Record<string, unknown>;
    verification: "verified" | "failed" | "not_applicable";
    detail: string;
    afterFingerprint: string | null;
    errorCode?: string;
    retryable?: boolean;
}): ActionReceipt {
    const endedWallMs = Math.max(args.startedWallMs, Date.now());
    const endedMonoNs = Math.max(args.startedMonoNs, monotonicNowNs());
    return {
        receipt_id: `rcpt_${randomUUID().replace(/-/g, "")}`,
        intervention_id: args.interventionId,
        authorization_id: args.authorizationId,
        manifest_sha256: args.manifestSha256,
        action_id: args.actionId,
        phase: args.phase,
        attempt: args.attempt,
        idempotency_key: `${args.authorizationId}:${args.actionId}:${args.phase}:${args.attempt}`,
        status: args.status,
        started_at_unix_ms: args.startedWallMs,
        ended_at_unix_ms: endedWallMs,
        started_at_mono_ns: args.startedMonoNs,
        ended_at_mono_ns: endedMonoNs,
        duration_ms: Math.floor((endedMonoNs - args.startedMonoNs) / 1_000_000),
        boot_id: args.bootId,
        inverse_payload_json: canonicalJson(args.inverse),
        verification: args.verification,
        verification_detail: args.detail.slice(0, 500),
        after_fingerprint: args.afterFingerprint,
        error_code: args.errorCode,
        error_message: args.errorCode ? args.detail.slice(0, 500) : undefined,
        retryable: args.retryable ?? false,
        source_client_type: null,
        source_client_id: null,
    };
}

type ReceiptArgs = Parameters<typeof receipt>[0];

function parseSuggestedResume(action: ManifestAction): ResumeTarget {
    const parameters = JSON.parse(action.parameters_json ?? "{}") as Record<string, unknown>;
    const suggested = parameters.suggested_action;
    if (typeof suggested !== "object" || suggested === null || Array.isArray(suggested)) {
        throw new Error("resume action lacks its immutable suggested action");
    }
    const value = suggested as Record<string, unknown>;
    if (
        value.action_id !== action.action_id
        || value.action_type !== "resume_last_active_file"
    ) {
        throw new Error("resume action differs from its immutable manifest");
    }
    const target = typeof value.target === "string" ? value.target.trim() : "";
    if (!target) throw new Error("resume target is empty");
    const separator = target.lastIndexOf(":");
    let filePath = target;
    let line = 1;
    if (separator > 0) {
        const candidate = Number.parseInt(target.slice(separator + 1), 10);
        if (Number.isInteger(candidate) && candidate > 0) {
            filePath = target.slice(0, separator);
            line = candidate;
        }
    }
    if (!path.isAbsolute(filePath)) {
        throw new Error("resume target must be an absolute local path");
    }
    return { path: filePath, line: Math.min(line, 1_000_000) };
}

async function resolveOwnedWorkspacePath(targetPath: string): Promise<string> {
    const uri = vscode.Uri.file(targetPath);
    const folder = vscode.workspace.getWorkspaceFolder(uri);
    if (!folder) throw new Error("resume target is outside the open workspace");
    const [realTarget, realFolder] = await Promise.all([
        fs.promises.realpath(targetPath),
        fs.promises.realpath(folder.uri.fsPath),
    ]);
    const relative = path.relative(realFolder, realTarget);
    if (relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))) {
        return realTarget;
    }
    throw new Error("resume target resolves outside the open workspace");
}

function captureResumeInverse(target: ResumeTarget): ResumeInverse {
    const prior = vscode.window.activeTextEditor;
    if (!prior || prior.document.uri.scheme !== "file") {
        throw new Error(
            "resume action has no reversible prior workspace-file focus",
        );
    }
    return {
        targetPath: target.path,
        targetLine: target.line,
        priorPath: prior?.document.uri.fsPath ?? null,
        priorSelection: prior ? {
            anchorLine: prior.selection.anchor.line,
            anchorCharacter: prior.selection.anchor.character,
            activeLine: prior.selection.active.line,
            activeCharacter: prior.selection.active.character,
        } : null,
    };
}

async function applyResume(target: ResumeTarget): Promise<string> {
    const uri = vscode.Uri.file(target.path);
    const line = Math.max(0, target.line - 1);
    const selection = new vscode.Range(line, 0, line, 0);
    const editor = await vscode.window.showTextDocument(uri, {
        preview: false,
        selection,
    });
    if (editor.document.uri.fsPath !== target.path) {
        throw new Error("VS Code opened a different document than authorized");
    }
    if (editor.selection.active.line !== line) {
        throw new Error("VS Code did not focus the exact authorized line");
    }
    return sha256Hex(canonicalJson({
        path: editor.document.uri.fsPath,
        version: editor.document.version,
        anchorLine: editor.selection.anchor.line,
        anchorCharacter: editor.selection.anchor.character,
        activeLine: editor.selection.active.line,
        activeCharacter: editor.selection.active.character,
    }));
}

function selectionMatches(
    actual: vscode.Selection,
    expected: ResumeInverse["priorSelection"],
): boolean {
    return expected !== null
        && actual.anchor.line === expected.anchorLine
        && actual.anchor.character === expected.anchorCharacter
        && actual.active.line === expected.activeLine
        && actual.active.character === expected.activeCharacter;
}

async function restoreResume(
    inverse: ResumeInverse,
    preserveUserSupersession = true,
): Promise<{
    status: "succeeded" | "failed" | "already_complete";
    detail: string;
    fingerprint: string | null;
}> {
    if (inverse.noEffect === true) {
        return {
            status: "already_complete",
            detail: "The exact editor target was already active",
            fingerprint: sha256Hex(canonicalJson({ noEffect: true })),
        };
    }
    const current = vscode.window.activeTextEditor;
    const cortexTargetLine = Math.max(0, inverse.targetLine - 1);
    if (
        preserveUserSupersession
        && (
            !current
            || current.document.uri.fsPath !== inverse.targetPath
            || current.selection.active.line !== cortexTargetLine
        )
    ) {
        return {
            status: "already_complete",
            detail: "User focus superseded the exact Cortex file position",
            fingerprint: current
                ? sha256Hex(canonicalJson({
                    path: current.document.uri.fsPath,
                    activeLine: current.selection.active.line,
                    activeCharacter: current.selection.active.character,
                    userSuperseded: true,
                }))
                : sha256Hex(canonicalJson({
                    activeEditor: null,
                    userSuperseded: true,
                })),
        };
    }
    if (!inverse.priorPath || !inverse.priorSelection) {
        return {
            status: "failed",
            detail: "Exact prior editor focus is unavailable",
            fingerprint: null,
        };
    }
    const priorPath = await resolveOwnedWorkspacePath(inverse.priorPath);
    const priorSelection = inverse.priorSelection;
    if (
        current
        && current.document.uri.fsPath === priorPath
        && selectionMatches(current.selection, priorSelection)
    ) {
        return {
            status: "already_complete",
            detail: "Exact prior editor focus is already active",
            fingerprint: sha256Hex(canonicalJson({
                path: priorPath,
                ...priorSelection,
            })),
        };
    }
    const selection = new vscode.Range(
        priorSelection.anchorLine,
        priorSelection.anchorCharacter,
        priorSelection.activeLine,
        priorSelection.activeCharacter,
    );
    const restored = await vscode.window.showTextDocument(
        vscode.Uri.file(priorPath),
        { preview: false, selection },
    );
    if (
        restored.document.uri.fsPath !== priorPath
        || !selectionMatches(restored.selection, priorSelection)
    ) {
        return { status: "failed", detail: "Prior editor failed verification", fingerprint: null };
    }
    return {
        status: "succeeded",
        detail: "Prior editor focus restored",
        fingerprint: sha256Hex(canonicalJson({
            path: priorPath,
            ...priorSelection,
        })),
    };
}

export class EditorTransactionAdapter {
    private _turn: Promise<void> = Promise.resolve();

    constructor(
        private readonly _globalState: vscode.Memento,
        private readonly _bootId: string,
        private readonly _sendReceipt: ReceiptSender,
        private readonly _mutationAllowed: MutationAllowed,
        private readonly _clientInstanceId = `vscode_${_bootId}`,
    ) {}

    handleApply(payload: unknown): Promise<void> {
        return this._serialized(() => this._handleApply(payload));
    }

    handleRestore(payload: unknown): Promise<boolean> {
        return this._serialized(() => this._handleRestore(payload));
    }

    flushPendingReceipts(): Promise<void> {
        return this._serialized(async () => {
            const journal = this._readJournal();
            for (const batch of journal.receipt_outbox) {
                this._sendReceipt(batch);
            }
        });
    }

    acknowledgeTransactionState(payload: Record<string, unknown>): Promise<void> {
        return this._serialized(async () => {
            const authorizationId = typeof payload.authorization_id === "string"
                ? payload.authorization_id
                : "";
            const interventionId = typeof payload.intervention_id === "string"
                ? payload.intervention_id
                : "";
            const state = typeof payload.state === "string" ? payload.state : "";
            if (
                !authorizationId
                || !new Set([
                    "applied", "partial", "failed", "restored", "restore_failed",
                ]).has(state)
            ) {
                return;
            }
            const journal = this._readJournal();
            journal.receipt_outbox = journal.receipt_outbox.filter(
                (batch) => batch.authorization_id !== authorizationId,
            );
            if (
                state === "restored"
                && interventionId
                && !journal.receipt_outbox.some(
                    (batch) => batch.intervention_id === interventionId,
                )
            ) {
                const retired: EditorOperation[] = [];
                for (const [key, operation] of Object.entries(journal.operations)) {
                    if (
                        operation.intervention_id === interventionId
                        && operation.state === "restored"
                    ) {
                        retired.push(operation);
                        delete journal.operations[key];
                        delete journal.consumed_authorizations[
                            operation.authorization_id
                        ];
                    }
                }
                for (const counterKey of Object.keys(journal.attempt_counters)) {
                    if (retired.some((operation) =>
                        counterKey === [
                            operation.authorization_id,
                            operation.action_id,
                            "apply",
                        ].join(":")
                        || counterKey.endsWith(`:${operation.action_id}:restore`)
                        || counterKey.endsWith(`:${operation.action_id}:compensate`)
                    )) {
                        delete journal.attempt_counters[counterKey];
                    }
                }
            }
            await this._writeJournal(journal);
        });
    }

    private async _serialized<T>(work: () => Promise<T>): Promise<T> {
        let release: (() => void) | undefined;
        const prior = this._turn;
        this._turn = new Promise<void>((resolve) => { release = resolve; });
        await prior;
        try {
            return await work();
        } finally {
            release?.();
        }
    }

    private _readJournal(): EditorJournal {
        const raw = this._globalState.get<unknown>(JOURNAL_KEY);
        if (raw === undefined) return emptyJournal();
        return validateEditorJournal(raw);
    }

    private async _writeJournal(journal: EditorJournal): Promise<void> {
        await this._globalState.update(JOURNAL_KEY, journal);
    }

    private async _makeReceipt(
        args: Omit<ReceiptArgs, "attempt">,
    ): Promise<ActionReceipt> {
        const journal = this._readJournal();
        const counterKey = [
            args.authorizationId,
            args.actionId,
            args.phase,
        ].join(":");
        const attempt = (journal.attempt_counters[counterKey] ?? 0) + 1;
        if (attempt > 100) throw new Error("receipt retry limit exceeded");
        journal.attempt_counters[counterKey] = attempt;
        await this._writeJournal(journal);
        return receipt({ ...args, attempt });
    }

    private async _send(
        interventionId: string,
        manifestSha256: string,
        authorizationId: string,
        receipts: ActionReceipt[],
    ): Promise<void> {
        if (receipts.length === 0) return;
        const batch: InterventionReceiptBatch = {
            intervention_id: interventionId,
            manifest_sha256: manifestSha256,
            authorization_id: authorizationId,
            receipts: receipts as [ActionReceipt, ...ActionReceipt[]],
        };
        const journal = this._readJournal();
        const receiptIds = new Set(receipts.map((item) => item.receipt_id));
        const alreadyQueued = journal.receipt_outbox.some((queued) =>
            queued.receipts.some((item) => receiptIds.has(item.receipt_id))
        );
        if (!alreadyQueued) {
            if (journal.receipt_outbox.length >= MAX_RECEIPT_OUTBOX) {
                throw new Error("editor transaction receipt outbox is full");
            }
            journal.receipt_outbox.push(batch);
            await this._writeJournal(journal);
        }
        this._sendReceipt(batch);
    }

    private async _handleApply(payload: unknown): Promise<void> {
        let verified: ReturnType<typeof verifyApplyCommand>;
        try {
            verified = verifyApplyCommand(
                payload,
                this._bootId,
                Date.now(),
                this._clientInstanceId,
            );
        } catch {
            return;
        }
        const authorization = verified.command.authorization;
        const authorizationId = authorization.authorization_id ?? "";
        if (!authorizationId) return;
        if (!this._mutationAllowed()) {
            const denied = await Promise.all(verified.ownActions.map((action) => {
                const wall = Date.now();
                const mono = monotonicNowNs();
                return this._makeReceipt({
                    bootId: this._bootId,
                    interventionId: verified.manifest.intervention_id,
                    authorizationId,
                    manifestSha256: verified.manifest.manifest_sha256,
                    actionId: action.action_id,
                    phase: "apply",
                    status: "failed",
                    startedWallMs: wall,
                    startedMonoNs: mono,
                    inverse: {},
                    verification: "failed",
                    detail: "Local execution mode denies workspace mutation",
                    afterFingerprint: null,
                    errorCode: "execution_mode_denied",
                });
            }));
            await this._send(
                verified.manifest.intervention_id,
                verified.manifest.manifest_sha256,
                authorizationId,
                denied,
            );
            return;
        }

        let journal: EditorJournal;
        try {
            journal = this._readJournal();
            if (journal.receipt_outbox.length >= MAX_RECEIPT_OUTBOX) {
                throw new Error("editor transaction receipt outbox is full");
            }
            const newOperationCount = verified.ownActions.filter((action) =>
                journal.operations[
                    operationKey(verified.manifest.intervention_id, action.action_id)
                ] === undefined
            ).length;
            if (
                Object.keys(journal.operations).length + newOperationCount
                > MAX_EDITOR_OPERATIONS
            ) {
                throw new Error("editor transaction operation journal is full");
            }
            for (const action of verified.ownActions) {
                const counterKey = [
                    authorizationId,
                    action.action_id,
                    "apply",
                ].join(":");
                if ((journal.attempt_counters[counterKey] ?? 0) >= 100) {
                    throw new Error("editor receipt retry limit reached before apply");
                }
            }
            const prior = journal.consumed_authorizations[authorizationId];
            const nonce = authorization.nonce ?? "";
            if (prior && (
                prior.manifest_sha256 !== verified.manifest.manifest_sha256
                || prior.nonce !== nonce
            )) {
                throw new Error("authorization replayed with different content");
            }
            if (!prior) {
                journal.consumed_authorizations[authorizationId] = {
                    manifest_sha256: verified.manifest.manifest_sha256,
                    nonce,
                    consumed_at_unix_ms: Date.now(),
                };
                const ids = Object.keys(journal.consumed_authorizations);
                if (ids.length > 256) {
                    ids.sort((left, right) =>
                        journal.consumed_authorizations[left].consumed_at_unix_ms
                        - journal.consumed_authorizations[right].consumed_at_unix_ms
                    ).slice(0, ids.length - 256).forEach(
                        (id) => delete journal.consumed_authorizations[id],
                    );
                }
                await this._writeJournal(journal);
            }
        } catch {
            return;
        }

        const receipts: ActionReceipt[] = [];
        for (const action of verified.ownActions) {
            const startedWallMs = Date.now();
            const startedMonoNs = monotonicNowNs();
            const key = operationKey(verified.manifest.intervention_id, action.action_id);
            journal = this._readJournal();
            const existing = journal.operations[key];
            if (existing) {
                let existingInverse: Record<string, unknown> = {};
                let durableRecordValid = true;
                try {
                    const parsed = JSON.parse(existing.inverse_payload_json) as unknown;
                    durableRecordValid = (
                        typeof parsed === "object"
                        && parsed !== null
                        && !Array.isArray(parsed)
                        && canonicalJson(parsed) === existing.inverse_payload_json
                    );
                    if (durableRecordValid) {
                        existingInverse = parsed as Record<string, unknown>;
                    }
                } catch {
                    durableRecordValid = false;
                }
                durableRecordValid = durableRecordValid
                    && existing.intervention_id === verified.manifest.intervention_id
                    && existing.manifest_sha256 === verified.manifest.manifest_sha256
                    && existing.action_id === action.action_id
                    && existing.authorization_id === authorizationId
                    && existing.capability === action.capability;
                if (!durableRecordValid) {
                    const detail = "Durable Cortex editor operation does not match this authorization";
                    receipts.push(await this._makeReceipt({
                        bootId: this._bootId,
                        interventionId: verified.manifest.intervention_id,
                        authorizationId,
                        manifestSha256: verified.manifest.manifest_sha256,
                        actionId: action.action_id,
                        phase: "apply",
                        status: "failed",
                        startedWallMs,
                        startedMonoNs,
                        inverse: {},
                        verification: "failed",
                        detail,
                        afterFingerprint: null,
                        errorCode: "durable_operation_mismatch",
                    }));
                    continue;
                }
                const succeeded = existing.state === "applied";
                receipts.push(await this._makeReceipt({
                    bootId: this._bootId,
                    interventionId: verified.manifest.intervention_id,
                    authorizationId,
                    manifestSha256: verified.manifest.manifest_sha256,
                    actionId: action.action_id,
                    phase: "apply",
                    status: succeeded ? "already_complete" : "failed",
                    startedWallMs,
                    startedMonoNs,
                    inverse: existingInverse,
                    verification: succeeded ? "verified" : "failed",
                    detail: succeeded
                        ? "Durable Cortex editor operation is already active"
                        : "Authorization replay rejected after a non-terminal attempt",
                    afterFingerprint: existing.after_fingerprint,
                    errorCode: succeeded ? undefined : "authorization_replay",
                    retryable: existing.state === "applying",
                }));
                continue;
            }

            let inverse: ResumeInverse | Record<string, unknown> = {};
            let intentPersisted = false;
            try {
                if (action.capability !== "resume_last_active_file") {
                    throw new Error(`unsupported editor capability ${action.capability}`);
                }
                const parsed = parseSuggestedResume(action);
                const ownedPath = await resolveOwnedWorkspacePath(parsed.path);
                const target = { path: ownedPath, line: parsed.line };
                const preparedInverse = captureResumeInverse(target);
                preparedInverse.priorPath = await resolveOwnedWorkspacePath(
                    preparedInverse.priorPath as string,
                );
                const priorSelection = preparedInverse.priorSelection;
                preparedInverse.noEffect = (
                    preparedInverse.priorPath === target.path
                    && priorSelection !== null
                    && priorSelection.anchorLine === target.line - 1
                    && priorSelection.activeLine === target.line - 1
                    && priorSelection.anchorCharacter === 0
                    && priorSelection.activeCharacter === 0
                );
                inverse = preparedInverse;
                journal.operations[key] = {
                    intervention_id: verified.manifest.intervention_id,
                    manifest_sha256: verified.manifest.manifest_sha256,
                    action_id: action.action_id,
                    authorization_id: authorizationId,
                    capability: action.capability,
                    state: "applying",
                    inverse_payload_json: canonicalJson(inverse),
                    after_fingerprint: null,
                    updated_at_unix_ms: Date.now(),
                };
                await this._writeJournal(journal);
                intentPersisted = true;
                const fingerprint = inverse.noEffect === true
                    ? sha256Hex(canonicalJson({
                        path: target.path,
                        line: target.line,
                        noEffect: true,
                    }))
                    : await applyResume(target);
                journal = this._readJournal();
                journal.operations[key] = {
                    ...journal.operations[key],
                    state: "applied",
                    after_fingerprint: fingerprint,
                    updated_at_unix_ms: Date.now(),
                };
                await this._writeJournal(journal);
                receipts.push(await this._makeReceipt({
                    bootId: this._bootId,
                    interventionId: verified.manifest.intervention_id,
                    authorizationId,
                    manifestSha256: verified.manifest.manifest_sha256,
                    actionId: action.action_id,
                    phase: "apply",
                    status: inverse.noEffect === true
                        ? "already_complete"
                        : "succeeded",
                    startedWallMs,
                    startedMonoNs,
                    inverse,
                    verification: "verified",
                    detail: inverse.noEffect === true
                        ? "The exact workspace file position was already active"
                        : "Authorized workspace file focused and verified",
                    afterFingerprint: fingerprint,
                }));
            } catch (error) {
                let detail = String(error);
                let effectMayExist = intentPersisted;
                if (
                    intentPersisted
                    && typeof inverse.targetPath === "string"
                    && typeof inverse.targetLine === "number"
                ) {
                    try {
                        const compensated = await restoreResume(
                            inverse as ResumeInverse,
                            false,
                        );
                        if (compensated.status !== "failed") {
                            detail = `${detail}; local compensation: ${compensated.detail}`;
                            effectMayExist = false;
                        } else {
                            detail = `${detail}; local compensation failed: ${compensated.detail}`;
                        }
                    } catch {
                        detail = `${detail}; local compensation also failed`;
                    }
                }
                try {
                    journal = this._readJournal();
                    const operation = journal.operations[key];
                    if (operation) {
                        operation.state = effectMayExist ? "applying" : "failed";
                        operation.updated_at_unix_ms = Date.now();
                        await this._writeJournal(journal);
                    }
                } catch { /* original failure remains authoritative */ }
                receipts.push(await this._makeReceipt({
                    bootId: this._bootId,
                    interventionId: verified.manifest.intervention_id,
                    authorizationId,
                    manifestSha256: verified.manifest.manifest_sha256,
                    actionId: action.action_id,
                    phase: "apply",
                    status: "failed",
                    startedWallMs,
                    startedMonoNs,
                    inverse: effectMayExist
                        ? { ...inverse, cortexEffectMayExist: true }
                        : inverse,
                    verification: "failed",
                    detail,
                    afterFingerprint: null,
                    errorCode: effectMayExist
                        ? "editor_effect_indeterminate"
                        : "editor_capability_failed",
                    retryable: effectMayExist,
                }));
            }
        }
        await this._send(
            verified.manifest.intervention_id,
            verified.manifest.manifest_sha256,
            authorizationId,
            receipts,
        );
    }

    private async _handleRestore(payload: unknown): Promise<boolean> {
        let verified: ReturnType<typeof verifyRestoreCommand>;
        try {
            verified = verifyRestoreCommand(
                payload,
                this._clientInstanceId,
            );
        } catch {
            return false;
        }
        const phase = verified.command.reason === "partial_compensation"
            ? "compensate"
            : "restore";
        const receipts: ActionReceipt[] = [];
        for (const action of verified.ownActions) {
            receipts.push(await this._restoreAction(verified.command, action, phase));
        }
        await this._send(
            verified.command.intervention_id,
            verified.command.manifest_sha256,
            verified.command.restore_id,
            receipts,
        );
        return true;
    }

    private async _restoreAction(
        command: ReturnType<typeof verifyRestoreCommand>["command"],
        action: RestoreAction,
        phase: "compensate" | "restore",
    ): Promise<ActionReceipt> {
        const wall = Date.now();
        const mono = monotonicNowNs();
        const key = operationKey(command.intervention_id, action.action_id);
        let inverse: ResumeInverse | Record<string, unknown> = {};
        let status: "succeeded" | "failed" | "already_complete" = "failed";
        let detail = "Restore failed";
        let fingerprint: string | null = null;
        try {
            const journal = this._readJournal();
            const operation = journal.operations[key];
            const effectiveInverseJson =
                action.inverse_payload_json === "{}"
                && operation?.state === "applying"
                    ? operation.inverse_payload_json
                    : action.inverse_payload_json;
            inverse = JSON.parse(effectiveInverseJson) as ResumeInverse;
            if (!operation) {
                inverse = { noEffect: true };
                status = "already_complete";
                detail = "No Cortex-owned editor effect was durably started on this client";
                fingerprint = sha256Hex(canonicalJson({
                    actionId: action.action_id,
                    state: "never_started",
                }));
            } else if (
                operation.intervention_id !== command.intervention_id
                || operation.manifest_sha256 !== command.manifest_sha256
                || operation.authorization_id !== action.original_authorization_id
                || operation.capability !== "resume_last_active_file"
                || action.reverse_capability !== "restore_active_file"
                || operation.inverse_payload_json !== effectiveInverseJson
            ) {
                detail = "Restore does not match a durable Cortex editor operation";
            } else if (operation.state === "restored") {
                status = "already_complete";
                detail = "Cortex editor operation was already restored";
                fingerprint = operation.after_fingerprint;
            } else {
                const restored = await restoreResume(inverse as ResumeInverse);
                status = restored.status;
                detail = restored.detail;
                fingerprint = restored.fingerprint;
                if (status !== "failed") {
                    operation.state = "restored";
                    operation.after_fingerprint = fingerprint;
                    operation.updated_at_unix_ms = Date.now();
                    await this._writeJournal(journal);
                }
            }
        } catch (error) {
            detail = String(error);
        }
        return await this._makeReceipt({
            bootId: this._bootId,
            interventionId: command.intervention_id,
            authorizationId: command.restore_id,
            manifestSha256: command.manifest_sha256,
            actionId: action.action_id,
            phase,
            status,
            startedWallMs: wall,
            startedMonoNs: mono,
            inverse,
            verification: status === "failed" ? "failed" : "verified",
            detail,
            afterFingerprint: fingerprint,
            errorCode: status === "failed" ? "restore_failed" : undefined,
            retryable: status === "failed",
        });
    }
}
