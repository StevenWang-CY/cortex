/**
 * Cortex Chrome Extension — Background Service Worker
 *
 * Maintains a WebSocket connection to the Cortex daemon (ws://127.0.0.1:9473).
 * Receives STATE_UPDATE and INTERVENTION_TRIGGER messages.
 * Injects the page surfaces under ``bg/surfaces/`` on intervention triggers.
 * Sends IDENTIFY and USER_ACTION messages to the daemon.
 */

import { reduceApplyResults, type ApplyOutcome } from "./lib/apply-state";
import { BadgeState } from "./lib/badge-state";
import {
    isDistractionBlockedRequest,
    isTerminalUserAction,
    type ExecuteAllRecommendedResponse,
} from "./lib/extension-protocol";
import { NativeHostStatusCache } from "./lib/native-host-status";
import {
    LAUNCH_FAILED_STATUS,
    type CortexState,
} from "./lib/popup-view-model";
import { surfaceCss } from "./bg/surfaces/tokens";
import {
    buildInterventionPanelModel,
    injectInterventionPanel,
} from "./bg/surfaces/intervention-panel";
import { buildCoachPanelModel, injectCoachPanel } from "./bg/surfaces/coach-panel";
import { injectDistractionInterceptor } from "./bg/surfaces/interceptor";
import { injectCortexToast } from "./bg/surfaces/toast";
import { removeCortexOverlay } from "./bg/surfaces/remove-overlay";
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
//
// ``CortexState`` has exactly one declaration, in ``lib/popup-view-model.ts``;
// the worker and the popup both import it.

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
// True from the moment the user asks to stop Cortex until they press Start
// again (or a launch succeeds). While set, neither the keepalive alarm nor a
// worker restart reconnects, so a stop cannot be undone by the reconnect loop.
let stopRequested = false;
const STOP_REQUESTED_KEY = "cortex_stop_requested";
// Close bookkeeping for the connectivity diagnostic: a socket that opened and
// was then closed by the daemon with a policy code is a handshake failure; a
// socket that never opened is simply "not running".
let socketWasOpen = false;
let lastCloseCode: number | null = null;
// Native-host reachability is cached (lib/native-host-status.ts) so socket
// churn while the app is closed never spawns the host process repeatedly.
const nativeHostStatusCache = new NativeHostStatusCache();
// One badge, fixed priority (pending intervention > unread recap).
const badgeState = new BadgeState();
// Stylesheet handed to every injected page surface.
const SURFACE_CSS = surfaceCss();
// Set by ``schedulePersist``; cleared by ``flushPersistedState``.
let persistDirty = false;
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
    persistDirty = true;
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
    // A stop the user asked for survives a worker restart; only a Start
    // press (CONNECT / LAUNCH_CORTEX) clears it.
    try {
        const stop = await chrome.storage.session.get(STOP_REQUESTED_KEY);
        if (stop[STOP_REQUESTED_KEY] === true) {
            stopRequested = true;
            if (connected || ws) disconnect();
        }
    } catch {
        // session storage unavailable — default to the in-memory flag.
    }
    // Phase 4d Task A: also rehydrate from chrome.storage.local — the
    // session bucket clears on browser restart so a HYPER-armed session
    // that survived a Chrome relaunch loses its blocklist otherwise.
    // The local restore is best-effort and runs even if session restore
    // populated nothing; the sanity check inside handles the
    // inconsistent ``autoFocusArmed && focusSession === null`` case.
    await restoreAutoFocusStateLocal();
}

// Restore persisted state on service worker startup. ``handleMessage`` awaits
// this promise before dispatching any frame, so a trigger that lands during
// boot cannot re-show an intervention whose dismissal cooldown has not been
// hydrated yet. ``connect()`` itself still runs synchronously at activation.
let stateHydrated = false;
const stateRestored: Promise<void> = restoreState()
    .catch((err: unknown) => {
        console.warn("[cortex.bg] restoreState failed:", err);
    })
    .then(() => {
        stateHydrated = true;
    });

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
            socketWasOpen = true;
            if (stopRequested) {
                // The user asked Cortex to stop; a socket that raced the
                // hydrated intent closes again without authenticating.
                disconnect();
                return;
            }
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

            // Notify popup, then refresh the diagnostic from cache so a
            // stale handshake error cannot outlive a healthy connection.
            broadcastToPopup({ type: "CONNECTION_CHANGED", connected: true });
            void probeConnectivity("connected").catch((err: unknown) => {
                if (DEBUG) console.debug("[cortex.bg] probeConnectivity(connected) failed: %o", err);
            });
        };

        ws.onmessage = (event) => handleMessage(event.data as string);

        ws.onclose = (event) => {
            handleDisconnect(typeof event?.code === "number" ? event.code : null);
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

/** Remember that the user asked Cortex to stop; survives a worker restart. */
async function setStopIntent(): Promise<void> {
    stopRequested = true;
    try {
        await chrome.storage.session.set({ [STOP_REQUESTED_KEY]: true });
    } catch { /* session storage unavailable */ }
    broadcastToPopup({ type: "STOP_INTENT", stopRequested: true });
}

/** The user pressed Start: reconnects may resume. */
async function clearStopIntent(): Promise<void> {
    if (!stopRequested) return;
    stopRequested = false;
    try {
        await chrome.storage.session.remove(STOP_REQUESTED_KEY);
    } catch { /* session storage unavailable */ }
    broadcastToPopup({ type: "STOP_INTENT", stopRequested: false });
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

function handleDisconnect(closeCode: number | null = null): void {
    ws = null;
    const wasOpen = socketWasOpen;
    socketWasOpen = false;
    lastCloseCode = closeCode;
    if (connected) {
        connected = false;
        broadcastToPopup({ type: "CONNECTION_CHANGED", connected: false });
    }
    // Refresh the popup diagnostic from the cached native-host status. A
    // socket that opened and was then closed by the daemon is the only
    // disconnect that can mean "handshake rejected".
    void probeConnectivity(wasOpen ? "closed_after_open" : "disconnected")
        .catch((err: unknown) => {
            if (DEBUG) console.debug("[cortex.bg] probeConnectivity(disconnected) failed: %o", err);
        });
    if (!intentionalDisconnect && !stopRequested) {
        scheduleReconnect();
    }
}

/** Close codes the daemon uses to refuse a client it will not serve. */
const HANDSHAKE_REJECTION_CLOSE_CODES = new Set([1002, 1008, 1011]);

type ConnectivityProbeTrigger =
    | "activation"
    | "install"
    | "startup"
    | "popup_open"
    | "connected"
    | "disconnected"
    | "closed_after_open";

/**
 * Emit a ``CONNECTIVITY_DIAGNOSTIC`` extension-internal message that the
 * popup maps onto its distinct states (not linked / not running / needs an
 * update / couldn't verify this browser).
 *
 * The native host is a separate process: it is only spawned on popup open
 * and install, or when the cached answer is older than five minutes. Every
 * other trigger (socket churn, the keepalive reconnect) answers from cache.
 * The daemon version comes from a 1.5 s ``/health`` fetch; the handshake
 * verdict comes from the last socket close, not from guesswork.
 */
async function probeConnectivity(trigger: ConnectivityProbeTrigger): Promise<void> {
    const forceNativeProbe = trigger === "popup_open" || trigger === "install";
    const nativeHost = await nativeHostStatusCache.probe(forceNativeProbe);

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

    // A rejection is only claimed when the daemon is reachable, the socket
    // had opened, and the daemon closed it with a policy code. Healthy
    // connects always clear it.
    const handshakeError = !connected
        && trigger === "closed_after_open"
        && daemonVersion !== null
        && lastCloseCode !== null
        && HANDSHAKE_REJECTION_CLOSE_CODES.has(lastCloseCode)
        ? "handshake_rejected"
        : null;

    broadcastToPopup({
        type: "CONNECTIVITY_DIAGNOSTIC",
        payload: {
            native_host_status: nativeHost.status,
            native_host_error: nativeHost.error,
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

// MV3 never dispatches ``runtime.onSuspend`` to an extension service worker,
// so nothing here relies on a shutdown hook. Durability comes from the
// debounced writes in ``schedulePersist`` plus an explicit flush on every
// keepalive alarm tick (``flushPersistedState`` below); a worker that is
// evicted mid-debounce loses at most half a second of bookkeeping.
function flushPersistedState(): void {
    if (!persistDirty) return;
    persistDirty = false;
    void browserSessionStore.saveSessionNow(persistedSessionSnapshot())
        .catch(() => { persistDirty = true; });
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

    // Parsing and the replay guard above stay synchronous; interpretation
    // waits for cooldowns and quiet state to hydrate on a cold worker so a
    // trigger cannot re-show a dismissed intervention. Every invocation
    // awaits the same promise, so frame order is preserved; once hydrated
    // the path is synchronous again.
    if (!stateHydrated) await stateRestored;

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
            // One channel per event: the page panel when it can be shown;
            // otherwise, when the desktop shell isn't focused either, a
            // single OS notification (P0 §3.12) — never both, and never
            // while the user has asked for quiet. The badge only lights
            // when no surface is on screen.
            const notFocused = plan.desktop_not_focused;
            void handleIntervention(msg.payload).then((overlayShown) => {
                if (overlayShown || quietMode) return;
                setInterventionBadge(true);
                if (!notFocused) return;
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
            });
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

        case "DISMISS_OVERLAY": {
            // The daemon's cross-surface dismiss cue (keyboard shortcut on
            // any surface). Presentation only: remove the page panels,
            // drop the mounted proposal, and tell the popup.
            const interventionId = typeof msg.payload.intervention_id === "string"
                ? msg.payload.intervention_id
                : null;
            interventionPresentation.clear();
            setInterventionBadge(false);
            try {
                chrome.storage.session.remove([
                    "cortex_active_intervention",
                    "cortex_active_intervention_cid",
                    "cortex_active_intervention_mounted_at",
                ]);
            } catch { /* session storage unavailable */ }
            await removeOverlaysEverywhere();
            broadcastToPopup({ type: "OVERLAY_DISMISSED", intervention_id: interventionId });
            break;
        }

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

        case "BREATHING_OVERLAY":
        case "PRE_BREAK_WARNING":
        case "ACTIVE_RECALL":
        case "LEETCODE_SHOW_LOCKOUT": {
            // Compatibility sinks. BREATHING_OVERLAY and PRE_BREAK_WARNING
            // carry physiology claims that have not passed reference
            // validation; ACTIVE_RECALL never had a page receiver; a lockout
            // changes what the user can do on the page and stays inert
            // until it has an exact authorization and receipt-backed
            // escape/restore path. None of them presents or mutates.
            break;
        }

        case "LEETCODE_SHOW_SCRATCHPAD":
        case "LEETCODE_SHOW_PATTERN_LADDER":
        case "LEETCODE_SHOW_SUBMISSION_GATE":
        case "LEETCODE_SHOW_SOLUTION_FRICTION":
        case "LEETCODE_SHOW_CONSOLIDATION": {
            // The coach only belongs on a LeetCode tab: prefer the active
            // one, otherwise the most recently accessed LeetCode tab, and
            // never any other page.
            try {
                const tabId = await findLeetCodeTabId();
                if (tabId !== null) {
                    await chrome.scripting.executeScript({
                        target: { tabId },
                        func: injectCoachPanel,
                        args: [buildCoachPanelModel(msg.type, msg.payload), SURFACE_CSS],
                    });
                }
            } catch (e) {
                if (DEBUG) console.error("Cortex: failed to inject LeetCode coach", e);
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

/** Tabs that may receive an injected surface: http(s), never incognito. */
function injectableTab(tab: chrome.tabs.Tab): tab is chrome.tabs.Tab & { id: number } {
    return typeof tab.id === "number"
        && !tab.incognito
        && typeof tab.url === "string"
        && /^https?:/.test(tab.url);
}

/**
 * Remove every Cortex page surface from every tab. Injection is
 * best-effort per tab (a tab without site access simply rejects), so an
 * overlay can never outlive its intervention on the tabs Cortex can reach.
 */
async function removeOverlaysEverywhere(): Promise<void> {
    let tabs: chrome.tabs.Tab[] = [];
    try {
        tabs = await chrome.tabs.query({});
    } catch {
        return;
    }
    await Promise.all(tabs.filter(injectableTab).map((tab) =>
        chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: removeCortexOverlay,
        }).catch((err: unknown) => {
            if (DEBUG) console.debug("[cortex.bg] removeCortexOverlay skipped a tab: %o", err);
        })));
}

const LEETCODE_TAB_PATTERNS = [
    "https://leetcode.com/*",
    "https://*.leetcode.com/*",
    "https://leetcode.cn/*",
    "https://*.leetcode.cn/*",
];

/** The active LeetCode tab if there is one, else the most recent; else null. */
async function findLeetCodeTabId(): Promise<number | null> {
    try {
        const tabs = (await chrome.tabs.query({ url: LEETCODE_TAB_PATTERNS }))
            .filter(injectableTab);
        if (tabs.length === 0) return null;
        const active = tabs.find((tab) => tab.active);
        if (active) return active.id;
        tabs.sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0));
        return tabs[0].id;
    } catch {
        return null;
    }
}

function paintBadge(): void {
    try {
        const action = (chrome as unknown as {
            action?: {
                setBadgeText: (details: { text: string }) => void;
                setBadgeBackgroundColor: (details: { color: string }) => void;
            };
        }).action;
        if (!action) return;
        const text = badgeState.text();
        action.setBadgeText({ text });
        if (text) action.setBadgeBackgroundColor({ color: "#D97757" });
    } catch {
        // action API may be unavailable in some contexts
    }
}

function setInterventionBadge(pending: boolean): void {
    badgeState.setIntervention(pending);
    paintBadge();
}

/**
 * Present a proposal. Returns whether the page panel is showing, so the
 * trigger path can decide whether any other channel (OS notification,
 * badge) is needed at all — one channel per event.
 */
async function handleIntervention(
    payload: Record<string, unknown>,
): Promise<boolean> {
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
    let overlayShown = false;
    if (uiPlan?.show_overlay || uiPlan?.dim_background) {
        try {
            const [tab] = await chrome.tabs.query({
                active: true,
                currentWindow: true,
            });
            if (tab && injectableTab(tab)) {
                // The page never sees the raw payload: the worker normalises
                // ``micro_steps`` (``MicroStep`` objects on the wire) and
                // filters placeholder copy before serialising the model.
                await chrome.scripting.executeScript({
                    target: { tabId: tab.id },
                    func: injectInterventionPanel,
                    args: [
                        buildInterventionPanelModel(payload, executableActionIds),
                        SURFACE_CSS,
                    ],
                });
                overlayShown = true;
            }
        } catch (e) {
            if (DEBUG) console.error("Cortex: failed to inject overlay", e);
        }
    }

    broadcastToPopup({
        type: "INTERVENTION_TRIGGER",
        payload,
    });
    return overlayShown;
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
    // No content script receives runtime messages, so the panels are
    // removed by injecting the self-contained remover into every tab.
    await removeOverlaysEverywhere();
    setInterventionBadge(false);
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
    // A launch is the user pressing Start: it clears any sticky stop intent
    // before the reconnect paths below run.
    await clearStopIntent();

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

        // ``lastError`` stays in the log; the popup shows consumer copy.
        if (DEBUG && lastError) console.debug("[cortex.bg] launch failed:", lastError);
        return {
            ok: false,
            status: "not_connected",
            error: LAUNCH_FAILED_STATUS,
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
    if (action.capability === "close_tab") {
        let originalAbsent = originalTabId !== null;
        if (originalTabId !== null) {
            try {
                await chrome.tabs.get(originalTabId);
                originalAbsent = false;
            } catch {
                originalAbsent = true;
            }
        }
        return {
            verified: originalAbsent,
            detail: originalAbsent
                ? "Exact closed-tab postcondition verified"
                : "Closed-tab postcondition could not be verified",
            fingerprint: { originalTabId, originalAbsent },
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
    // ``bookmark_and_close`` is never authorised by the capability policy and
    // the extension no longer holds the ``bookmarks`` permission; the handler
    // exists only because the executor type covers every wire action_type.
    bookmark_and_close: async (action) => ({
        action_id: action.action_id,
        success: false,
        message: "Cortex does not bookmark or close existing tabs",
        reversible: false,
    }),
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
    const title = "Time to stand and stretch";
    const body = `Step away for ${minutes} minute${minutes === 1 ? "" : "s"}.`;

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

// One apply per intervention at a time: a second click (or a popup and a
// page panel racing) joins the in-flight authorization instead of sending a
// duplicate INTERVENTION_AUTHORIZE.
const applyInFlight = new Map<string, Promise<ExecuteAllRecommendedResponse>>();

function executeAllRecommended(
    interventionId: string,
): Promise<ExecuteAllRecommendedResponse> {
    const inFlight = applyInFlight.get(interventionId);
    if (inFlight) return inFlight;
    const run = (async (): Promise<ExecuteAllRecommendedResponse> => {
        let results: ActionExecuteResult[];
        try {
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
            results = await authorizeActionIds(interventionId, actionIds);
        } catch (error) {
            const outcome: ApplyOutcome = reduceApplyResults(
                { success: false, message: String(error) },
            );
            return { ok: false, results: [], outcome };
        }
        // The proposal stays mounted: an applied change is undoable for the
        // whole window and the daemon owns the intervention's lifecycle.
        // Sending ``engaged`` here would make the daemon end (and restore)
        // the intervention seconds after it was applied.
        const outcome = reduceApplyResults(results);
        const response: ExecuteAllRecommendedResponse = {
            ok: outcome.phase !== "failed",
            results,
            outcome,
        };
        // Both surfaces render the same outcome: the requester through the
        // response, every other open surface through this broadcast.
        broadcastToPopup({
            type: "INTERVENTION_APPLIED",
            intervention_id: interventionId,
            results,
            outcome,
        });
        return response;
    })();
    applyInFlight.set(interventionId, run);
    void run.finally(() => {
        if (applyInFlight.get(interventionId) === run) {
            applyInFlight.delete(interventionId);
        }
    });
    return run;
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

/**
 * One channel per health nudge: the page toast where the user is looking;
 * the popup only hears about it when no page could show the toast.
 */
function showHealthNotification(title: string, body: string): void {
    void injectToast(title, body).then((shown) => {
        if (!shown) broadcastToPopup({ type: "HEALTH_ALERT", title, body });
    });
}

/** Inject the tokenised toast into the active tab; resolves whether it showed. */
async function injectToast(title: string, body: string): Promise<boolean> {
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab || !injectableTab(tab)) return false;
        await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: injectCortexToast,
            args: [title, body, SURFACE_CSS],
        });
        return true;
    } catch {
        return false;
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

    // The badge is owned by ``badgeState`` (set by the trigger path when no
    // surface is on screen); this function only owns the OS notification.
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
    badgeState.setRecap(on);
    paintBadge();
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
                                stopRequested,
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
                    stopRequested,
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
                // Start: lift any sticky stop intent, then reconnect.
                void clearStopIntent().then(() => connect());
                sendResponse({ ok: true });
                break;

            case "DISCONNECT":
                disconnect();
                sendResponse({ ok: true });
                break;

            case "STOP_CORTEX":
                // Sticky until the user presses Start: the keepalive alarm
                // and a worker restart must not quietly reconnect.
                void setStopIntent();
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
                // ``dismissed`` is the only action that records a cooldown.
                // ``expired`` (a panel that timed out untouched) closes the
                // intervention without teaching the trigger policy anything;
                // ``engaged`` / ``restore`` pass through to the daemon.
                const action = isTerminalUserAction(message.action)
                    ? message.action
                    : "dismissed";
                send({
                    type: "USER_ACTION",
                    payload: {
                        action,
                        intervention_id: message.intervention_id,
                        timestamp: Date.now() / 1000,
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                    correlation_id: outboundCid,
                });
                if (action === "dismissed" || action === "expired") {
                    // A close from any surface removes the page panels
                    // everywhere; the popup card follows via the broadcast.
                    void removeOverlaysEverywhere();
                    setInterventionBadge(false);
                }
                if (action === "expired") {
                    interventionPresentation.clear();
                    try {
                        chrome.storage.session.remove([
                            "cortex_active_intervention",
                            "cortex_active_intervention_cid",
                            "cortex_active_intervention_mounted_at",
                        ]);
                    } catch {}
                }
                if (action === "dismissed") {
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

            case "DISTRACTION_BLOCKED": {
                // User chose "Go back" on the distraction interceptor. A
                // fresh tab has no history to return to, so the page asks
                // for the tab to be closed rather than left blank.
                if (focusSession) {
                    focusSession.distractionsBlocked++;
                    schedulePersist();
                }
                const senderTabId = sender.tab?.id;
                if (
                    isDistractionBlockedRequest(message)
                    && message.leave === "close"
                    && typeof senderTabId === "number"
                ) {
                    chrome.tabs.remove(senderTabId).catch((err: unknown) => {
                        if (DEBUG) console.debug("[cortex.bg] close intercepted tab failed: %o", err);
                    });
                }
                sendResponse({ ok: true });
                break;
            }

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

            case "EXECUTE_ALL_RECOMMENDED": {
                if (!workspaceMutationAllowed()) {
                    const denied: ExecuteAllRecommendedResponse = {
                        ok: false,
                        results: [],
                        outcome: reduceApplyResults({
                            success: false,
                            message: "Action unavailable in suggest-only mode",
                        }),
                    };
                    sendResponse(denied);
                    break;
                }
                executeAllRecommended(String(message.intervention_id || ""))
                    .then((response) => {
                        sendResponse(response);
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
                    .catch((error: unknown) => {
                        const failed: ExecuteAllRecommendedResponse = {
                            ok: false,
                            results: [],
                            outcome: reduceApplyResults({
                                success: false,
                                message: String(error),
                            }),
                        };
                        sendResponse(failed);
                    });
                return true; // async
            }

            case "UNDO_ACTION":
                undoAction(message.action_id as string)
                    .then((success) => sendResponse({ ok: success }));
                return true; // async

            case "UNDO_ALL_RECENT": {
                // Reverse locally first (works offline), then tell the daemon
                // so its transaction journal records the user's undo and
                // any exact inverse it still owns runs against a consistent
                // workspace.
                const undoInterventionId = typeof message.intervention_id === "string"
                    ? message.intervention_id
                    : interventionPresentation.active?.plan.intervention_id;
                undoAllRecent()
                    .then(() => {
                        if (typeof undoInterventionId === "string" && undoInterventionId) {
                            send({
                                type: "USER_ACTION",
                                payload: {
                                    action: "restore",
                                    intervention_id: undoInterventionId,
                                    timestamp: Date.now() / 1000,
                                },
                                timestamp: Date.now() / 1000,
                                sequence: ++sequence,
                                correlation_id: interventionPresentation.active?.correlation_id,
                            });
                        }
                        sendResponse({ ok: true });
                    });
                return true; // async
            }

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
    // Distraction blocking during focus sessions. No content script
    // listens for runtime messages, so the interceptor is injected
    // directly; incognito tabs are never touched.
    if (focusSession && changeInfo.url && !_tab.incognito) {
        const url = changeInfo.url;
        if (isDistractionUrl(url, _tab.title)) {
            const snap = getFocusSessionSnapshot();
            let domain = "";
            try {
                domain = new URL(url).hostname.replace(/^www\./, "");
            } catch {
                domain = "";
            }
            chrome.scripting.executeScript({
                target: { tabId },
                func: injectDistractionInterceptor,
                args: [
                    {
                        focusMin: Math.round((snap?.focusMs ?? 0) / 60000),
                        streakMin: snap?.longestStreakMin ?? 0,
                        distractionsBlocked: snap?.distractionsBlocked ?? 0,
                        domain,
                    },
                    SURFACE_CSS,
                ],
            }).catch((err: unknown) => {
                if (DEBUG) console.debug("[cortex.bg] scripting.executeScript distraction interceptor failed: %o", err);
            });
        }
    }
});

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
        // Each tick also flushes debounced session state (MV3 has no
        // suspend hook) and honours a sticky stop: no reconnect until the
        // user presses Start.
        flushPersistedState();
        if (!connected && !stopRequested) {
            connect();
        }
    } else if (alarm.name === "cortex-break-timer") {
        void injectToast("Break's over", "Ready when you are.");
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
    const interventionId =
        interventionPresentation.active
        && typeof interventionPresentation.active.plan.intervention_id === "string"
            ? interventionPresentation.active.plan.intervention_id
            : null;
    try {
        // The daemon hears about it when connected; the page panels are
        // removed either way.
        if (connected && ws) send({
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
    // Clear the locally-mounted state and remove the page panels right
    // away, before the daemon's DISMISS_OVERLAY cue round-trips.
    if (interventionPresentation.active) {
        interventionPresentation.clear();
    }
    setInterventionBadge(false);
    void removeOverlaysEverywhere();
    broadcastToPopup({ type: "OVERLAY_DISMISSED", intervention_id: interventionId });
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
                setInterventionBadge(false);
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
            setInterventionBadge(false);
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
