/** Fail-closed validation for daemon-issued editor transaction commands. */

import { createHash } from "crypto";
import type {
    ActionManifest,
    ActionManifestBody,
    InterventionApplyCommand,
    InterventionRestoreCommand,
    ManifestAction,
    RestoreAction,
} from "./generated/cortex_schemas";

export interface VerifiedManifest {
    manifest: ActionManifest;
    body: ActionManifestBody;
    actionsById: Map<string, ManifestAction>;
}

export interface VerifiedApplyCommand extends VerifiedManifest {
    command: InterventionApplyCommand;
    ownActions: ManifestAction[];
}

export interface VerifiedRestoreCommand {
    command: InterventionRestoreCommand & { restore_id: string };
    ownActions: RestoreAction[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function canonicalJson(value: unknown): string {
    if (value === null || typeof value === "string" || typeof value === "boolean") {
        return JSON.stringify(value);
    }
    if (typeof value === "number") {
        if (!Number.isFinite(value)) throw new Error("non-finite JSON number");
        return JSON.stringify(value);
    }
    if (Array.isArray(value)) {
        return `[${value.map(canonicalJson).join(",")}]`;
    }
    if (isRecord(value)) {
        return `{${Object.keys(value).sort().map(
            (key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
        ).join(",")}}`;
    }
    throw new Error("value is not JSON serializable");
}

export function sha256Hex(value: string): string {
    return createHash("sha256").update(value, "utf8").digest("hex");
}

function requireString(value: unknown, label: string): string {
    if (typeof value !== "string" || value.length === 0) {
        throw new Error(`${label} must be a non-empty string`);
    }
    return value;
}

function isBoundedAuthorizationSourceId(value: unknown): value is string {
    return typeof value === "string" && value.length > 0 && value.length <= 128;
}

const EXECUTORS = new Set(["browser", "editor", "terminal", "desktop", "daemon"]);
const SOURCES = new Set(["planner_command", "suggested_action", "system"]);
const BROWSER_CAPABILITIES = new Set([
    "open_url", "search_error", "highlight_tab",
]);
const EDITOR_CAPABILITIES = new Set(["resume_last_active_file"]);
const CAPABILITY_POLICY: Record<string, {
    executor: "browser" | "editor";
    reverse: string | null;
    workspaceMutation: boolean;
}> = {
    open_url: { executor: "browser", reverse: "close_created_tab", workspaceMutation: true },
    search_error: { executor: "browser", reverse: "close_created_tab", workspaceMutation: true },
    highlight_tab: { executor: "browser", reverse: "restore_active_tab", workspaceMutation: true },
    resume_last_active_file: { executor: "editor", reverse: "restore_active_file", workspaceMutation: true },
};
const PLANNER_CAPABILITIES = new Set<string>();
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const RESTORE_REASONS = new Set([
    "user_undo", "dismissed", "snoozed", "timed_out", "natural_recovery",
    "system_cancelled", "partial_compensation", "startup_recovery",
    "emergency_restore",
]);

function supportedCapability(executor: string, capability: string): boolean {
    if (executor === "browser") return BROWSER_CAPABILITIES.has(capability);
    if (executor === "editor") return EDITOR_CAPABILITIES.has(capability);
    return false;
}

function validateManifestAction(value: unknown): ManifestAction {
    if (!isRecord(value)) throw new Error("manifest action must be an object");
    const actionId = requireString(value.action_id, "action_id");
    const executor = requireString(value.executor, "executor");
    const capability = requireString(value.capability, "capability");
    const policy = CAPABILITY_POLICY[capability];
    if (
        actionId.length > 128
        || !EXECUTORS.has(executor)
        || !supportedCapability(executor, capability)
        || !policy
        || policy.executor !== executor
    ) {
        throw new Error("manifest capability is not locally supported");
    }
    const source = String(value.source);
    if (!SOURCES.has(source) || source === "system") {
        throw new Error("manifest source is invalid");
    }
    if (
        !Number.isInteger(value.ordinal)
        || Number(value.ordinal) < 0
        || Number(value.ordinal) > 31
    ) {
        throw new Error("manifest ordinal is invalid");
    }
    if (
        !Number.isInteger(value.required_consent_level)
        || Number(value.required_consent_level) < 0
        || Number(value.required_consent_level) > 4
        || typeof value.workspace_mutation !== "boolean"
    ) {
        throw new Error("manifest action policy fields are invalid");
    }
    if (
        (value.reverse_capability ?? null) !== policy.reverse
        || value.workspace_mutation !== policy.workspaceMutation
        || (source === "planner_command" && !PLANNER_CAPABILITIES.has(capability))
    ) {
        throw new Error("manifest capability semantics are invalid");
    }
    if (
        typeof value.parameters_json !== "string"
        || value.parameters_json.length > 32_768
    ) {
        throw new Error("manifest parameters_json is missing");
    }
    const parameters: unknown = JSON.parse(value.parameters_json);
    if (!isRecord(parameters)) {
        throw new Error("manifest parameters_json must encode an object");
    }
    return value as unknown as ManifestAction;
}

export function verifyActionManifest(
    value: unknown,
    nowUnixMs: number = Date.now(),
): VerifiedManifest {
    if (!isRecord(value)) throw new Error("action manifest is missing");
    const canonical = requireString(value.canonical_json, "canonical_json");
    const digest = requireString(value.manifest_sha256, "manifest_sha256");
    if (!/^[0-9a-f]{64}$/.test(digest) || sha256Hex(canonical) !== digest) {
        throw new Error("manifest digest mismatch");
    }
    const rawBody: unknown = JSON.parse(canonical);
    if (!isRecord(rawBody) || canonicalJson(rawBody) !== canonical) {
        throw new Error("manifest body is not canonical");
    }
    const interventionId = requireString(value.intervention_id, "intervention_id");
    if (
        interventionId.length > 128
        || !UUID_PATTERN.test(String(value.boot_id))
        || value.schema_version !== "1"
        || rawBody.schema_version !== "1"
        || rawBody.intervention_id !== interventionId
    ) {
        throw new Error("manifest identity is invalid");
    }
    const rawActions = Array.isArray(rawBody.actions) ? rawBody.actions : [];
    if (
        !Number.isInteger(value.action_count)
        || rawActions.length !== value.action_count
        || rawActions.length > 32
    ) {
        throw new Error("manifest action count mismatch");
    }
    const actions = rawActions.map(validateManifestAction);
    const ids = new Set<string>();
    let priorOrdinal = -1;
    for (const action of actions) {
        if (ids.has(action.action_id) || action.ordinal !== priorOrdinal + 1) {
            throw new Error("manifest action identity/order is invalid");
        }
        ids.add(action.action_id);
        priorOrdinal = action.ordinal;
    }
    const createdAt = Number(value.created_at_unix_ms);
    const expiresAt = Number(value.expires_at_unix_ms);
    const createdMono = Number(value.created_at_mono_ns);
    const ttlMs = Number(value.ttl_ms);
    if (
        !Number.isSafeInteger(createdAt)
        || createdAt < 0
        || !Number.isSafeInteger(expiresAt)
        || expiresAt < 0
        || !Number.isSafeInteger(createdMono)
        || createdMono < 0
        || !Number.isSafeInteger(ttlMs)
        || ttlMs < 1
        || ttlMs > 3_600_000
        || expiresAt !== createdAt + ttlMs
        || nowUnixMs >= expiresAt
    ) {
        throw new Error("manifest expired or has invalid lifetime");
    }
    return {
        manifest: value as unknown as ActionManifest,
        body: rawBody as unknown as ActionManifestBody,
        actionsById: new Map(actions.map((action) => [action.action_id, action])),
    };
}

export function verifyApplyCommand(
    value: unknown,
    clientBootId: string,
    nowUnixMs: number = Date.now(),
    clientInstanceId?: string,
): VerifiedApplyCommand {
    if (!isRecord(value) || !isRecord(value.authorization)) {
        throw new Error("apply command is malformed");
    }
    const verified = verifyActionManifest(value.manifest, nowUnixMs);
    const authorization = value.authorization;
    const authorizationId = requireString(authorization.authorization_id, "authorization id");
    const requestId = requireString(
        authorization.authorization_request_id,
        "authorization request id",
    );
    const nonce = requireString(authorization.nonce, "authorization nonce");
    if (
        authorizationId.length > 128
        || requestId.length > 128
        || nonce.length < 32
        || nonce.length > 64
        || !new Set(["user_confirmed", "research_autonomous"]).has(
            String(authorization.authorization_kind),
        )
        || !new Set(["browser", "vscode", "desktop", "http", "daemon"]).has(
            String(authorization.source_surface),
        )
        || (
            authorization.authorization_kind === "research_autonomous"
            && authorization.source_surface !== "daemon"
        )
        || (
            authorization.authorization_kind === "user_confirmed"
            && authorization.source_surface === "daemon"
        )
        || !Number.isSafeInteger(authorization.consent_revision)
        || Number(authorization.consent_revision) < 0
        || !UUID_PATTERN.test(String(authorization.requester_boot_id))
        || !UUID_PATTERN.test(String(authorization.boot_id))
    ) {
        throw new Error("authorization identity or policy fields are invalid");
    }
    if (
        authorization.intervention_id !== verified.manifest.intervention_id
        || authorization.manifest_sha256 !== verified.manifest.manifest_sha256
        || authorization.boot_id !== verified.manifest.boot_id
    ) {
        throw new Error("authorization does not match manifest");
    }
    if (
        authorization.source_client_id !== null
        && authorization.source_client_id !== undefined
        && !isBoundedAuthorizationSourceId(authorization.source_client_id)
    ) {
        throw new Error("authorization source client id is invalid");
    }
    if (
        authorization.source_surface === "vscode"
        && authorization.requester_boot_id !== clientBootId
    ) {
        throw new Error("authorization belongs to another editor instance");
    }
    if (
        clientInstanceId
        && authorization.source_surface === "vscode"
        && authorization.source_client_id !== clientInstanceId
    ) {
        throw new Error("authorization belongs to another stable editor instance");
    }
    const issuedAt = Number(authorization.issued_at_unix_ms);
    const expiresAt = Number(authorization.expires_at_unix_ms);
    const issuedMono = Number(authorization.issued_at_mono_ns);
    const ttlMs = Number(authorization.ttl_ms);
    if (
        !Number.isSafeInteger(issuedAt)
        || issuedAt < 0
        || !Number.isSafeInteger(expiresAt)
        || expiresAt < 0
        || !Number.isSafeInteger(issuedMono)
        || issuedMono < 0
        || !Number.isSafeInteger(ttlMs)
        || ttlMs < 1
        || ttlMs > 300_000
        || expiresAt !== issuedAt + ttlMs
        || expiresAt > Number(verified.manifest.expires_at_unix_ms)
        || issuedMono < Number(verified.manifest.created_at_mono_ns)
        || nowUnixMs >= expiresAt
    ) {
        throw new Error("authorization expired or has invalid lifetime");
    }
    const ids = Array.isArray(authorization.authorized_action_ids)
        ? authorization.authorized_action_ids.map(String)
        : [];
    if (
        ids.length === 0
        || ids.some((actionId) => actionId.length === 0 || actionId.length > 128)
        || canonicalJson(ids) !== canonicalJson([...new Set(ids)].sort())
    ) {
        throw new Error("authorization action ids are not canonical");
    }
    if (!Array.isArray(value.actions) || value.actions.length === 0) {
        throw new Error("apply actions are missing");
    }
    const actions = value.actions.map(validateManifestAction);
    if (canonicalJson(actions.map((action) => action.action_id).sort()) !== canonicalJson(ids)) {
        throw new Error("apply action set differs from authorization");
    }
    for (const action of actions) {
        const immutable = verified.actionsById.get(action.action_id);
        if (!immutable || canonicalJson(action) !== canonicalJson(immutable)) {
            throw new Error("apply action differs from immutable manifest");
        }
    }
    const ownActions = actions.filter((action) => action.executor === "editor");
    if (ownActions.length === 0) throw new Error("apply command has no editor capability");
    return {
        ...verified,
        command: value as unknown as InterventionApplyCommand,
        ownActions,
    };
}

export function verifyRestoreCommand(
    value: unknown,
    clientInstanceId: string,
): VerifiedRestoreCommand {
    if (!isRecord(value)) throw new Error("restore command is malformed");
    const restoreId = requireString(value.restore_id, "restore_id");
    const interventionId = requireString(value.intervention_id, "intervention_id");
    if (!RESTORE_REASONS.has(String(value.reason))) throw new Error("restore reason is invalid");
    const digest = requireString(value.manifest_sha256, "manifest_sha256");
    if (!/^[0-9a-f]{64}$/.test(digest)) throw new Error("restore digest is invalid");
    if (
        restoreId.length > 128
        || interventionId.length > 128
        || !UUID_PATTERN.test(String(value.boot_id))
        || !Number.isSafeInteger(value.requested_at_unix_ms)
        || Number(value.requested_at_unix_ms) < 0
        || !Number.isSafeInteger(value.requested_at_mono_ns)
        || Number(value.requested_at_mono_ns) < 0
    ) {
        throw new Error("restore identity or timestamp is invalid");
    }
    const rawActions = Array.isArray(value.actions) ? value.actions : [];
    if (rawActions.length === 0 || rawActions.length > 32) {
        throw new Error("restore actions are missing or oversized");
    }
    const ids = new Set<string>();
    const ownActions: RestoreAction[] = [];
    for (const raw of rawActions) {
        if (!isRecord(raw)) throw new Error("restore action is malformed");
        const id = requireString(raw.action_id, "restore action id");
        if (ids.has(id)) throw new Error("duplicate restore action");
        ids.add(id);
        if (id.length > 128) throw new Error("restore action id is oversized");
        const executor = requireString(raw.executor, "restore executor");
        if (!EXECUTORS.has(executor)) throw new Error("restore executor is invalid");
        const reverseCapability = requireString(raw.reverse_capability, "reverse capability");
        const originalAuthorizationId = requireString(
            raw.original_authorization_id,
            "original authorization id",
        );
        if (reverseCapability.length > 96 || originalAuthorizationId.length > 128) {
            throw new Error("restore capability provenance is oversized");
        }
        if (
            raw.inverse_receipt_id !== null
            && raw.inverse_receipt_id !== undefined
            && (
                typeof raw.inverse_receipt_id !== "string"
                || raw.inverse_receipt_id.length === 0
                || raw.inverse_receipt_id.length > 128
            )
        ) {
            throw new Error("inverse receipt id is invalid");
        }
        const ownerClientInstanceId = requireString(
            raw.owner_client_instance_id,
            "owner client instance id",
        );
        if (ownerClientInstanceId.length > 128) {
            throw new Error("owner client instance id is oversized");
        }
        const inverseJson = requireString(raw.inverse_payload_json, "inverse payload");
        if (inverseJson.length > 32_768) throw new Error("inverse payload is oversized");
        const inverse: unknown = JSON.parse(inverseJson);
        if (!isRecord(inverse) || canonicalJson(inverse) !== inverseJson) {
            throw new Error("inverse payload must encode an object");
        }
        const allowedReverse = executor === "browser"
            ? new Set(["close_created_tab", "restore_active_tab"])
            : executor === "editor"
                ? new Set(["restore_active_file"])
                : new Set<string>();
        if (!allowedReverse.has(String(raw.reverse_capability))) {
            throw new Error("restore capability is invalid for executor");
        }
        if (
            executor === "editor"
            && ownerClientInstanceId === clientInstanceId
        ) {
            ownActions.push(raw as unknown as RestoreAction);
        }
    }
    if (ownActions.length === 0) {
        throw new Error(
            "restore command has no editor capability for this client instance",
        );
    }
    return {
        command: value as unknown as InterventionRestoreCommand & { restore_id: string },
        ownActions,
    };
}
