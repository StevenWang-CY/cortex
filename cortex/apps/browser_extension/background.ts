/**
 * Cortex Chrome Extension — Background Service Worker
 *
 * Maintains a WebSocket connection to the Cortex daemon (ws://127.0.0.1:9473).
 * Receives STATE_UPDATE and INTERVENTION_TRIGGER messages.
 * Dispatches content script injection on intervention triggers.
 * Sends IDENTIFY and USER_ACTION messages to the daemon.
 */

import {
    groupSpecificTabs,
    hideNonActiveTabs,
    getSnapshot as getTabManagerSnapshot,
    restoreAllTabs,
    restoreHiddenTabs as restoreTabsForIntervention,
    saveTabSession,
    restoreTabSession,
    IndeterminateBrowserMutationError,
} from "./tab-manager";
import { getAuthToken } from "./lib/auth";
import { detectBrowser } from "./lib/browser";
import {
    PAGE_EXCERPT_MAX_CHARS,
    sanitizeContextText,
} from "./lib/context-privacy";
import {
    sanitizeActivityRecord,
    type ActivityRecord,
} from "./lib/activity-privacy";
import {
    canonicalizeActivityUrl as canonicalizeUrl,
    configureActivityStore,
    enrichWithRelatedTabs,
    loadActivities,
    prepareActivityRecordForStorage,
    saveActivities,
    scrubStoredActivityContent,
    upsertActivity,
} from "./lib/activity-store";
export { prepareActivityRecordForStorage } from "./lib/activity-store";
import {
    isCortexState,
    isSuggestedAction,
    normaliseInterventionPayload,
    truncatePayloadForLog,
} from "./lib/state-guards";
import {
    canonicalJson,
    sha256Hex,
    verifyActionManifest,
    verifyApplyCommand,
    verifiedPresentedActionIds,
    verifyRestoreCommand,
} from "./lib/intervention-transaction";
import { mayExtractPageContent } from "./lib/site-access";
import {
    createFocusSession,
    emptyDailyStats,
    focusSessionSnapshot,
    isDistractionForSession,
    resolveFocusPreset,
    updateFocusSessionState,
    type DailyStats,
    type FocusSession,
} from "./lib/focus-session";
import {
    BrowserContextCollector,
    type TabData,
} from "./lib/context-collector";
import {
    BrowserSessionStore,
    type PersistedSessionState,
} from "./lib/persisted-session";
import {
    ClientIdentityStore,
    FrameReplayGuard,
    ParseErrorWindow,
    ReconnectBackoff,
    SerialCommandQueue,
    WireEnvelopeEncoder,
    newWireId,
} from "./lib/daemon-connection";
import {
    CapabilityExecutor,
    UnsupportedCapabilityError,
    type CapabilityHandlers,
} from "./lib/capability-executor";
import { InterventionPresentationState } from "./lib/intervention-presentation";
import { TabActivationTelemetry } from "./lib/browser-telemetry";
import { sendNativeHostMessage } from "./lib/native-messaging";
import {
    DAEMON_WS_URL,
    DAEMON_HTTP_URL,
    LAUNCHER_HTTP_URL,
} from "./config";

// --- Types (generated from Pydantic — Debt-1 closure) ---
//
// ``WSMessage`` and ``SuggestedAction`` are emitted by
// ``python -m cortex.scripts.generate_ts_schemas`` from
// ``cortex/libs/schemas/*.py``. Hand-written copies of these
// interfaces previously lived in this file and drifted from the
// Pydantic models (F42/F43/F44/F45). The import below is the only
// canonical source; CI fails if it goes stale.
import type {
    SuggestedAction as SuggestedActionSchema,
    WSMessage as WSMessageSchema,
    LeetCodeStage as LeetCodeStageSchema,
    SubmissionResult as SubmissionResultSchema,
    InterventionTriggerPayload,
    SessionRecap as SessionRecapSchema,
    CostResponse as CostResponseSchema,
    ActionManifest,
    ActionReceipt,
    InterventionApplyCommand,
    InterventionAuthorizationRequest,
    InterventionReceiptBatch,
    InterventionRestoreCommand,
    ManifestAction,
    RestoreAction,
} from "./types/generated/cortex_schemas";

// The Pydantic JSON Schema marks default-having fields as optional
// (it reflects the deserialise-side contract). On the wire the
// serializer always emits every field — including those whose
// Python-side ``Field(default=...)`` makes the JSON Schema mark them
// optional. We promote the always-emitted fields back to required here
// so consumers below can rely on them existing. Genuinely-optional
// fields (``correlation_id`` etc.) stay optional.
type WSMetadataKeys =
    | "schema_version"
    | "protocol_version"
    | "event_id"
    | "sent_at_unix_ms"
    | "sent_at_mono_ns"
    | "boot_id";
type WSMessage = Omit<WSMessageSchema, "payload" | WSMetadataKeys> &
    Partial<Pick<WSMessageSchema, WSMetadataKeys>> & {
    payload: Record<string, unknown>;
};

// The default-factory fields on ``SuggestedAction`` (``action_id``,
// ``target``, ``category``, ``reversible``, ``metadata``) always exist
// on the wire — the Pydantic serializer materialises them. We narrow
// them to non-optional locally; the canonical type definition stays
// the generated one.
type SuggestedAction = Omit<
    SuggestedActionSchema,
    "action_id" | "target" | "label" | "reason" | "category"
        | "reversible" | "metadata"
> & {
    action_id: string;
    target: string;
    label: string;
    reason: string;
    category: NonNullable<SuggestedActionSchema["category"]>;
    reversible: boolean;
    metadata: Record<string, unknown>;
};

// --- Types (extension-local — not part of any Pydantic schema) ---

interface CortexState {
    state: string;
    support_state?: string;
    status?: "estimated" | "insufficient_evidence" | "warming_up";
    confidence: number;
    evidence_coverage?: number;
    scores: Record<string, number>;
    signal_quality: Record<string, number>;
    dwell_seconds: number;
    reasons: string[];
}

// --- Debug ---
// F46: DEBUG is now a *variable* with two layered sources:
//   1. Build-time env (`import.meta.env.CORTEX_DEBUG === "true"`).
//      Plasmo exposes process.env.PLASMO_PUBLIC_*; for our purposes we
//      read CORTEX_DEBUG via both `import.meta.env` (vite/vitest) and
//      `process.env` (node) so tests can flip it.
//   2. Runtime override via `chrome.storage.local.cortex_debug`. A
//      change to that key flips `DEBUG` immediately, no reload required.
function readBuildDebug(): boolean {
    try {
        const ime = (import.meta as unknown as { env?: Record<string, unknown> }).env;
        if (ime && typeof ime.CORTEX_DEBUG === "string" && ime.CORTEX_DEBUG === "true") {
            return true;
        }
    } catch {
        // import.meta may not be available in all execution contexts.
    }
    try {
        if (typeof process !== "undefined" && process.env && process.env.CORTEX_DEBUG === "true") {
            return true;
        }
    } catch {
        // process is not defined in plain browser builds.
    }
    return false;
}

let DEBUG = readBuildDebug();

// Hydrate runtime override on startup, then keep listening for changes.
try {
    chrome.storage.local.get("cortex_debug", (data) => {
        if (data && data.cortex_debug === true) DEBUG = true;
        if (data && data.cortex_debug === false) DEBUG = readBuildDebug();
    });
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== "local" || !changes.cortex_debug) return;
        const next = changes.cortex_debug.newValue;
        if (next === true) DEBUG = true;
        else if (next === false) DEBUG = readBuildDebug();
    });
} catch {
    // chrome.storage may not be present in some test harness branches.
}

/** Test-only: read the current resolved DEBUG value. */
export function _getDebugFlag(): boolean {
    return DEBUG;
}

// --- State ---

let ws: WebSocket | null = null;
let connected = false;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
const reconnectBackoff = new ReconnectBackoff(3_000, 30_000);
let intentionalDisconnect = false;
let sequence = 0;
// Browser WebSocket callbacks do not await an async `onmessage` handler. The
// envelope is still parsed and sequence-checked synchronously, while exact
// APPLY/RESTORE capability work is serialized on this dedicated chain.
const transactionCommands = new SerialCommandQueue((error: unknown) => {
    console.warn(
        "[cortex.bg] transaction command failed closed:",
        String(error),
    );
});

function enqueueTransactionCommand(
    operation: () => Promise<void>,
): Promise<void> {
    return transactionCommands.enqueue(operation);
}
const PROTOCOL_VERSION = "2.0";
const SUPPORTED_PROTOCOL_VERSIONS = ["2.0", "1.0"] as const;
let negotiatedProtocolVersion: (typeof SUPPORTED_PROTOCOL_VERSIONS)[number] =
    PROTOCOL_VERSION;
const clientIdentityStore = new ClientIdentityStore();
const wireEncoder = new WireEnvelopeEncoder({
    schemaVersion: "2.0",
    protocolVersion: () => negotiatedProtocolVersion,
});
const CLIENT_BOOT_ID = wireEncoder.bootId;

async function getClientInstanceId(): Promise<string> {
    return clientIdentityStore.get();
}

function withWireMetadata(msg: WSMessage): WSMessage {
    return wireEncoder.encode(msg as WSMessage & Record<string, unknown>) as WSMessage;
}
// DAEMON_WS_URL, DAEMON_HTTP_URL, LAUNCHER_HTTP_URL — imported from "./config"

let currentState: CortexState | null = null;

/**
 * F16: active intervention is now an atomic swap by correlation_id.
 *
 * A burst of overlapping INTERVENTION_TRIGGER frames must not overwrite
 * each other in arbitrary order. We mount the latest one and stamp it
 * with the daemon's correlation_id; outgoing USER_ACTION carries the
 * same cid so the daemon can ignore stale ACKs from a now-superseded
 * plan. `mountedAt` is the local mount timestamp (ms since epoch).
 */
const interventionPresentation = new InterventionPresentationState();
const browserContextCollector = new BrowserContextCollector();
let quietMode = false;

type ExecutionMode = "suggest_only" | "authorized" | "research_autonomous";
let currentExecutionMode: ExecutionMode = "suggest_only";

function parseExecutionMode(value: unknown): ExecutionMode {
    return value === "authorized" || value === "research_autonomous"
        ? value
        : "suggest_only";
}

function workspaceMutationAllowed(): boolean {
    return currentExecutionMode !== "suggest_only";
}

interface BrowserOperationRecord {
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

interface ConsumedAuthorizationRecord {
    manifest_sha256: string;
    nonce: string;
    consumed_at_unix_ms: number;
}

interface BrowserTransactionJournal {
    schema_version: "1";
    consumed_authorizations: Record<string, ConsumedAuthorizationRecord>;
    operations: Record<string, BrowserOperationRecord>;
    attempt_counters: Record<string, number>;
    receipt_outbox: InterventionReceiptBatch[];
}

const TRANSACTION_JOURNAL_KEY = "cortex_intervention_transaction_journal_v1";
const MAX_TRANSACTION_OPERATIONS = 512;
const MAX_TRANSACTION_COUNTERS = 2_048;
const MAX_RECEIPT_OUTBOX = 256;
const MAX_INVERSE_JSON_BYTES = 65_536;
const CREATED_TAB_STAGE_PREFIX = "about:blank#cortex-created-tab=";
let transactionJournalLock: Promise<void> = Promise.resolve();

function emptyTransactionJournal(): BrowserTransactionJournal {
    return {
        schema_version: "1",
        consumed_authorizations: {},
        operations: {},
        attempt_counters: {},
        receipt_outbox: [],
    };
}

function isJournalRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBoundedJournalString(
    value: unknown,
    maximum = 256,
): value is string {
    return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function isCanonicalObjectJson(value: unknown): value is string {
    if (
        typeof value !== "string"
        || value.length === 0
        || value.length > MAX_INVERSE_JSON_BYTES
    ) return false;
    try {
        const parsed = JSON.parse(value) as unknown;
        return isJournalRecord(parsed) && canonicalJson(parsed) === value;
    } catch {
        return false;
    }
}

function validateJournalReceiptBatch(value: unknown): InterventionReceiptBatch {
    if (!isJournalRecord(value)) throw new Error("receipt outbox batch is malformed");
    const interventionId = value.intervention_id;
    const authorizationId = value.authorization_id;
    const manifestSha256 = value.manifest_sha256;
    if (
        !isBoundedJournalString(interventionId)
        || !isBoundedJournalString(authorizationId)
        || typeof manifestSha256 !== "string"
        || !/^[0-9a-f]{64}$/.test(manifestSha256)
        || !Array.isArray(value.receipts)
        || value.receipts.length < 1
        || value.receipts.length > 96
    ) {
        throw new Error("receipt outbox batch fields are invalid");
    }
    for (const receipt of value.receipts) {
        if (!isJournalRecord(receipt)) throw new Error("receipt outbox entry is malformed");
        const numericFields = [
            receipt.started_at_unix_ms,
            receipt.ended_at_unix_ms,
            receipt.started_at_mono_ns,
            receipt.ended_at_mono_ns,
            receipt.duration_ms,
        ];
        if (
            !isBoundedJournalString(receipt.receipt_id, 128)
            || receipt.intervention_id !== interventionId
            || receipt.authorization_id !== authorizationId
            || receipt.manifest_sha256 !== manifestSha256
            || !isBoundedJournalString(receipt.action_id, 128)
            || !new Set(["apply", "compensate", "restore"]).has(String(receipt.phase))
            || !Number.isInteger(receipt.attempt)
            || Number(receipt.attempt) < 1
            || Number(receipt.attempt) > 100
            || !isBoundedJournalString(receipt.idempotency_key, 512)
            || !new Set(["succeeded", "failed", "already_complete"]).has(String(receipt.status))
            || numericFields.some((item) => typeof item !== "number" || !Number.isFinite(item) || item < 0)
            || Number(receipt.ended_at_unix_ms) < Number(receipt.started_at_unix_ms)
            || Number(receipt.ended_at_mono_ns) < Number(receipt.started_at_mono_ns)
            || !isBoundedJournalString(receipt.boot_id, 128)
            || !new Set(["verified", "failed", "not_applicable"]).has(String(receipt.verification))
            || (
                receipt.inverse_payload_json !== null
                && receipt.inverse_payload_json !== undefined
                && !isCanonicalObjectJson(receipt.inverse_payload_json)
            )
            || (
                receipt.after_fingerprint !== null
                && receipt.after_fingerprint !== undefined
                && (
                    typeof receipt.after_fingerprint !== "string"
                    || !/^[0-9a-f]{64}$/.test(receipt.after_fingerprint)
                )
            )
            || (receipt.source_client_type !== null && receipt.source_client_type !== undefined)
            || (receipt.source_client_id !== null && receipt.source_client_id !== undefined)
        ) {
            throw new Error("receipt outbox entry fields are invalid");
        }
    }
    return value as unknown as InterventionReceiptBatch;
}

function validateBrowserTransactionJournal(raw: unknown): BrowserTransactionJournal {
    if (!isJournalRecord(raw) || raw.schema_version !== "1") {
        throw new Error("Cortex transaction journal is corrupt");
    }
    const consumed = raw.consumed_authorizations;
    const operations = raw.operations;
    const counters = raw.attempt_counters ?? {};
    const outbox = raw.receipt_outbox ?? [];
    if (
        !isJournalRecord(consumed)
        || !isJournalRecord(operations)
        || !isJournalRecord(counters)
        || !Array.isArray(outbox)
        || Object.keys(consumed).length > 256
        || Object.keys(operations).length > MAX_TRANSACTION_OPERATIONS
        || Object.keys(counters).length > MAX_TRANSACTION_COUNTERS
        || outbox.length > MAX_RECEIPT_OUTBOX
    ) {
        throw new Error("Cortex transaction journal is corrupt");
    }
    const validatedConsumed: Record<string, ConsumedAuthorizationRecord> = {};
    for (const [authorizationId, candidate] of Object.entries(consumed)) {
        if (
            !isBoundedJournalString(authorizationId)
            || !isJournalRecord(candidate)
            || typeof candidate.manifest_sha256 !== "string"
            || !/^[0-9a-f]{64}$/.test(candidate.manifest_sha256)
            || !isBoundedJournalString(candidate.nonce, 256)
            || typeof candidate.consumed_at_unix_ms !== "number"
            || !Number.isFinite(candidate.consumed_at_unix_ms)
            || candidate.consumed_at_unix_ms < 0
        ) throw new Error("consumed authorization journal entry is invalid");
        validatedConsumed[authorizationId] = candidate as unknown as ConsumedAuthorizationRecord;
    }
    const knownCapabilities = new Set([
        "open_url", "search_error", "highlight_tab",
    ]);
    const validatedOperations: Record<string, BrowserOperationRecord> = {};
    for (const [key, candidate] of Object.entries(operations)) {
        if (
            !isBoundedJournalString(key, 300)
            || !isJournalRecord(candidate)
            || !isBoundedJournalString(candidate.intervention_id)
            || !isBoundedJournalString(candidate.action_id, 128)
            || key !== operationKey(candidate.intervention_id, candidate.action_id)
            || typeof candidate.manifest_sha256 !== "string"
            || !/^[0-9a-f]{64}$/.test(candidate.manifest_sha256)
            || !isBoundedJournalString(candidate.authorization_id)
            || typeof candidate.capability !== "string"
            || !knownCapabilities.has(candidate.capability)
            || !new Set(["applying", "applied", "failed", "restored"]).has(String(candidate.state))
            || !isCanonicalObjectJson(candidate.inverse_payload_json)
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
        ) throw new Error("browser operation journal entry is invalid");
        validatedOperations[key] = candidate as unknown as BrowserOperationRecord;
    }
    const validatedCounters: Record<string, number> = {};
    for (const [key, value] of Object.entries(counters)) {
        if (
            !isBoundedJournalString(key, 512)
            || !Number.isInteger(value)
            || Number(value) < 0
            || Number(value) > 100
        ) throw new Error("receipt attempt counter is invalid");
        validatedCounters[key] = Number(value);
    }
    const validatedOutbox = outbox.map(validateJournalReceiptBatch);
    const receiptIds = new Set<string>();
    for (const batch of validatedOutbox) {
        for (const receipt of batch.receipts) {
            const receiptId = String(receipt.receipt_id);
            if (receiptIds.has(receiptId)) {
                throw new Error("duplicate receipt id in transaction outbox");
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

async function readTransactionJournal(): Promise<BrowserTransactionJournal> {
    const data = await chrome.storage.local.get(TRANSACTION_JOURNAL_KEY);
    const raw = data[TRANSACTION_JOURNAL_KEY] as unknown;
    if (raw === undefined) return emptyTransactionJournal();
    return validateBrowserTransactionJournal(raw);
}

async function mutateTransactionJournal<T>(
    mutate: (journal: BrowserTransactionJournal) => T | Promise<T>,
): Promise<T> {
    let resolveTurn: (() => void) | undefined;
    const prior = transactionJournalLock;
    transactionJournalLock = new Promise<void>((resolve) => {
        resolveTurn = resolve;
    });
    await prior;
    try {
        const journal = await readTransactionJournal();
        const result = await mutate(journal);
        await chrome.storage.local.set({ [TRANSACTION_JOURNAL_KEY]: journal });
        return result;
    } finally {
        resolveTurn?.();
    }
}

interface PendingAuthorization {
    interventionId: string;
    actionIds: string[];
    authorizationId?: string;
    localResults: Map<string, ActionExecuteResult>;
    resolve: (results: ActionExecuteResult[]) => void;
    timeout: ReturnType<typeof setTimeout>;
}

const pendingAuthorizations = new Map<string, PendingAuthorization>();

/** Test-only witness for the fail-closed client authority state. */
export function _getExecutionMode(): ExecutionMode {
    return currentExecutionMode;
}

// --- Focus Session State ---

let focusSession: FocusSession | null = null;

// P0 §3.10: track whether the daemon armed the active focus session so
// the symmetric STOP_FOCUS_AUTO knows whether to tear down. The
// session goal carries the human-readable reason for popup display.
let autoFocusArmed: boolean = false;
let autoFocusEndsAt: number | null = null;
let autoFocusAlarmName: string | null = null;
// P0 §3.10: domain blocklist presets. Reduces the per-tick query to a
// stable preset → patterns map. The browser extension is the source
// of truth for the per-preset list; the daemon ships only the preset
// name + any user custom_domains.
let activeFocusPresetPatterns: RegExp[] = [];
let activeFocusCustomDomains: string[] = [];
// Phase-3 P1-DF-10.4: persisted preset name so the SW can rebuild
// ``activeFocusPresetPatterns`` after eviction (regex literals don't
// survive chrome.storage round-trips, but the string preset name does).
let _activeFocusPresetName: string = "developer";

// --- Recently-visited tab protection ---
// Track when each tab was last activated so we can protect recently-used tabs from closing
const tabActivationTelemetry = new TabActivationTelemetry();
const RECENTLY_ACTIVE_PROTECTION_MS = 5 * 60 * 1000; // 5 minutes

chrome.tabs.onActivated.addListener((activeInfo) => {
    tabActivationTelemetry.recordActivation(activeInfo.tabId);
    schedulePersist();
});

chrome.tabs.onRemoved.addListener((tabId) => {
    tabActivationTelemetry.recordRemoval(tabId);
    schedulePersist();
});

// --- State Persistence (survives MV3 service worker restarts) ---

const browserSessionStore = new BrowserSessionStore();

async function persistAutoFocusState(): Promise<void> {
    browserSessionStore.scheduleAutoFocus({
        autoFocusArmed,
        presetName: _activeFocusPresetName,
        presetPatternSources: activeFocusPresetPatterns.map(
            (pattern) => pattern.source,
        ),
    });
}

async function restoreAutoFocusStateLocal(): Promise<void> {
    try {
        const blob = await browserSessionStore.loadAutoFocus();
        if (!blob) return;
        // session storage takes precedence — it's the freshest source
        // when both are available. Only adopt local-state fields the
        // session restore left unset.
        if (blob.autoFocusArmed === true && !autoFocusArmed) {
            _activeFocusPresetName = blob.presetName;
            autoFocusArmed = true;
            // Rebuild patterns from the preset name (regex literals
            // don't survive JSON; the preset string does).
            activeFocusPresetPatterns = resolveFocusPreset(
                _activeFocusPresetName,
            );
        }
        // Sanity check (spec): inconsistent state where we claim
        // ``autoFocusArmed=true`` but there is no ``focusSession`` means
        // the SW restarted, restoreState lost the session payload, and
        // the auto bit got stranded on. Recover by clearing the bit and
        // notifying the daemon so its mirror bit clears too.
        if (autoFocusArmed && focusSession === null) {
            autoFocusArmed = false;
            activeFocusPresetPatterns = [];
            activeFocusCustomDomains = [];
            _activeFocusPresetName = "developer";
            await persistAutoFocusState();
            if (connected && ws) {
                try {
                    send({
                        type: "USER_ACTION",
                        payload: {
                            action: "auto_focus_inconsistent_state_recovered",
                            source: "browser_extension",
                            timestamp: Date.now() / 1000,
                        },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                } catch {
                    // WS may be mid-reconnect; the daemon will
                    // reconcile on the next STATE_UPDATE tick.
                }
            }
        }
    } catch {
        // storage.local may be unavailable (test contexts without the
        // mock); the in-memory defaults are safe.
    }
}

function schedulePersist(): void {
    browserSessionStore.scheduleSession(persistedSessionSnapshot());
}

function persistedSessionSnapshot(): PersistedSessionState<FocusSession, UndoEntry> {
    const cooldowns = interventionPresentation.cooldownSnapshot();
    return {
        focusSession,
        undoStack,
        dismissedInterventions: cooldowns.interventions,
        dismissedUrlPatterns: cooldowns.urls,
        quietMode,
        tabLastActivated: tabActivationTelemetry.entries(),
        autoFocusArmed,
        autoFocusEndsAt,
        autoFocusPreset: _activeFocusPresetName,
        autoFocusCustomDomains: activeFocusCustomDomains,
    };
}

/**
 * Shape of the persisted session state written by {@link schedulePersist}.
 * `chrome.storage.session.get` is typed `{ [key: string]: any }` → `{}` under
 * `--strict`, so we cast the result to this explicit shape to recover the
 * per-key element types (Map entries round-trip as `[K, V][]` arrays).
 */
async function restoreState(): Promise<void> {
    const data = await browserSessionStore.loadSession<FocusSession, UndoEntry>();
    if (data.focusSession) focusSession = data.focusSession;
    if (data.undoStack) {
        undoStack.splice(0, undoStack.length, ...data.undoStack);
    }
    interventionPresentation.hydrateCooldowns({
        interventions: data.dismissedInterventions,
        urls: data.dismissedUrlPatterns,
    });
    if (data.quietMode !== undefined) quietMode = data.quietMode;
    if (data.tabLastActivated) {
        tabActivationTelemetry.hydrate(data.tabLastActivated);
    }
    // Phase-3 P1-DF-10.4: rehydrate auto-focus state so isDistractionUrl
    // keeps blocking even across MV3 SW eviction.
    if (data.autoFocusArmed !== undefined) {
        autoFocusArmed = Boolean(data.autoFocusArmed);
    }
    if (typeof data.autoFocusEndsAt === "number") {
        autoFocusEndsAt = data.autoFocusEndsAt;
    }
    if (typeof data.autoFocusPreset === "string") {
        _activeFocusPresetName = data.autoFocusPreset;
        activeFocusPresetPatterns = resolveFocusPreset(
            _activeFocusPresetName,
        );
    }
    if (Array.isArray(data.autoFocusCustomDomains)) {
        activeFocusCustomDomains = data.autoFocusCustomDomains
            .filter((d: unknown): d is string => typeof d === "string");
    }
    // Auto-expire if a stale auto-armed session outlived its window.
    if (autoFocusArmed && autoFocusEndsAt !== null && Date.now() > autoFocusEndsAt) {
        stopAutoFocusSession("duration_elapsed_post_restore");
    }
    // Phase 4d Task A: also rehydrate from chrome.storage.local — the
    // session bucket clears on browser restart so a HYPER-armed session
    // that survived a Chrome relaunch loses its blocklist otherwise.
    // The local restore is best-effort and runs even if session restore
    // populated nothing; the sanity check inside handles the
    // inconsistent ``autoFocusArmed && focusSession === null`` case.
    await restoreAutoFocusStateLocal();
}

// Restore persisted state on service worker startup
restoreState();

// Health alert state
let lastHeadNeckAlert = 0;
let lastBlinkAlert = 0;
let lowBlinkStart = 0;
let headNeckFlexionStart = 0;
const HEALTH_ALERT_COOLDOWN = 300_000; // 5 min between alerts
const HEAD_NECK_ALERT_THRESHOLD = 180_000; // 3 min beyond calibrated neutral range
const BLINK_ALERT_THRESHOLD = 180_000;  // 3 min low blink rate

// Break recommendation state

// --- Activity Tracking State ---

configureActivityStore({
    isConnected: () => connected,
    sync: (activities) => {
        send({
            type: "ACTIVITY_SYNC",
            payload: { activities },
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
        });
    },
});

// --- WebSocket Connection ---

function connect(): void {
    if (connected || ws) {
        return;
    }
    intentionalDisconnect = false;

    try {
        ws = new WebSocket(DAEMON_WS_URL);

        ws.onopen = () => {
            connected = true;
            // F32: reset the reconnect backoff on every successful open so a
            // long-running disconnect cycle that finally succeeds doesn't
            // keep waiting 30s on the next transient drop.
            reconnectBackoff.reset();
            negotiatedProtocolVersion = PROTOCOL_VERSION;
            // F17 (audit): clear the per-type sequence tracker. The
            // daemon restarts its WSMessage.sequence counter from 0
            // each boot; keeping the pre-restart values would reject
            // every post-restart frame as stale until the new daemon's
            // counter caught up.
            _resetLastSeqByType();

            // Debt-2 (audit): AUTH is the contractual first frame on
            // every WebSocket connection. The daemon refuses every other
            // type until this validates. We fire-and-forget the token
            // fetch and send the AUTH frame as soon as it resolves;
            // IDENTIFY follows so the daemon can tag this client
            // ``client_type="chrome"`` for targeted broadcasts.
            //
            // ``getAuthToken`` is async because it may need to roundtrip
            // through the native host. The socket stays open in the
            // meantime; the daemon's gate will simply close us if we
            // delay too long, at which point ``onclose`` runs and the
            // reconnect loop retries with the (now-cached) token.
            void (async () => {
                try {
                    const authToken = await getAuthToken();
                    if (!ws || ws.readyState !== WebSocket.OPEN) {
                        return;
                    }
                    ws.send(JSON.stringify(withWireMetadata({
                        type: "AUTH",
                        payload: {
                            auth_token: authToken,
                            protocol_version: PROTOCOL_VERSION,
                            supported_protocol_versions: [...SUPPORTED_PROTOCOL_VERSIONS],
                        },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    } as WSMessage)));
                    // Identify the host browser (AFTER auth so the
                    // daemon-side ``IDENTIFY`` handler runs in the
                    // authenticated branch of dispatch). The same JS
                    // ships to both Chrome and Edge; ``detectBrowser()``
                    // chooses the right ``client_type`` so the desktop
                    // dashboard lights up the correct connection dot
                    // and downstream broadcasts that target a specific
                    // browser ("send only to Edge") work as advertised.
                    const browserClientType = detectBrowser();
                    const clientInstanceId = await getClientInstanceId();
                    send({
                        type: "IDENTIFY",
                        payload: {
                            client_type: browserClientType,
                            client_instance_id: clientInstanceId,
                        },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                    // P0 §3.3: ask the daemon to re-broadcast the most
                    // recent SESSION_RECAP it has cached. If a recap
                    // fired while the extension was disconnected (e.g.
                    // service-worker eviction, transient network drop),
                    // the popup still needs to surface it on next open.
                    send({
                        type: "REQUEST_SESSION_RECAP",
                        payload: {},
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                    // P0 §3.2: prime the "Last 7 days" sparkbar strip
                    // in the popup. We fire on every fresh WS connect
                    // so a freshly-opened popup immediately has trends
                    // data to render without waiting on the 30-minute
                    // refresh timer below. ``refresh: false`` asks the
                    // daemon to serve the cached aggregator output
                    // rather than re-running the (expensive) rollup.
                    send({
                        type: "REQUEST_TRENDS",
                        payload: { window: "week", refresh: false },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                } catch {
                    // Token unavailable — let the daemon close us; the
                    // reconnect loop will retry. Silent failure here is
                    // safer than crashing the service worker.
                }
            })();

            // Notify popup
            broadcastToPopup({ type: "CONNECTION_CHANGED", connected: true });
        };

        ws.onmessage = (event) => handleMessage(event.data as string);

        ws.onclose = () => {
            handleDisconnect();
        };

        ws.onerror = () => {
            // onclose will follow
        };
    } catch {
        scheduleReconnect();
    }
}

function disconnect(): void {
    intentionalDisconnect = true;
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
    }
    if (connected) {
        connected = false;
        broadcastToPopup({ type: "CONNECTION_CHANGED", connected: false });
    }
}

function send(msg: WSMessage): void {
    if (!ws || !connected) return;
    try {
        ws.send(JSON.stringify(withWireMetadata(msg)));
    } catch {
        // Connection may have dropped
    }
}

/**
 * B.2: ack an intervention apply / restore phase back to the daemon.
 *
 * The daemon's executor uses an _OptimisticInterventionAdapter that
 * defaults every Mutation.success to True. Without this ack, the
 * browser side (where >80% of mutations live — tab hides, overlay
 * injections, distraction blocks) silently reports success regardless
 * of actual outcome. See cortex/services/runtime_daemon.py
 * `_handle_intervention_applied`.
 */
function sendInterventionApplied(
    interventionId: string,
    phase: "apply" | "restore",
    success: boolean,
    appliedActions: string[],
    errors: string[],
): void {
    send({
        type: "INTERVENTION_APPLIED",
        payload: {
            intervention_id: interventionId,
            phase,
            success,
            applied_actions: appliedActions,
            errors,
        },
        timestamp: Date.now() / 1000,
        sequence: ++sequence,
    });
}

function handleDisconnect(): void {
    ws = null;
    if (connected) {
        connected = false;
        broadcastToPopup({ type: "CONNECTION_CHANGED", connected: false });
    }
    // G2 (audit-prod): probe the four-state diagnostic on disconnect so
    // the popup can render an actionable error (not_installed /
    // installed_no_daemon / version_mismatch / handshake_failed).
    void probeConnectivity("disconnected").catch((err: unknown) => {
        if (DEBUG) console.debug("[cortex.bg] probeConnectivity(disconnected) failed: %o", err);
    });
    if (!intentionalDisconnect) {
        scheduleReconnect();
    }
}

/**
 * G2 (audit-prod): emit a ``CONNECTIVITY_DIAGNOSTIC`` extension-internal
 * message that the popup consumes (popup.tsx:423). Three probes:
 *  1. native-host present? (chrome.runtime.sendNativeMessage round-trip)
 *  2. daemon version (HTTP /health → ``version`` field)
 *  3. last WS close reason (handshake error?)
 *
 * Each probe is best-effort with a tight timeout; failures slot into
 * the diagnostic payload as ``missing`` / ``null`` so the popup can map
 * to its four-state UI.
 */
async function probeConnectivity(trigger: string): Promise<void> {
    let nativeHostStatus: "present" | "missing" = "missing";
    let nativeHostError: string | null = null;
    try {
        const response = await sendNativeHostMessage(
            { command: "status" },
            { timeoutMs: 5_000 },
        );
        if (response.command !== "status") {
            throw new Error("unexpected_native_host_response");
        }
        nativeHostStatus = "present";
    } catch (error) {
        nativeHostStatus = "missing";
        nativeHostError = error instanceof Error ? error.message : String(error);
    }

    let daemonVersion: string | null = null;
    try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 1500);
        const resp = await fetch(`${DAEMON_HTTP_URL}/health`, {
            signal: ctrl.signal,
        });
        clearTimeout(t);
        if (resp.ok) {
            const body = (await resp.json()) as { version?: string | null };
            daemonVersion = body.version ?? null;
        }
    } catch {
        daemonVersion = null;
    }

    let handshakeError: string | null = null;
    if (trigger === "disconnected" && !connected) {
        // The close reason is captured by the ws.onclose listener; for
        // now, surface a generic indicator that the WS path failed.
        handshakeError = daemonVersion === null && nativeHostStatus === "missing"
            ? null
            : "websocket_failed";
    }

    broadcastToPopup({
        type: "CONNECTIVITY_DIAGNOSTIC",
        payload: {
            native_host_status: nativeHostStatus,
            native_host_error: nativeHostError,
            daemon_version: daemonVersion,
            handshake_error: handshakeError,
        },
    });
}

function scheduleReconnect(): void {
    if (reconnectTimer || intentionalDisconnect) return;
    const delay = reconnectBackoff.takeAndAdvance();
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, delay);
}

// swift-concurrency-pro rule (transferred to JS): tear down all in-flight
// timers when the service worker is suspended so they don't fire against a
// torn-down WS instance and cause spurious reconnect attempts. Chrome
// emits ``runtime.onSuspend`` ~30s before evicting the worker.
if (typeof chrome !== "undefined" && chrome.runtime?.onSuspend) {
    chrome.runtime.onSuspend.addListener(() => {
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
        browserSessionStore.cancelPendingWrites();
        void browserSessionStore.saveSessionNow(persistedSessionSnapshot());
        try {
            disconnect();
        } catch {
            /* worker is going away anyway */
        }
    });
}

// --- Text Scraping ---

async function scrapeVisibleText(tabId?: number): Promise<string> {
    try {
        let targetTabId = tabId;
        if (!targetTabId) {
            // F3 (Phase-4 audit): destructure-and-check rather than the
            // ``[0]?.id`` shorthand. The shorthand worked but obscured
            // the empty-array contingency; the explicit guard documents
            // the "no active tab" branch and lets us log it.
            const tabs = await chrome.tabs.query({
                active: true,
                currentWindow: true,
            });
            if (!tabs.length) {
                console.warn("[cortex.bg] scrapeVisibleText: no active tab");
                return "";
            }
            targetTabId = tabs[0]?.id;
        }
        if (!targetTabId) return "";
        const response = await chrome.tabs.sendMessage(targetTabId, { type: "EXTRACT_TEXT" });
        return response?.text || "";
    } catch (err) {
        console.warn("[cortex.bg] scrapeVisibleText failed:", err);
        return "";
    }
}

// --- Message Handling ---

/**
 * F15: WS streaming JSON parse failures are surfaced rather than silently
 * dropped. We count failures within a 10s window and force a reconnect
 * after 3 errors so a corrupted upstream stream produces a recovery
 * cycle instead of an indefinitely silent black hole.
 */
const WS_PARSE_ERROR_WINDOW_MS = 10_000;
const WS_PARSE_ERROR_RECONNECT_THRESHOLD = 3;
const parseErrors = new ParseErrorWindow(
    WS_PARSE_ERROR_WINDOW_MS,
    WS_PARSE_ERROR_RECONNECT_THRESHOLD,
);

export function _resetWsParseErrorCounter(): void {
    parseErrors.reset();
}

export function _getWsParseErrorCount(): number {
    return parseErrors.count;
}

/** Test-only: expose the F32 reconnect delay so tests can verify it
 * resets on every successful WS open. */
export function _getReconnectDelay(): number {
    return reconnectBackoff.current;
}

export function _getInitialReconnectDelay(): number {
    return reconnectBackoff.initialDelay;
}

/**
 * F17 (audit): per-message-type last-applied envelope ``sequence``.
 *
 * The daemon's WS server increments ``WSMessage.sequence`` once per
 * outbound message; we drop any frame whose sequence is not strictly
 * greater than the last applied value for its type. This protects
 * INTERVENTION_TRIGGER (where a reordered frame could clobber the
 * active intervention plan) and STATE_UPDATE (where stale frames
 * could overwrite the user-visible biometric state).
 *
 * Frames with ``sequence === 0`` (older daemons, broadcast types that
 * the server never bumps) bypass the check — the default behaviour
 * is to apply the frame, preserving backwards compatibility with the
 * pre-F17 contract.
 *
 * Reset to ``{}`` on every successful WS open: the daemon's counter
 * starts at 0 each restart, so retaining the pre-restart values would
 * reject every post-restart frame as "stale".
 */
const frameReplayGuard = new FrameReplayGuard(512);

export function _resetLastSeqByType(): void {
    frameReplayGuard.reset();
}

export function _getLastSeq(msgType: string): number {
    return frameReplayGuard.lastSequence(msgType);
}

/**
 * Returns true iff the frame should be APPLIED (i.e. its sequence
 * advances the per-type counter). The function updates the counter
 * as a side effect when it accepts the frame; callers that decide to
 * accept-then-discard for a different reason are still safe because
 * the counter only moves forward.
 */
function acceptSequencedFrame(msg: WSMessage): boolean {
    return frameReplayGuard.accept(msg);
}

/** Test-only: expose the sequence-drop predicate so vitest can exercise
 * the F17 logic without spinning a real WebSocket / handleMessage. */
export function _acceptSequencedFrame(msg: WSMessage): boolean {
    return acceptSequencedFrame(msg);
}

function recordWsParseError(err: unknown, msg: Partial<WSMessage> | null): void {
    const cid =
        msg && typeof (msg as { correlation_id?: unknown }).correlation_id === "string"
            ? (msg as { correlation_id?: string }).correlation_id
            : "-";
    console.warn(`cortex.ws.parse_error cid=${cid} err=${String(err)}`);
    const storm = parseErrors.record();
    if (storm && ws !== null) {
        console.warn(
            `cortex.ws.parse_error_storm count=${parseErrors.count} ` +
                `window_ms=${WS_PARSE_ERROR_WINDOW_MS} — forcing reconnect`,
        );
        parseErrors.reset();
        try {
            // Bypass `disconnect()` because that sets intentionalDisconnect
            // and suppresses the reconnect we want.
            ws.close(1008, "cortex.ws.parse_error_storm");
        } catch {
            // ws already closed; let onclose drive reconnect.
        }
    }
}

async function handleMessage(raw: string): Promise<void> {
    let msg: WSMessage;
    try {
        msg = JSON.parse(raw) as WSMessage;
    } catch (err) {
        recordWsParseError(err, null);
        return;
    }
    // Reset the rolling counter on a clean parse so transient flakes do
    // not stay armed forever.
    if (parseErrors.count > 0) {
        parseErrors.reset();
    }

    // F17 (audit): drop reordered or duplicated frames before they reach
    // the per-type handlers. AUTH_OK is excluded from the check by the
    // ``seq <= 0`` early-return inside acceptSequencedFrame (the daemon
    // emits AUTH_OK without bumping the sequence counter; see
    // websocket_server.py::_auth_ok_frame).
    if (!acceptSequencedFrame(msg)) {
        if (DEBUG) {
            console.warn(
                `[cortex.bg] F17: dropping stale ${msg.type} seq=${msg.sequence} ` +
                `last=${frameReplayGuard.lastSequence(msg.type)}`,
            );
        }
        return;
    }

    switch (msg.type) {
        case "AUTH_OK": {
            // Debt-2 (audit): the daemon ACKed our AUTH frame. Nothing
            // to do — the daemon will start broadcasting STATE_UPDATE
            // and other types on its own cadence. We accept this frame
            // as a known type so the legacy `default` branch (which
            // would otherwise treat it as "unknown") cannot accidentally
            // re-classify it as a parse error.
            const selected = msg.payload.selected_protocol_version;
            if (selected === "1.0" || selected === "2.0") {
                negotiatedProtocolVersion = selected;
            }
            // Receipts are persisted before their first send. Replaying the
            // bounded outbox after authentication closes the socket-drop
            // window between a workspace effect and daemon acknowledgement;
            // server-side receipt/idempotency keys make this safe.
            void flushReceiptOutbox();
            break;
        }

        case "PROTOCOL_ERROR":
            console.error(
                `[cortex.bg] protocol negotiation failed: ${JSON.stringify(msg.payload)}`,
            );
            intentionalDisconnect = true;
            try {
                ws?.close(1002, "unsupported Cortex protocol");
            } catch {
                // Socket may already be closing after the server error frame.
            }
            break;

        case "STATE_UPDATE":
            // F1 (Phase-4 audit): validate the payload shape at runtime
            // before committing it to ``currentState``. A malformed
            // STATE_UPDATE (legacy daemon, fuzzed frame, corrupted
            // upstream) would otherwise blow up the popup's
            // ``Object.entries`` / numeric reads at render time. Drop
            // the message and warn with a truncated dump so the bug
            // shows up in dev tools without leaking the full payload.
            if (!isCortexState(msg.payload)) {
                console.warn(
                    "[cortex.bg] F1: dropping malformed STATE_UPDATE payload:",
                    truncatePayloadForLog(msg.payload),
                );
                break;
            }
            // F1: ``isCortexState`` narrows to the runtime-validated
            // shape, so the assignment is now safe without the
            // ``as unknown as`` ladder. The local interface differs
            // from the guard's interface only in optional fields.
            currentState = {
                state: msg.payload.state,
                support_state: msg.payload.support_state,
                status: msg.payload.status,
                confidence: msg.payload.confidence,
                evidence_coverage: msg.payload.evidence_coverage,
                scores: msg.payload.scores,
                signal_quality: msg.payload.signal_quality,
                dwell_seconds: msg.payload.dwell_seconds,
                reasons: msg.payload.reasons,
            };
            updateFocusSession(msg.payload);
            checkHealthAlerts(msg.payload);
            broadcastToPopup({
                type: "STATE_UPDATE",
                payload: msg.payload,
                focusSession: focusSession ? getFocusSessionSnapshot() : null,
            });
            // Forward to all content scripts for ambient effects
            broadcastToContentScripts({
                type: "AMBIENT_STATE_UPDATE",
                payload: msg.payload,
            });
            break;

        case "INTERVENTION_TRIGGER": {
            // F2 (Phase-4 audit): normalise the payload into a typed
            // shape before dispatching. Missing ``intervention_id`` or
            // ``intervention_type`` → log + skip; missing numeric
            // fields default to 0; missing ``actions`` defaults to [].
            // The downstream ``handleIntervention`` still receives the
            // raw payload for fields it reads opportunistically (e.g.
            // ui_plan, hide_targets, micro_steps).
            const plan = normaliseInterventionPayload(msg.payload);
            if (plan === null) {
                console.warn(
                    "[cortex.bg] F2: dropping malformed INTERVENTION_TRIGGER:",
                    truncatePayloadForLog(msg.payload),
                );
                break;
            }
            // Finding 5: typed view of the wire payload against the
            // generated InterventionTriggerPayload so the OS-notification
            // path's field reads are caught at compile time on a daemon
            // rename. The raw object is still ``Record<string, unknown>``
            // on the wire; the cast is the single boundary point.
            const triggerPayload = msg.payload as
                & InterventionTriggerPayload
                & Record<string, unknown>;
            // Missing and unknown modes fail closed. A legacy trigger can
            // therefore never inherit authority from an earlier frame.
            currentExecutionMode = parseExecutionMode(
                triggerPayload.execution_mode,
            );
            const iid = plan.intervention_id;
            const now = Date.now();

            const triggerUrl = plan.trigger_url;
            const suppressedBy = interventionPresentation.suppression(
                iid,
                triggerUrl,
                now,
            );
            if (suppressedBy !== null) {
                if (DEBUG) {
                    console.log(
                        `Cortex: skipping intervention ${iid} — ${suppressedBy} cooldown active`,
                    );
                }
                break;
            }

            // F16: atomic swap by correlation_id. The latest INTERVENTION_TRIGGER
            // always wins; any in-flight USER_ACTION ACK for a superseded plan
            // is ignored by the daemon (F16-srv). The local cid falls back to
            // a synthetic value so the swap still works when the daemon omits
            // a correlation_id (e.g. legacy frames).
            const inboundCid = typeof msg.correlation_id === "string" && msg.correlation_id.length > 0
                ? msg.correlation_id
                : `local_${now.toString(36)}_${Math.random().toString(36).slice(2, 10)}`;

            if (interventionPresentation.active) {
                if (DEBUG) {
                    console.log(
                        `Cortex: superseding intervention cid=${interventionPresentation.active.correlation_id} ` +
                        `→ cid=${inboundCid}`,
                    );
                }
            }

            interventionPresentation.mount(msg.payload, inboundCid, now);
            // Persist so popup can load it after SW restart
            try {
                chrome.storage.session.set({
                    cortex_active_intervention: msg.payload,
                    cortex_active_intervention_cid: inboundCid,
                    cortex_active_intervention_mounted_at: now,
                });
            } catch (err) {
                // F4: storage.session may be unavailable (very rare —
                // e.g. SW in odd reload state). Log so we have a
                // diagnostic trail when popup-after-SW-restart breaks.
                console.warn(
                    "[cortex.bg] persist active intervention failed:",
                    err,
                );
            }
            handleIntervention(msg.payload);
            // P0 §3.12: when the desktop dashboard isn't focused the
            // daemon stamps ``desktop_not_focused: true`` on the wire.
            // Bump the toolbar badge + fire ``chrome.notifications`` so
            // the user notices the intervention from another Space /
            // fullscreen app. The notification body is the
            // LLM-generated headline only (already F09-sanitised).
            // Phase-3 P2-DF-12.5: respect quiet mode — if the user
            // has snoozed or paused, the OS notification fall-through
            // is just another surrogate overlay and should also be
            // suppressed.
            if (plan.desktop_not_focused && !quietMode) {
                try {
                    surfaceInterventionOSNotification(triggerPayload, inboundCid);
                } catch (err) {
                    // chrome.notifications may not be available in odd
                    // test environments; never crash the dispatcher.
                    console.warn(
                        "[cortex.bg] surfaceInterventionOSNotification failed:",
                        err,
                    );
                }
            }
            break;
        }

        case "START_FOCUS_AUTO": {
            // A daemon state transition is not a one-time workspace grant.
            // Manual START_FOCUS remains available; autonomous arming stays
            // contained until it is expressed as a manifest-bound capability.
            console.warn("[cortex.bg] rejected legacy START_FOCUS_AUTO");
            break;
        }

        case "STOP_FOCUS_AUTO": {
            // P0 §3.10: daemon detected sustained recovery (or the
            // user disarmed). Only tear down if WE armed the session.
            const reason = (msg.payload.reason as string | undefined) || "sustained_recovery";
            try {
                stopAutoFocusSession(reason);
            } catch (e) {
                console.warn("Cortex: failed to stop auto focus session", e);
            }
            break;
        }

        case "QUIET_MODE_STATE": {
            // P0 §3.11: keep the popup pill in sync with the daemon's
            // active quiet/pause mode. The popup mirrors quietMode for
            // its existing display logic; the new ``quietModeKind``
            // surface lets future popup builds show the specific kind
            // ("paused"/"snoozed") without a separate WS hop.
            const kind = (msg.payload.kind as string | undefined) || "off";
            quietMode = kind !== "off";
            schedulePersist();
            // Phase-3 P0-2: cache the full envelope so a popup
            // mounted AFTER the broadcast (or after SW eviction)
            // sees the right kind / countdown / source on first paint.
            try {
                chrome.storage.session.set({
                    cortex_quiet_state: msg.payload,
                });
            } catch { /* session storage unavailable */ }
            broadcastToPopup({
                type: "QUIET_MODE_STATE",
                payload: msg.payload,
            });
            break;
        }

        case "CONTEXT_REQUEST":
            handleContextRequest(msg);
            break;

        case "INTERVENTION_APPLY":
            await enqueueTransactionCommand(
                () => handleInterventionApplyCommand(msg.payload),
            );
            break;

        case "INTERVENTION_AUTHORIZATION_DENIED":
            settleDeniedAuthorization(msg.payload);
            break;

        case "INTERVENTION_TRANSACTION_STATE":
            await acknowledgeReceiptOutbox(msg.payload);
            settleAuthorizationFromState(msg.payload);
            broadcastToPopup({
                type: "INTERVENTION_TRANSACTION_STATE",
                payload: msg.payload,
            });
            break;

        case "INTERVENTION_RESTORE":
            await enqueueTransactionCommand(() => handleRestore(msg.payload));
            break;

        case "SETTINGS_SYNC":
            quietMode = Boolean(msg.payload.quiet_mode);
            currentExecutionMode = parseExecutionMode(
                msg.payload.execution_mode,
            );
            interventionPresentation.configureCooldowns({
                interventionMs: msg.payload.intervention_dismiss_cooldown_ms,
                urlMs: msg.payload.url_dismiss_cooldown_ms,
            });
            schedulePersist();
            broadcastToPopup({ type: "SETTINGS_SYNC", payload: msg.payload });
            break;

        case "ACTION_DISPATCH": {
            // Compatibility frames are presentation-only. Since WP-6, the
            // sole mutation entry point is an exact INTERVENTION_APPLY whose
            // authorization and immutable manifest both validate locally.
            console.warn("[cortex.bg] rejected legacy ACTION_DISPATCH");
            break;
        }

        case "BREATHING_OVERLAY": {
            // Disabled compatibility message: the producing algorithm has
            // not passed reference validation, so do not present its claim.
            break;
        }
        case "ACTIVE_RECALL": {
            // Get visible text, add to payload, then route to content script
            const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
            if (tab?.id) {
                const visibleText = await scrapeVisibleText(tab.id);
                chrome.tabs.sendMessage(tab.id, {
                    type: "SHOW_ACTIVE_RECALL",
                    payload: { ...msg.payload, visible_text: visibleText },
                });
            }
            break;
        }

        case "PRE_BREAK_WARNING": {
            // Disabled compatibility message; see BREATHING_OVERLAY above.
            break;
        }



        case "LEETCODE_SHOW_LOCKOUT": {
            // A lockout changes what the user can do on the page. Keep this
            // compatibility message inert until it has an exact authorization
            // and receipt-backed escape/restore path.
            break;
        }

        case "LEETCODE_SHOW_SCRATCHPAD":
        case "LEETCODE_SHOW_PATTERN_LADDER":
        case "LEETCODE_SHOW_SUBMISSION_GATE":
        case "LEETCODE_SHOW_SOLUTION_FRICTION":
        case "LEETCODE_SHOW_CONSOLIDATION": {
            try {
                const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                if (tab?.id) {
                    await chrome.scripting.executeScript({
                        target: { tabId: tab.id },
                        func: injectLeetCodeCoachOverlay,
                        args: [msg.type, msg.payload],
                    });
                }
            } catch (e) {
                if (DEBUG) console.error("Cortex: failed to inject LeetCode coach overlay", e);
            }
            break;
        }

        case "MORNING_BRIEFING": {
            broadcastToPopup({
                type: "MORNING_BRIEFING",
                payload: msg.payload,
            });
            break;
        }

        case "BREAK_RECOMMENDATION": {
            // Only explicit elapsed-focus reminders are product-eligible.
            // Legacy stress-integral messages remain a silent decode sink.
            if (msg.payload.basis !== "elapsed_focus") break;
            const reason = typeof msg.payload.reason === "string"
                ? msg.payload.reason
                : "You've reached your preferred focus interval.";
            injectToast("Break reminder", reason);
            broadcastToPopup({ type: "BREAK_SUGGESTED", reason });
            break;
        }

        case "WHY_DETAIL": {
            // P0 §3.9: response to a WHY_DETAIL_REQUEST issued from the
            // popup. The daemon resolved the structured causal signals
            // against the per-intervention cache (or the live feature
            // vector as fallback); pipe them into the popup so the
            // drilldown can render without an extra round-trip.
            broadcastToPopup({
                type: "WHY_DETAIL",
                payload: msg.payload,
            });
            break;
        }

        case "SESSION_RECAP": {
            // P0 §3.3: end-of-session recap landed. Cache the full
            // SessionReport so the popup can render it on next open
            // (even if the WS has since disconnected), notify any
            // currently-open popup so it re-renders immediately, and
            // light up the toolbar badge as the user-facing signal that
            // a fresh recap is waiting.
            //
            // Phase 4 hardening:
            //   1. Gate on a non-empty ``session_id``. Phase 4.A made
            //      the daemon respond to REQUEST_SESSION_RECAP with
            //      ``{}`` when no recap is cached (rather than silently
            //      dropping the request). Treat any payload missing a
            //      string ``session_id`` as "no recap" — do not cache,
            //      do not badge, do not broadcast — so the empty
            //      handshake reply cannot resurface a phantom card.
            //   2. Respect a previously-dismissed session_id. The user
            //      explicitly clicked "Dismiss" on this recap; the
            //      daemon may re-broadcast (e.g. on extension reconnect)
            //      but we honour the dismissal.
            // C4: the daemon sends the declared SessionRecap wrapper
            // ``{report: SessionReport, generated_at: str, persisted: bool}``
            // so schema == wire. The session_id lives at
            // ``payload.report.session_id`` (NOT ``payload.session_id`` —
            // that was the pre-C4 flattened shape that drifted from the
            // SessionRecap schema). We cache + broadcast the full wrapper
            // unchanged so the popup can read ``payload.report.*`` and
            // ``payload.persisted``.
            const recapPayload = msg.payload as
                | (Partial<SessionRecapSchema> & Record<string, unknown>)
                | undefined;
            const recapReport = (recapPayload?.report ?? null) as
                | { session_id?: unknown }
                | null;
            const sessionIdRaw = recapReport?.session_id;
            const hasValidSessionId =
                typeof sessionIdRaw === "string" && sessionIdRaw.length > 0;
            if (!hasValidSessionId) {
                if (DEBUG) {
                    console.debug(
                        "[cortex.bg] SESSION_RECAP with empty/missing session_id; " +
                            "skipping cache + badge + broadcast.",
                    );
                }
                break;
            }
            const incomingSessionId = sessionIdRaw as string;
            chrome.storage.local.get(
                ["cortex.dismissedRecapSessionId"],
                (data) => {
                    const dismissedId = data?.[
                        "cortex.dismissedRecapSessionId"
                    ] as string | undefined;
                    if (dismissedId && dismissedId === incomingSessionId) {
                        if (DEBUG) {
                            console.debug(
                                `[cortex.bg] SESSION_RECAP session_id=${incomingSessionId} ` +
                                    "was already dismissed by user; suppressing.",
                            );
                        }
                        return;
                    }
                    const timestamp = Date.now();
                    try {
                        chrome.storage.local.set({
                            "cortex.lastRecap": recapPayload,
                            "cortex.lastRecapTimestamp": timestamp,
                        });
                    } catch (err) {
                        // storage.local may be unavailable in odd test
                        // environments — log so a real regression is
                        // visible rather than silently swallowed.
                        if (DEBUG) {
                            console.warn(
                                "[cortex.bg] SESSION_RECAP storage.local.set failed",
                                err,
                            );
                        }
                    }
                    broadcastToPopup({
                        type: "SESSION_RECAP_READY",
                        payload: recapPayload,
                        timestamp,
                    });
                    setRecapBadge(true);
                },
            );
            break;
        }

        case "SESSION_LIST":
        case "SESSION_DETAIL": {
            // P0 §3.1: silent forward-compatibility no-op. The desktop
            // shell consumes these directly; the popup does not render
            // them yet. A debug log keeps the frame visible to anyone
            // debugging the wire without surfacing in production.
            if (DEBUG) {
                console.debug(
                    `Cortex: received ${msg.type} (no extension handler — see desktop shell)`,
                );
            }
            break;
        }

        case "TRENDS_PAYLOAD": {
            // P0 §3.2: cache the trends rollup so the popup can render
            // its "Last 7 days" sparkbar strip on next mount even if
            // the WS has since dropped, then broadcast TRENDS_READY so
            // any currently-open popup re-renders immediately. Mirrors
            // the SESSION_RECAP wiring above.
            const trendsPayload = msg.payload;
            const timestamp = Date.now();
            try {
                chrome.storage.local.set({
                    "cortex.lastTrends": trendsPayload,
                    "cortex.lastTrendsTimestamp": timestamp,
                });
            } catch {
                // storage.local may be unavailable in odd test environments
            }
            broadcastToPopup({
                type: "TRENDS_READY",
                payload: trendsPayload,
                timestamp,
            });
            break;
        }

        case "INTERVENTION_FAILED": {
            // P1-FC-INTERVENTION-FAILED: the daemon's InterventionExecutor
            // returned only failed mutations — the workspace was NOT
            // changed. This message previously had no consumer on the
            // browser surface, so a total mutation failure was silently
            // invisible. Relay it to the popup so the intervention card
            // flips to an error state and disables its CTA.
            broadcastToPopup({
                type: "INTERVENTION_FAILED",
                payload: msg.payload,
            });
            break;
        }

        case "INTERVENTION_PROMPT": {
            // P1-FC-INTERVENTION-PROMPT: cross-surface micro-commit /
            // movement-break prompt. Consumed inline on the desktop
            // overlay but previously DROPPED on the browser, so a
            // popup-open user got no awareness of an active prompt.
            // Forward it so the popup can render the prompt text inline
            // above the action card (informational).
            broadcastToPopup({
                type: "INTERVENTION_PROMPT",
                payload: msg.payload,
            });
            break;
        }

        default: {
            // Defensive visibility for a schema-valid frame introduced
            // before this client has gained a dedicated handler. The
            // repository contract gate separately rejects catalogue
            // members without a production producer or consumer.
            if (DEBUG) {
                console.warn(
                    "Cortex: received WS frame with no handler",
                    msg.type,
                );
            }
            break;
        }
    }
}

/**
 * Injected directly into the page via chrome.scripting.executeScript.
 * Creates the intervention overlay using Shadow DOM.
 *
 * Design: dark, high-end tech (Linear/Raycast-inspired).
 * Consistent with popup and all other Cortex UI.
 */
export function injectOverlay(
    payload: Record<string, unknown>,
    executableActionIds: string[] = [],
): void {
    const OID = "cortex-somatic-overlay";
    type ManagedOverlayHost = HTMLElement & {
        __cortexCleanup?: () => void;
    };
    const existingHost = document.getElementById(OID) as ManagedOverlayHost | null;
    existingHost?.__cortexCleanup?.();
    const isUpdate = existingHost !== null;

    const headline = String(payload.headline || "");
    const summary = String(payload.situation_summary || "");
    const steps = (payload.micro_steps as string[]) || [];
    const esc = (s: string) =>
        s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

    const actions: Array<Record<string, unknown>> = [
        ...((payload.suggested_actions as Array<Record<string, unknown>>) || []),
    ];
    const tabRecs = payload.tab_recommendations as { tabs: Array<Record<string, unknown>>; summary: string } | undefined;
    const errA = payload.error_analysis as Record<string, string> | undefined;
    // The background worker verified these IDs against the digest-covered
    // manifest immediately before injection. Recommendations absent from
    // that set remain readable guidance; they never acquire an affordance.
    const executableIdSet = new Set(executableActionIds);
    const executableRecommended = actions.filter((action) =>
        action.category === "recommended"
        && typeof action.action_id === "string"
        && executableIdSet.has(action.action_id)
    );
    const canExecute = payload.execution_mode === "authorized"
        || payload.execution_mode === "research_autonomous";

    // --- Build tab list with per-tab Keep buttons (LAYER 5) ---
    let closingHtml = "";
    let keepCount = 0;
    let closeCount = 0;
    if (tabRecs && tabRecs.tabs && tabRecs.tabs.length > 0) {
        const closeTabs = tabRecs.tabs.filter(t => t.action === "close" || t.action === "bookmark_and_close");
        const keepTabs = tabRecs.tabs.filter(t => t.action === "keep");
        keepCount = keepTabs.length;
        closeCount = closeTabs.length;

        if (closeTabs.length > 0) {
            closingHtml = `<div class="tl">`;
            for (let ti = 0; ti < closeTabs.length; ti++) {
                const t = closeTabs[ti];
                const tabTitle = esc(String(t.tab_title || "Untitled"));
                const genericReasonPhrases = ["not essential for", "not relevant to", "not related to",
                    "may be distracting", "could be a distraction", "is a distraction", "not needed for",
                    "distracting you from", "not useful for"];
                const rawReason = String(t.reason || "");
                const cleanReason = genericReasonPhrases.some(p => rawReason.toLowerCase().includes(p)) ? "" : rawReason;
                const tabReason = cleanReason ? `<div class="trr">${esc(cleanReason)}</div>` : "";
                closingHtml += `<div class="tr"><span class="tx">\u00b7</span><div class="tc"><span class="tn">${tabTitle}</span>${tabReason}</div></div>`;
            }
            closingHtml += `</div>`;
        }
    }

    // --- Error (filter generic placeholders) ---
    let errHtml = "";
    const genericErrPhrases = ["no specific errors", "no errors detected", "not applicable", "no error", "n/a", "none detected"];
    const hasRealError = errA && errA.root_cause && !genericErrPhrases.some(
        p => (errA.root_cause ?? "").toLowerCase().includes(p)
    );
    if (hasRealError && errA) {
        errHtml = `<div class="eb"><div class="eh">Error</div><div class="et">${esc(errA.root_cause)}</div>`;
        if (errA.suggested_fix) {
            errHtml += `<pre class="ec">${esc(errA.suggested_fix)}</pre>`;
        }
        errHtml += `</div>`;
    }

    // --- Steps (filter generic advice) ---
    const genericStepPhrases = ["take a moment to breathe", "take a break", "focus on your current task",
        "continue focusing", "focus on the task at hand", "stay focused", "keep going", "take a deep breath"];
    const realSteps = steps.filter(s => !genericStepPhrases.some(p => s.toLowerCase().includes(p)));
    let stepsHtml = "";
    if (realSteps.length > 0) {
        stepsHtml = `<div class="sl">`;
        for (const s of realSteps) {
            stepsHtml += `<div class="si">${esc(s)}</div>`;
        }
        stepsHtml += `</div>`;
    }

    // --- CTA label ---
    let ctaLabel = `Apply ${executableRecommended.length} change${executableRecommended.length !== 1 ? "s" : ""}`;
    if (executableRecommended.length === 1) {
        const actionType = String(executableRecommended[0].action_type || "");
        if (actionType === "search_error") ctaLabel = "Search this error";
        if (actionType === "open_url") ctaLabel = "Open recommended page";
        if (actionType === "highlight_tab") ctaLabel = "Switch to recommended tab";
    }
    const hasManualSuggestions = closeCount > 0
        || actions.some((action) =>
            action.category === "recommended"
            && typeof action.action_id === "string"
            && !executableIdSet.has(action.action_id)
        );
    const actionNote = !canExecute && executableRecommended.length > 0
        ? "Suggestions only — workspace changes are off."
        : hasManualSuggestions
            ? "Manual review — Cortex won’t close or regroup existing tabs automatically."
            : "";

    const host = (existingHost ?? document.createElement("div")) as ManagedOverlayHost;
    if (!existingHost) {
        host.id = OID;
        host.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483647;pointer-events:none;";
    }

    const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
<style>
@keyframes panelIn{from{transform:translateY(8px) scale(.98);opacity:0}to{transform:translateY(0) scale(1);opacity:1}}
@keyframes panelOut{from{transform:translateY(0) scale(1);opacity:1}to{transform:translateY(8px) scale(.98);opacity:0}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes fadeOut{from{opacity:1}to{opacity:0}}
*{box-sizing:border-box;margin:0;padding:0}

.bk{position:fixed;inset:0;background:transparent;pointer-events:none;animation:fadeIn 160ms cubic-bezier(.23,1,.32,1)}

.pn{
  position:fixed;bottom:20px;right:20px;width:340px;max-height:calc(100vh - 40px);overflow-y:auto;
  pointer-events:auto;
  background:#111113;
  border-radius:12px;
  border:1px solid rgba(255,255,255,.06);
  box-shadow:0 0 0 .5px rgba(0,0,0,.3),0 4px 20px rgba(0,0,0,.4),0 16px 40px rgba(0,0,0,.2);
  font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text',system-ui,sans-serif;
  color:#e4e4e7;padding:18px 16px 14px;
  animation:panelIn 200ms cubic-bezier(.23,1,.32,1);
}
.pn.cx-update{animation:none}
.pn.cx-exit{animation:panelOut 160ms cubic-bezier(.4,0,1,1) forwards}
.bk.cx-exit{animation:fadeOut 160ms cubic-bezier(.4,0,1,1) forwards}
.pn::-webkit-scrollbar{width:0}

/* Close */
.xb{position:absolute;top:10px;right:10px;width:30px;height:30px;border:none;background:rgba(255,255,255,.04);border-radius:7px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background-color 120ms cubic-bezier(.23,1,.32,1),transform 120ms cubic-bezier(.23,1,.32,1)}
.xb:active{transform:scale(.96)}
.xb:focus-visible,.btn:focus-visible,.dm:focus-visible,.ul:focus-visible{outline:2px solid #dfb15b;outline-offset:2px}
.xb svg{width:9px;height:9px;stroke:#71717a;stroke-width:2}

/* Text */
.hd{font-size:13px;font-weight:600;color:#e4e4e7;padding-right:26px;margin-bottom:4px;letter-spacing:-.2px;line-height:1.4}
.ds{font-size:12px;color:#71717a;line-height:1.5;margin-bottom:14px}
.dv{height:1px;background:rgba(255,255,255,.04);margin-bottom:12px}

/* Tabs */
.sh{font-size:11px;font-weight:500;color:#71717a;margin-bottom:6px}
.tl{margin-bottom:10px}
.tr{display:flex;align-items:center;gap:7px;padding:3px 0}
.tx{color:#ef4444;font-size:12px;font-weight:500;width:13px;text-align:center;flex-shrink:0;font-family:'SF Mono','Fira Code',ui-monospace,monospace}
.tc{overflow:hidden;min-width:0}
.tn{font-size:12px;color:#71717a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.trr{font-size:10px;color:#3f3f46;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kn{font-size:11px;color:#3f3f46;margin-bottom:12px}
.kc{color:#10b981}
.nt{font-size:10px;color:#71717a;line-height:1.45;margin:0 0 12px;padding:8px 10px;background:rgba(255,255,255,.025);border-radius:6px}

/* Error */
.eb{padding:10px 12px;background:rgba(239,68,68,.08);border-radius:8px;border:1px solid rgba(239,68,68,.06);margin-bottom:12px}
.eh{font-size:10px;font-weight:600;color:#ef4444;margin-bottom:3px;font-family:'SF Mono','Fira Code',ui-monospace,monospace;letter-spacing:.5px}
.et{font-size:12px;color:#e4e4e7;line-height:1.5}
.ec{font-size:11px;color:#71717a;line-height:1.5;margin-top:6px;padding:8px;background:rgba(0,0,0,.3);border-radius:6px;font-family:'SF Mono','Fira Code',ui-monospace,monospace;white-space:pre-wrap;border:none}

/* Steps */
.sl{margin-bottom:12px}
.si{font-size:12px;color:#71717a;line-height:1.5;padding:2px 0 2px 14px;position:relative}
.si::before{content:'';position:absolute;left:3px;top:8px;width:3px;height:3px;border-radius:50%;background:#3f3f46}

/* CTA */
.btn{display:block;width:100%;padding:9px;border:none;border-radius:8px;background:#e4e4e7;color:#09090b;font-size:12px;font-weight:600;cursor:pointer;transition:background-color 120ms cubic-bezier(.23,1,.32,1),transform 120ms cubic-bezier(.23,1,.32,1);letter-spacing:-.1px;text-align:center;font-family:inherit}
.btn:active{transform:scale(.98)}
.btn.ok{background:#10b981;color:#fff;cursor:default;pointer-events:none}

.ur{display:flex;align-items:center;justify-content:center;gap:6px;padding:6px 0;font-size:11px;color:#3f3f46;margin-top:4px}
.ul{color:#3b82f6;cursor:pointer;font-weight:500;text-decoration:none;border:none;background:none;font-size:11px;font-family:inherit;padding:0}

/* Dismiss */
.dm{display:block;width:100%;padding:8px;margin-top:4px;border:none;border-radius:6px;background:none;color:#a1a1aa;cursor:pointer;font-size:11px;font-family:inherit;transition:color 120ms cubic-bezier(.23,1,.32,1),background-color 120ms cubic-bezier(.23,1,.32,1),transform 120ms cubic-bezier(.23,1,.32,1)}
.dm:active{transform:scale(.98)}
@media (hover:hover) and (pointer:fine){.xb:hover{background:rgba(255,255,255,.08)}.btn:hover{background:#f4f4f5}.ul:hover{text-decoration:underline}.dm:hover{color:#e4e4e7;background:rgba(255,255,255,.04)}}
@media (prefers-reduced-motion:reduce){.pn,.bk{animation:none!important}.btn,.xb,.dm{transition-property:background-color,color!important}.btn:active,.xb:active,.dm:active{transform:none}}
</style>

<div class="bk" id="bk"></div>
<div class="pn${isUpdate ? " cx-update" : ""}" role="region" aria-labelledby="cortex-intervention-title" aria-describedby="cortex-intervention-summary">
  <button class="xb" id="xb" type="button" aria-label="Dismiss intervention"><svg viewBox="0 0 10 10" fill="none"><path d="M1 1l8 8M9 1l-8 8"/></svg></button>
  <div class="hd" id="cortex-intervention-title" role="heading" aria-level="2">${esc(headline)}</div>
  <div class="ds" id="cortex-intervention-summary" aria-live="polite">${esc(summary)}</div>
  <div class="dv"></div>
  ${closingHtml ? `<div class="sh">Review ${closeCount} tab suggestion${closeCount !== 1 ? "s" : ""}</div>${closingHtml}` : ""}
  ${keepCount > 0 ? `<div class="kn">Keeping <span class="kc">${keepCount}</span> you need</div>` : ""}
  ${actionNote ? `<div class="nt">${esc(actionNote)}</div>` : ""}
  ${errHtml}
  ${stepsHtml ? `<div class="dv"></div>${stepsHtml}` : ""}
  ${canExecute && executableRecommended.length > 0 ? `<button class="btn" id="cta">${esc(ctaLabel)}</button><div class="ur" id="undo-bar" style="display:none"><span>Done.</span><button class="ul" id="undo-btn">Undo</button></div>` : ""}
  <button class="dm" id="dm">Dismiss</button>
</div>`;

    if (!existingHost) document.body.appendChild(host);

    let dismissed = false;
    let removalTimer = 0;
    let autoDismissTimer = 0;
    const reducedMotion = window.matchMedia(
        "(prefers-reduced-motion: reduce)",
    ).matches;
    const handleKeydown = (event: KeyboardEvent) => {
        if (event.key === "Escape") dismiss();
    };
    const dismiss = () => {
        if (dismissed) return;
        dismissed = true;
        // Notify background to record cooldown and restore tabs
        chrome.runtime.sendMessage({
            type: "USER_ACTION",
            action: "dismissed",
            intervention_id: payload.intervention_id,
        }).catch((err: unknown) => {
            // F4 (Phase-4 audit): this runs in the *page* context via
            // executeScript injection, so the SW may have been torn
            // down between the click and the send. Log so we at least
            // see it in the page console; the UI still tears down.
            console.warn("[cortex.overlay] dismiss notify failed:", err);
        });
        window.clearTimeout(autoDismissTimer);
        document.removeEventListener("keydown", handleKeydown);
        const panel = shadow.querySelector<HTMLElement>(".pn");
        const backdrop = shadow.querySelector<HTMLElement>(".bk");
        if (reducedMotion || !panel) {
            host.remove();
            return;
        }
        panel.classList.add("cx-exit");
        backdrop?.classList.add("cx-exit");
        removalTimer = window.setTimeout(() => host.remove(), 170);
    };
    shadow.getElementById("xb")?.addEventListener("click", dismiss);
    shadow.getElementById("dm")?.addEventListener("click", dismiss);
    shadow.getElementById("bk")?.addEventListener("click", dismiss);
    document.addEventListener("keydown", handleKeydown);

    // CTA
    const ctaEl = shadow.getElementById("cta");
    if (ctaEl) {
        ctaEl.addEventListener("click", () => {
            const toExecute = executableRecommended;
            if (toExecute.length === 0) return;
            (ctaEl as HTMLButtonElement).disabled = true;
            ctaEl.textContent = "Working\u2026";
            ctaEl.style.opacity = "0.5";

            chrome.runtime.sendMessage({
                type: "EXECUTE_ALL_RECOMMENDED",
                actions: toExecute,
                intervention_id: payload.intervention_id,
            }, (results: Array<Record<string, unknown>>) => {
                const failCount = Array.isArray(results) ? results.filter(r => !r.success).length : 0;
                const successCount = (Array.isArray(results) ? results.length : 0) - failCount;

                ctaEl.style.opacity = "1";
                ctaEl.classList.add("ok");
                ctaEl.textContent = failCount > 0
                    ? `Done (${failCount} skipped)`
                    : `${successCount} change${successCount !== 1 ? "s" : ""} applied`;

                const undoBar = shadow.getElementById("undo-bar");
                if (undoBar) undoBar.style.display = "flex";
            });
        });
    }

    // Undo
    const undoBtn = shadow.getElementById("undo-btn");
    if (undoBtn) {
        undoBtn.addEventListener("click", () => {
            chrome.runtime.sendMessage({
                type: "UNDO_ALL_RECENT",
                intervention_id: payload.intervention_id,
            }, () => {
                const undoBar = shadow.getElementById("undo-bar");
                if (undoBar) undoBar.innerHTML = `<span>Restored.</span>`;
                if (ctaEl) {
                    ctaEl.classList.remove("ok");
                    (ctaEl as HTMLButtonElement).disabled = false;
                    ctaEl.textContent = esc(ctaLabel);
                }
            });
        });
    }

    autoDismissTimer = window.setTimeout(dismiss, 5 * 60 * 1000);
    host.__cortexCleanup = () => {
        window.clearTimeout(autoDismissTimer);
        window.clearTimeout(removalTimer);
        document.removeEventListener("keydown", handleKeydown);
    };
}


/**
 * Injected into the active tab to show a lockout countdown overlay.
 * Uses the same Shadow DOM pattern as the intervention overlay.
 *
 * payload.duration_s  — lockout duration in seconds
 * payload.reason      — brief message explaining why
 */
export function injectLockoutOverlay(payload: Record<string, unknown>): void {
    const OID = "cortex-lockout-overlay";
    type ManagedLockoutHost = HTMLElement & {
        __cortexCleanup?: () => void;
        __cortexPreviousFocus?: HTMLElement | null;
    };
    const existingHost = document.getElementById(OID) as ManagedLockoutHost | null;
    existingHost?.__cortexCleanup?.();
    const isUpdate = existingHost !== null;

    const durationS = Math.max(1, Math.round(Number(payload.duration_s) || 60));
    const reason = String(
        payload.reason || "Take a moment to step back and think before continuing.",
    );
    const esc = (s: string) =>
        s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

    function formatCountdown(totalSeconds: number): string {
        const m = Math.floor(totalSeconds / 60);
        const s = totalSeconds % 60;
        return `${m}:${String(s).padStart(2, "0")}`;
    }

    const host = (existingHost ?? document.createElement("div")) as ManagedLockoutHost;
    const previousFocus = existingHost?.__cortexPreviousFocus
        ?? (document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null);
    host.__cortexPreviousFocus = previousFocus;
    if (!existingHost) {
        host.id = OID;
        host.style.cssText =
            "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483647;pointer-events:none;";
    }

    const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
<style>
@keyframes panelIn{from{transform:translate(-50%,calc(-50% + 8px)) scale(.98);opacity:0}to{transform:translate(-50%,-50%) scale(1);opacity:1}}
@keyframes panelOut{from{transform:translate(-50%,-50%) scale(1);opacity:1}to{transform:translate(-50%,calc(-50% + 8px)) scale(.98);opacity:0}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes fadeOut{from{opacity:1}to{opacity:0}}
*{box-sizing:border-box;margin:0;padding:0}
.bk{position:fixed;inset:0;background:rgba(0,0,0,.55);pointer-events:auto;animation:fadeIn 160ms cubic-bezier(.23,1,.32,1)}
.pn{
  position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:360px;
  pointer-events:auto;
  background:#111113;
  border-radius:14px;
  border:1px solid rgba(255,255,255,.06);
  box-shadow:0 0 0 .5px rgba(0,0,0,.3),0 4px 20px rgba(0,0,0,.4),0 16px 40px rgba(0,0,0,.2);
  font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text',system-ui,sans-serif;
  color:#e4e4e7;padding:28px 24px 22px;text-align:center;
  animation:panelIn 200ms cubic-bezier(.23,1,.32,1);
}
.pn.cx-update{animation:none}
.pn.cx-exit{animation:panelOut 160ms cubic-bezier(.4,0,1,1) forwards}
.bk.cx-exit{animation:fadeOut 160ms cubic-bezier(.4,0,1,1) forwards}
.hd{font-size:15px;font-weight:600;color:#e4e4e7;margin-bottom:8px;letter-spacing:-.2px}
.rs{font-size:12px;color:#71717a;line-height:1.5;margin-bottom:20px}
.tm{font-size:40px;font-weight:700;color:#e4e4e7;font-variant-numeric:tabular-nums;margin-bottom:20px;font-family:'SF Mono','Fira Code',ui-monospace,monospace}
.sk{display:inline-block;padding:7px 18px;border:1px solid rgba(255,255,255,.08);border-radius:8px;background:none;color:#71717a;font-size:11px;cursor:pointer;font-family:inherit;transition:color 120ms cubic-bezier(.23,1,.32,1),border-color 120ms cubic-bezier(.23,1,.32,1),transform 120ms cubic-bezier(.23,1,.32,1)}
.sk:active{transform:scale(.97)}
.sk:focus-visible{outline:2px solid #dfb15b;outline-offset:2px}
@media (hover:hover) and (pointer:fine){.sk:hover{color:#e4e4e7;border-color:rgba(255,255,255,.15)}}
@media (prefers-reduced-motion:reduce){.pn,.bk{animation:none!important}.sk{transition:color 120ms cubic-bezier(.23,1,.32,1),border-color 120ms cubic-bezier(.23,1,.32,1)!important}.sk:active{transform:none}}
</style>
<div class="bk" id="bk"></div>
<div class="pn${isUpdate ? " cx-update" : ""}" role="dialog" aria-modal="true" aria-labelledby="cortex-lockout-title" aria-describedby="cortex-lockout-reason cortex-lockout-countdown">
  <div class="hd" id="cortex-lockout-title">Lockout Active</div>
  <div class="rs" id="cortex-lockout-reason">${esc(reason)}</div>
  <div class="tm" id="cortex-lockout-countdown" role="timer">${formatCountdown(durationS)}</div>
  <button class="sk" id="skip">I need to continue</button>
</div>
`;

    if (!existingHost) document.body.appendChild(host);

    let remaining = durationS;
    let dismissed = false;
    let removalTimer = 0;
    const reducedMotion = window.matchMedia(
        "(prefers-reduced-motion: reduce)",
    ).matches;

    function removeAndRestoreFocus(): void {
        host.remove();
        const target = host.__cortexPreviousFocus;
        if (!target?.isConnected) return;
        try {
            target.focus({ preventScroll: true });
        } catch {
            target.focus();
        }
    }

    function dismiss(): void {
        if (dismissed) return;
        dismissed = true;
        window.clearInterval(timer);
        document.removeEventListener("keydown", handleKeydown);
        if (reducedMotion) {
            removeAndRestoreFocus();
            return;
        }
        shadow.querySelector(".pn")?.classList.add("cx-exit");
        shadow.querySelector(".bk")?.classList.add("cx-exit");
        removalTimer = window.setTimeout(removeAndRestoreFocus, 170);
    }

    const timer = setInterval(() => {
        remaining--;
        const el = shadow.getElementById("cortex-lockout-countdown");
        if (el) el.textContent = formatCountdown(remaining);
        if (remaining <= 0) {
            clearInterval(timer);
            dismiss();
        }
    }, 1000);

    // Skip button — no penalty, just dismiss. Lives in injected page-context
    // (executeScript), so the service-worker DEBUG flag is out of scope here.
    const skipButton = shadow.getElementById("skip") as HTMLButtonElement | null;
    skipButton?.addEventListener("click", () => {
        dismiss();
    });

    const handleKeydown = (event: KeyboardEvent) => {
        if (event.key === "Escape") {
            event.preventDefault();
            dismiss();
            return;
        }
        if (event.key === "Tab") {
            event.preventDefault();
            skipButton?.focus({ preventScroll: true });
        }
    };
    document.addEventListener("keydown", handleKeydown);
    skipButton?.focus({ preventScroll: true });

    host.__cortexCleanup = () => {
        window.clearInterval(timer);
        window.clearTimeout(removalTimer);
        document.removeEventListener("keydown", handleKeydown);
    };

    // Clicking the backdrop does not dismiss. The explicit skip control and
    // standard Escape key both preserve user agency and restore prior focus.
}

export function injectLeetCodeCoachOverlay(kind: string, payload: Record<string, unknown>): void {
    const OID = "cortex-leetcode-coach";
    type ManagedCoachHost = HTMLElement & {
        __cortexCleanup?: () => void;
    };
    const existingHost = document.getElementById(OID) as ManagedCoachHost | null;
    existingHost?.__cortexCleanup?.();
    const isUpdate = existingHost !== null;

    const esc = (s: string) =>
        s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const tags = Array.isArray(payload.tags)
        ? (payload.tags as unknown[]).map(String).slice(0, 5)
        : [];

    let title = "Cortex LeetCode Coach";
    let body = "Pause briefly and make the next move explicit.";
    let extra = "";

    if (kind === "LEETCODE_SHOW_SCRATCHPAD") {
        title = "Restate Before Solving";
        body = `Write the input, output, and invariant for ${esc(String(payload.problem_title || "this problem"))}.`;
        extra = `<textarea id="lc-note" placeholder="In my own words, the problem asks..." spellcheck="false"></textarea>`;
    } else if (kind === "LEETCODE_SHOW_PATTERN_LADDER") {
        title = "Pattern Ladder";
        body = "Reveal only as much help as you need. Start with the category, not code.";
        const tagHtml = tags.map((t) => `<span>${esc(t)}</span>`).join("");
        extra = `<div class="tags">${tagHtml || "<span>unknown pattern</span>"}</div><button id="lc-reveal">Reveal next hint</button><div id="lc-hint" class="hint">Hint 1: classify the problem type before choosing data structures.</div>`;
    } else if (kind === "LEETCODE_SHOW_SUBMISSION_GATE") {
        title = "Submission Gate";
        body = `${Number(payload.wrong_answer_count || 0)} wrong answers so far. Add one concrete failing test before the next submit.`;
        extra = `<label><input id="lc-check" type="checkbox"> I traced one failing case by hand</label>`;
    } else if (kind === "LEETCODE_SHOW_SOLUTION_FRICTION") {
        title = "Before Opening Solutions";
        body = "Write what you expect the editorial's key idea to be. This keeps the solution useful instead of replacing the learning step.";
        extra = `<textarea id="lc-note" placeholder="My hypothesis is..." spellcheck="false"></textarea>`;
    } else if (kind === "LEETCODE_SHOW_CONSOLIDATION") {
        title = "Consolidate the Solve";
        body = "Capture the reusable pattern while the successful path is still fresh.";
        extra = `<textarea id="lc-note" placeholder="The transferable pattern was..." spellcheck="false"></textarea>`;
    }

    const host = (existingHost ?? document.createElement("div")) as ManagedCoachHost;
    if (!existingHost) {
        host.id = OID;
        host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    }
    const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
<style>
*{box-sizing:border-box}
.card{position:fixed;right:22px;bottom:22px;width:min(380px,calc(100vw - 28px));pointer-events:auto;background:#101112;color:#f3f0e8;border:1px solid rgba(243,240,232,.12);border-radius:18px;box-shadow:0 18px 60px rgba(0,0,0,.35);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;padding:18px;animation:in 200ms cubic-bezier(.23,1,.32,1)}
@keyframes in{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes out{from{opacity:1;transform:translateY(0) scale(1)}to{opacity:0;transform:translateY(10px) scale(.98)}}
.card.cx-update{animation:none}.card.cx-exit{animation:out 160ms cubic-bezier(.4,0,1,1) forwards}
.top{display:flex;align-items:center;gap:10px;margin-bottom:10px}.dot{width:9px;height:9px;border-radius:99px;background:#dfb15b;box-shadow:0 0 18px rgba(223,177,91,.55)}.ttl{font-size:14px;font-weight:700;letter-spacing:-.02em;flex:1}.x{display:grid;place-items:center;width:32px;height:32px;padding:0;border:0;background:transparent;color:#9b9488;font-size:18px;line-height:1;cursor:pointer}.body{font-size:13px;line-height:1.5;color:#cfc7b7;margin-bottom:13px}textarea{width:100%;height:92px;resize:vertical;background:#18191a;color:#f3f0e8;border:1px solid rgba(243,240,232,.14);border-radius:12px;padding:10px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;outline:none}textarea:focus{border-color:#dfb15b}.tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}.tags span{font-size:11px;color:#dfb15b;border:1px solid rgba(223,177,91,.25);border-radius:99px;padding:4px 8px;background:rgba(223,177,91,.08)}button{border:1px solid rgba(243,240,232,.14);background:#dfb15b;color:#15110a;border-radius:10px;padding:8px 11px;font-size:12px;font-weight:700;cursor:pointer;transition:transform 120ms cubic-bezier(.23,1,.32,1),filter 120ms cubic-bezier(.23,1,.32,1),background-color 120ms cubic-bezier(.23,1,.32,1)}button:active{transform:scale(.97)}button:focus-visible{outline:2px solid #f3f0e8;outline-offset:2px}.hint{margin-top:10px;font-size:12px;line-height:1.45;color:#cfc7b7;background:#18191a;border-radius:10px;padding:10px}label{display:flex;gap:8px;align-items:center;font-size:12px;color:#cfc7b7}@media (hover:hover) and (pointer:fine){button:hover{filter:brightness(.94)}.x:hover{background:rgba(243,240,232,.08);filter:none}}@media (prefers-reduced-motion:reduce){.card{animation:none!important}button{transition:filter 120ms cubic-bezier(.23,1,.32,1),background-color 120ms cubic-bezier(.23,1,.32,1)!important}button:active{transform:none}}
</style>
<div class="card${isUpdate ? " cx-update" : ""}" role="region" aria-labelledby="cortex-coach-title">
  <div class="top"><span class="dot"></span><div class="ttl" id="cortex-coach-title">${esc(title)}</div><button class="x" id="lc-close" type="button" aria-label="Dismiss Cortex coach">×</button></div>
  <div class="body">${body}</div>
  ${extra}
</div>`;
    if (!existingHost) document.body.appendChild(host);

    let dismissed = false;
    let removalTimer = 0;
    const reducedMotion = window.matchMedia(
        "(prefers-reduced-motion: reduce)",
    ).matches;
    const dismiss = () => {
        if (dismissed) return;
        dismissed = true;
        const card = shadow.querySelector<HTMLElement>(".card");
        if (reducedMotion || !card) {
            host.remove();
            return;
        }
        card.classList.add("cx-exit");
        removalTimer = window.setTimeout(() => host.remove(), 170);
    };
    shadow.getElementById("lc-close")?.addEventListener("click", dismiss);
    shadow.getElementById("lc-reveal")?.addEventListener("click", () => {
        const hint = shadow.getElementById("lc-hint");
        if (hint) {
            hint.textContent = "Hint 2: define the state transition and one invariant before writing more code.";
        }
    });
    host.__cortexCleanup = () => window.clearTimeout(removalTimer);
}

async function handleIntervention(
    payload: Record<string, unknown>,
): Promise<void> {
    const uiPlan = payload.ui_plan as Record<string, boolean> | undefined;
    let executableActionIds: string[] = [];
    try {
        executableActionIds = await verifiedPresentedActionIds(
            payload,
            "browser",
        );
    } catch (error) {
        // A proposal may still contain useful manual guidance, but an invalid,
        // stale, or absent manifest must never produce an enabled affordance.
        if (DEBUG) {
            console.debug(
                "[cortex.bg] intervention has no valid executable manifest:",
                String(error),
            );
        }
    }

    // INTERVENTION_TRIGGER is a proposal event, never an apply command.
    // Presentation is allowed; tab grouping/hiding and action execution require
    // a separate exact authorization transaction. Legacy triggers therefore
    // remain safe even when they contain hide_targets.
    if (uiPlan?.show_overlay || uiPlan?.dim_background) {
        try {
            const [tab] = await chrome.tabs.query({
                active: true,
                currentWindow: true,
            });
            if (tab?.id && !tab.incognito) {
                await chrome.scripting.executeScript({
                    target: { tabId: tab.id },
                    func: injectOverlay,
                    args: [payload, executableActionIds],
                });
            }
        } catch (e) {
            if (DEBUG) console.error("Cortex: failed to inject overlay", e);
        }
    }

    broadcastToPopup({
        type: "INTERVENTION_TRIGGER",
        payload,
    });
}

async function handleContextRequest(msg: WSMessage): Promise<void> {
    try {
        const { tabs, activeTab, contentExcerpt } = await browserContextCollector.collect({
            focusGoal: focusSession?.goal,
            lastActivated: tabActivationTelemetry.snapshot(),
        });
        // Save this tab list so the intervention snapshot uses the same ordering
        // the LLM will see — prevents tab_index misalignment.
        lastContextTabs = tabs;
        lastContextTabsTimestamp = Date.now();

        send({
            type: "CONTEXT_RESPONSE",
            payload: {
                browser_context: {
                    active_tab_title: activeTab?.title ?? "",
                    active_tab_url: activeTab?.url ?? "",
                    active_tab_content_excerpt: contentExcerpt,
                    all_tabs: tabs,
                    tab_type_classification: tabs.reduce<Record<string, number>>(
                        (counts, tab) => {
                            counts[tab.tab_type] = (counts[tab.tab_type] ?? 0) + 1;
                            return counts;
                        },
                        {},
                    ),
                    focus_goal: focusSession?.goal ?? null,
                },
            },
            timestamp: Date.now() / 1000,
            sequence: msg.sequence,
            correlation_id: msg.correlation_id,
        });
    } catch {
        send({
            type: "CONTEXT_RESPONSE",
            payload: { error: "context_gather_failed" },
            timestamp: Date.now() / 1000,
            sequence: msg.sequence,
            correlation_id: msg.correlation_id,
        });
    }
}

async function handleRestore(payload: Record<string, unknown>): Promise<void> {
    // A legacy restore frame closes presentation only. Workspace restoration
    // requires an exact restore_id plus receipt-derived inverse actions and is
    // handled by the fail-closed transaction adapter below.
    const exactRestore = await handleExactRestoreCommand(payload);
    try {
        const tabs = await chrome.tabs.query({});
        await Promise.all(
            tabs
                .filter((tab) => typeof tab.id === "number")
                .map((tab) =>
                    chrome.tabs.sendMessage(tab.id as number, {
                        type: "REMOVE_OVERLAY",
                    }).catch((err: unknown) => {
                        if (DEBUG) console.debug("[cortex.bg] REMOVE_OVERLAY sendMessage failed (tab may not have content script): %o", err);
                    }),
                ),
        );
    } catch { /* best-effort presentation cleanup */ }
    // F4 (Phase-4 audit): the in-memory latch MUST be nulled even if
    // session-storage clearing throws. The earlier ``try { ... } catch {}``
    // both swallowed the failure and worked correctly, but a noisy
    // platform bug (e.g. quota exhausted) would never surface for
    // debugging. Log + still null the latch so behaviour is identical
    // but observable.
    interventionPresentation.clear();
    try {
        chrome.storage.session.remove([
            "cortex_active_intervention",
            "cortex_active_intervention_cid",
            "cortex_active_intervention_mounted_at",
            ...(exactRestore
                ? ["cortex_tab_snapshot", "cortex_tab_mgr_snapshots"]
                : []),
        ]);
    } catch (err) {
        console.warn(
            "[cortex.bg] F4: storage.session.remove failed during restore:",
            err,
        );
    }
    broadcastToPopup({ type: "INTERVENTION_RESTORE", payload });
}

// --- Daemon launch helper ---

interface LaunchResult {
    ok: boolean;
    status: string;
    error?: string;
}

/**
 * Phase-3 P0-N3: factored out of the LAUNCH_CORTEX message handler
 * so notification onClicked / onButtonClicked can invoke it directly.
 * ``chrome.runtime.sendMessage`` from a service worker does NOT
 * dispatch to the same SW's onMessage listener, so the previous
 * "Open" button was a no-op.
 */
async function runLaunchCortex(): Promise<LaunchResult> {
    let lastError = "";

    const waitAndEnableCamera = async (maxAttempts: number): Promise<boolean> => {
        if (!connected) connect();
        let attempts = 0;
        while (!connected && attempts < maxAttempts) {
            await new Promise((r) => setTimeout(r, 500));
            attempts++;
        }
        if (connected && ws) {
            send({
                type: "SETTINGS_SYNC",
                payload: { webcam_enabled: true },
                timestamp: Date.now() / 1000,
                sequence: ++sequence,
            });
            return true;
        }
        return false;
    };

    try {
        // Path 1: HTTP launcher agent on port 9471
        try {
            let launchAuthToken: string | null = null;
            try {
                launchAuthToken = await getAuthToken();
            } catch {
                launchAuthToken = null;
            }
            const resp = await fetch(`${LAUNCHER_HTTP_URL}/launch`, {
                method: "POST",
                signal: AbortSignal.timeout(12000),
                headers: launchAuthToken
                    ? { "X-Cortex-Auth-Token": launchAuthToken }
                    : undefined,
            });
            const data = await resp.json();
            if (data.status === "starting" || data.status === "already_running") {
                if (await waitAndEnableCamera(16)) {
                    return { ok: true, status: "camera_enabled" };
                }
                lastError = "Daemon started via launcher but WebSocket not connected";
            }
        } catch {
            // Launcher not running — try next path
        }

        // Path 2: Native messaging
        let nativeResult: { status: string; error?: string };
        try {
            const response = await sendNativeHostMessage(
                { command: "launch" },
                { timeoutMs: 30_000 },
            );
            nativeResult = response.command === "launch"
                ? { status: response.status, error: response.error ?? undefined }
                : { status: "native_error", error: "Unexpected native host response" };
        } catch (error) {
            nativeResult = {
                status: "native_unavailable",
                error: error instanceof Error ? error.message : String(error),
            };
        }

        if (nativeResult.status === "launched" || nativeResult.status === "already_running") {
            if (await waitAndEnableCamera(10)) {
                return { ok: true, status: "camera_enabled" };
            }
            lastError = "Daemon started via native messaging but WebSocket not connected";
        } else {
            lastError = nativeResult.error || "Native messaging failed";
        }

        // Path 3: Direct WebSocket — daemon may already be running
        if (await waitAndEnableCamera(4)) {
            return { ok: true, status: "camera_enabled" };
        }

        return {
            ok: false,
            status: "not_connected",
            error: lastError || "Could not start daemon. Run in terminal: python -m cortex.scripts.run_dev",
        };
    } catch (e) {
        return { ok: false, status: "error", error: String(e) };
    }
}

// --- Focus Session Logic ---

function startFocusSession(goal: string): void {
    const now = Date.now();
    focusSession = createFocusSession(goal, now);
    schedulePersist();
    broadcastToPopup({ type: "FOCUS_SESSION_STARTED", goal });
}

function stopFocusSession(): FocusSession | null {
    if (!focusSession) return null;
    const session = { ...focusSession };
    // Save to daily stats
    saveToDailyStats(session);
    focusSession = null;
    autoFocusArmed = false;
    autoFocusEndsAt = null;
    activeFocusPresetPatterns = [];
    activeFocusCustomDomains = [];
    _activeFocusPresetName = "developer";
    if (autoFocusAlarmName) {
        try {
            chrome.alarms.clear(autoFocusAlarmName);
        } catch { /* alarms API may be unavailable */ }
        autoFocusAlarmName = null;
    }
    schedulePersist();
    // Phase 4d Task A: mirror the cleared auto-focus state to
    // chrome.storage.local so the next SW boot sees a consistent
    // zeroed blob instead of stale armed=true.
    persistAutoFocusState();
    broadcastToPopup({ type: "FOCUS_SESSION_ENDED", session });
    return session;
}

/**
 * P0 §3.10: daemon-armed focus-session arming.
 *
 * Distinct from {@link startFocusSession} so the symmetric
 * STOP_FOCUS_AUTO knows whether the tear-down is legitimate. The
 * session goal is set to a synthetic value so the existing popup +
 * dashboard surfaces can render meaningful copy without modification.
 */
function startAutoFocusSession(opts: {
    preset: string;
    customDomains: string[];
    durationMinutes: number;
    reason: string;
}): void {
    const now = Date.now();
    // If a manual session is already running, do NOT override it — the
    // user's explicit intent wins. The daemon retries after the next
    // tick if HYPER persists.
    if (focusSession && !autoFocusArmed) {
        if (DEBUG) {
            console.log("Cortex: skipping auto-arm — manual focus session active");
        }
        return;
    }
    focusSession = createFocusSession(`Auto-focus (${opts.reason})`, now);
    autoFocusArmed = true;
    autoFocusEndsAt = now + opts.durationMinutes * 60_000;
    _activeFocusPresetName = opts.preset;
    activeFocusPresetPatterns = resolveFocusPreset(opts.preset);
    activeFocusCustomDomains = opts.customDomains.slice(0, 100);
    // Phase 4d Task A: mirror to chrome.storage.local before scheduling
    // the alarm so a worst-case SW eviction immediately after arming
    // still leaves a durable trail for the next boot.
    persistAutoFocusState();
    // Schedule a chrome.alarm to terminate the session after duration
    // — survives MV3 service-worker restarts (a setTimeout would not).
    autoFocusAlarmName = `cortex_auto_focus_${now}`;
    try {
        chrome.alarms.create(autoFocusAlarmName, {
            when: autoFocusEndsAt,
        });
    } catch { /* alarms API may be unavailable in test harnesses */ }
    schedulePersist();
    broadcastToPopup({
        type: "FOCUS_SESSION_STARTED",
        goal: focusSession.goal,
        autoArmed: true,
        preset: opts.preset,
        endsAt: autoFocusEndsAt,
    });
    if (DEBUG) {
        console.log(
            `Cortex: auto-armed focus session (preset=${opts.preset}, ${opts.durationMinutes} min)`,
        );
    }
}

/**
 * P0 §3.10: tear down only an auto-armed focus session. A manual
 * session is left untouched. Phase-3 P0-DF-10.1: notify the daemon
 * when WE initiated the stop (duration_elapsed / post_restore) so
 * its ``_auto_focus_armed`` flag clears in lockstep with ours;
 * without this round-trip the daemon believes the session is still
 * armed and skips re-arming on the next HYPER episode.
 */
function stopAutoFocusSession(reason: string): FocusSession | null {
    if (!focusSession || !autoFocusArmed) return null;
    const result = stopFocusSession();
    // Only the extension's local tear-down paths (alarm timeout /
    // post-restore expiry) need to inform the daemon; receipt of a
    // daemon-emitted STOP_FOCUS_AUTO has the daemon already cleared.
    const isExtensionInitiated =
        reason === "duration_elapsed"
        || reason === "duration_elapsed_post_restore"
        || reason === "user_disarm_local";
    if (isExtensionInitiated && connected && ws) {
        try {
            send({
                type: "USER_ACTION",
                payload: {
                    action: "auto_focus_stopped",
                    reason,
                    source: "browser_extension",
                    timestamp: Date.now() / 1000,
                },
                timestamp: Date.now() / 1000,
                sequence: ++sequence,
            });
        } catch {
            // WS may be mid-reconnect; daemon will reconcile on next
            // STATE_UPDATE tick since exit_gate is symmetric.
        }
    }
    return result;
}

function updateFocusSession(payload: Record<string, unknown>): void {
    if (!focusSession) return;
    updateFocusSessionState(focusSession, payload);
    schedulePersist();
}

function getFocusSessionSnapshot() {
    if (!focusSession) return null;
    return focusSessionSnapshot(focusSession);
}

async function saveToDailyStats(session: FocusSession): Promise<void> {
    const today = new Date().toISOString().slice(0, 10);
    const result = await chrome.storage.local.get("cortex_daily_stats");
    let stats: DailyStats = result.cortex_daily_stats as DailyStats;
    if (!stats || stats.date !== today) {
        stats = emptyDailyStats(today);
    }
    const sessionMin = (Date.now() - session.startTime) / 60000;
    const focusMin = session.totalFocusMs / 60000;
    stats.totalFocusMin += focusMin;
    stats.totalSessionMin += sessionMin;
    stats.sessions += 1;
    stats.distractionsBlocked += session.distractionsBlocked;
    const streakMin = session.longestStreakMs / 60000;
    if (streakMin > stats.longestStreakMin) stats.longestStreakMin = streakMin;
    await chrome.storage.local.set({ cortex_daily_stats: stats });
}

// --- Distraction Blocking ---

function isDistractionUrl(url: string, title?: string): boolean {
    return isDistractionForSession({
        url,
        title,
        session: focusSession,
        presetPatterns: activeFocusPresetPatterns,
        customDomains: activeFocusCustomDomains,
    });
}

function injectDistractionInterceptor(
    focusMin: number,
    streakMin: number,
    distractionsBlocked: number,
    url: string,
): void {
    const domain = new URL(url).hostname.replace("www.", "");

    // Create a full-screen overlay instead of replacing body content.
    // This preserves the original page underneath so "Continue" can reveal it
    // without a reload flash.
    const overlay = document.createElement("div");
    overlay.id = "cortex-distraction-interceptor";
    overlay.style.cssText =
        "position:fixed;inset:0;z-index:2147483647;" +
        "display:flex;align-items:center;justify-content:center;" +
        "background:#09090b;font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text',system-ui,sans-serif;color:#e4e4e7;";

    const container = document.createElement("div");
    container.style.cssText = "text-align:center;max-width:380px;padding:40px;";
    container.innerHTML = `
        <div style="width:48px;height:48px;margin:0 auto 28px;border-radius:50%;background:rgba(16,185,129,.1);display:flex;align-items:center;justify-content:center">
            <div style="width:8px;height:8px;border-radius:50%;background:#10b981;box-shadow:0 0 10px rgba(16,185,129,.4)"></div>
        </div>
        <h1 style="font-size:16px;font-weight:600;margin:0 0 6px;letter-spacing:-.3px;color:#e4e4e7">
            Focus session active
        </h1>
        <p style="font-size:13px;color:#71717a;margin:0 0 28px;line-height:1.6">
            <span style="color:#e4e4e7;font-family:'SF Mono','Fira Code',ui-monospace,monospace;font-size:12px">${focusMin}m</span> focused,
            <span style="color:#e4e4e7;font-family:'SF Mono','Fira Code',ui-monospace,monospace;font-size:12px">${streakMin}m</span> streak.
            <br><span style="color:#3f3f46">${domain}</span> will break your flow.
        </p>
        <div style="display:flex;gap:8px;justify-content:center">
            <button id="cortex-go-back" style="padding:9px 24px;border:none;border-radius:8px;background:#e4e4e7;color:#09090b;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit">
                Go back
            </button>
            <button id="cortex-continue" style="padding:9px 24px;border:1px solid rgba(255,255,255,.06);border-radius:8px;background:transparent;color:#3f3f46;font-size:12px;cursor:pointer;font-family:inherit">
                Continue
            </button>
        </div>
        <p style="font-size:11px;color:#3f3f46;margin-top:20px;font-family:'SF Mono','Fira Code',ui-monospace,monospace;letter-spacing:.3px">
            ${distractionsBlocked} blocked
        </p>
    `;
    overlay.appendChild(container);
    document.body.appendChild(overlay);

    document.getElementById("cortex-go-back")?.addEventListener("click", () => {
        // Notify background that user resisted distraction
        try { chrome.runtime.sendMessage({ type: "DISTRACTION_BLOCKED" }); } catch {}
        history.back();
    });
    document.getElementById("cortex-continue")?.addEventListener("click", () => {
        // Remove overlay to reveal the original page — no reload needed
        overlay.remove();
    });
}

// --- Action Execution Engine ---
//
// ``SuggestedAction`` is imported from the generated Pydantic types
// (Debt-1 closure, top of file). The TypeScript compiler now narrows
// ``action.action_type`` to the exact ``Literal`` union the Python
// validator enforces, so the ``default`` arm of ``executeAction``'s
// switch is structurally unreachable when the daemon sends a valid
// plan (F42 close).

interface ActionExecuteResult {
    action_id: string;
    success: boolean;
    message: string;
    // F44 closure: aligned with Pydantic's ``SuggestedAction.reversible``.
    // Was previously ``undo_available`` here — the two names referred to
    // the same concept and drifted independently. The result envelope
    // now uses the canonical name on both sides.
    reversible: boolean;
}

interface UndoEntry {
    action_id: string;
    action_type: SuggestedAction["action_type"];
    undo_data: Record<string, unknown>;
    timestamp: number;
}

// Tab snapshot: maps tab_index → {chromeTabId, url, title} at intervention time.
// Also persisted to chrome.storage.session so it survives MV3 service worker restarts.
let interventionTabSnapshot: Map<number, { chromeTabId: number; url: string; title: string }> = new Map();
// Saved tab list from the most recent CONTEXT_RESPONSE — used to ensure tab_index
// alignment between what the LLM saw and what the action executor targets.
let lastContextTabs: TabData[] | null = null;
let lastContextTabsTimestamp = 0; // LAYER 3: track when context was captured
const CONTEXT_STALENESS_LIMIT = 30_000; // 30s max age for tab snapshots
const undoStack: UndoEntry[] = [];
const MAX_UNDO_ENTRIES = 50;
const MIN_TABS_TO_KEEP = 3; // Never close tabs if it would leave fewer than this many open

/**
 * Snapshot tabs for intervention action resolution.
 * Uses the saved context-time tab list (from the last CONTEXT_RESPONSE) to ensure
 * tab_index values from the LLM align with the actual Chrome tab IDs.
 * Falls back to a fresh query if no saved list exists.
 * Persists to chrome.storage.session for service worker restart resilience.
 */
async function snapshotTabsForIntervention(): Promise<void> {
    interventionTabSnapshot = new Map();
    // LAYER 3: Discard stale context (>30s old) to prevent wrong-tab targeting
    if (lastContextTabs && Date.now() - lastContextTabsTimestamp > CONTEXT_STALENESS_LIMIT) {
        if (DEBUG) console.log("Cortex: discarding stale tab context (>30s old), refreshing");
        lastContextTabs = null;
    }
    const tabs = lastContextTabs ?? (await browserContextCollector.collect({
        focusGoal: focusSession?.goal,
        lastActivated: tabActivationTelemetry.snapshot(),
    })).tabs;
    const snapData: Record<string, { chromeTabId: number; url: string; title: string }> = {};
    for (let i = 0; i < tabs.length; i++) {
        const entry = {
            chromeTabId: tabs[i].tab_id,
            url: tabs[i].url,
            title: tabs[i].title,
        };
        interventionTabSnapshot.set(i, entry);
        snapData[String(i)] = entry;
    }
    // Verify all snapshot tab IDs still exist (tabs may have been closed)
    try {
        const liveTabs = (await chrome.tabs.query({})).filter(
            (tab) => !tab.incognito,
        );
        const liveIds = new Set(liveTabs.map(t => t.id));
        for (const [idx, entry] of interventionTabSnapshot) {
            if (!liveIds.has(entry.chromeTabId)) {
                interventionTabSnapshot.delete(idx);
                delete snapData[String(idx)];
            }
        }
    } catch (err) {
        // F12 (Phase-4 audit): live-tab reconciliation is best-effort
        // — a query failure simply leaves the snapshot stale (the next
        // intervention will rebuild it). Log so we can detect chronic
        // failures.
        console.warn(
            "[cortex.bg] snapshot live-tab reconciliation failed:",
            err,
        );
    }

    // Persist for service worker restart resilience
    try {
        await chrome.storage.session.set({ cortex_tab_snapshot: snapData });
    } catch {
        // storage.session may not be available
    }
}

/** Load snapshot from session storage (after service worker restart). */
async function loadSnapshotFromStorage(): Promise<void> {
    if (interventionTabSnapshot.size > 0) return; // already in memory
    try {
        const data = await chrome.storage.session.get("cortex_tab_snapshot");
        const snapData = data.cortex_tab_snapshot as Record<string, { chromeTabId: number; url: string; title: string }> | undefined;
        if (snapData) {
            interventionTabSnapshot = new Map();
            for (const [key, entry] of Object.entries(snapData)) {
                interventionTabSnapshot.set(Number(key), entry);
            }
        }
    } catch {
        // storage.session not available
    }
}

/** Validate a tab still exists and URL matches before executing an action. */
async function validateTab(
    tabIndex: number,
): Promise<{ valid: boolean; tabId: number; message: string }> {
    // Ensure snapshot is loaded (handles service worker restart)
    await loadSnapshotFromStorage();

    const snap = interventionTabSnapshot.get(tabIndex);
    if (!snap) {
        return { valid: false, tabId: -1, message: `Tab index ${tabIndex} not in snapshot` };
    }
    try {
        const tab = await chrome.tabs.get(snap.chromeTabId);
        if (!tab) {
            return { valid: false, tabId: snap.chromeTabId, message: "Tab already closed" };
        }
        // LAYER 1: Never allow closing the active tab
        if (tab.active) {
            return { valid: false, tabId: snap.chromeTabId, message: "Tab is currently active — refusing to close" };
        }
        // LAYER 1b: Protect recently-visited tabs (activated within last 5 minutes)
        const lastActive = tabActivationTelemetry.lastActivation(snap.chromeTabId);
        if (lastActive && Date.now() - lastActive < RECENTLY_ACTIVE_PROTECTION_MS) {
            const agoSec = Math.round((Date.now() - lastActive) / 1000);
            return { valid: false, tabId: snap.chromeTabId, message: `Tab was recently active (${agoSec}s ago) — protected` };
        }
        // Check hostname still matches
        try {
            const snapHost = new URL(snap.url).hostname;
            const currentHost = new URL(tab.url || "").hostname;
            if (snapHost !== currentHost) {
                return {
                    valid: false,
                    tabId: snap.chromeTabId,
                    message: `Tab navigated away (was ${snapHost}, now ${currentHost})`,
                };
            }
        } catch {
            // URL parse failed, skip host check
        }
        // LAYER 4: Check title similarity — reject if tab content changed significantly
        if (snap.title && tab.title) {
            const snapWords = new Set(snap.title.toLowerCase().split(/\s+/).filter(w => w.length > 1));
            const liveWords = new Set(tab.title!.toLowerCase().split(/\s+/).filter(w => w.length > 1));
            if (snapWords.size > 0 && liveWords.size > 0) {
                let overlap = 0;
                for (const w of snapWords) { if (liveWords.has(w)) overlap++; }
                const similarity = overlap / Math.max(snapWords.size, liveWords.size);
                if (similarity < 0.4) {
                    return { valid: false, tabId: snap.chromeTabId, message: "Tab content changed significantly" };
                }
            }
        }
        return { valid: true, tabId: snap.chromeTabId, message: "ok" };
    } catch {
        return { valid: false, tabId: snap.chromeTabId, message: "Tab already closed" };
    }
}

function pushUndo(entry: UndoEntry): void {
    undoStack.push(entry);
    if (undoStack.length > MAX_UNDO_ENTRIES) {
        undoStack.shift();
    }
    schedulePersist();
}

interface BrowserCapabilityResult {
    result: ActionExecuteResult;
    inverse: Record<string, unknown>;
    status: "succeeded" | "failed" | "already_complete";
}

interface BrowserEffectVerification {
    verified: boolean;
    detail: string;
    fingerprint: Record<string, unknown>;
}

function urlsMatch(left: unknown, right: unknown): boolean {
    if (typeof left !== "string" || typeof right !== "string") return false;
    try {
        return new URL(left).href === new URL(right).href;
    } catch {
        return left === right;
    }
}

async function verifyBrowserEffect(
    action: ManifestAction,
    inverse: Record<string, unknown>,
): Promise<BrowserEffectVerification> {
    if (inverse.noEffect === true) {
        return {
            verified: true,
            detail: "No eligible workspace object required a change",
            fingerprint: { capability: action.capability, noEffect: true },
        };
    }
    if (action.capability === "hide_tabs_except_active") {
        const groupId = typeof inverse.groupId === "number" ? inverse.groupId : null;
        const tabIds = Array.isArray(inverse.hiddenTabIds)
            ? inverse.hiddenTabIds.filter((id): id is number => typeof id === "number")
            : [];
        const tabs = (await chrome.tabs.query({})).filter((tab) => !tab.incognito);
        const grouped = tabs.filter(
            (tab) => typeof tab.id === "number" && tabIds.includes(tab.id),
        );
        let collapsed = false;
        if (groupId !== null) {
            try {
                collapsed = (await chrome.tabGroups.get(groupId)).collapsed === true;
            } catch {
                collapsed = false;
            }
        }
        const verified = groupId !== null
            && tabIds.length > 0
            && grouped.length === tabIds.length
            && grouped.every((tab) => tab.groupId === groupId)
            && collapsed;
        return {
            verified,
            detail: verified
                ? "Exact Cortex tab group verified"
                : "Tab group postcondition could not be verified",
            fingerprint: {
                groupId,
                tabIds: [...tabIds].sort((a, b) => a - b),
                collapsed,
            },
        };
    }
    const originalTabId = typeof inverse.originalTabId === "number"
        ? inverse.originalTabId
        : null;
    if (action.capability === "close_tab" || action.capability === "bookmark_and_close") {
        let originalAbsent = originalTabId !== null;
        if (originalTabId !== null) {
            try {
                await chrome.tabs.get(originalTabId);
                originalAbsent = false;
            } catch {
                originalAbsent = true;
            }
        }
        let bookmarkVerified = true;
        const bookmarkId = typeof inverse.bookmarkId === "string"
            ? inverse.bookmarkId
            : "";
        if (action.capability === "bookmark_and_close") {
            bookmarkVerified = false;
            if (bookmarkId) {
                try {
                    const [bookmark] = await chrome.bookmarks.get(bookmarkId);
                    bookmarkVerified = Boolean(
                        bookmark && urlsMatch(bookmark.url, inverse.url),
                    );
                } catch {
                    bookmarkVerified = false;
                }
            }
        }
        const verified = originalAbsent && bookmarkVerified;
        return {
            verified,
            detail: verified
                ? "Exact closed-tab postcondition verified"
                : "Closed-tab postcondition could not be verified",
            fingerprint: { originalTabId, originalAbsent, bookmarkId, bookmarkVerified },
        };
    }
    if (action.capability === "group_tabs") {
        const groupId = typeof inverse.groupId === "number" ? inverse.groupId : null;
        const tabIds = Array.isArray(inverse.tabIds)
            ? inverse.tabIds.filter((id): id is number => typeof id === "number")
            : [];
        const tabs = await chrome.tabs.query({});
        const grouped = tabs.filter(
            (tab) => typeof tab.id === "number" && tabIds.includes(tab.id),
        );
        let collapsed = false;
        if (groupId !== null) {
            try {
                collapsed = (await chrome.tabGroups.get(groupId)).collapsed === true;
            } catch {
                collapsed = false;
            }
        }
        const verified = groupId !== null
            && tabIds.length > 0
            && grouped.length === tabIds.length
            && grouped.every((tab) => tab.groupId === groupId)
            && collapsed;
        return {
            verified,
            detail: verified
                ? "Exact grouped tabs verified"
                : "Grouped-tab postcondition could not be verified",
            fingerprint: {
                groupId,
                tabIds: [...tabIds].sort((a, b) => a - b),
                collapsed,
            },
        };
    }
    if (action.capability === "open_url" || action.capability === "search_error") {
        const tabId = typeof inverse.tabId === "number" ? inverse.tabId : null;
        let actualUrl: string | null = null;
        let pendingUrl: string | null = null;
        if (tabId !== null) {
            try {
                const tab = await chrome.tabs.get(tabId);
                actualUrl = tab.url ?? null;
                pendingUrl = tab.pendingUrl ?? null;
            } catch {
                actualUrl = null;
                pendingUrl = null;
            }
        }
        const verified = tabId !== null && (
            urlsMatch(actualUrl, inverse.url)
            || urlsMatch(pendingUrl, inverse.url)
        );
        return {
            verified,
            detail: verified
                ? "Exact Cortex-created tab verified"
                : "Created-tab postcondition could not be verified",
            fingerprint: { tabId, actualUrl, pendingUrl },
        };
    }
    if (action.capability === "highlight_tab") {
        const targetTabId = typeof inverse.targetTabId === "number"
            ? inverse.targetTabId
            : null;
        const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
        const verified = targetTabId !== null && active?.id === targetTabId;
        return {
            verified,
            detail: verified
                ? "Exact active tab verified"
                : "Active-tab postcondition could not be verified",
            fingerprint: { targetTabId, activeTabId: active?.id ?? null },
        };
    }
    return {
        verified: false,
        detail: `No verifier exists for ${action.capability}`,
        fingerprint: { capability: action.capability },
    };
}

function monotonicNowNs(): number {
    const milliseconds = typeof globalThis.performance?.now === "function"
        ? globalThis.performance.now()
        : 0;
    return Math.max(0, Math.round(milliseconds * 1_000_000));
}

function operationKey(interventionId: string, actionId: string): string {
    return `${interventionId}:${actionId}`;
}

function newCreatedTabStageUrl(): string {
    return `${CREATED_TAB_STAGE_PREFIX}${newWireId()}`;
}

function validCreatedTabStageUrl(value: unknown): value is string {
    if (typeof value !== "string" || !value.startsWith(CREATED_TAB_STAGE_PREFIX)) {
        return false;
    }
    const token = value.slice(CREATED_TAB_STAGE_PREFIX.length);
    return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
        .test(token);
}

/**
 * Recover the identity of a tab created immediately before an MV3 worker
 * termination. A created tab receives a unique, inert staging URL before
 * Cortex checkpoints its Chrome tab id; navigation to the requested URL only
 * happens after that checkpoint is durable. Therefore zero marker matches
 * proves that no created-tab effect remains, while one match is the exact
 * Cortex-owned object that compensation may remove.
 */
async function reconcileCreatedTabInverse(
    inverse: Record<string, unknown>,
): Promise<Record<string, unknown>> {
    if (typeof inverse.tabId === "number") return { ...inverse };
    if (!validCreatedTabStageUrl(inverse.stagingUrl)) return { ...inverse };

    const tabs = await chrome.tabs.query({});
    const matches = tabs.filter((tab) =>
        typeof tab.id === "number"
        && (
            urlsMatch(tab.url, inverse.stagingUrl)
            || urlsMatch(tab.pendingUrl, inverse.stagingUrl)
        )
    );
    if (matches.length === 0) {
        const recovered: Record<string, unknown> = { ...inverse, noEffect: true };
        delete recovered.tabId;
        delete recovered.cortexEffectMayExist;
        return recovered;
    }
    if (matches.length === 1) {
        const recovered: Record<string, unknown> = {
            ...inverse,
            tabId: matches[0].id,
            cortexEffectMayExist: true,
        };
        delete recovered.noEffect;
        return recovered;
    }
    throw new IndeterminateBrowserMutationError(
        "Multiple tabs share a Cortex recovery marker; automatic ownership is ambiguous",
        {
            ...inverse,
            cortexEffectMayExist: true,
            recoveryConflictCount: matches.length,
        },
    );
}

function suggestedActionFromManifest(action: ManifestAction): SuggestedAction {
    const parameters = JSON.parse(action.parameters_json || "{}") as Record<string, unknown>;
    const raw = parameters.suggested_action;
    if (!isSuggestedAction(raw)) {
        throw new Error("manifest suggested action failed runtime validation");
    }
    if (raw.action_id !== action.action_id || raw.action_type !== action.capability) {
        throw new Error("suggested action differs from manifest capability");
    }
    const candidate = raw as unknown as Record<string, unknown>;
    const target = candidate.target;
    const label = candidate.label;
    const reason = candidate.reason;
    const tabIndex = candidate.tab_index;
    const metadata = candidate.metadata;
    if (
        (target !== undefined && (typeof target !== "string" || target.length > 500))
        || typeof label !== "string"
        || label.length === 0
        || label.length > 200
        || (reason !== undefined && (typeof reason !== "string" || reason.length > 300))
        || (
            tabIndex !== undefined
            && tabIndex !== null
            && (
                typeof tabIndex !== "number"
                || !Number.isInteger(tabIndex)
                || tabIndex < 0
            )
        )
        || (
            metadata !== undefined
            && (
                typeof metadata !== "object"
                || metadata === null
                || Array.isArray(metadata)
            )
        )
    ) {
        throw new Error("suggested action fields are invalid");
    }
    if (
        new Set(["close_tab", "bookmark_and_close", "highlight_tab"])
            .has(action.capability)
        && (typeof tabIndex !== "number" || !Number.isInteger(tabIndex))
    ) {
        throw new Error("tab action lacks an exact non-negative index");
    }
    const metadataRecord = (metadata ?? {}) as Record<string, unknown>;
    if (action.capability === "group_tabs") {
        const rawIndices = metadataRecord.tab_indices;
        const indices = Array.isArray(rawIndices) ? [...rawIndices] : [];
        if (typeof tabIndex === "number" && Number.isInteger(tabIndex)) {
            indices.push(tabIndex);
        }
        if (
            indices.length === 0
            || indices.length > 32
            || indices.some((value) =>
                typeof value !== "number"
                || !Number.isInteger(value)
                || value < 0
            )
        ) {
            throw new Error("group_tabs lacks bounded exact tab indices");
        }
    }
    if (action.capability === "open_url") {
        try {
            const parsed = new URL(String(target ?? ""));
            if (!new Set(["http:", "https:"]).has(parsed.protocol) || !parsed.hostname) {
                throw new Error("unsafe URL");
            }
        } catch {
            throw new Error("open_url target must be an absolute HTTP(S) URL");
        }
    }
    if (action.capability === "search_error") {
        const query = String(metadataRecord.search_query ?? target ?? "");
        if (!query || query.length > 200 || /[\r\n]/.test(query)) {
            throw new Error("search_error query is invalid");
        }
    }
    if (action.capability === "start_timer") {
        const minutes = metadataRecord.minutes ?? 5;
        if (
            typeof minutes !== "number"
            || !Number.isInteger(minutes)
            || minutes < 1
            || minutes > 240
        ) {
            throw new Error("timer duration must be 1..240 minutes");
        }
    }
    return raw as SuggestedAction;
}

function normalizedSuggestionForConsent(
    value: unknown,
): Record<string, unknown> | null {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        return null;
    }
    const candidate = value as Record<string, unknown>;
    if (
        typeof candidate.action_id !== "string"
        || !candidate.action_id
        || typeof candidate.action_type !== "string"
        || !candidate.action_type
        || typeof candidate.label !== "string"
        || !candidate.label
    ) {
        return null;
    }
    const tabIndex = candidate.tab_index ?? null;
    const target = candidate.target ?? "";
    const reason = candidate.reason ?? "";
    const category = candidate.category ?? "recommended";
    const reversible = candidate.reversible ?? false;
    const groupId = candidate.group_id ?? null;
    const metadata = candidate.metadata ?? {};
    const catalogId = candidate.catalog_id ?? null;
    if (
        (tabIndex !== null && (
            typeof tabIndex !== "number"
            || !Number.isInteger(tabIndex)
            || tabIndex < 0
        ))
        || typeof target !== "string"
        || typeof reason !== "string"
        || !new Set(["recommended", "optional", "informational"]).has(
            String(category),
        )
        || typeof reversible !== "boolean"
        || (groupId !== null && typeof groupId !== "string")
        || typeof metadata !== "object"
        || metadata === null
        || Array.isArray(metadata)
        || (catalogId !== null && typeof catalogId !== "string")
    ) {
        return null;
    }
    return {
        action_id: candidate.action_id,
        action_type: candidate.action_type,
        tab_index: tabIndex,
        target,
        label: candidate.label,
        reason,
        category,
        reversible,
        group_id: groupId,
        metadata,
        catalog_id: catalogId,
    };
}

function presentationMatchesManifestAction(
    action: ManifestAction,
    candidate: unknown,
): boolean {
    if (action.source !== "suggested_action") return false;
    try {
        const expected = normalizedSuggestionForConsent(
            suggestedActionFromManifest(action),
        );
        const displayed = normalizedSuggestionForConsent(candidate);
        return expected !== null
            && displayed !== null
            && canonicalJson(expected) === canonicalJson(displayed);
    } catch {
        return false;
    }
}

async function prepareBrowserInverse(
    interventionId: string,
    action: ManifestAction,
): Promise<Record<string, unknown>> {
    if (action.capability === "hide_tabs_except_active") {
        const tabs = await chrome.tabs.query({ currentWindow: true });
        return {
            interventionId,
            activeTabId: tabs.find((tab) => tab.active)?.id ?? null,
            candidateTabIds: tabs
                .filter((tab) => !tab.active && typeof tab.id === "number")
                .map((tab) => tab.id),
        };
    }
    if (action.source !== "suggested_action") return {};
    const suggested = suggestedActionFromManifest(action);
    if (
        suggested.action_type === "close_tab"
        || suggested.action_type === "bookmark_and_close"
    ) {
        await loadSnapshotFromStorage();
        const index = suggested.tab_index;
        const snapshot = typeof index === "number"
            ? interventionTabSnapshot.get(index)
            : undefined;
        let live: chrome.tabs.Tab | null = null;
        if (typeof snapshot?.chromeTabId === "number") {
            try {
                live = await chrome.tabs.get(snapshot.chromeTabId);
            } catch {
                live = null;
            }
        }
        return {
            url: snapshot?.url || "",
            title: snapshot?.title || "",
            originalTabId: snapshot?.chromeTabId ?? null,
            windowId: live?.windowId ?? null,
            index: live?.index ?? null,
            pinned: live?.pinned ?? false,
        };
    }
    if (suggested.action_type === "group_tabs") {
        await loadSnapshotFromStorage();
        const metadata = suggested.metadata || {};
        const indices = Array.isArray(metadata.tab_indices)
            ? metadata.tab_indices.filter(
                (value): value is number => Number.isInteger(value),
            )
            : [];
        if (typeof suggested.tab_index === "number") indices.push(suggested.tab_index);
        return {
            tabIds: indices
                .map((index) => interventionTabSnapshot.get(index)?.chromeTabId)
                .filter((value): value is number => typeof value === "number"),
        };
    }
    if (
        suggested.action_type === "open_url"
        || suggested.action_type === "search_error"
    ) {
        const query = String((suggested.metadata || {}).search_query || suggested.target || "");
        const [active] = await chrome.tabs.query({
            active: true,
            currentWindow: true,
        });
        return {
            url: suggested.action_type === "search_error"
                ? `https://www.google.com/search?q=${encodeURIComponent(query)}`
                : suggested.target,
            stagingUrl: newCreatedTabStageUrl(),
            createdAfterUnixMs: Date.now(),
            blockedIncognito: active?.incognito === true,
            windowId: active?.incognito === true ? null : active?.windowId ?? null,
        };
    }
    if (suggested.action_type === "highlight_tab") {
        const [prior] = await chrome.tabs.query({ active: true, currentWindow: true });
        const target = typeof suggested.tab_index === "number"
            ? interventionTabSnapshot.get(suggested.tab_index)
            : undefined;
        return {
            priorActiveTabId: prior?.id ?? null,
            targetTabId: target?.chromeTabId ?? null,
            noEffect: typeof prior?.id === "number"
                && prior.id === target?.chromeTabId,
        };
    }
    return {};
}

function latestUndoData(actionId: string): Record<string, unknown> | null {
    for (let index = undoStack.length - 1; index >= 0; index--) {
        if (undoStack[index]?.action_id === actionId) {
            return { ...undoStack[index].undo_data };
        }
    }
    return null;
}

async function executeBrowserManifestAction(
    interventionId: string,
    action: ManifestAction,
    preparedInverse: Record<string, unknown>,
    checkpointInverse: (inverse: Record<string, unknown>) => Promise<void>,
): Promise<BrowserCapabilityResult> {
    if (action.capability === "hide_tabs_except_active") {
        const existing = getTabManagerSnapshot(interventionId);
        if (existing) {
            return {
                result: {
                    action_id: action.action_id,
                    success: true,
                    message: "Tabs were already simplified by Cortex",
                    reversible: true,
                },
                inverse: { ...existing },
                status: "already_complete",
            };
        }
        const snapshot = await hideNonActiveTabs(
            interventionId,
            undefined,
            async (grouped) => checkpointInverse({
                ...preparedInverse,
                ...grouped,
            }),
        );
        if (snapshot === null) {
            const candidates = Array.isArray(preparedInverse.candidateTabIds)
                ? preparedInverse.candidateTabIds
                : [];
            if (candidates.length > 0) {
                return {
                    result: {
                        action_id: action.action_id,
                        success: false,
                        message: "Could not create the exact Cortex tab group",
                        reversible: false,
                    },
                    inverse: { ...preparedInverse },
                    status: "failed",
                };
            }
            return {
                result: {
                    action_id: action.action_id,
                    success: true,
                    message: "No eligible tabs needed hiding",
                    reversible: true,
                },
                inverse: { ...preparedInverse, noEffect: true },
                status: "already_complete",
            };
        }
        return {
            result: {
                action_id: action.action_id,
                success: true,
                message: `${snapshot.hiddenTabIds.length} tabs simplified`,
                reversible: true,
            },
            inverse: { ...snapshot },
            status: "succeeded",
        };
    }
    if (action.source !== "suggested_action") {
        throw new Error(`unsupported browser capability ${action.capability}`);
    }
    const suggested = suggestedActionFromManifest(action);
    if (action.capability === "highlight_tab" && preparedInverse.noEffect === true) {
        return {
            result: {
                action_id: action.action_id,
                success: true,
                message: "The exact target tab was already active",
                reversible: false,
            },
            inverse: { ...preparedInverse },
            status: "already_complete",
        };
    }
    const result = await executeAction(
        suggested,
        preparedInverse,
        checkpointInverse,
    );
    const inverse = {
        ...preparedInverse,
        ...(latestUndoData(action.action_id) || {}),
    };
    if (
        (suggested.action_type === "open_url"
            || suggested.action_type === "search_error")
        && typeof inverse.tabId === "number"
    ) {
        inverse.url = preparedInverse.url;
    }
    return {
        result: {
            ...result,
            reversible: result.success && Boolean(action.reverse_capability),
        },
        inverse,
        status: result.success ? "succeeded" : "failed",
    };
}

async function makeActionReceipt(args: {
    interventionId: string;
    authorizationId: string;
    manifestSha256: string;
    actionId: string;
    phase: "apply" | "compensate" | "restore";
    status: "succeeded" | "failed" | "already_complete";
    startedWallMs: number;
    startedMonoNs: number;
    inverse: Record<string, unknown>;
    verification: "verified" | "failed" | "not_applicable";
    verificationDetail: string;
    afterFingerprint: string | null;
    errorCode?: string;
    errorMessage?: string;
    retryable?: boolean;
}): Promise<ActionReceipt> {
    const counterKey = [
        args.authorizationId,
        args.actionId,
        args.phase,
    ].join(":");
    const attempt = await mutateTransactionJournal((journal) => {
        const next = (journal.attempt_counters[counterKey] ?? 0) + 1;
        if (next > 100) {
            throw new Error("receipt retry limit exceeded");
        }
        journal.attempt_counters[counterKey] = next;
        return next;
    });
    const endedMonoNs = Math.max(args.startedMonoNs, monotonicNowNs());
    const endedWallMs = Math.max(args.startedWallMs, Date.now());
    return {
        receipt_id: `rcpt_${newWireId().replace(/-/g, "")}`,
        intervention_id: args.interventionId,
        authorization_id: args.authorizationId,
        manifest_sha256: args.manifestSha256,
        action_id: args.actionId,
        phase: args.phase,
        attempt,
        idempotency_key: `${args.authorizationId}:${args.actionId}:${args.phase}:${attempt}`,
        status: args.status,
        started_at_unix_ms: args.startedWallMs,
        ended_at_unix_ms: endedWallMs,
        started_at_mono_ns: args.startedMonoNs,
        ended_at_mono_ns: endedMonoNs,
        duration_ms: Math.floor((endedMonoNs - args.startedMonoNs) / 1_000_000),
        boot_id: CLIENT_BOOT_ID,
        inverse_payload_json: canonicalJson(args.inverse),
        verification: args.verification,
        verification_detail: args.verificationDetail.slice(0, 500),
        after_fingerprint: args.afterFingerprint,
        error_code: args.errorCode,
        error_message: args.errorMessage?.slice(0, 500),
        retryable: args.retryable ?? false,
        source_client_type: null,
        source_client_id: null,
    };
}

async function sendReceiptBatch(batch: InterventionReceiptBatch): Promise<void> {
    await mutateTransactionJournal((journal) => {
        const receiptIds = new Set(batch.receipts.map((receipt) => receipt.receipt_id));
        const alreadyQueued = journal.receipt_outbox.some((queued) =>
            queued.receipts.some((receipt) => receiptIds.has(receipt.receipt_id))
        );
        if (!alreadyQueued) {
            if (journal.receipt_outbox.length >= MAX_RECEIPT_OUTBOX) {
                throw new Error("transaction receipt outbox is full");
            }
            journal.receipt_outbox.push(batch);
        }
    });
    send({
        type: "INTERVENTION_RECEIPT",
        payload: batch as unknown as Record<string, unknown>,
        timestamp: Date.now() / 1000,
        sequence: ++sequence,
    });
}

async function flushReceiptOutbox(): Promise<void> {
    let journal: BrowserTransactionJournal;
    try {
        journal = await readTransactionJournal();
    } catch (error) {
        console.warn("[cortex.bg] cannot flush corrupt receipt outbox:", String(error));
        return;
    }
    for (const batch of journal.receipt_outbox) {
        send({
            type: "INTERVENTION_RECEIPT",
            payload: batch as unknown as Record<string, unknown>,
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
        });
    }
}

async function acknowledgeReceiptOutbox(payload: Record<string, unknown>): Promise<void> {
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
    try {
        await mutateTransactionJournal((journal) => {
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
                const retired: BrowserOperationRecord[] = [];
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
        });
    } catch (error) {
        console.warn("[cortex.bg] receipt acknowledgement persistence failed:", String(error));
    }
}

function pendingAuthorizationForPayload(
    payload: Record<string, unknown>,
): [string, PendingAuthorization] | null {
    const requestId = typeof payload.authorization_request_id === "string"
        ? payload.authorization_request_id
        : null;
    if (requestId) {
        const pending = pendingAuthorizations.get(requestId);
        if (pending) return [requestId, pending];
    }
    const authorizationId = typeof payload.authorization_id === "string"
        ? payload.authorization_id
        : null;
    if (!authorizationId) return null;
    for (const [candidateRequestId, pending] of pendingAuthorizations) {
        if (pending.authorizationId === authorizationId) {
            return [candidateRequestId, pending];
        }
    }
    return null;
}

function settleDeniedAuthorization(payload: Record<string, unknown>): void {
    const match = pendingAuthorizationForPayload(payload);
    if (!match) return;
    const [requestId, pending] = match;
    clearTimeout(pending.timeout);
    pendingAuthorizations.delete(requestId);
    const detail = typeof payload.detail === "string" && payload.detail.length > 0
        ? payload.detail
        : "The action was not authorized";
    pending.resolve(pending.actionIds.map((actionId) => ({
        action_id: actionId,
        success: false,
        message: detail,
        reversible: false,
    })));
}

function settleAuthorizationFromState(payload: Record<string, unknown>): void {
    const match = pendingAuthorizationForPayload(payload);
    if (!match) return;
    const [requestId, pending] = match;
    if (typeof payload.authorization_id === "string") {
        pending.authorizationId = payload.authorization_id;
    }

    const rawResults = Array.isArray(payload.receipt_results)
        ? payload.receipt_results
        : [];
    for (const raw of rawResults) {
        if (typeof raw !== "object" || raw === null || Array.isArray(raw)) continue;
        const result = raw as Record<string, unknown>;
        const actionId = typeof result.action_id === "string"
            ? result.action_id
            : "";
        if (!pending.actionIds.includes(actionId)) continue;
        const status = typeof result.status === "string" ? result.status : "failed";
        pending.localResults.set(actionId, {
            action_id: actionId,
            success: status === "succeeded" || status === "already_complete",
            message: typeof result.detail === "string" && result.detail.length > 0
                ? result.detail
                : status === "succeeded" || status === "already_complete"
                    ? "Action verified"
                    : "Action failed verification",
            reversible: Boolean(result.reversible),
        });
    }

    const state = typeof payload.state === "string" ? payload.state : "";
    if (!new Set([
        "applied", "partial", "failed", "restored", "restore_failed",
    ]).has(state)) return;
    clearTimeout(pending.timeout);
    pendingAuthorizations.delete(requestId);
    if (state === "restored" || state === "restore_failed") {
        pending.resolve(pending.actionIds.map((actionId) => ({
            action_id: actionId,
            success: false,
            message: state === "restored"
                ? "The apply acknowledgement was lost, so Cortex safely restored the action"
                : "Cortex could not verify recovery after the apply acknowledgement was lost",
            reversible: state === "restore_failed",
        })));
        return;
    }
    pending.resolve(pending.actionIds.map((actionId) => {
        const exact = pending.localResults.get(actionId);
        if (exact) return exact;
        if (state === "applied") {
            return {
                action_id: actionId,
                success: true,
                message: "Action was applied and verified",
                reversible: true,
            };
        }
        return {
            action_id: actionId,
            success: false,
            message: state === "partial"
                ? "The transaction only partially completed"
                : "The transaction failed",
            reversible: false,
        };
    }));
}

async function authorizeActionIds(
    interventionId: string,
    actionIds: string[],
    presentedActions?: unknown[],
): Promise<ActionExecuteResult[]> {
    if (!workspaceMutationAllowed()) {
        throw new Error("Action unavailable in suggest-only mode");
    }
    if (
        !interventionPresentation.active
        || interventionPresentation.active.plan.intervention_id !== interventionId
    ) {
        throw new Error("Intervention is no longer active");
    }
    const verified = await verifyActionManifest(
        interventionPresentation.active.plan.action_manifest,
    );
    const approved = [...new Set(actionIds)].sort();
    if (
        approved.length === 0
        || approved.some((actionId) => !verified.actionsById.has(actionId))
    ) {
        throw new Error("Requested action is absent from the manifest");
    }
    const mountedSuggestions = Array.isArray(
        interventionPresentation.active.plan.suggested_actions,
    )
        ? interventionPresentation.active.plan.suggested_actions
        : [];
    for (const actionId of approved) {
        const immutable = verified.actionsById.get(actionId);
        const mounted = mountedSuggestions.find((candidate) =>
            typeof candidate === "object"
            && candidate !== null
            && !Array.isArray(candidate)
            && (candidate as Record<string, unknown>).action_id === actionId
        );
        const supplied = presentedActions?.find((candidate) =>
            typeof candidate === "object"
            && candidate !== null
            && !Array.isArray(candidate)
            && (candidate as Record<string, unknown>).action_id === actionId
        );
        if (
            !immutable
            || !presentationMatchesManifestAction(immutable, mounted)
            || (
                presentedActions !== undefined
                && !presentationMatchesManifestAction(immutable, supplied)
            )
        ) {
            throw new Error(
                "Displayed action differs from the immutable manifest",
            );
        }
    }
    const requestId = `req_browser_${newWireId().replace(/-/g, "")}`;
    const nowMono = monotonicNowNs();
    const request: InterventionAuthorizationRequest = {
        authorization_request_id: requestId,
        intervention_id: interventionId,
        manifest_sha256: verified.manifest.manifest_sha256,
        approved_action_ids: approved as [string, ...string[]],
        source_surface: "browser",
        requested_at_unix_ms: Date.now(),
        requested_at_mono_ns: nowMono,
        boot_id: CLIENT_BOOT_ID,
    };
    return await new Promise<ActionExecuteResult[]>((resolve) => {
        const timeout = setTimeout(() => {
            pendingAuthorizations.delete(requestId);
            resolve(approved.map((actionId) => ({
                action_id: actionId,
                success: false,
                message: "Authorization timed out before execution",
                reversible: false,
            })));
        }, 35_000);
        pendingAuthorizations.set(requestId, {
            interventionId,
            actionIds: approved,
            localResults: new Map(),
            resolve,
            timeout,
        });
        send({
            type: "INTERVENTION_AUTHORIZE",
            payload: request as unknown as Record<string, unknown>,
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
            correlation_id: interventionPresentation.active?.correlation_id,
        });
    });
}

async function handleInterventionApplyCommand(payload: unknown): Promise<void> {
    let verified: Awaited<ReturnType<typeof verifyApplyCommand>>;
    try {
        const stableClientInstanceId = await getClientInstanceId();
        verified = await verifyApplyCommand(
            payload,
            "browser",
            CLIENT_BOOT_ID,
            Date.now(),
            stableClientInstanceId,
        );
    } catch (error) {
        console.warn("[cortex.bg] rejected INTERVENTION_APPLY:", String(error));
        return;
    }
    const authorization = verified.command.authorization;
    const authorizationId = String(authorization.authorization_id || "");
    const nonce = String(authorization.nonce || "");
    if (!authorizationId || !nonce) return;
    if (!workspaceMutationAllowed()) {
        const receipts = await Promise.all(verified.ownActions.map((action) => {
            const startedWallMs = Date.now();
            const startedMonoNs = monotonicNowNs();
            return makeActionReceipt({
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
                verificationDetail: "Local execution mode denies workspace mutation",
                afterFingerprint: null,
                errorCode: "execution_mode_denied",
                errorMessage: "Local execution mode denies workspace mutation",
                retryable: false,
            });
        }));
        await sendReceiptBatch({
            intervention_id: verified.manifest.intervention_id,
            manifest_sha256: verified.manifest.manifest_sha256,
            authorization_id: authorizationId,
            receipts: receipts as [ActionReceipt, ...ActionReceipt[]],
        });
        return;
    }

    try {
        await mutateTransactionJournal((journal) => {
            if (journal.receipt_outbox.length >= MAX_RECEIPT_OUTBOX) {
                throw new Error("transaction receipt outbox is full");
            }
            const newOperationCount = verified.ownActions.filter((action) =>
                journal.operations[
                    operationKey(verified.manifest.intervention_id, action.action_id)
                ] === undefined
            ).length;
            if (
                Object.keys(journal.operations).length + newOperationCount
                > MAX_TRANSACTION_OPERATIONS
            ) {
                throw new Error("transaction operation journal is full");
            }
            for (const action of verified.ownActions) {
                const counterKey = [
                    authorizationId,
                    action.action_id,
                    "apply",
                ].join(":");
                if ((journal.attempt_counters[counterKey] ?? 0) >= 100) {
                    throw new Error("receipt retry limit reached before apply");
                }
            }
            const prior = journal.consumed_authorizations[authorizationId];
            if (
                prior
                && (
                    prior.manifest_sha256 !== verified.manifest.manifest_sha256
                    || prior.nonce !== nonce
                )
            ) {
                throw new Error("authorization replayed with different content");
            }
            if (!prior) {
                journal.consumed_authorizations[authorizationId] = {
                    manifest_sha256: verified.manifest.manifest_sha256,
                    nonce,
                    consumed_at_unix_ms: Date.now(),
                };
            }
            const authorizationIds = Object.keys(journal.consumed_authorizations);
            if (authorizationIds.length > 256) {
                authorizationIds
                    .sort((left, right) =>
                        journal.consumed_authorizations[left].consumed_at_unix_ms
                        - journal.consumed_authorizations[right].consumed_at_unix_ms
                    )
                    .slice(0, authorizationIds.length - 256)
                    .forEach((id) => delete journal.consumed_authorizations[id]);
            }
        });
    } catch (error) {
        console.warn("[cortex.bg] authorization consumption failed:", String(error));
        return;
    }

    if (verified.ownActions.some((action) => action.source === "suggested_action")) {
        await snapshotTabsForIntervention();
    }

    const receipts: ActionReceipt[] = [];
    const localResults: ActionExecuteResult[] = [];
    for (const action of verified.ownActions) {
        const startedWallMs = Date.now();
        const startedMonoNs = monotonicNowNs();
        const key = operationKey(verified.manifest.intervention_id, action.action_id);
        const existing = await readTransactionJournal().then(
            (journal) => journal.operations[key],
        );
        let existingInverse: Record<string, unknown> = {};
        if (existing) {
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
                const message = "Durable Cortex operation does not match this authorization";
                localResults.push({
                    action_id: action.action_id,
                    success: false,
                    message,
                    reversible: Boolean(action.reverse_capability),
                });
                receipts.push(await makeActionReceipt({
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
                    verificationDetail: message,
                    afterFingerprint: null,
                    errorCode: "durable_operation_mismatch",
                    errorMessage: message,
                    retryable: false,
                }));
                continue;
            }
        }
        if (existing?.state === "applied") {
            const result: ActionExecuteResult = {
                action_id: action.action_id,
                success: true,
                message: "Action was already applied by Cortex",
                reversible: Boolean(action.reverse_capability),
            };
            localResults.push(result);
            receipts.push(await makeActionReceipt({
                interventionId: verified.manifest.intervention_id,
                authorizationId,
                manifestSha256: verified.manifest.manifest_sha256,
                actionId: action.action_id,
                phase: "apply",
                status: "already_complete",
                startedWallMs,
                startedMonoNs,
                inverse: existingInverse,
                verification: action.workspace_mutation === false
                    ? "not_applicable"
                    : "verified",
                verificationDetail: "durable Cortex operation already active",
                afterFingerprint: existing.after_fingerprint,
            }));
            continue;
        }
        if (existing?.state === "applying" && (
            action.capability === "open_url"
            || action.capability === "search_error"
        )) {
            try {
                existingInverse = await reconcileCreatedTabInverse(existingInverse);
            } catch (error) {
                existingInverse = error instanceof IndeterminateBrowserMutationError
                    ? { ...existingInverse, ...error.inverse }
                    : {
                        ...existingInverse,
                        cortexEffectMayExist: true,
                        recoveryError: String(error).slice(0, 300),
                    };
            }
            await mutateTransactionJournal((journal) => {
                const operation = journal.operations[key];
                if (
                    operation
                    && operation.state === "applying"
                    && operation.authorization_id === authorizationId
                    && operation.manifest_sha256 === verified.manifest.manifest_sha256
                ) {
                    operation.inverse_payload_json = canonicalJson(existingInverse);
                    if (existingInverse.noEffect === true) operation.state = "failed";
                    operation.updated_at_unix_ms = Date.now();
                }
            });
        }
        if (
            existing?.state === "applying"
            || existing?.state === "failed"
            || existing?.state === "restored"
        ) {
            const message = existing.state === "applying"
                ? "Prior apply attempt is indeterminate; recovery required"
                : existing.state === "restored"
                    ? "Authorization replay rejected after restoration"
                    : "Authorization replay rejected after a failed attempt";
            const result: ActionExecuteResult = {
                action_id: action.action_id,
                success: false,
                message,
                reversible: Boolean(action.reverse_capability),
            };
            localResults.push(result);
            receipts.push(await makeActionReceipt({
                interventionId: verified.manifest.intervention_id,
                authorizationId,
                manifestSha256: verified.manifest.manifest_sha256,
                actionId: action.action_id,
                phase: "apply",
                status: "failed",
                startedWallMs,
                startedMonoNs,
                inverse: existingInverse,
                verification: "failed",
                verificationDetail: message,
                afterFingerprint: existing.after_fingerprint,
                errorCode: existing.state === "applying"
                    ? "indeterminate_prior_attempt"
                    : "authorization_replay",
                errorMessage: message,
                retryable: existing.state === "applying"
                    && existingInverse.noEffect !== true,
            }));
            continue;
        }

        let preparedInverse: Record<string, unknown> = {};
        try {
            preparedInverse = await prepareBrowserInverse(
                verified.manifest.intervention_id,
                action,
            );
            await mutateTransactionJournal((journal) => {
                journal.operations[key] = {
                    intervention_id: verified.manifest.intervention_id,
                    manifest_sha256: verified.manifest.manifest_sha256,
                    action_id: action.action_id,
                    authorization_id: authorizationId,
                    capability: action.capability,
                    state: "applying",
                    inverse_payload_json: canonicalJson(preparedInverse),
                    after_fingerprint: null,
                    updated_at_unix_ms: Date.now(),
                };
            });
            const executed = await executeBrowserManifestAction(
                verified.manifest.intervention_id,
                action,
                preparedInverse,
                async (inverse) => {
                    await mutateTransactionJournal((journal) => {
                        const operation = journal.operations[key];
                        if (
                            !operation
                            || operation.state !== "applying"
                            || operation.authorization_id !== authorizationId
                            || operation.manifest_sha256 !== verified.manifest.manifest_sha256
                        ) {
                            throw new Error("durable operation changed before inverse checkpoint");
                        }
                        operation.inverse_payload_json = canonicalJson(inverse);
                        operation.updated_at_unix_ms = Date.now();
                    });
                },
            );
            let effectVerification: BrowserEffectVerification = {
                verified: false,
                detail: executed.result.message,
                fingerprint: { capability: action.capability },
            };
            if (executed.result.success) {
                try {
                    effectVerification = await verifyBrowserEffect(
                        action,
                        executed.inverse,
                    );
                } catch (error) {
                    effectVerification = {
                        verified: false,
                        detail: `Postcondition verification failed: ${String(error)}`,
                        fingerprint: { capability: action.capability },
                    };
                }
            }
            const effectMayExist = executed.result.success
                && !effectVerification.verified;
            const receiptInverse = effectMayExist
                ? { ...executed.inverse, cortexEffectMayExist: true }
                : executed.inverse;
            const inverseJson = canonicalJson(receiptInverse);
            const fingerprint = executed.result.success
                ? await sha256Hex(canonicalJson(effectVerification.fingerprint))
                : null;
            await mutateTransactionJournal((journal) => {
                journal.operations[key] = {
                    intervention_id: verified.manifest.intervention_id,
                    manifest_sha256: verified.manifest.manifest_sha256,
                    action_id: action.action_id,
                    authorization_id: authorizationId,
                    capability: action.capability,
                    state: executed.result.success && effectVerification.verified
                        ? "applied"
                        : executed.result.success
                            ? "applying"
                            : "failed",
                    inverse_payload_json: inverseJson,
                    after_fingerprint: fingerprint,
                    updated_at_unix_ms: Date.now(),
                };
            });
            const truthfulResult = executed.result.success
                && effectVerification.verified
                ? executed.result
                : {
                    ...executed.result,
                    success: false,
                    message: executed.result.success
                        ? effectVerification.detail
                        : executed.result.message,
                };
            localResults.push(truthfulResult);
            receipts.push(await makeActionReceipt({
                interventionId: verified.manifest.intervention_id,
                authorizationId,
                manifestSha256: verified.manifest.manifest_sha256,
                actionId: action.action_id,
                phase: "apply",
                status: truthfulResult.success ? executed.status : "failed",
                startedWallMs,
                startedMonoNs,
                inverse: receiptInverse,
                verification: truthfulResult.success ? "verified" : "failed",
                verificationDetail: truthfulResult.success
                    ? effectVerification.detail
                    : truthfulResult.message,
                afterFingerprint: fingerprint,
                errorCode: truthfulResult.success
                    ? undefined
                    : effectMayExist
                        ? "postcondition_unverified"
                        : "capability_failed",
                errorMessage: truthfulResult.success
                    ? undefined
                    : truthfulResult.message,
                retryable: effectMayExist,
            }));
        } catch (error) {
            const message = String(error);
            let latestInverse = preparedInverse;
            try {
                const journal = await readTransactionJournal();
                const operation = journal.operations[key];
                if (operation?.inverse_payload_json) {
                    latestInverse = JSON.parse(
                        operation.inverse_payload_json,
                    ) as Record<string, unknown>;
                }
            } catch {
                // The pre-effect inverse remains the safest available proof.
            }
            if (error instanceof IndeterminateBrowserMutationError) {
                latestInverse = { ...latestInverse, ...error.inverse };
            }
            const uncertainInverse = {
                ...latestInverse,
                cortexEffectMayExist: true,
            };
            await mutateTransactionJournal((journal) => {
                const operation = journal.operations[key];
                if (operation) {
                    operation.state = "applying";
                    operation.inverse_payload_json = canonicalJson(
                        uncertainInverse,
                    );
                    operation.updated_at_unix_ms = Date.now();
                }
            });
            const result: ActionExecuteResult = {
                action_id: action.action_id,
                success: false,
                message,
                reversible: Boolean(action.reverse_capability),
            };
            localResults.push(result);
            receipts.push(await makeActionReceipt({
                interventionId: verified.manifest.intervention_id,
                authorizationId,
                manifestSha256: verified.manifest.manifest_sha256,
                actionId: action.action_id,
                phase: "apply",
                status: "failed",
                startedWallMs,
                startedMonoNs,
                inverse: uncertainInverse,
                verification: "failed",
                verificationDetail: message,
                afterFingerprint: null,
                errorCode: "capability_exception",
                errorMessage: message,
                retryable: true,
            }));
        }
    }
    const pending = pendingAuthorizations.get(
        authorization.authorization_request_id,
    );
    if (pending) {
        pending.authorizationId = authorizationId;
        for (const result of localResults) {
            pending.localResults.set(result.action_id, result);
        }
    }
    if (receipts.length > 0) {
        await sendReceiptBatch({
            intervention_id: verified.manifest.intervention_id,
            manifest_sha256: verified.manifest.manifest_sha256,
            authorization_id: authorizationId,
            receipts: receipts as [ActionReceipt, ...ActionReceipt[]],
        });
    }
}

async function verifyBrowserRestoreEffect(
    action: RestoreAction,
    inverse: Record<string, unknown>,
): Promise<BrowserEffectVerification> {
    if (inverse.noEffect === true) {
        return {
            verified: true,
            detail: "No Cortex-owned effect exists",
            fingerprint: { reverseCapability: action.reverse_capability, noEffect: true },
        };
    }
    if (action.reverse_capability === "close_created_tab") {
        const tabId = typeof inverse.tabId === "number" ? inverse.tabId : null;
        let absent = tabId !== null;
        if (tabId !== null) {
            try {
                await chrome.tabs.get(tabId);
                absent = false;
            } catch {
                absent = true;
            }
        }
        return {
            verified: absent,
            detail: absent
                ? "Exact Cortex-created tab is absent"
                : "Cortex-created tab still exists",
            fingerprint: { tabId, absent },
        };
    }
    if (action.reverse_capability === "restore_active_tab") {
        const targetId = typeof inverse.targetTabId === "number"
            ? inverse.targetTabId
            : null;
        const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
        const verified = targetId !== null && active?.id !== targetId;
        return {
            verified,
            detail: verified
                ? "Cortex-selected tab no longer owns focus"
                : "Cortex-selected tab still owns focus",
            fingerprint: { targetId, activeTabId: active?.id ?? null },
        };
    }
    return {
        verified: false,
        detail: `No restore verifier exists for ${action.reverse_capability}`,
        fingerprint: { reverseCapability: action.reverse_capability },
    };
}

async function performBrowserRestore(
    action: RestoreAction,
    inverse: Record<string, unknown>,
): Promise<{ status: "succeeded" | "failed" | "already_complete"; detail: string }> {
    if (inverse.noEffect === true) {
        return {
            status: "already_complete",
            detail: "No Cortex-owned workspace effect was created",
        };
    }
    switch (action.reverse_capability) {
        case "close_created_tab": {
            const tabId = typeof inverse.tabId === "number" ? inverse.tabId : null;
            if (tabId === null) {
                return { status: "failed", detail: "Missing Cortex-created tab id" };
            }
            let tab: chrome.tabs.Tab;
            try {
                tab = await chrome.tabs.get(tabId);
            } catch {
                return { status: "already_complete", detail: "Created tab already closed" };
            }
            const expectedUrl = typeof inverse.url === "string" ? inverse.url : "";
            const expectedStagingUrl = validCreatedTabStageUrl(inverse.stagingUrl)
                ? inverse.stagingUrl
                : "";
            const ownsCurrentUrl = (
                (Boolean(expectedUrl) && (
                    urlsMatch(tab.url, expectedUrl)
                    || urlsMatch(tab.pendingUrl, expectedUrl)
                ))
                || (Boolean(expectedStagingUrl) && (
                    urlsMatch(tab.url, expectedStagingUrl)
                    || urlsMatch(tab.pendingUrl, expectedStagingUrl)
                ))
            );
            if (!ownsCurrentUrl) {
                return { status: "failed", detail: "Created tab was reused or navigated" };
            }
            try {
                await chrome.tabs.remove(tabId);
            } catch {
                return { status: "failed", detail: "Could not close the Cortex-created tab" };
            }
            return { status: "succeeded", detail: "Cortex-created tab closed" };
        }
        case "restore_active_tab": {
            const priorId = typeof inverse.priorActiveTabId === "number"
                ? inverse.priorActiveTabId
                : null;
            if (priorId === null) return { status: "failed", detail: "Prior active tab missing" };
            const targetId = typeof inverse.targetTabId === "number"
                ? inverse.targetTabId
                : null;
            const [current] = await chrome.tabs.query({ active: true, currentWindow: true });
            if (targetId !== null && current?.id !== targetId) {
                return {
                    status: "already_complete",
                    detail: "User focus superseded the Cortex tab focus",
                };
            }
            const tab = await chrome.tabs.get(priorId);
            if (tab.active) return { status: "already_complete", detail: "Prior tab already active" };
            await chrome.tabs.update(priorId, { active: true });
            return { status: "succeeded", detail: "Prior active tab restored" };
        }
        default:
            return {
                status: "failed",
                detail: `Unsupported reverse capability ${action.reverse_capability}`,
            };
    }
}

async function handleExactRestoreCommand(payload: unknown): Promise<boolean> {
    let verified: ReturnType<typeof verifyRestoreCommand>;
    try {
        verified = verifyRestoreCommand(
            payload,
            "browser",
            await getClientInstanceId(),
        );
    } catch {
        return false;
    }
    const phase = verified.command.reason === "partial_compensation"
        ? "compensate"
        : "restore";
    const receipts: ActionReceipt[] = [];
    for (const action of verified.ownActions) {
        const startedWallMs = Date.now();
        const startedMonoNs = monotonicNowNs();
        const key = operationKey(verified.command.intervention_id, action.action_id);
        const journal = await readTransactionJournal();
        const operation = journal.operations[key];
        let inverse: Record<string, unknown>;
        try {
            inverse = JSON.parse(action.inverse_payload_json) as Record<string, unknown>;
        } catch {
            inverse = {};
        }
        const expectedReverse: Record<string, string | undefined> = {
            open_url: "close_created_tab",
            search_error: "close_created_tab",
            highlight_tab: "restore_active_tab",
        };
        let restored: Awaited<ReturnType<typeof performBrowserRestore>>;
        let restoreVerification: BrowserEffectVerification = {
            verified: false,
            detail: "Restore did not run",
            fingerprint: { actionId: action.action_id, state: "not_run" },
        };
        try {
            if (!operation) {
                restored = {
                    status: "already_complete",
                    detail: "No Cortex-owned effect was durably started on this client",
                };
                inverse = { noEffect: true };
                restoreVerification = {
                    verified: true,
                    detail: "The write-ahead journal contains no Cortex-owned effect",
                    fingerprint: {
                        actionId: action.action_id,
                        state: "never_started",
                    },
                };
            } else {
                const durableInverseBeforeRecovery = operation.inverse_payload_json;
                if (
                    operation.state === "applying"
                    && (
                        operation.capability === "open_url"
                        || operation.capability === "search_error"
                    )
                ) {
                    let localInverse = JSON.parse(
                        durableInverseBeforeRecovery,
                    ) as Record<string, unknown>;
                    localInverse = await reconcileCreatedTabInverse(localInverse);
                    const reconciledJson = canonicalJson(localInverse);
                    if (reconciledJson !== operation.inverse_payload_json) {
                        await mutateTransactionJournal((mutable) => {
                            const record = mutable.operations[key];
                            if (
                                !record
                                || record.state !== "applying"
                                || record.authorization_id !== action.original_authorization_id
                                || record.inverse_payload_json !== durableInverseBeforeRecovery
                            ) {
                                throw new Error(
                                    "durable operation changed during created-tab recovery",
                                );
                            }
                            record.inverse_payload_json = reconciledJson;
                            record.updated_at_unix_ms = Date.now();
                        });
                        operation.inverse_payload_json = reconciledJson;
                    }
                }
                const commandAcceptsLocalRecovery =
                    action.inverse_payload_json === "{}"
                    || action.inverse_payload_json === durableInverseBeforeRecovery;
                const effectiveInverseJson = commandAcceptsLocalRecovery
                    && operation.state === "applying"
                    ? operation.inverse_payload_json
                    : action.inverse_payload_json;
                inverse = JSON.parse(effectiveInverseJson) as Record<string, unknown>;
                if (
                operation.intervention_id !== verified.command.intervention_id
                || operation.manifest_sha256 !== verified.command.manifest_sha256
                || operation.authorization_id !== action.original_authorization_id
                || expectedReverse[operation.capability] !== action.reverse_capability
                || operation.inverse_payload_json !== effectiveInverseJson
                ) {
                    restored = {
                        status: "failed",
                        detail: "Restore action does not match the durable Cortex operation",
                    };
                } else if (operation.state === "restored") {
                    restored = {
                        status: "already_complete",
                        detail: "Cortex operation was already restored",
                    };
                    restoreVerification = {
                        verified: true,
                        detail: "Durable restore completion was already recorded",
                        fingerprint: {
                            actionId: action.action_id,
                            state: "restored",
                            priorFingerprint: operation.after_fingerprint,
                        },
                    };
                } else {
                    restored = await performBrowserRestore(
                        action,
                        inverse,
                    );
                    if (restored.status !== "failed") {
                        restoreVerification = await verifyBrowserRestoreEffect(
                            action,
                            inverse,
                        );
                        if (!restoreVerification.verified) {
                            restored = {
                                status: "failed",
                                detail: restoreVerification.detail,
                            };
                        }
                    }
                }
            }
        } catch (error) {
            restored = { status: "failed", detail: String(error) };
        }
        if (restored.status !== "failed") {
            const restoreFingerprint = await sha256Hex(canonicalJson(
                restoreVerification.fingerprint,
            ));
            await mutateTransactionJournal((mutable) => {
                const record = mutable.operations[key];
                if (record) {
                    record.state = "restored";
                    record.after_fingerprint = restoreFingerprint;
                    record.updated_at_unix_ms = Date.now();
                }
            });
        }
        receipts.push(await makeActionReceipt({
            interventionId: verified.command.intervention_id,
            authorizationId: verified.command.restore_id,
            manifestSha256: verified.command.manifest_sha256,
            actionId: action.action_id,
            phase,
            status: restored.status,
            startedWallMs,
            startedMonoNs,
            inverse,
            verification: restored.status === "failed" ? "failed" : "verified",
            verificationDetail: restored.status === "failed"
                ? restored.detail
                : restoreVerification.detail,
            afterFingerprint: restored.status === "failed"
                ? null
                : await sha256Hex(canonicalJson(restoreVerification.fingerprint)),
            errorCode: restored.status === "failed" ? "restore_failed" : undefined,
            errorMessage: restored.status === "failed" ? restored.detail : undefined,
            retryable: restored.status === "failed",
        }));
    }
    await sendReceiptBatch({
        intervention_id: verified.command.intervention_id,
        manifest_sha256: verified.command.manifest_sha256,
        authorization_id: verified.command.restore_id,
        receipts: receipts as [ActionReceipt, ...ActionReceipt[]],
    });
    return true;
}

interface BrowserCapabilityContext {
    preparedInverse: Record<string, unknown>;
    checkpointInverse: (inverse: Record<string, unknown>) => Promise<void>;
}

const browserCapabilityHandlers: CapabilityHandlers<
    SuggestedAction,
    BrowserCapabilityContext,
    ActionExecuteResult
> = {
    close_tab: (action) => executeCloseTab(action),
    group_tabs: (action, context) => executeGroupTabs(
        action,
        context.preparedInverse,
        context.checkpointInverse,
    ),
    bookmark_and_close: (action, context) => executeBookmarkAndClose(
        action,
        context.preparedInverse,
        context.checkpointInverse,
    ),
    open_url: (action, context) => executeOpenUrl(
        action,
        context.preparedInverse,
        context.checkpointInverse,
    ),
    search_error: (action, context) => executeSearchError(
        action,
        context.preparedInverse,
        context.checkpointInverse,
    ),
    highlight_tab: (action) => executeHighlightTab(action),
    save_session: (action) => executeSaveSession(action),
    copy_to_clipboard: (action) => executeCopyToClipboard(action),
    start_timer: (action) => executeStartTimer(action),
    resume_last_active_file: (action) => executeResumeLastActiveFile(action),
    suggest_movement_break: (action) => executeSuggestMovementBreak(action),
    prompt_micro_commit: (action) => executePromptMicroCommit(action),
    take_biology_break: async (action) => ({
        action_id: action.action_id,
        success: true,
        message: "Break started on desktop",
        reversible: true,
    }),
};

const browserCapabilityExecutor = new CapabilityExecutor(
    browserCapabilityHandlers,
);

async function executeAction(
    action: SuggestedAction,
    preparedInverse: Record<string, unknown>,
    checkpointInverse: (inverse: Record<string, unknown>) => Promise<void>,
): Promise<ActionExecuteResult> {
    try {
        return await browserCapabilityExecutor.execute(action, {
            preparedInverse,
            checkpointInverse,
        });
    } catch (error) {
        if (error instanceof IndeterminateBrowserMutationError) throw error;
        const message = error instanceof UnsupportedCapabilityError
            ? `Unknown action type: ${error.capability}`
            : String(error);
        return {
            action_id: action.action_id,
            success: false,
            message,
            reversible: false,
        };
    }
}

async function executeCloseTab(action: SuggestedAction): Promise<ActionExecuteResult> {
    const aid = action.action_id || `close_${Date.now()}`;

    // Check if tab closing is disabled by user toggle
    try {
        const { cortex_tab_close_disabled } = await chrome.storage.local.get("cortex_tab_close_disabled");
        if (cortex_tab_close_disabled === true) {
            return { action_id: aid, success: false, message: "Tab closing is disabled", reversible: false };
        }
    } catch { /* storage read failed — proceed normally */ }
    // Phase 4d Task C: Pydantic's SuggestedAction.tab_index is strict
    // ``int | None`` — a string-typed payload (e.g. from a buggy LLM
    // adapter) gets rejected server-side, so we mirror that contract
    // here and drop the action rather than silently coercing.
    if (
        action.tab_index === null
        || action.tab_index === undefined
        || typeof action.tab_index !== "number"
        || !Number.isInteger(action.tab_index)
        || action.tab_index < 0
    ) {
        if (DEBUG) {
            console.warn(
                "Cortex: invalid tab_index in close_tab action, dropping",
                { action_id: aid, tab_index: action.tab_index },
            );
        }
        return { action_id: aid, success: false, message: "Invalid tab_index", reversible: false };
    }
    const tabIndex = action.tab_index;

    // Exact target ownership: a missing/stale snapshot fails closed. Title
    // matching can select a different tab and is therefore never an
    // acceptable fallback for an authorized transaction.
    const v = await validateTab(tabIndex);
    if (!v.valid) {
        return {
            action_id: aid,
            success: false,
            message: v.message || "Exact tab target is unavailable",
            reversible: false,
        };
    }
    const tabId = v.tabId;
    const snap = interventionTabSnapshot.get(tabIndex);
    const tabUrl = snap?.url || "";
    const tabTitle = snap?.title || "";

    // LAYER 0: Minimum tab count — never leave fewer than MIN_TABS_TO_KEEP tabs open
    try {
        const currentWindowTabs = await chrome.tabs.query({ currentWindow: true });
        if (currentWindowTabs.length <= MIN_TABS_TO_KEEP) {
            return { action_id: aid, success: false, message: `Only ${currentWindowTabs.length} tabs open — refusing to close (minimum ${MIN_TABS_TO_KEEP})`, reversible: false };
        }
    } catch { /* query failed — proceed with other guards */ }

    // LAYER 1 (redundant): Final active-tab guard before close
    try {
        const liveTab = await chrome.tabs.get(tabId);
        if (liveTab.active) {
            return { action_id: aid, success: false, message: "Refusing to close the active tab", reversible: false };
        }
    } catch {
        return { action_id: aid, success: false, message: "Tab already closed", reversible: false };
    }

    // LAYER 4: Verify expected title if provided
    if (action.metadata?.expected_title) {
        try {
            const liveTab = await chrome.tabs.get(tabId);
            const expected = String(action.metadata.expected_title).toLowerCase();
            const actual = (liveTab.title || "").toLowerCase();
            if (!actual.includes(expected) && !expected.includes(actual)) {
                return { action_id: aid, success: false, message: "Tab title doesn't match expected — skipping", reversible: false };
            }
        } catch {
            return { action_id: aid, success: false, message: "Tab already closed", reversible: false };
        }
    }

    try {
        await chrome.tabs.remove(tabId);
    } catch {
        return { action_id: aid, success: false, message: "Failed to close tab", reversible: false };
    }
    pushUndo({
        action_id: aid,
        action_type: "close_tab",
        undo_data: { url: tabUrl, title: tabTitle },
        timestamp: Date.now(),
    });
    return { action_id: aid, success: true, message: "Tab closed", reversible: true };
}

async function executeGroupTabs(
    action: SuggestedAction,
    preparedInverse: Record<string, unknown>,
    checkpointInverse: (inverse: Record<string, unknown>) => Promise<void>,
): Promise<ActionExecuteResult> {
    const meta = action.metadata || {};
    const tabIndices = Array.isArray(meta.tab_indices)
        ? [...meta.tab_indices]
        : [];
    if (action.tab_index !== null && action.tab_index !== undefined) {
        tabIndices.push(action.tab_index);
    }
    const exactIndices = [...new Set(tabIndices)];
    const tabIds: number[] = [];
    for (const idx of exactIndices) {
        if (typeof idx !== "number" || !Number.isInteger(idx) || idx < 0) {
            return {
                action_id: action.action_id,
                success: false,
                message: "A requested tab index is invalid",
                reversible: false,
            };
        }
        const v = await validateTab(idx);
        if (!v.valid) {
            return {
                action_id: action.action_id,
                success: false,
                message: v.message || "An exact tab target is unavailable",
                reversible: false,
            };
        }
        tabIds.push(v.tabId);
    }
    if (tabIds.length === 0) {
        return { action_id: action.action_id, success: false, message: "No valid tabs to group", reversible: false };
    }
    const groupName = ((action.metadata || {}).group_name as string) || action.label || "Grouped";
    const groupId = await groupSpecificTabs(
        tabIds,
        groupName,
        "blue",
        async (createdGroupId) => checkpointInverse({
            ...preparedInverse,
            tabIds,
            groupId: createdGroupId,
        }),
    );
    if (groupId === null) {
        return {
            action_id: action.action_id,
            success: false,
            message: "Could not create the exact requested tab group",
            reversible: false,
        };
    }
    pushUndo({
        action_id: action.action_id,
        action_type: "group_tabs",
        undo_data: { tabIds, groupId },
        timestamp: Date.now(),
    });
    return { action_id: action.action_id, success: true, message: `${tabIds.length} tabs grouped`, reversible: true };
}

async function executeBookmarkAndClose(
    action: SuggestedAction,
    preparedInverse: Record<string, unknown>,
    checkpointInverse: (inverse: Record<string, unknown>) => Promise<void>,
): Promise<ActionExecuteResult> {
    const aid = action.action_id || `bmc_${Date.now()}`;

    // Check if tab closing is disabled by user toggle
    try {
        const { cortex_tab_close_disabled } = await chrome.storage.local.get("cortex_tab_close_disabled");
        if (cortex_tab_close_disabled === true) {
            return { action_id: aid, success: false, message: "Tab closing is disabled", reversible: false };
        }
    } catch { /* storage read failed — proceed normally */ }
    // Phase 4d Task C: strict tab_index parity with the Python schema.
    if (
        action.tab_index === null
        || action.tab_index === undefined
        || typeof action.tab_index !== "number"
        || !Number.isInteger(action.tab_index)
        || action.tab_index < 0
    ) {
        if (DEBUG) {
            console.warn(
                "Cortex: invalid tab_index in bookmark_and_close action, dropping",
                { action_id: aid, tab_index: action.tab_index },
            );
        }
        return { action_id: aid, success: false, message: "Invalid tab_index", reversible: false };
    }
    const tabIndex = action.tab_index;

    const v = await validateTab(tabIndex);
    if (!v.valid) {
        return {
            action_id: aid,
            success: false,
            message: v.message || "Exact tab target is unavailable",
            reversible: false,
        };
    }
    const tabId = v.tabId;
    const snap = interventionTabSnapshot.get(tabIndex);
    const tabUrl = snap?.url || "";
    const tabTitle = snap?.title || "";

    // LAYER 1: Final active-tab guard
    try {
        const liveTab = await chrome.tabs.get(tabId);
        if (liveTab.active) {
            return { action_id: aid, success: false, message: "Refusing to close the active tab", reversible: false };
        }
    } catch {
        return { action_id: aid, success: false, message: "Tab already closed", reversible: false };
    }

    if (!tabUrl) {
        return {
            action_id: aid,
            success: false,
            message: "Exact tab URL is unavailable",
            reversible: false,
        };
    }
    let bookmarkId: string;
    let bookmark;
    try {
        bookmark = await chrome.bookmarks.create({
            title: tabTitle || "Cortex bookmark",
            url: tabUrl,
        });
        bookmarkId = bookmark.id;
    } catch {
        return {
            action_id: aid,
            success: false,
            message: "Could not create the requested bookmark",
            reversible: false,
        };
    }
    try {
        await checkpointInverse({
            ...preparedInverse,
            bookmarkId,
        });
    } catch (error) {
        try {
            await chrome.bookmarks.remove(bookmarkId);
        } catch {
            throw new IndeterminateBrowserMutationError(
                "Bookmark exists but its inverse checkpoint failed",
                { ...preparedInverse, bookmarkId },
                error,
            );
        }
        return {
            action_id: aid,
            success: false,
            message: "Bookmark checkpoint failed; the bookmark was rolled back",
            reversible: false,
        };
    }
    try {
        await chrome.tabs.remove(tabId);
    } catch {
        try {
            await chrome.bookmarks.remove(bookmarkId);
        } catch {
            throw new IndeterminateBrowserMutationError(
                "Tab close failed and the Cortex bookmark may still exist",
                { ...preparedInverse, bookmarkId },
            );
        }
        return { action_id: aid, success: false, message: "Failed to close tab", reversible: false };
    }
    pushUndo({
        action_id: aid,
        action_type: "bookmark_and_close",
        undo_data: {
            url: tabUrl,
            title: tabTitle,
            bookmarkId,
        },
        timestamp: Date.now(),
    });
    return { action_id: aid, success: true, message: "Bookmarked & closed", reversible: true };
}

async function executeOpenUrl(
    action: SuggestedAction,
    preparedInverse: Record<string, unknown>,
    checkpointInverse: (inverse: Record<string, unknown>) => Promise<void>,
): Promise<ActionExecuteResult> {
    if (!action.target) {
        return { action_id: action.action_id, success: false, message: "No URL provided", reversible: false };
    }
    if (preparedInverse.blockedIncognito === true) {
        return {
            action_id: action.action_id,
            success: false,
            message: "Cortex never changes incognito windows",
            reversible: false,
        };
    }
    const stagingUrl = preparedInverse.stagingUrl;
    if (!validCreatedTabStageUrl(stagingUrl)) {
        throw new Error("Created-tab recovery marker is missing or invalid");
    }
    const windowId = typeof preparedInverse.windowId === "number"
        ? preparedInverse.windowId
        : undefined;
    const tab = await chrome.tabs.create({
        url: stagingUrl,
        active: false,
        ...(windowId === undefined ? {} : { windowId }),
    });
    if (typeof tab.id !== "number") {
        throw new IndeterminateBrowserMutationError(
            "Chrome created a staged tab without returning its identity",
            preparedInverse,
        );
    }
    try {
        await checkpointInverse({ ...preparedInverse, tabId: tab.id });
    } catch (error) {
        try {
            await chrome.tabs.remove(tab.id);
        } catch {
            throw new IndeterminateBrowserMutationError(
                "Created tab exists but its inverse checkpoint failed",
                { ...preparedInverse, tabId: tab.id },
                error,
            );
        }
        return {
            action_id: action.action_id,
            success: false,
            message: "Tab checkpoint failed; the created tab was rolled back",
            reversible: false,
        };
    }
    try {
        await chrome.tabs.update(tab.id, { url: action.target });
    } catch (error) {
        try {
            await chrome.tabs.remove(tab.id);
        } catch {
            throw new IndeterminateBrowserMutationError(
                "Created tab could not navigate or be removed",
                { ...preparedInverse, tabId: tab.id },
                error,
            );
        }
        return {
            action_id: action.action_id,
            success: false,
            message: "Tab navigation failed; the staged tab was rolled back",
            reversible: false,
        };
    }
    pushUndo({
        action_id: action.action_id,
        action_type: "open_url",
        undo_data: { tabId: tab.id },
        timestamp: Date.now(),
    });
    return { action_id: action.action_id, success: true, message: "Opened in background", reversible: true };
}

async function executeSearchError(
    action: SuggestedAction,
    preparedInverse: Record<string, unknown>,
    checkpointInverse: (inverse: Record<string, unknown>) => Promise<void>,
): Promise<ActionExecuteResult> {
    const query = ((action.metadata || {}).search_query as string) || action.target || "";
    if (!query) {
        return { action_id: action.action_id, success: false, message: "No search query", reversible: false };
    }
    if (preparedInverse.blockedIncognito === true) {
        return {
            action_id: action.action_id,
            success: false,
            message: "Cortex never changes incognito windows",
            reversible: false,
        };
    }
    const url = `https://www.google.com/search?q=${encodeURIComponent(query)}`;
    const stagingUrl = preparedInverse.stagingUrl;
    if (!validCreatedTabStageUrl(stagingUrl)) {
        throw new Error("Created-tab recovery marker is missing or invalid");
    }
    const windowId = typeof preparedInverse.windowId === "number"
        ? preparedInverse.windowId
        : undefined;
    const tab = await chrome.tabs.create({
        url: stagingUrl,
        active: false,
        ...(windowId === undefined ? {} : { windowId }),
    });
    if (typeof tab.id !== "number") {
        throw new IndeterminateBrowserMutationError(
            "Chrome created a staged search tab without returning its identity",
            preparedInverse,
        );
    }
    try {
        await checkpointInverse({ ...preparedInverse, tabId: tab.id, url });
    } catch (error) {
        try {
            await chrome.tabs.remove(tab.id);
        } catch {
            throw new IndeterminateBrowserMutationError(
                "Created search tab exists but its inverse checkpoint failed",
                { ...preparedInverse, tabId: tab.id, url },
                error,
            );
        }
        return {
            action_id: action.action_id,
            success: false,
            message: "Search-tab checkpoint failed; the tab was rolled back",
            reversible: false,
        };
    }
    try {
        await chrome.tabs.update(tab.id, { url });
    } catch (error) {
        try {
            await chrome.tabs.remove(tab.id);
        } catch {
            throw new IndeterminateBrowserMutationError(
                "Created search tab could not navigate or be removed",
                { ...preparedInverse, tabId: tab.id, url },
                error,
            );
        }
        return {
            action_id: action.action_id,
            success: false,
            message: "Search navigation failed; the staged tab was rolled back",
            reversible: false,
        };
    }
    pushUndo({
        action_id: action.action_id,
        action_type: "search_error",
        undo_data: { tabId: tab.id },
        timestamp: Date.now(),
    });
    return { action_id: action.action_id, success: true, message: "Search opened", reversible: true };
}

async function executeHighlightTab(action: SuggestedAction): Promise<ActionExecuteResult> {
    if (action.tab_index === null || action.tab_index === undefined) {
        return { action_id: action.action_id, success: false, message: "No tab_index", reversible: false };
    }
    const v = await validateTab(action.tab_index);
    if (!v.valid) {
        return { action_id: action.action_id, success: false, message: v.message, reversible: false };
    }
    await chrome.tabs.update(v.tabId, { active: true });
    return { action_id: action.action_id, success: true, message: "Tab activated", reversible: false };
}

async function executeSaveSession(action: SuggestedAction): Promise<ActionExecuteResult> {
    const name = action.target || `Session ${new Date().toLocaleTimeString()}`;
    await saveTabSession(name, focusSession?.goal);
    return { action_id: action.action_id, success: true, message: "Session saved", reversible: false };
}

async function executeCopyToClipboard(action: SuggestedAction): Promise<ActionExecuteResult> {
    const text = action.target || ((action.metadata || {}).text as string) || "";
    if (!text) {
        return { action_id: action.action_id, success: false, message: "Nothing to copy", reversible: false };
    }
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab?.id) {
            await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                func: (t: string) => navigator.clipboard.writeText(t),
                args: [text],
            });
        }
    } catch {
        return { action_id: action.action_id, success: false, message: "Clipboard access failed", reversible: false };
    }
    return { action_id: action.action_id, success: true, message: "Copied to clipboard", reversible: false };
}

async function executeStartTimer(action: SuggestedAction): Promise<ActionExecuteResult> {
    const minutes = ((action.metadata || {}).minutes as number) || 5;
    await chrome.alarms.create("cortex-break-timer", { delayInMinutes: minutes });
    injectToast("Break timer started", `Timer set for ${minutes} minutes. We'll remind you when it's done.`);
    return { action_id: action.action_id, success: true, message: `${minutes}min timer started`, reversible: false };
}

// ---------------------------------------------------------------------
// P0 §3.5 — HYPO / RECOVERY action handlers
// ---------------------------------------------------------------------

/**
 * Relay a "resume the user's last active file" request to a connected
 * VS Code editor via the daemon's WS bridge. The daemon owns the editor
 * connection; the browser extension cannot open files directly. If no
 * editor is connected, we degrade to a soft popup toast so the user
 * still sees the suggestion.
 *
 * ``action.target`` is "file_path:line" (validated server-side by
 * SuggestedAction's _TARGET_MAX_LEN cap).
 */
async function executeResumeLastActiveFile(
    action: SuggestedAction,
): Promise<ActionExecuteResult> {
    const aid = action.action_id || `resume_${Date.now()}`;
    const target = (action.target || "").trim();
    if (!target) {
        return {
            action_id: aid,
            success: false,
            message: "No file_path:line provided",
            reversible: false,
        };
    }

    // Best-effort: ask the daemon to relay the action to its connected
    // VS Code (or fallback editor) client. The daemon is the single
    // owner of editor WS sessions; we never address the editor directly.
    try {
        send({
            type: "USER_ACTION",
            payload: {
                action: "execute_action",
                action_type: "resume_last_active_file",
                target,
                action_id: aid,
                intervention_id:
                    typeof interventionPresentation.active?.plan.intervention_id === "string"
                        ? (interventionPresentation.active.plan.intervention_id as string)
                        : undefined,
                timestamp: Date.now() / 1000,
            },
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
            correlation_id: interventionPresentation.active?.correlation_id,
        });
    } catch {
        // Daemon may be offline — fall through to the soft toast.
    }

    // Always show a soft toast so the user gets feedback even when no
    // editor is connected. The toast is informational, not a replacement
    // for the editor jump.
    const [filePath, lineStr] = target.split(":");
    const line = lineStr ? parseInt(lineStr, 10) : NaN;
    const label = !Number.isNaN(line)
        ? `Open ${filePath} at line ${line}`
        : `Open ${filePath}`;
    injectToast("Resume where you left off", label);

    return {
        action_id: aid,
        success: true,
        message: `Relayed resume to editor: ${label}`,
        reversible: true,
    };
}

/**
 * Surface a non-blocking "time to stretch" suggestion. Uses
 * chrome.notifications when the permission is granted; otherwise falls
 * back to an injected toast in the active tab so the user still sees it.
 *
 * ``action.target`` may be a duration string (minutes) — defaults to 2.
 */
async function executeSuggestMovementBreak(
    action: SuggestedAction,
): Promise<ActionExecuteResult> {
    const aid = action.action_id || `movement_${Date.now()}`;
    const rawMinutes = parseInt((action.target || "").trim(), 10);
    const minutes = Number.isFinite(rawMinutes) && rawMinutes > 0 ? rawMinutes : 2;
    const title = "Time for a 2-minute stand & stretch";
    const body = `Step away for ${minutes} minute${minutes === 1 ? "" : "s"}. Your back will thank you.`;

    // Prefer chrome.notifications if available + permitted. We never block
    // on it — a missing permission silently falls through to the toast.
    let notificationShown = false;
    try {
        if (chrome.notifications && typeof chrome.notifications.create === "function") {
            await new Promise<void>((resolve) => {
                try {
                    chrome.notifications.create(
                        `cortex-movement-${aid}`,
                        {
                            type: "basic",
                            iconUrl: chrome.runtime.getURL("assets/icon.png"),
                            title,
                            message: body,
                            priority: 0,
                        },
                        () => {
                            notificationShown = !chrome.runtime.lastError;
                            resolve();
                        },
                    );
                } catch {
                    resolve();
                }
            });
        }
    } catch {
        // chrome.notifications missing or rejected — fall through.
    }

    if (!notificationShown) {
        injectToast(title, body);
    }

    return {
        action_id: aid,
        success: true,
        message: `Movement break suggested (${minutes} min)`,
        reversible: false,
    };
}

/**
 * Surface a "you have N+ unstaged changes — consider a draft commit"
 * popup toast. Informational only; we never run ``git commit`` for the
 * user. The unstaged count is read from action.metadata.unstaged_count
 * when present, otherwise the toast omits the number.
 */
async function executePromptMicroCommit(
    action: SuggestedAction,
): Promise<ActionExecuteResult> {
    const aid = action.action_id || `commit_${Date.now()}`;
    const meta = (action.metadata || {}) as Record<string, unknown>;
    const rawCount = meta.unstaged_count;
    const count = typeof rawCount === "number" && rawCount > 0
        ? Math.floor(rawCount)
        : null;

    const title = "Consider a draft commit";
    const body = count !== null
        ? `You have ${count}+ unstaged changes — saving a checkpoint takes 10 seconds.`
        : "You have unstaged changes — saving a checkpoint takes 10 seconds.";

    injectToast(title, body);

    return {
        action_id: aid,
        success: true,
        message: count !== null
            ? `Micro-commit nudge surfaced (${count} unstaged)`
            : "Micro-commit nudge surfaced",
        reversible: false,
    };
}

// ---------------------------------------------------------------------

async function undoAction(actionId: string): Promise<boolean> {
    const idx = undoStack.findIndex((e) => e.action_id === actionId);
    if (idx === -1) return false;
    const entry = undoStack[idx];
    undoStack.splice(idx, 1);
    schedulePersist();

    try {
        switch (entry.action_type) {
            case "close_tab":
            case "bookmark_and_close": {
                // Reopen from saved URL
                const url = entry.undo_data.url as string;
                if (url) {
                    try {
                        await chrome.tabs.create({ url, active: false });
                    } catch {
                        // Failed to reopen
                    }
                }
                break;
            }
            case "group_tabs": {
                const tabIds = entry.undo_data.tabIds as number[];
                if (tabIds.length > 0) {
                    try {
                        // chrome.tabs.ungroup wants a non-empty tuple; the
                        // guard above makes this cast sound.
                        await chrome.tabs.ungroup(
                            tabIds as [number, ...number[]],
                        );
                    } catch {
                        // Some tabs may be gone
                    }
                }
                break;
            }
            case "open_url":
            case "search_error": {
                const tabId = entry.undo_data.tabId as number;
                if (tabId) {
                    try {
                        await chrome.tabs.remove(tabId);
                    } catch {
                        // Already closed
                    }
                }
                break;
            }
        }
        return true;
    } catch {
        return false;
    }
}

async function executeAllRecommended(
    interventionId: string,
): Promise<ActionExecuteResult[]> {
    if (
        !interventionPresentation.active
        || interventionPresentation.active.plan.intervention_id !== interventionId
    ) {
        throw new Error("Intervention is no longer active");
    }
    const verified = await verifyActionManifest(
        interventionPresentation.active.plan.action_manifest,
    );
    const actionIds = [...verified.actionsById.values()]
        .filter((action) => {
            if (action.source !== "suggested_action") return false;
            try {
                return suggestedActionFromManifest(action).category === "recommended";
            } catch {
                return false;
            }
        })
        .map((action) => action.action_id);
    const results = await authorizeActionIds(interventionId, actionIds);

    // Clear the intervention after execution so popup doesn't show stale data
    const hadIntervention = interventionPresentation.active !== null;
    const mountedInterventionId =
        typeof interventionPresentation.active?.plan.intervention_id === "string"
            ? (interventionPresentation.active.plan.intervention_id as string)
            : undefined;
    // F16: outbound USER_ACTION carries the same cid the daemon stamped on
    // the plan, so a superseded ACK is ignored by `_handle_user_action`.
    const interventionCid = interventionPresentation.active?.correlation_id;
    interventionPresentation.clear();
    // Persist cleared state
    try { await chrome.storage.session.remove(["cortex_active_intervention", "cortex_active_intervention_cid", "cortex_active_intervention_mounted_at", "cortex_tab_snapshot", "cortex_tab_mgr_snapshots"]); } catch {}

    // Notify daemon that user engaged with the intervention
    if (hadIntervention && mountedInterventionId) {
        send({
            type: "USER_ACTION",
            payload: {
                action: "engaged",
                intervention_id: mountedInterventionId,
                timestamp: Date.now() / 1000,
            },
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
            correlation_id: interventionCid,
        });
    }

    // Broadcast to popup so it clears the intervention card
    broadcastToPopup({
        type: "INTERVENTION_RESTORE",
        payload: { intervention_id: mountedInterventionId },
    });

    return results;
}

/** Undo all recent actions (used by the overlay's "Undo" button). */
async function undoAllRecent(): Promise<void> {
    // Undo in reverse order
    const toUndo = [...undoStack].reverse();
    for (const entry of toUndo) {
        await undoAction(entry.action_id);
    }
}

// --- Comfort Alerts (Head/Neck Proxy & Eye Strain) ---

function checkHealthAlerts(payload: Record<string, unknown>): void {
    const bio = payload.biometrics as {
        blink_rate?: number | null;
        head_neck_flexion_score?: number | null;
        head_neck_proxy_available?: boolean;
    } | undefined;
    if (!bio) return;
    const now = Date.now();

    // Low blink rate → eye strain
    const blinkRate = bio.blink_rate;
    if (blinkRate !== null && blinkRate !== undefined && blinkRate < 10) {
        if (lowBlinkStart === 0) lowBlinkStart = now;
        if (
            now - lowBlinkStart > BLINK_ALERT_THRESHOLD &&
            now - lastBlinkAlert > HEALTH_ALERT_COOLDOWN
        ) {
            lastBlinkAlert = now;
            showHealthNotification(
                "Eye strain detected",
                "Your blink rate is low. Look away from the screen for 20 seconds (20-20-20 rule).",
            );
            lowBlinkStart = 0;
        }
    } else {
        lowBlinkStart = 0;
    }

    // Camera-relative head/neck proxy. This is intentionally not called
    // posture: no torso or shoulder landmarks are measured. The daemon marks
    // the proxy unavailable after a camera/scale/calibration mismatch.
    const proxyAvailable = bio.head_neck_proxy_available === true;
    const flexion = bio.head_neck_flexion_score;
    if (
        proxyAvailable &&
        flexion !== null &&
        flexion !== undefined &&
        flexion > 0.6
    ) {
        if (headNeckFlexionStart === 0) headNeckFlexionStart = now;
        if (
            now - headNeckFlexionStart > HEAD_NECK_ALERT_THRESHOLD &&
            now - lastHeadNeckAlert > HEALTH_ALERT_COOLDOWN
        ) {
            lastHeadNeckAlert = now;
            showHealthNotification(
                "Head and neck comfort check",
                "Your camera-relative head angle has stayed beyond your calibrated neutral range. Adjust only if a different position feels more comfortable.",
            );
            headNeckFlexionStart = 0;
        }
    } else {
        headNeckFlexionStart = 0;
    }
}

function showHealthNotification(title: string, body: string): void {
    broadcastToPopup({ type: "HEALTH_ALERT", title, body });
    // Also inject a small toast into active tab
    injectToast(title, body);
}

async function injectToast(title: string, body: string): Promise<void> {
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab?.id && tab.url && !tab.incognito && !tab.url.startsWith("chrome://")) {
            await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                func: (t: string, b: string) => {
                    const id = "cortex-toast";
                    document.getElementById(id)?.remove();
                    const host = document.createElement("div");
                    host.id = id;
                    host.style.cssText =
                        "position:fixed;top:16px;right:16px;z-index:2147483647;";
                    host.setAttribute("role", "status");
                    host.setAttribute("aria-live", "polite");
                    const shadow = host.attachShadow({ mode: "open" });
                    const style = document.createElement("style");
                    style.textContent = `
                        *{box-sizing:border-box}
                        .toast{position:relative;width:min(320px,calc(100vw - 32px));padding:12px 42px 12px 14px;border-radius:10px;font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text',system-ui,sans-serif;background:#111113;color:#e4e4e7;border:1px solid rgba(255,255,255,.06);box-shadow:0 4px 20px rgba(0,0,0,.4);font-size:12px;line-height:1.5}
                        .title{font-weight:600;margin-bottom:3px;font-size:12px;color:#e4e4e7}
                        .body{color:#a1a1aa;font-size:11px}
                        .close{position:absolute;top:6px;right:6px;width:32px;height:32px;border:0;border-radius:7px;background:transparent;color:#a1a1aa;font:16px/1 system-ui;cursor:pointer;transition:background-color 120ms cubic-bezier(.23,1,.32,1),color 120ms cubic-bezier(.23,1,.32,1),transform 120ms cubic-bezier(.23,1,.32,1)}
                        .close:active{transform:scale(.96)}
                        .close:focus-visible{outline:2px solid #dfb15b;outline-offset:1px}
                        @media (hover:hover) and (pointer:fine){.close:hover{background:rgba(255,255,255,.07);color:#e4e4e7}}
                        @media (prefers-reduced-motion:reduce){.close{transition:background-color 120ms cubic-bezier(.23,1,.32,1),color 120ms cubic-bezier(.23,1,.32,1)}.close:active{transform:none}}
                    `;
                    const toast = document.createElement("div");
                    toast.className = "toast";
                    const titleEl = document.createElement("div");
                    titleEl.className = "title";
                    titleEl.textContent = t;
                    const bodyEl = document.createElement("div");
                    bodyEl.className = "body";
                    bodyEl.textContent = b;
                    const close = document.createElement("button");
                    close.className = "close";
                    close.type = "button";
                    close.setAttribute("aria-label", "Dismiss health notification");
                    close.textContent = "×";
                    toast.append(titleEl, bodyEl, close);
                    shadow.append(style, toast);
                    document.body.appendChild(host);

                    const reduced = window.matchMedia(
                        "(prefers-reduced-motion: reduce)",
                    ).matches;
                    let dismissed = false;
                    let timeoutId = 0;
                    const dismiss = () => {
                        if (dismissed) return;
                        dismissed = true;
                        window.clearTimeout(timeoutId);
                        toast.getAnimations().forEach((animation) =>
                            animation.cancel(),
                        );
                        if (reduced) {
                            host.remove();
                            return;
                        }
                        const exit = toast.animate(
                            [
                                { opacity: 1, transform: "translateY(0)" },
                                {
                                    opacity: 0,
                                    transform: "translateY(-8px)",
                                },
                            ],
                            {
                                duration: 140,
                                easing: "cubic-bezier(.4,0,1,1)",
                                fill: "forwards",
                            },
                        );
                        exit.onfinish = () => host.remove();
                    };
                    close.addEventListener("click", dismiss);
                    if (!reduced) {
                        toast.animate(
                            [
                                {
                                    opacity: 0,
                                    transform: "translateY(-8px)",
                                },
                                { opacity: 1, transform: "translateY(0)" },
                            ],
                            {
                                duration: 160,
                                easing: "cubic-bezier(.23,1,.32,1)",
                            },
                        );
                    }
                    timeoutId = window.setTimeout(dismiss, 8000);
                },
                args: [title, body],
            });
        }
    } catch {
        // Injection failed
    }
}

// --- Popup Communication ---

function broadcastToPopup(message: Record<string, unknown>): void {
    try {
        chrome.runtime.sendMessage(message).catch(() => {
            // Popup not open
        });
    } catch {
        // No listener
    }
}

/**
 * P0 §3.12: surface an INTERVENTION_TRIGGER via OS-level cues when the
 * desktop dashboard isn't the active window.
 *
 * Two surfaces in parallel:
 *   1. ``chrome.action.setBadgeText`` bumps the toolbar badge to "1"
 *      so users glancing at the toolbar see the pending cue.
 *   2. ``chrome.notifications.create`` fires a system notification
 *      with two buttons: Open (focuses the dashboard via
 *      ``LAUNCH_CORTEX``) and Snooze (sends SNOOZE_REQUEST).
 *
 * Privacy invariant: the body shows only the LLM-generated headline
 * (already F09-sanitised). No biometric values cross the boundary.
 */
function surfaceInterventionOSNotification(
    // Finding 5: bind the read fields to the generated
    // InterventionTriggerPayload so a daemon-side rename of
    // ``headline`` / ``primary_focus`` / ``intervention_id`` breaks the
    // type-check here rather than silently producing an empty
    // notification. We keep the index signature so the function still
    // accepts the raw ``Record<string, unknown>`` wire object.
    payload: Pick<
        InterventionTriggerPayload,
        "headline" | "primary_focus" | "intervention_id"
    > &
        Record<string, unknown>,
    correlationId: string,
): void {
    const headline = String(payload.headline || "Cortex");
    const focusHint = String(payload.primary_focus || "");
    const interventionId = String(payload.intervention_id || "");

    // 1) Badge bump — visible in any tab.
    try {
        const action = (chrome as unknown as {
            action?: { setBadgeText: (d: { text: string }) => void;
                setBadgeBackgroundColor: (d: { color: string }) => void; };
        }).action;
        if (action) {
            action.setBadgeText({ text: "1" });
            action.setBadgeBackgroundColor({ color: "#D97757" });
        }
    } catch { /* badge unavailable */ }

    // 2) System notification with action buttons.
    try {
        const notifications = (chrome as unknown as {
            notifications?: {
                create: (
                    id: string,
                    opts: Record<string, unknown>,
                    cb?: (id: string) => void,
                ) => void;
            };
        }).notifications;
        if (!notifications || typeof notifications.create !== "function") {
            return;
        }
        const notificationId = `cortex_intervention_${interventionId || correlationId}`;
        // Phase-3 P0-N1: ``assets/icon128.png`` does not exist in the
        // Plasmo build output (the manifest's per-size icons are
        // emitted with content hashes that change every build). The
        // only stable, packaged asset is ``assets/icon.png`` (512x512),
        // resolved here via ``chrome.runtime.getURL`` which gives an
        // absolute ``chrome-extension://`` URL that chrome.notifications
        // can fetch. Without this fix the OS notification path is
        // silently broken in every signed production build.
        const iconUrl = chrome.runtime.getURL("assets/icon.png");
        const body = focusHint
            ? `${focusHint}`
            : "Cortex has a suggestion for you.";
        notifications.create(notificationId, {
            type: "basic",
            title: `Cortex · ${headline}`.slice(0, 96),
            message: body.slice(0, 240),
            iconUrl,
            priority: 1,
            requireInteraction: false,
            buttons: [
                { title: "Open" },
                { title: "Snooze" },
            ],
        }, (createdId?: string) => {
            // Phase-3 P0-N1: surface the lastError so a future
            // missing-asset regression fails loudly in logs instead
            // of silently dropping the notification.
            if (chrome.runtime.lastError) {
                console.warn(
                    "cortex.bg notifications.create lastError",
                    chrome.runtime.lastError.message,
                );
            } else if (!createdId) {
                console.debug("cortex.bg notifications.create returned no id");
            }
        });
    } catch {
        // notifications unavailable
    }
}

/**
 * P0 §3.3: toggle the toolbar action badge to signal a pending session
 * recap. ``chrome.action.*`` is MV3-only; we guard the call so the
 * background script keeps loading cleanly in test harnesses (jsdom)
 * that omit the API.
 */
function setRecapBadge(on: boolean): void {
    try {
        const action = (chrome as unknown as {
            action?: {
                setBadgeText: (details: { text: string }) => void;
                setBadgeBackgroundColor: (details: { color: string }) => void;
            };
        }).action;
        if (!action) return;
        if (on) {
            action.setBadgeText({ text: "✓" });
            action.setBadgeBackgroundColor({ color: "#D97757" });
        } else {
            action.setBadgeText({ text: "" });
        }
    } catch {
        // action API may be unavailable in some contexts
    }
}

let lastAmbientBroadcast = 0;
const AMBIENT_THROTTLE_MS = 2000; // Send ambient updates every 2s, not every 500ms

async function broadcastToContentScripts(
    message: Record<string, unknown>,
): Promise<void> {
    const now = Date.now();
    if (now - lastAmbientBroadcast < AMBIENT_THROTTLE_MS) return;
    lastAmbientBroadcast = now;

    try {
        const tabs = (await chrome.tabs.query({})).filter(
            (tab) => !tab.incognito,
        );
        for (const tab of tabs) {
            if (tab.id && tab.url && !tab.url.startsWith("chrome://")) {
                chrome.tabs.sendMessage(tab.id, message).catch(() => {
                    // Content script not available on this tab
                });
            }
        }
    } catch {
        // Tabs query failed
    }
}

// --- Message Listener (from popup and content scripts) ---

chrome.runtime.onMessage.addListener(
    (
        message: Record<string, unknown>,
        sender: chrome.runtime.MessageSender,
        sendResponse: (response: unknown) => void,
    ) => {
        // F19b: every popup/newtab message can carry a correlation_id minted
        // by `sendWithCid`. Log it on receive so the chain
        // `popup → bg → daemon` is greppable end-to-end.
        const __cid = typeof message.correlation_id === "string" ? message.correlation_id : null;
        if (__cid) {
            console.debug(`cortex.bg.recv cid=${__cid} type=${String(message.type)}`);
        }
        switch (message.type) {
            case "SITE_ACCESS_REVOKED":
                // Drop the only context-time tab snapshot immediately. Page
                // excerpts are never persisted; subsequent daemon requests
                // will fail the optional-host check and return an empty
                // excerpt. Incognito tabs are excluded independently.
                lastContextTabs = null;
                lastContextTabsTimestamp = 0;
                scrubStoredActivityContent()
                    .then(() => sendResponse({ ok: true }))
                    .catch(() => sendResponse({ ok: false }));
                return true;

            case "GET_STATE":
                // If presentation state was lost (SW restart), rehydrate it.
                if (!interventionPresentation.active) {
                    chrome.storage.session.get(
                        [
                            "cortex_active_intervention",
                            "cortex_active_intervention_cid",
                            "cortex_active_intervention_mounted_at",
                        ],
                        (data) => {
                            const stored = data?.cortex_active_intervention || null;
                            if (stored) {
                                interventionPresentation.mount(
                                    stored as Record<string, unknown>,
                                    (data?.cortex_active_intervention_cid as string)
                                        || `restore_${Date.now().toString(36)}`,
                                    (data?.cortex_active_intervention_mounted_at as number)
                                        || Date.now(),
                                );
                            }
                            sendResponse({
                                connected,
                                state: currentState,
                                intervention: interventionPresentation.active?.plan ?? null,
                                focusSession: focusSession ? getFocusSessionSnapshot() : null,
                            });
                        },
                    );
                    return true; // async
                }
                sendResponse({
                    connected,
                    state: currentState,
                    intervention: interventionPresentation.active.plan,
                    focusSession: focusSession ? getFocusSessionSnapshot() : null,
                });
                break;

            case "START_FOCUS":
                startFocusSession((message.goal as string) || "Study session");
                sendResponse({ ok: true });
                break;

            case "STOP_FOCUS": {
                const result = stopFocusSession();
                sendResponse({ ok: true, session: result });
                break;
            }

            case "GET_DAILY_STATS":
                chrome.storage.local.get("cortex_daily_stats", (data) => {
                    sendResponse(data.cortex_daily_stats || null);
                });
                return true; // async

            case "REQUEST_CONNECTIVITY_DIAGNOSTIC":
                // G2 (audit-prod): popup opened; refresh the diagnostic.
                void probeConnectivity("popup_open").catch((err: unknown) => {
                    if (DEBUG) console.debug("[cortex.bg] probeConnectivity(popup_open) failed: %o", err);
                });
                sendResponse({ ok: true });
                break;

            case "CONNECT":
                connect();
                sendResponse({ ok: true });
                break;

            case "DISCONNECT":
                disconnect();
                sendResponse({ ok: true });
                break;

            case "STOP_CORTEX":
                // End any active focus session
                stopFocusSession();
                // Clear state
                currentState = null;
                interventionPresentation.clear();
                (async () => {
                    // Clear persisted intervention/snapshot state so popup does not
                    // resurrect stale UI after service-worker restart.
                    try {
                        await chrome.storage.session.remove([
                            "cortex_active_intervention",
                            "cortex_tab_snapshot",
                            "cortex_tab_mgr_snapshots",
                        ]);
                    } catch { /* storage.session may be unavailable */ }

                    // F07b/F08b: fetch the local capability token (cached in
                    // chrome.storage.session after the first call) so the
                    // gated SHUTDOWN/`/stop` endpoints accept this client.
                    // A token-fetch failure is non-fatal: the steps below
                    // still get tried, the auth-gated ones will 401 cleanly.
                    let authToken: string | null = null;
                    try {
                        authToken = await getAuthToken();
                    } catch (e) {
                        // Audit-2 fix: gate this with DEBUG so production
                        // builds don't logspam Chrome's console on every
                        // stop attempt that lacks a token (the path-2
                        // native-messaging fallback is correct and silent).
                        if (DEBUG) {
                            console.warn(`cortex.auth.token_unavailable err=${String(e)}`);
                        }
                    }

                    // Step 1: Send SHUTDOWN over WebSocket (graceful — triggers daemon stop chain)
                    if (ws && connected) {
                        try {
                            send({
                                type: "SHUTDOWN",
                                payload: authToken ? { auth_token: authToken } : {},
                                timestamp: Date.now() / 1000,
                                sequence: ++sequence,
                                // F19b: carry the popup's cid through so the
                                // daemon can correlate the SHUTDOWN with the
                                // upstream user click.
                                correlation_id: __cid ?? undefined,
                            });
                            await new Promise((r) => setTimeout(r, 500));
                        } catch { /* ws may already be closing */ }
                    }
                    // Step 2: Disconnect our WebSocket
                    disconnect();
                    // Step 3: HTTP shutdown via daemon API (backup)
                    try {
                        await fetch(`${DAEMON_HTTP_URL}/shutdown`, {
                            method: "POST",
                            signal: AbortSignal.timeout(3000),
                            headers: authToken ? { "X-Cortex-Auth-Token": authToken } : undefined,
                        });
                    } catch { /* daemon may already be dead */ }
                    // Step 4: Wait briefly for graceful shutdown to complete
                    await new Promise((r) => setTimeout(r, 1000));
                    // Step 5: Nuclear kill via native messaging — sends SIGTERM to the
                    // daemon process by PID. This is the most reliable kill mechanism
                    // because it works even when HTTP/WebSocket are unresponsive.
                    try {
                        await sendNativeHostMessage(
                            { command: "stop" },
                            { timeoutMs: 15_000 },
                        );
                    } catch { /* native messaging may not be available */ }
                    // Step 6: HTTP stop via launcher agent (port 9471) as final backup
                    try {
                        await fetch(`${LAUNCHER_HTTP_URL}/stop`, {
                            method: "POST",
                            signal: AbortSignal.timeout(3000),
                            headers: authToken ? { "X-Cortex-Auth-Token": authToken } : undefined,
                        });
                    } catch { /* launcher may not be running */ }
                    // Close any onboarding tabs
                    try {
                        const onboardingUrl = chrome.runtime.getURL("tabs/onboarding.html");
                        chrome.tabs.query({}, (tabs) => {
                            for (const tab of tabs) {
                                if (tab.id && tab.url && tab.url.startsWith(onboardingUrl)) {
                                    chrome.tabs.remove(tab.id);
                                }
                            }
                        });
                    } catch { /* ignore */ }
                    sendResponse({ ok: true });
                })();
                return true; // async response

            case "TOGGLE_QUIET_MODE":
                quietMode = Boolean(message.quiet);
                schedulePersist();
                // Notify daemon if connected
                if (connected && ws) {
                    send({
                        type: "SETTINGS_SYNC",
                        payload: { quiet_mode: quietMode },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                }
                sendResponse({ ok: true, quietMode });
                break;

            case "QUIET_MODE_TOGGLE":
                // P0 §3.11: the popup surfaces the three-kind quiet
                // menu (Snooze 15 / Quiet rest of session / Pause).
                // Relay the kind + optional duration to the daemon so
                // QUIET_MODE_STATE broadcasts back the unified state.
                if (connected && ws) {
                    send({
                        type: "QUIET_MODE_TOGGLE",
                        payload: {
                            kind: (message.kind as string | undefined) || "off",
                            duration_minutes:
                                typeof message.duration_minutes === "number"
                                    ? message.duration_minutes
                                    : null,
                            source: "popup",
                        },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                }
                sendResponse({ ok: true });
                break;

            case "SNOOZE_REQUEST":
                // P0 §3.11: relay a popup snooze click as the canonical
                // SNOOZE_REQUEST wire type so the daemon's source
                // attribution stays accurate.
                if (connected && ws) {
                    send({
                        type: "SNOOZE_REQUEST",
                        payload: {
                            duration_minutes:
                                typeof message.duration_minutes === "number"
                                    ? message.duration_minutes
                                    : 15,
                            source: "popup",
                        },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                }
                sendResponse({ ok: true });
                break;

            case "LAUNCH_CORTEX":
                // Try three launch paths in order:
                // 1. HTTP launcher agent (port 9471) — works if user started launcher manually
                // 2. Native messaging — Chrome invokes native_host.py directly
                // 3. Direct WebSocket — daemon may already be running
                runLaunchCortex().then((result) => {
                    try {
                        sendResponse(result);
                    } catch { /* response port may be closed */ }
                });
                return true; // async

            case "USER_ACTION": {
                // F16: stamp every outbound USER_ACTION with the cid of the
                // currently mounted plan so the daemon can ignore a stale ACK
                // when an intervention has been superseded.
                const outboundCid =
                    typeof message.correlation_id === "string" && message.correlation_id.length > 0
                        ? (message.correlation_id as string)
                        : interventionPresentation.active?.correlation_id;
                send({
                    type: "USER_ACTION",
                    payload: {
                        action: message.action,
                        intervention_id: message.intervention_id,
                        timestamp: Date.now() / 1000,
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                    correlation_id: outboundCid,
                });
                if (message.action === "dismissed") {
                    const activePlanId =
                        typeof interventionPresentation.active?.plan.intervention_id === "string"
                            ? (interventionPresentation.active.plan.intervention_id as string)
                            : null;
                    const interventionId =
                        typeof message.intervention_id === "string"
                            ? (message.intervention_id as string)
                            : activePlanId;

                    // Record dismissal for cooldown
                    const now = Date.now();
                    if (interventionId) {
                        interventionPresentation.dismiss(interventionId, null, now);
                        schedulePersist();
                    }
                    // Also record URL-based cooldown from the active tab
                    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
                        if (tab?.url) {
                            interventionPresentation.dismiss(
                                interventionId || "",
                                tab.url,
                                now,
                            );
                            schedulePersist();
                        }
                    }).catch((err: unknown) => {
                        if (DEBUG) console.debug("[cortex.bg] tabs.query(active) for URL dismiss cooldown failed: %o", err);
                    });
                    interventionPresentation.clear();
                    // Keep exact inverse snapshots until the daemon sends a
                    // receipt-derived INTERVENTION_RESTORE. Dismissal itself
                    // is never workspace authority.
                    try {
                        chrome.storage.session.remove([
                            "cortex_active_intervention",
                            "cortex_active_intervention_cid",
                            "cortex_active_intervention_mounted_at",
                        ]);
                    } catch {}
                }
                sendResponse({ ok: true });
                break;
            }

            case "RESTORE_TABS":
                restoreAllTabs()
                    .then(() => sendResponse({ ok: true }))
                    .catch((err: unknown) => {
                        if (DEBUG) console.debug("[cortex.bg] restoreAllTabs failed on RESTORE_TABS message: %o", err);
                        sendResponse({ ok: false, error: String(err) });
                    });
                return true; // Async response

            case "CONTENT_EXTRACTED":
                // Content script extracted text for us
                sendResponse({ ok: true });
                break;

            case "DISTRACTION_BLOCKED":
                // User clicked "Go back" on the distraction interceptor
                if (focusSession) {
                    focusSession.distractionsBlocked++;
                    schedulePersist();
                }
                sendResponse({ ok: true });
                break;

            case "USER_RATING":
                // P0 §3.8: relay 👍/👎 ratings. ``context`` is an
                // optional one-line free-text comment from a 👎 — stays
                // local on the daemon side; never reaches the LLM.
                send({
                    type: "USER_RATING",
                    payload: {
                        intervention_id: message.intervention_id,
                        rating: message.rating,
                        ...(typeof message.context === "string" && message.context.length > 0
                            ? { context: message.context.slice(0, 200) }
                            : {}),
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                });
                sendResponse({ ok: true });
                break;

            case "MICRO_STEP_TOGGLED":
                // P0 §3.6: relay the popup/webview's micro-step toggle to
                // the daemon. Mirrors the USER_RATING relay above — the
                // wire envelope's payload carries the three fields the
                // daemon's _handle_micro_step_toggled validates
                // (intervention_id, step_index, new_status).
                send({
                    type: "MICRO_STEP_TOGGLED",
                    payload: {
                        intervention_id: message.intervention_id,
                        step_index: message.step_index,
                        new_status: message.new_status,
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                });
                sendResponse({ ok: true });
                break;

            case "WHY_DETAIL_REQUEST":
                // P0 §3.9: relay the popup/webview's "Why?" expansion
                // to the daemon. The daemon resolves the structured
                // causal signals (cached per intervention, or computed
                // live from the most recent FeatureVector + baselines)
                // and replies with a WHY_DETAIL frame.
                send({
                    type: "WHY_DETAIL_REQUEST",
                    payload: {
                        intervention_id: message.intervention_id,
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                });
                sendResponse({ ok: true });
                break;

            case "EXECUTE_ACTION":
                if (!workspaceMutationAllowed()) {
                    sendResponse({
                        action_id: String(
                            (message.action as Partial<SuggestedAction> | undefined)
                                ?.action_id ?? "",
                        ),
                        success: false,
                        message: "Action unavailable in suggest-only mode",
                        reversible: false,
                    });
                    break;
                }
                authorizeActionIds(
                    String(message.intervention_id || ""),
                    [String(
                        (message.action as Partial<SuggestedAction> | undefined)
                            ?.action_id || "",
                    )],
                    [message.action],
                )
                    .then(([result]) => sendResponse(result))
                    .catch((error: unknown) => sendResponse({
                        action_id: String(
                            (message.action as Partial<SuggestedAction> | undefined)
                                ?.action_id || "",
                        ),
                        success: false,
                        message: String(error),
                        reversible: false,
                    }));
                return true; // async

            case "EXECUTE_ALL_RECOMMENDED":
                if (!workspaceMutationAllowed()) {
                    sendResponse({
                        success: false,
                        message: "Actions unavailable in suggest-only mode",
                        results: [],
                    });
                    break;
                }
                executeAllRecommended(
                    String(message.intervention_id || ""),
                )
                    .then((results) => {
                        sendResponse(results);
                        // Send per-tab relevance feedback to daemon
                        const keptTabs = message.kept_tabs as Array<{url: string; title: string}> | undefined;
                        const closedTabs = message.closed_tabs as Array<{url: string; title: string}> | undefined;
                        if ((keptTabs && keptTabs.length > 0) || (closedTabs && closedTabs.length > 0)) {
                            send({
                                type: "TAB_RELEVANCE_FEEDBACK",
                                payload: {
                                    intervention_id: message.intervention_id,
                                    kept_tabs: keptTabs || [],
                                    closed_tabs: closedTabs || [],
                                },
                                timestamp: Date.now() / 1000,
                                sequence: ++sequence,
                            });
                        }
                    })
                    .catch((error: unknown) => sendResponse({
                        success: false,
                        message: String(error),
                        results: [],
                    }));
                return true; // async

            case "UNDO_ACTION":
                undoAction(message.action_id as string)
                    .then((success) => sendResponse({ ok: success }));
                return true; // async

            case "UNDO_ALL_RECENT":
                undoAllRecent()
                    .then(() => sendResponse({ ok: true }));
                return true; // async

            case "SAVE_TAB_SESSION":
                saveTabSession(
                    (message.name as string) || `Session ${Date.now()}`,
                    focusSession?.goal,
                ).then(() => sendResponse({ ok: true }));
                return true; // async

            case "RESTORE_TAB_SESSION":
                restoreTabSession(message.name as string)
                    .then((ok) => sendResponse({ ok }));
                return true; // async

            case "GET_SAVED_SESSIONS":
                chrome.storage.local.get("cortex_sessions", (data) => {
                    sendResponse(data.cortex_sessions || []);
                });
                return true; // async

            case "LEETCODE_CONTEXT_UPDATE": {
                const payload = (message.payload || {}) as Record<string, unknown>;
                const tab = sender.tab;
                (async () => {
                    const allowPageContent = Boolean(
                        tab && !tab.incognito && await mayExtractPageContent(tab),
                    );
                    const safePayload = { ...payload };
                    if (!allowPageContent) safePayload.code_snapshot = "";
                    else safePayload.code_snapshot = sanitizeContextText(
                        String(payload.code_snapshot ?? ""),
                        PAGE_EXCERPT_MAX_CHARS,
                    ).value;
                    send({
                        type: "LEETCODE_CONTEXT_UPDATE",
                        payload: safePayload,
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                    sendResponse({ ok: true });
                })().catch(() => sendResponse({ ok: false }));
                return true;
            }

            case "ACTIVITY_UPDATE": {
                prepareActivityRecordForStorage(message.record, sender.tab)
                    .then(async (record) => {
                        if (!record) {
                            sendResponse({ ok: false, ignored: true });
                            return;
                        }
                        await enrichWithRelatedTabs(record);
                        await upsertActivity(record);
                        sendResponse({ ok: true });
                    })
                    .catch(() => sendResponse({ ok: false }));
                return true;
            }

            case "GET_RECENT_ACTIVITIES":
                loadActivities().then((activities) => {
                    const recent = Object.values(activities)
                        .filter(a => a.max_completion_pct < 95 && !a.dismissed)
                        .sort((a, b) => b.last_visited - a.last_visited)
                        .slice(0, (message.limit as number) || 5);
                    sendResponse(recent);
                });
                return true; // async

            case "DISMISS_RESUME": {
                const contentId = message.content_id as string;
                if (contentId) {
                    loadActivities().then(async (activities) => {
                        if (activities[contentId]) {
                            activities[contentId].dismissed = true;
                            await saveActivities(activities);
                        }
                        sendResponse({ ok: true });
                    });
                    return true; // async
                }
                sendResponse({ ok: true });
                break;
            }

            case "GET_CACHED_RECAP": {
                // P0 §3.3: popup-on-mount handshake. Return whatever the
                // background script has cached so the popup can render
                // the recap card without waiting for a fresh WS frame.
                chrome.storage.local.get(
                    ["cortex.lastRecap", "cortex.lastRecapTimestamp"],
                    (data) => {
                        sendResponse({
                            recap: data?.["cortex.lastRecap"] ?? null,
                            timestamp:
                                (data?.["cortex.lastRecapTimestamp"] as
                                    | number
                                    | undefined) ?? null,
                        });
                    },
                );
                return true; // async
            }

            case "DISMISS_RECAP": {
                // P0 §3.3 (Phase 4 hardening): user clicked "Dismiss"
                // in the recap card. Clear the cache and the toolbar
                // badge so the next popup open does not resurface this
                // recap, AND remember the dismissed session_id so a
                // subsequent SESSION_RECAP for the same session (e.g.
                // a re-broadcast triggered by the on-connect
                // REQUEST_SESSION_RECAP handshake) is suppressed.
                chrome.storage.local.get(
                    [
                        "cortex.lastRecap",
                        "cortex.lastRecapTimestamp",
                    ],
                    (data) => {
                        // C4: the cached recap is the SessionRecap wrapper;
                        // the session_id lives under ``.report.session_id``.
                        const lastRecap = data?.["cortex.lastRecap"] as
                            | { report?: { session_id?: unknown } }
                            | undefined;
                        const dismissedSessionId =
                            typeof lastRecap?.report?.session_id === "string"
                                ? (lastRecap.report.session_id as string)
                                : null;
                        const updates: Record<string, unknown> = {};
                        if (dismissedSessionId) {
                            updates["cortex.dismissedRecapSessionId"] =
                                dismissedSessionId;
                        }
                        try {
                            if (Object.keys(updates).length > 0) {
                                chrome.storage.local.set(updates);
                            }
                            chrome.storage.local.remove([
                                "cortex.lastRecap",
                                "cortex.lastRecapTimestamp",
                            ]);
                        } catch (err) {
                            // storage.local unavailable — badge clearing
                            // still matters; log so a real regression
                            // surfaces rather than being swallowed.
                            if (DEBUG) {
                                console.warn(
                                    "[cortex.bg] DISMISS_RECAP storage update failed",
                                    err,
                                );
                            }
                        }
                        setRecapBadge(false);
                        sendResponse({ ok: true });
                    },
                );
                return true; // async — sendResponse fires inside the get cb
            }

            case "RECAP_VIEWED": {
                // P0 §3.3: popup mounted and is rendering the cached
                // recap to the user. The badge ("your recap is ready")
                // has served its purpose; clear it. The cache stays
                // intact for up to 24h so the popup can re-render it
                // on subsequent opens.
                setRecapBadge(false);
                sendResponse({ ok: true });
                break;
            }

            case "GET_COST": {
                // F21 (Phase-4 audit / §3.15 §3 follow-up): proxy a
                // cost-telemetry fetch from popup → daemon HTTP API.
                // Popup polls every 30s; we fan out to ``/api/cost``
                // so the popup doesn't need cross-origin credentials.
                (async () => {
                    try {
                        // C1: /api/cost is capability-token-gated. Attach the
                        // cached local token (same retrieval used by the STOP
                        // path at ~4233) or the daemon 401s. A token-fetch
                        // failure is non-fatal — the request still goes out and
                        // 401s cleanly rather than hanging.
                        let authToken: string | null = null;
                        try {
                            authToken = await getAuthToken();
                        } catch (e) {
                            if (DEBUG) {
                                console.warn(
                                    `cortex.auth.token_unavailable err=${String(e)}`,
                                );
                            }
                        }
                        const ctrl = new AbortController();
                        const t = setTimeout(() => ctrl.abort(), 1500);
                        const resp = await fetch(
                            `${DAEMON_HTTP_URL}/api/cost`,
                            {
                                signal: ctrl.signal,
                                headers: authToken
                                    ? { "X-Cortex-Auth-Token": authToken }
                                    : undefined,
                            },
                        );
                        clearTimeout(t);
                        if (!resp.ok) {
                            sendResponse({ ok: false, error: `status_${resp.status}` });
                            return;
                        }
                        const body = (await resp.json()) as CostResponseSchema;
                        sendResponse({ ok: true, cost: body });
                    } catch (err) {
                        sendResponse({
                            ok: false,
                            error: (err as Error)?.message ?? "fetch_failed",
                        });
                    }
                })();
                return true; // async
            }

            case "GET_CACHED_TRENDS": {
                // P0 §3.2: popup-on-mount handshake for the "Last 7
                // days" sparkbar strip. Return whatever rollup the
                // background script has in chrome.storage.local so the
                // popup can render bars without waiting for a fresh WS
                // frame.
                chrome.storage.local.get(
                    ["cortex.lastTrends", "cortex.lastTrendsTimestamp"],
                    (data) => {
                        sendResponse({
                            trends: data?.["cortex.lastTrends"] ?? null,
                            timestamp:
                                (data?.["cortex.lastTrendsTimestamp"] as
                                    | number
                                    | undefined) ?? null,
                        });
                    },
                );
                return true; // async
            }

            case "REQUEST_TRENDS": {
                // P0 §3.2: popup asked us to nudge the daemon for a
                // fresh trends rollup (typically when the cached one
                // is older than the popup's 6h staleness window). We
                // fire the WS request, then synchronously echo back
                // whatever cached payload we have so the popup can
                // render stale bars while the fresh ones are in flight.
                //
                // Phase 4 hardening: forward the caller's ``window``
                // (``"week"`` | ``"month"``) and ``refresh`` flag so a
                // future popup-side "Last 30 days" toggle or pull-to-
                // refresh gesture isn't silently downgraded to the
                // weekly cached path. Defaults match the on-connect
                // priming call so callers that omit them get the same
                // behaviour as before.
                const reqWindowRaw = (message as Record<string, unknown>)
                    .window;
                const reqWindow =
                    reqWindowRaw === "month" ? "month" : "week";
                const reqRefresh =
                    (message as Record<string, unknown>).refresh === true;
                if (connected) {
                    send({
                        type: "REQUEST_TRENDS",
                        payload: { window: reqWindow, refresh: reqRefresh },
                        timestamp: Date.now() / 1000,
                        sequence: ++sequence,
                    });
                }
                chrome.storage.local.get(
                    ["cortex.lastTrends", "cortex.lastTrendsTimestamp"],
                    (data) => {
                        sendResponse({
                            trends: data?.["cortex.lastTrends"] ?? null,
                            timestamp:
                                (data?.["cortex.lastTrendsTimestamp"] as
                                    | number
                                    | undefined) ?? null,
                        });
                    },
                );
                return true; // async
            }

            case "OPEN_DASHBOARD_HISTORY": {
                // P0 §3.1 / §3.3: popup asked to raise the desktop
                // dashboard's History tab. The extension cannot launch
                // native apps directly, so we route the request through
                // the existing native-messaging launcher. If the native
                // host is not installed (Chrome-only install, no desktop
                // app), respond ``unavailable`` so the popup can show
                // "Install desktop app to view history".
                void (async () => {
                    try {
                        const response = await sendNativeHostMessage(
                            { command: "raise_dashboard", target: "history" },
                            { timeoutMs: 8_000 },
                        );
                        sendResponse({
                            status: response.command === "raise_dashboard"
                                ? response.status
                                : "unavailable",
                        });
                    } catch (error) {
                        sendResponse({
                            status: "unavailable",
                            error: error instanceof Error
                                ? error.message
                                : String(error),
                        });
                    }
                })();
                return true; // async
            }
        }
        return false;
    },
);

// --- LeetCode → Activity Bridge ---
// Bridges contents/leetcode-observer.ts session data into the unified
// ActivityRecord format. The observer ships as a content-script-only
// module under contents/ (audit Phase-I bundle hygiene) so its code
// never gets pulled into the background service worker bundle.

/**
 * Storage record written by `contents/leetcode-observer.ts::saveSessionState`
 * under the `cortex_leetcode_session` key. This is the persisted-state shape,
 * a subset of the wire-level `LeetCodeContext` plus a `saved_at` timestamp;
 * `chrome.storage.onChanged` types `newValue` as `any` → `{}` under `--strict`,
 * so we narrow it explicitly here to catch observer/bridge field drift.
 */
interface LeetCodeSession {
    problem_id: string;
    title?: string;
    difficulty?: string;
    tags?: string[];
    code_snapshot?: string;
    stage?: LeetCodeStageSchema;
    time_elapsed_s?: number;
    wrong_answer_count?: number;
    last_submission_result?: SubmissionResultSchema | null;
    accepted?: boolean;
    saved_at?: number;
}

chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.cortex_leetcode_session) return;
    const session = changes.cortex_leetcode_session.newValue as
        | LeetCodeSession
        | undefined;
    if (!session?.problem_id) return;

    const record: ActivityRecord = {
        content_id: canonicalizeUrl(`https://leetcode.com/problems/${session.problem_id}`),
        platform: "leetcode",
        content_type: "code_problem",
        title: session.title || session.problem_id,
        url: `https://leetcode.com/problems/${session.problem_id}`,
        favicon_url: "",
        position: {
            type: "code_problem",
            stage: session.stage || "IMPLEMENT",
            wrong_answer_count: session.wrong_answer_count || 0,
            accepted: session.accepted || false,
            time_elapsed_s: session.time_elapsed_s || 0,
            // Session persistence deliberately excludes raw source text.
            code_snapshot: undefined,
        },
        content_duration_s: 0,
        duration_spent_s: session.time_elapsed_s || 0,
        session_duration_s: session.time_elapsed_s || 0,
        first_visited: (session.saved_at || Date.now()) - (session.time_elapsed_s || 0) * 1000,
        last_visited: session.saved_at || Date.now(),
        context_snapshot: "",
        topic_tags: session.tags || [],
        completion_pct: session.accepted ? 100 : Math.min((session.time_elapsed_s || 0) / 1800 * 50, 50),
        max_completion_pct: session.accepted ? 100 : 0,
        cognitive_state: "",
        visit_count: 1,
        dismissed: false,
        is_playlist: false,
        playlist_id: "",
        playlist_index: -1,
        related_tabs: [],
    };
    const safeRecord = sanitizeActivityRecord(record, false);
    if (safeRecord) upsertActivity(safeRecord);
});

// --- Distraction Blocking (tab navigation listener) ---

chrome.tabs.onUpdated.addListener((tabId, changeInfo, _tab) => {
    // Distraction blocking during focus sessions
    if (focusSession && changeInfo.url) {
        const url = changeInfo.url;
        if (isDistractionUrl(url, _tab.title)) {
            const snap = getFocusSessionSnapshot();
            const domain = new URL(url).hostname.replace("www.", "");
            chrome.tabs.sendMessage(tabId, {
                type: "SHOW_DISTRACTION_BLOCKER",
                payload: {
                    focusMin: Math.round((snap?.focusMs ?? 0) / 60000),
                    streakMin: snap?.longestStreakMin ?? 0,
                    distractionsBlocked: snap?.distractionsBlocked ?? 0,
                    domain,
                    goal: focusSession?.goal ?? "",
                },
            }).catch(() => {
                // Content script not ready — fall back to executeScript
                chrome.scripting.executeScript({
                    target: { tabId },
                    func: injectDistractionInterceptor,
                    args: [
                        Math.round((snap?.focusMs ?? 0) / 60000),
                        snap?.longestStreakMin ?? 0,
                        snap?.distractionsBlocked ?? 0,
                        url,
                    ],
                }).catch((err: unknown) => {
                    if (DEBUG) console.debug("[cortex.bg] scripting.executeScript distraction interceptor failed: %o", err);
                });
            });
        }
    }

    // --- Resume trigger: show resume card when returning to tracked content ---
    if (changeInfo.status === "complete" && _tab.url) {
        const tabUrl = _tab.url;
        // Skip chrome:// and extension pages
        if (tabUrl.startsWith("chrome://") || tabUrl.startsWith("chrome-extension://") || tabUrl.startsWith("edge://")) return;

        const canonical = canonicalizeUrl(tabUrl);
        loadActivities().then((activities) => {
            const activity = activities[canonical];
            if (
                activity
                && Date.now() - activity.last_visited > 3600_000   // >1 hour since last visit
                && activity.max_completion_pct < 95                 // Not completed
                && !activity.dismissed                              // Not dismissed
                && activity.duration_spent_s >= 120                 // Was meaningful (>2 min)
            ) {
                chrome.tabs.sendMessage(tabId, {
                    type: "SHOW_RESUME_CARD",
                    activity,
                }).catch(() => {
                    // Content script not ready yet
                });
            }
        });
    }
});

// --- SPA Navigation Resume Trigger (backup for tabs.onUpdated) ---

try {
    chrome.webNavigation.onHistoryStateUpdated.addListener(async (details) => {
        if (details.frameId !== 0) return; // Only main frame
        const url = details.url;
        if (!url || url.startsWith("chrome://") || url.startsWith("edge://")) return;

        const canonical = canonicalizeUrl(url);
        const activities = await loadActivities();
        const activity = activities[canonical];

        if (
            activity
            && Date.now() - activity.last_visited > 3600_000
            && activity.max_completion_pct < 95
            && !activity.dismissed
            && activity.duration_spent_s >= 120
        ) {
            chrome.tabs.sendMessage(details.tabId, {
                type: "SHOW_RESUME_CARD",
                activity,
            }).catch((err: unknown) => {
                if (DEBUG) console.debug("[cortex.bg] SHOW_RESUME_CARD sendMessage failed (SPA nav, content script may not be ready): %o", err);
            });
        }
    });
} catch {
    // webNavigation permission may not be available
}

// --- Keepalive alarm (prevents MV3 service worker from going idle) ---

// P0 §3.2: weekly-trends refresh runs through chrome.alarms (NOT
// setInterval) because MV3 service workers are evicted after ~30s of
// idle and any in-memory ``setInterval`` handle dies with them. Alarms
// survive eviction — the next fire wakes the SW back up so the popup's
// "Last 7 days" sparkbar strip stays warm without the user touching
// anything. Registered here for the cold-start path and re-registered
// on both ``onInstalled`` and ``onStartup`` below so reinstalls /
// browser restarts don't silently drop the schedule.
const TRENDS_REFRESH_ALARM_NAME = "cortex-trends-refresh";
const TRENDS_REFRESH_PERIOD_MINUTES = 30;

chrome.alarms.create("cortex-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.create("cortex-activity-cleanup", { periodInMinutes: 1440 }); // Daily
chrome.alarms.create(TRENDS_REFRESH_ALARM_NAME, {
    periodInMinutes: TRENDS_REFRESH_PERIOD_MINUTES,
});

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "cortex-keepalive") {
        if (!connected) {
            connect();
        }
    } else if (alarm.name === "cortex-break-timer") {
        injectToast("Break's over!", "Time to get back to work. You've got this.");
        broadcastToPopup({ type: "BREAK_TIMER_DONE" });
    } else if (alarm.name === "cortex-activity-cleanup") {
        // Evict activities older than 90 days
        loadActivities().then(async (activities) => {
            const now = Date.now();
            const TTL_MS = 90 * 24 * 60 * 60 * 1000;
            let changed = false;
            for (const [id, a] of Object.entries(activities)) {
                if (now - a.last_visited > TTL_MS) {
                    delete activities[id];
                    changed = true;
                }
            }
            if (changed) await saveActivities(activities);
        });
    } else if (alarm.name && alarm.name.startsWith("cortex_auto_focus_")) {
        // P0 §3.10: auto-focus session timed out. Tear down cleanly so
        // the user isn't permanently kept in focus mode after a HYPER
        // episode that didn't recover within the configured window.
        if (autoFocusArmed) {
            stopAutoFocusSession("duration_elapsed");
        }
    } else if (alarm.name === TRENDS_REFRESH_ALARM_NAME) {
        // P0 §3.2: keep the popup's "Last 7 days" sparkbar strip warm
        // without requiring the user to explicitly refresh. The alarm
        // fires every 30 minutes; when we're connected we ask the
        // daemon for the latest weekly rollup. ``refresh: false`` keeps
        // the daemon on its cached aggregator output rather than
        // re-running the (expensive) rollup pipeline on every alarm.
        if (!connected) return;
        send({
            type: "REQUEST_TRENDS",
            payload: { window: "week", refresh: false },
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
        });
    }
});

// --- Phase 4d Task G: §3.21 global browser shortcuts ---
//
// Three keyboard commands declared in package.json/manifest.commands
// route here. Each maps to the canonical WS frame the popup would
// otherwise emit so the daemon's audit log treats keyboard-driven
// usage identically to UI-driven usage.
//
//   * pause-cortex    → QUIET_MODE_TOGGLE (toggles ``pause`` on/off)
//   * dismiss-overlay → USER_ACTION {action: "dismiss_overlay"}
//   * view-history    → relay to native_host raise_dashboard
//
// The native-host raise_dashboard contract is owned by Phase 4d-retry-2
// (see ``cortex/scripts/native_host.py``). This site stubs the
// ``chrome.runtime.sendNativeMessage`` call against the assumed
// contract: ``{command: "raise_dashboard", target: "history"}`` →
// ``{status: "ok" | "unavailable", error?: string}``.
function handleCommandPauseCortex(): void {
    if (!connected || !ws) return;
    const next = quietMode ? "off" : "pause";
    try {
        send({
            type: "QUIET_MODE_TOGGLE",
            payload: {
                kind: next,
                duration_minutes: null,
                source: "shortcut",
            },
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
        });
    } catch {
        // WS may be mid-reconnect — silently drop. The daemon will
        // resync on the next QUIET_MODE_STATE broadcast.
    }
}

function handleCommandDismissOverlay(): void {
    if (!connected || !ws) return;
    const interventionId =
        interventionPresentation.active
        && typeof interventionPresentation.active.plan.intervention_id === "string"
            ? interventionPresentation.active.plan.intervention_id
            : null;
    try {
        send({
            type: "USER_ACTION",
            payload: {
                action: "dismiss_overlay",
                intervention_id: interventionId,
                source: "shortcut",
                timestamp: Date.now() / 1000,
            },
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
            correlation_id: interventionPresentation.active?.correlation_id,
        });
    } catch {
        // No active intervention or WS down — both are no-ops.
    }
    // Also clear the locally-mounted overlay state so the popup
    // collapses immediately even before the daemon round-trips.
    if (interventionPresentation.active) {
        interventionPresentation.clear();
        broadcastToPopup({ type: "OVERLAY_DISMISSED" });
    }
}

function handleCommandViewHistory(): void {
    void sendNativeHostMessage(
        { command: "raise_dashboard", target: "history" },
        { timeoutMs: 8_000 },
    )
        .catch((error: unknown) => {
            if (!DEBUG) return;
            console.warn("[cortex.bg] view-history shortcut threw", error);
        });
}

// chrome.commands is only present in MV3 contexts that actually
// declared a commands block in the manifest — older browsers and the
// test harness leave it undefined, so we guard before subscribing.
try {
    const cmd = (chrome as unknown as {
        commands?: {
            onCommand?: { addListener: (cb: (command: string) => void) => void };
        };
    }).commands;
    if (cmd && cmd.onCommand && cmd.onCommand.addListener) {
        cmd.onCommand.addListener((command: string) => {
            switch (command) {
                case "pause-cortex":
                    handleCommandPauseCortex();
                    break;
                case "dismiss-overlay":
                    handleCommandDismissOverlay();
                    break;
                case "view-history":
                    handleCommandViewHistory();
                    break;
                default:
                    if (DEBUG) {
                        console.debug(
                            "[cortex.bg] unknown command",
                            command,
                        );
                    }
            }
        });
    }
} catch {
    // chrome.commands isn't critical to startup — log only in DEBUG.
}

// --- Auto-connect on install/startup ---

chrome.runtime.onInstalled.addListener((details) => {
    chrome.alarms.create("cortex-keepalive", { periodInMinutes: 0.4 });
    // P0 §3.2: (re-)register the trends-refresh alarm so an extension
    // update/reinstall doesn't leave the schedule in an indeterminate
    // state. ``chrome.alarms.create`` with the same name overwrites
    // the existing schedule, which is the documented MV3 behaviour.
    chrome.alarms.create(TRENDS_REFRESH_ALARM_NAME, {
        periodInMinutes: TRENDS_REFRESH_PERIOD_MINUTES,
    });
    connect();
    // G2 (audit-prod): probe connectivity on install so the popup
    // immediately renders the right four-state UI before any WS attempt.
    void probeConnectivity("install").catch((err: unknown) => {
        if (DEBUG) console.debug("[cortex.bg] probeConnectivity(install) failed: %o", err);
    });
    // Open onboarding tab only on first-ever install (not updates/reloads)
    if (details.reason === "install") {
        chrome.storage.local.get("cortex_onboarded", (data) => {
            if (!data.cortex_onboarded) {
                chrome.storage.local.set({ cortex_onboarded: true });
                chrome.tabs.create({ url: chrome.runtime.getURL("tabs/onboarding.html") });
            }
        });
    }
});

chrome.runtime.onStartup.addListener(() => {
    // P0 §3.2: re-register the trends-refresh alarm on browser startup.
    // Alarms persist across SW eviction within a session but not across
    // browser restarts in all profiles, so this guarantees we always
    // have the schedule installed once the SW first wakes up.
    chrome.alarms.create(TRENDS_REFRESH_ALARM_NAME, {
        periodInMinutes: TRENDS_REFRESH_PERIOD_MINUTES,
    });
    connect();
    // G2 (audit-prod): same probe on every browser-startup activation.
    void probeConnectivity("startup").catch((err: unknown) => {
        if (DEBUG) console.debug("[cortex.bg] probeConnectivity(startup) failed: %o", err);
    });
});

// Start immediately (service worker activation)
connect();
// G2 (audit-prod): cold-start probe so a popup opened immediately after
// service-worker activation has a non-null connectivity diagnostic.
void probeConnectivity("activation").catch((err: unknown) => {
    if (DEBUG) console.debug("[cortex.bg] probeConnectivity(activation) failed: %o", err);
});

// P0 §3.12: notification button click handlers. The OS-notification
// path (``surfaceInterventionOSNotification``) emits notifications with
// id ``cortex_intervention_<id>`` and two buttons: 0 = "Open", 1 = "Snooze".
// Cancel the notification after click so it doesn't linger in the
// Notification Center.
try {
    const notifications = (chrome as unknown as {
        notifications?: {
            onButtonClicked?: {
                addListener: (
                    cb: (notificationId: string, buttonIndex: number) => void,
                ) => void;
            };
            onClicked?: {
                addListener: (cb: (notificationId: string) => void) => void;
            };
            clear?: (id: string, cb?: (wasCleared: boolean) => void) => void;
        };
    }).notifications;
    if (notifications && notifications.onButtonClicked) {
        notifications.onButtonClicked.addListener(
            (notificationId: string, buttonIndex: number) => {
                if (!notificationId.startsWith("cortex_intervention_")) return;
                const interventionId = notificationId.replace(
                    "cortex_intervention_",
                    "",
                );
                if (buttonIndex === 0) {
                    // Phase-3 P0-N3: ``chrome.runtime.sendMessage``
                    // from the SW does NOT dispatch to the same SW's
                    // ``onMessage`` listener — call the launch path
                    // directly. We also record the engagement as a
                    // USER_ACTION so the helpfulness tracker can
                    // attribute "user opened from notification" vs
                    // "user ignored it".
                    if (connected && ws && interventionId) {
                        send({
                            type: "USER_ACTION",
                            payload: {
                                action: "os_notification_opened",
                                intervention_id: interventionId,
                                source: "os_notification",
                                timestamp: Date.now() / 1000,
                            },
                            timestamp: Date.now() / 1000,
                            sequence: ++sequence,
                        });
                    }
                    void runLaunchCortex().catch((e) => {
                        console.warn("cortex.bg LAUNCH from notification failed", e);
                    });
                } else if (buttonIndex === 1) {
                    // Snooze → SNOOZE_REQUEST for 15 min. The daemon
                    // unifies into QUIET_MODE_STATE so every surface
                    // mirrors.
                    if (connected && ws) {
                        send({
                            type: "SNOOZE_REQUEST",
                            payload: {
                                intervention_id: interventionId,
                                duration_minutes: 15,
                                source: "os_notification",
                            },
                            timestamp: Date.now() / 1000,
                            sequence: ++sequence,
                        });
                    }
                }
                if (notifications.clear) {
                    try {
                        notifications.clear(notificationId);
                    } catch { /* clear unavailable */ }
                }
                // Drop the action badge once the user dealt with the notification.
                try {
                    const action = (chrome as unknown as {
                        action?: { setBadgeText: (d: { text: string }) => void };
                    }).action;
                    if (action) action.setBadgeText({ text: "" });
                } catch { /* badge unavailable */ }
            },
        );
    }
    if (notifications && notifications.onClicked) {
        notifications.onClicked.addListener((notificationId: string) => {
            if (!notificationId.startsWith("cortex_intervention_")) return;
            const interventionId = notificationId.replace(
                "cortex_intervention_", "",
            );
            if (connected && ws && interventionId) {
                send({
                    type: "USER_ACTION",
                    payload: {
                        action: "os_notification_opened",
                        intervention_id: interventionId,
                        source: "os_notification",
                        timestamp: Date.now() / 1000,
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                });
            }
            void runLaunchCortex().catch((e) => {
                console.warn("cortex.bg LAUNCH from notification failed", e);
            });
            if (notifications.clear) {
                try {
                    notifications.clear(notificationId);
                } catch { /* clear unavailable */ }
            }
            try {
                const action = (chrome as unknown as {
                    action?: { setBadgeText: (d: { text: string }) => void };
                }).action;
                if (action) action.setBadgeText({ text: "" });
            } catch { /* badge unavailable */ }
        });
    }
} catch {
    // notifications API may be unavailable in test harness; safe to ignore.
}

// P0 §3.2: the trends-refresh poll lives on ``chrome.alarms`` (see the
// ``cortex-trends-refresh`` registration above) rather than
// ``setInterval``. MV3 service workers are evicted after ~30s of idle
// and any in-memory timer handle dies with them; chrome.alarms is the
// only persistence-safe scheduler in MV3.
