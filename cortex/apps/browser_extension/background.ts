/**
 * Cortex Chrome Extension — Background Service Worker
 *
 * Maintains a WebSocket connection to the Cortex daemon (ws://127.0.0.1:9473).
 * Receives STATE_UPDATE and INTERVENTION_TRIGGER messages.
 * Dispatches content script injection on intervention triggers.
 * Sends IDENTIFY and USER_ACTION messages to the daemon.
 */

import {
    classifyTabType as classifyBrowserTabType,
    classifyTabTypeWithGoal,
    groupSpecificTabs,
    hideNonActiveTabs as hideTabsForIntervention,
    restoreAllTabs,
    restoreHiddenTabs as restoreTabsForIntervention,
    saveTabSession,
    restoreTabSession,
} from "./tab-manager";

// --- Types ---

interface WSMessage {
    type: string;
    payload: Record<string, unknown>;
    timestamp: number;
    sequence: number;
    correlation_id?: string;
    target_client_types?: string[];
    source_client_type?: string;
}

interface CortexState {
    state: string;
    confidence: number;
    scores: Record<string, number>;
    signal_quality: Record<string, number>;
    dwell_seconds: number;
    reasons: string[];
}

// --- Debug ---
// Flip to true while debugging to surface intervention/overlay/tab-context noise.
// Production builds should keep this false to avoid leaking internals to the
// devtools console of any inspected service worker.
const DEBUG = false;

// --- State ---

let ws: WebSocket | null = null;
let connected = false;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = 3000;
const MAX_RECONNECT_DELAY = 30000;
let intentionalDisconnect = false;
let sequence = 0;
const DAEMON_WS_URL = "ws://127.0.0.1:9473";
const DAEMON_HTTP_URL = "http://127.0.0.1:9472";
const LAUNCHER_HTTP_URL = "http://127.0.0.1:9471";

let currentState: CortexState | null = null;
let activeIntervention: Record<string, unknown> | null = null;
let quietMode = false;

// Dismissal cooldown: maps intervention_id → timestamp when dismissed
// Prevents the same intervention from re-triggering within the cooldown window
const dismissedInterventions = new Map<string, number>();
const DEFAULT_INTERVENTION_DISMISS_COOLDOWN = 30 * 60 * 1000; // 30 min cooldown after dismiss
let interventionDismissCooldown = DEFAULT_INTERVENTION_DISMISS_COOLDOWN;
// Also track by URL pattern to prevent same-site re-triggers
const dismissedUrlPatterns = new Map<string, number>();
const DEFAULT_URL_DISMISS_COOLDOWN = 10 * 60 * 1000; // 10 min cooldown for same URL
let urlDismissCooldown = DEFAULT_URL_DISMISS_COOLDOWN;

// --- Focus Session State ---

interface FocusSession {
    startTime: number;
    totalFocusMs: number;      // biometrically-verified focus milliseconds
    distractionsBlocked: number;
    lastFocusCheck: number;
    lastStateWasFocus: boolean;
    longestStreakMs: number;
    currentStreakStart: number;
    goal: string;
}

interface DailyStats {
    date: string; // YYYY-MM-DD
    totalFocusMin: number;
    totalSessionMin: number;
    sessions: number;
    distractionsBlocked: number;
    longestStreakMin: number;
    avgHrDuringFocus: number;
    hrSamples: number;
}

let focusSession: FocusSession | null = null;

// Two-tier distraction detection
const ALWAYS_DISTRACTION = [
    /instagram\.com/i, /tiktok\.com/i, /netflix\.com/i,
    /twitch\.tv/i, /9gag\.com/i, /buzzfeed\.com/i, /tumblr\.com/i,
];
const CONDITIONAL_DISTRACTION = [
    /reddit\.com/i, /twitter\.com/i, /x\.com/i, /facebook\.com/i,
];
const AI_ASSISTANT_URL_PATTERN = /gemini\.google\.com|chatgpt\.com|chat\.openai\.com|claude\.ai|copilot\.microsoft\.com|perplexity\.ai/i;
const VIDEO_PLATFORM_URL_PATTERN = /youtube\.com|youtu\.be/i;

// --- Recently-visited tab protection ---
// Track when each tab was last activated so we can protect recently-used tabs from closing
const tabLastActivated = new Map<number, number>();
const RECENTLY_ACTIVE_PROTECTION_MS = 5 * 60 * 1000; // 5 minutes

chrome.tabs.onActivated.addListener((activeInfo) => {
    tabLastActivated.set(activeInfo.tabId, Date.now());
    schedulePersist();
});

chrome.tabs.onRemoved.addListener((tabId) => {
    tabLastActivated.delete(tabId);
    schedulePersist();
});

// --- State Persistence (survives MV3 service worker restarts) ---

let persistTimer: ReturnType<typeof setTimeout> | null = null;
const PERSIST_KEYS = ["focusSession", "undoStack", "dismissedInterventions", "dismissedUrlPatterns", "quietMode", "tabLastActivated"] as const;

function schedulePersist(): void {
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(async () => {
        await chrome.storage.session.set({
            focusSession,
            undoStack,
            dismissedInterventions: [...dismissedInterventions.entries()],
            dismissedUrlPatterns: [...dismissedUrlPatterns.entries()],
            quietMode,
            tabLastActivated: [...tabLastActivated.entries()],
        });
    }, 500);
}

async function restoreState(): Promise<void> {
    const data = await chrome.storage.session.get(PERSIST_KEYS as unknown as string[]);
    if (data.focusSession) focusSession = data.focusSession;
    if (data.undoStack) {
        undoStack.splice(0, undoStack.length, ...data.undoStack);
    }
    if (data.dismissedInterventions) {
        dismissedInterventions.clear();
        for (const [k, v] of data.dismissedInterventions) dismissedInterventions.set(k, v);
    }
    if (data.dismissedUrlPatterns) {
        dismissedUrlPatterns.clear();
        for (const [k, v] of data.dismissedUrlPatterns) dismissedUrlPatterns.set(k, v);
    }
    if (data.quietMode !== undefined) quietMode = data.quietMode;
    if (data.tabLastActivated) {
        tabLastActivated.clear();
        for (const [k, v] of data.tabLastActivated) tabLastActivated.set(k, v);
    }
}

// Restore persisted state on service worker startup
restoreState();

// Health alert state
let lastPostureAlert = 0;
let lastBlinkAlert = 0;
let lowBlinkStart = 0;
let leaningStart = 0;
const HEALTH_ALERT_COOLDOWN = 300_000; // 5 min between alerts
const POSTURE_ALERT_THRESHOLD = 180_000; // 3 min leaning
const BLINK_ALERT_THRESHOLD = 180_000;  // 3 min low blink rate

// Break recommendation state
let lastBreakSuggestion = 0;
let consecutiveStressUpdates = 0;

// --- Activity Tracking State ---

interface ActivityPosition {
    type: "video" | "scroll" | "code_problem" | "notebook" | "pdf" | "slides" | "general";
    [key: string]: unknown;
}

interface ActivityRecord {
    content_id: string;
    platform: string;
    content_type: "video" | "article" | "code_problem" | "documentation"
        | "course_lecture" | "notebook" | "pdf" | "slides" | "general";
    title: string;
    url: string;
    favicon_url: string;
    position: ActivityPosition;
    content_duration_s: number;
    duration_spent_s: number;
    session_duration_s: number;
    first_visited: number;
    last_visited: number;
    context_snapshot: string;
    topic_tags: string[];
    completion_pct: number;
    max_completion_pct: number;
    cognitive_state: string;
    visit_count: number;
    dismissed: boolean;
    is_playlist: boolean;
    playlist_id: string;
    playlist_index: number;
    related_tabs: string[];
}

let lastActivitySyncTime = 0;
const ACTIVITY_SYNC_INTERVAL = 60_000; // Sync to daemon every 60s
const ACTIVITY_STORAGE_KEY = "cortex_activities";
const MAX_ACTIVITIES = 200;

async function loadActivities(): Promise<Record<string, ActivityRecord>> {
    const data = await chrome.storage.local.get(ACTIVITY_STORAGE_KEY);
    return (data[ACTIVITY_STORAGE_KEY] as Record<string, ActivityRecord>) || {};
}

async function saveActivities(activities: Record<string, ActivityRecord>): Promise<void> {
    await chrome.storage.local.set({ [ACTIVITY_STORAGE_KEY]: activities });
}

async function upsertActivity(record: ActivityRecord): Promise<void> {
    const activities = await loadActivities();
    const existing = activities[record.content_id];

    if (existing) {
        // Determine if this is a continuation of the same session or a new visit.
        // Same session: first_visited matches (content script uses sessionStartTime).
        // New visit: first_visited differs (content script reset via resetForNewPage).
        const isSameSession = existing.first_visited === record.first_visited
            || (record.first_visited > existing.last_visited - 10_000); // within 10s = same session

        if (isSameSession) {
            // Replace session contribution: subtract old session time, add new
            existing.duration_spent_s = (existing.duration_spent_s - existing.session_duration_s) + record.duration_spent_s;
            existing.session_duration_s = record.duration_spent_s;
        } else {
            // New visit: add the new session's dwell time
            existing.duration_spent_s += record.duration_spent_s;
            existing.session_duration_s = record.duration_spent_s;
            existing.visit_count++;
            // Re-visiting means user may want resume card next time
            existing.dismissed = false;
        }

        existing.position = record.position;
        existing.last_visited = record.last_visited;
        existing.context_snapshot = record.context_snapshot;
        if (record.cognitive_state) existing.cognitive_state = record.cognitive_state;
        // Only increase completion, never decrease
        existing.completion_pct = Math.max(existing.completion_pct, record.completion_pct);
        existing.max_completion_pct = Math.max(existing.max_completion_pct, record.completion_pct);
        // Merge related tabs
        const tabSet = new Set([...existing.related_tabs, ...record.related_tabs]);
        existing.related_tabs = Array.from(tabSet).slice(0, 5);
        // Update title if non-empty
        if (record.title) existing.title = record.title;
        // Keep the original first_visited
        activities[record.content_id] = existing;
    } else {
        activities[record.content_id] = record;
    }

    // Enforce cap with LRU eviction
    const entries = Object.entries(activities);
    if (entries.length > MAX_ACTIVITIES) {
        const now = Date.now();
        const SEVEN_DAYS = 7 * 24 * 60 * 60 * 1000;
        // Sort by last_visited ascending (oldest first)
        entries.sort((a, b) => a[1].last_visited - b[1].last_visited);
        while (entries.length > MAX_ACTIVITIES) {
            const oldest = entries[0];
            // Prefer evicting entries older than 7 days
            if (now - oldest[1].last_visited > SEVEN_DAYS || entries.length > MAX_ACTIVITIES + 10) {
                delete activities[oldest[0]];
                entries.shift();
            } else {
                break;
            }
        }
        // If still over cap, evict oldest regardless
        while (Object.keys(activities).length > MAX_ACTIVITIES) {
            const allEntries = Object.entries(activities).sort((a, b) => a[1].last_visited - b[1].last_visited);
            delete activities[allEntries[0][0]];
        }
    }

    await saveActivities(activities);

    // Sync to daemon if connected and enough time has passed
    const now = Date.now();
    if (connected && now - lastActivitySyncTime > ACTIVITY_SYNC_INTERVAL) {
        lastActivitySyncTime = now;
        syncActivitiesToDaemon(activities);
    }
}

function syncActivitiesToDaemon(activities: Record<string, ActivityRecord>): void {
    const top10 = Object.values(activities)
        .sort((a, b) => b.last_visited - a.last_visited)
        .slice(0, 10)
        .map(a => ({
            content_id: a.content_id,
            platform: a.platform,
            content_type: a.content_type,
            title: a.title,
            url: a.url,
            position_description: formatPositionDescription(a),
            duration_spent_s: a.duration_spent_s,
            last_visited: a.last_visited,
            completion_pct: a.completion_pct,
            topic_tags: a.topic_tags,
            context_snapshot: a.context_snapshot,
        }));

    send({
        type: "ACTIVITY_SYNC",
        payload: { activities: top10 },
        timestamp: Date.now() / 1000,
        sequence: ++sequence,
    });
}

function formatPositionDescription(a: ActivityRecord): string {
    const pos = a.position;
    switch (pos.type) {
        case "video": {
            const ts = pos.timestamp_s as number;
            const dur = pos.duration_s as number;
            return `${formatTime(ts)} / ${formatTime(dur)}`;
        }
        case "scroll":
            return `${Math.round(pos.scroll_pct as number)}% read`;
        case "code_problem":
            return `Stage: ${pos.stage} · ${pos.wrong_answer_count} WA`;
        case "notebook":
            return `Cell ${(pos.cell_index as number) + 1}`;
        case "pdf":
            return `Page ${pos.page}/${pos.total_pages}`;
        case "slides":
            return `Slide ${(pos.slide_index as number) + 1}/${pos.total_slides}`;
        case "general":
            return `${Math.round(pos.scroll_pct as number)}% scrolled`;
        default:
            return "";
    }
}

function formatTime(seconds: number): string {
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    return `${m}:${String(sec).padStart(2, "0")}`;
}

function canonicalizeUrl(rawUrl: string): string {
    let u: URL;
    try { u = new URL(rawUrl); } catch { return rawUrl; }

    const STRIP = ["utm_source","utm_medium","utm_campaign","utm_term","utm_content",
                    "fbclid","gclid","ref","source","si","feature","pp"];
    for (const p of STRIP) u.searchParams.delete(p);
    u.hostname = u.hostname.replace(/^www\./, "");

    if (u.hostname.includes("youtube.com") || u.hostname.includes("youtu.be")) {
        const v = u.searchParams.get("v");
        if (v) return `https://youtube.com/watch?v=${v}`;
        if (u.hostname === "youtu.be") return `https://youtube.com/watch?v=${u.pathname.slice(1)}`;
    }
    if (u.hostname.includes("bilibili.com")) {
        const match = u.pathname.match(/\/video\/(BV\w+)/);
        const p = u.searchParams.get("p") || "1";
        if (match) return `https://bilibili.com/video/${match[1]}?p=${p}`;
    }
    if (u.hostname.includes("leetcode")) {
        const match = u.pathname.match(/\/problems\/([^/]+)/);
        if (match) return `https://${u.hostname}/problems/${match[1]}`;
    }

    const KEEP_HASH = [/docs\.google\.com\/presentation/, /\.pdf$/i];
    if (!KEEP_HASH.some(p => p.test(rawUrl))) u.hash = "";

    return u.toString();
}

async function enrichWithRelatedTabs(record: ActivityRecord): Promise<void> {
    try {
        const allTabs = await chrome.tabs.query({});
        const activities = await loadActivities();
        const relatedIds: string[] = [];
        for (const tab of allTabs) {
            if (!tab.url || tab.url === record.url) continue;
            const canonical = canonicalizeUrl(tab.url);
            if (activities[canonical]) {
                relatedIds.push(canonical);
            }
        }
        record.related_tabs = relatedIds.slice(0, 5);
    } catch {
        // tabs query may fail
    }
}

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
            reconnectDelay = 3000;

            // Identify as Chrome extension
            send({
                type: "IDENTIFY",
                payload: { client_type: "chrome" },
                timestamp: Date.now() / 1000,
                sequence: ++sequence,
            });

            // Notify popup
            broadcastToPopup({ type: "CONNECTION_CHANGED", connected: true });
        };

        ws.onmessage = (event) => {
            handleMessage(event.data as string);
        };

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
        ws.send(JSON.stringify(msg));
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
    if (!intentionalDisconnect) {
        scheduleReconnect();
    }
}

function scheduleReconnect(): void {
    if (reconnectTimer || intentionalDisconnect) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
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
        if (persistTimer) {
            clearTimeout(persistTimer);
            persistTimer = null;
        }
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
        const targetTabId = tabId || (await chrome.tabs.query({ active: true, currentWindow: true }))[0]?.id;
        if (!targetTabId) return "";
        const response = await chrome.tabs.sendMessage(targetTabId, { type: "EXTRACT_TEXT" });
        return response?.text || "";
    } catch {
        return "";
    }
}

// --- Message Handling ---

async function handleMessage(raw: string): Promise<void> {
    let msg: WSMessage;
    try {
        msg = JSON.parse(raw) as WSMessage;
    } catch {
        return;
    }

    switch (msg.type) {
        case "STATE_UPDATE":
            currentState = msg.payload as unknown as CortexState;
            updateFocusSession(msg.payload);
            checkHealthAlerts(msg.payload);
            checkBreakNeeded(msg.payload);
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
            // Skip if an intervention is already being shown
            if (activeIntervention) {
                if (DEBUG) console.log("Cortex: skipping intervention — one is already active");
                break;
            }

            const iid = typeof msg.payload.intervention_id === "string" ? msg.payload.intervention_id : null;
            const now = Date.now();

            // Check cooldown: skip if this intervention was recently dismissed
            if (iid && dismissedInterventions.has(iid)) {
                const dismissedAt = dismissedInterventions.get(iid)!;
                if (now - dismissedAt < interventionDismissCooldown) {
                    if (DEBUG) console.log(`Cortex: skipping intervention ${iid} — dismissed ${Math.round((now - dismissedAt) / 1000)}s ago`);
                    break;
                }
                dismissedInterventions.delete(iid);
                schedulePersist();
            }

            // Check URL-based cooldown: don't re-trigger for same site within window
            const triggerUrl = typeof msg.payload.trigger_url === "string" ? msg.payload.trigger_url : null;
            const urlKey = triggerUrl ? new URL(triggerUrl).hostname : null;
            if (urlKey && dismissedUrlPatterns.has(urlKey)) {
                const dismissedAt = dismissedUrlPatterns.get(urlKey)!;
                if (now - dismissedAt < urlDismissCooldown) {
                    if (DEBUG) console.log(`Cortex: skipping intervention for ${urlKey} — dismissed ${Math.round((now - dismissedAt) / 1000)}s ago`);
                    break;
                }
                dismissedUrlPatterns.delete(urlKey);
                schedulePersist();
            }

            activeIntervention = msg.payload;
            // Persist so popup can load it after SW restart
            try { chrome.storage.session.set({ cortex_active_intervention: msg.payload }); } catch {}
            handleIntervention(msg.payload);
            break;
        }

        case "CONTEXT_REQUEST":
            handleContextRequest(msg);
            break;

        case "INTERVENTION_RESTORE":
            handleRestore(msg.payload);
            break;

        case "SETTINGS_SYNC":
            quietMode = Boolean(msg.payload.quiet_mode);
            // Sync cooldown values from daemon config, keeping defaults as fallbacks
            if (typeof msg.payload.intervention_dismiss_cooldown_ms === "number") {
                interventionDismissCooldown = msg.payload.intervention_dismiss_cooldown_ms as number;
            }
            if (typeof msg.payload.url_dismiss_cooldown_ms === "number") {
                urlDismissCooldown = msg.payload.url_dismiss_cooldown_ms as number;
            }
            schedulePersist();
            broadcastToPopup({ type: "SETTINGS_SYNC", payload: msg.payload });
            break;

        case "BREATHING_OVERLAY": {
            // Route to active tab's content script
            const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
            if (activeTab?.id) {
                chrome.tabs.sendMessage(activeTab.id, {
                    type: "SHOW_BREATHING_OVERLAY",
                    payload: msg.payload,
                });
            }
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
            const headline = String(msg.payload.headline || "Biological load rising");
            const summary = String(
                msg.payload.situation_summary || "Consider a short reset before stress load crosses the break threshold.",
            );
            injectToast(headline, summary);
            broadcastToPopup({ type: "PRE_BREAK_WARNING", payload: msg.payload });
            break;
        }

        case "LEETCODE_SHOW_LOCKOUT": {
            // Inject lockout overlay into the active tab
            try {
                const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                if (tab?.id) {
                    await chrome.scripting.executeScript({
                        target: { tabId: tab.id },
                        func: injectLockoutOverlay,
                        args: [msg.payload],
                    });
                }
            } catch (e) {
                if (DEBUG) console.error("Cortex: failed to inject lockout overlay", e);
            }
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
    }
}

/**
 * Injected directly into the page via chrome.scripting.executeScript.
 * Creates the intervention overlay using Shadow DOM.
 *
 * Design: dark, high-end tech (Linear/Raycast-inspired).
 * Consistent with popup and all other Cortex UI.
 */
function injectOverlay(payload: Record<string, unknown>): void {
    const OID = "cortex-somatic-overlay";
    document.getElementById(OID)?.remove();

    const headline = String(payload.headline || "");
    const summary = String(payload.situation_summary || "");
    const steps = (payload.micro_steps as string[]) || [];
    const esc = (s: string) =>
        s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

    const actions: Array<Record<string, unknown>> = [...((payload.suggested_actions as Array<Record<string, unknown>>) || [])];
    const tabRecs = payload.tab_recommendations as { tabs: Array<Record<string, unknown>>; summary: string } | undefined;
    const errA = payload.error_analysis as Record<string, string> | undefined;

    // Synthesize close_tab actions from tab_recommendations when the LLM
    // generated recommendations but no matching suggested_actions.
    if (tabRecs && tabRecs.tabs && tabRecs.tabs.length > 0) {
        const hasCloseAction = actions.some(a => a.action_type === "close_tab" || a.action_type === "bookmark_and_close");
        if (!hasCloseAction) {
            const closeable = tabRecs.tabs.filter(t => t.action === "close" || t.action === "bookmark_and_close");
            for (let ci = 0; ci < closeable.length; ci++) {
                const t = closeable[ci];
                actions.push({
                    action_id: `synth_${Date.now()}_${ci}`,
                    action_type: t.action === "bookmark_and_close" ? "bookmark_and_close" : "close_tab",
                    tab_index: typeof t.tab_index === "number" ? t.tab_index : Number(t.tab_index),
                    target: "",
                    label: `Close ${t.tab_title || "tab"}`,
                    reason: t.reason || "",
                    category: "recommended",
                    reversible: true,
                    metadata: {
                        expected_title: t.tab_title || "",
                        expected_url: t.url || "",
                    },
                });
            }
        }
    }

    const recommended = actions.filter(a => a.category === "recommended");

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
                closingHtml += `<div class="tr" id="tr-${ti}" data-tab-idx="${ti}"><span class="tx">\u00d7</span><div class="tc"><span class="tn">${tabTitle}</span>${tabReason}</div><button class="kb" data-keep-idx="${ti}">Keep</button></div>`;
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
    let ctaLabel = "Clean up";
    if (closeCount > 0) {
        ctaLabel = `Close ${closeCount} tab${closeCount !== 1 ? "s" : ""}`;
    } else if (hasRealError) {
        ctaLabel = "Help me fix this";
    } else if (recommended.length > 0) {
        ctaLabel = `Apply ${recommended.length} change${recommended.length !== 1 ? "s" : ""}`;
    }

    const host = document.createElement("div");
    host.id = OID;
    host.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483647;pointer-events:none;";

    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
<style>
@keyframes panelIn{from{transform:translateY(12px) scale(.99);opacity:0}to{transform:translateY(0) scale(1);opacity:1}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
*{box-sizing:border-box;margin:0;padding:0}

.bk{position:fixed;inset:0;background:transparent;pointer-events:none;animation:fadeIn .25s ease}

.pn{
  position:fixed;bottom:20px;right:20px;width:340px;max-height:calc(100vh - 40px);overflow-y:auto;
  pointer-events:auto;
  background:#111113;
  border-radius:12px;
  border:1px solid rgba(255,255,255,.06);
  box-shadow:0 0 0 .5px rgba(0,0,0,.3),0 4px 20px rgba(0,0,0,.4),0 16px 40px rgba(0,0,0,.2);
  font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text',system-ui,sans-serif;
  color:#e4e4e7;padding:18px 16px 14px;
  animation:panelIn .3s cubic-bezier(.16,1,.3,1);
}
.pn::-webkit-scrollbar{width:0}

/* Close */
.xb{position:absolute;top:14px;right:14px;width:22px;height:22px;border:none;background:rgba(255,255,255,.04);border-radius:6px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .12s}
.xb:hover{background:rgba(255,255,255,.08)}
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
.btn{display:block;width:100%;padding:9px;border:none;border-radius:8px;background:#e4e4e7;color:#09090b;font-size:12px;font-weight:600;cursor:pointer;transition:all .12s;letter-spacing:-.1px;text-align:center;font-family:inherit}
.btn:hover{background:#f4f4f5}
.btn:active{transform:scale(.98)}
.btn.ok{background:#10b981;color:#fff;cursor:default;pointer-events:none}

.ur{display:flex;align-items:center;justify-content:center;gap:6px;padding:6px 0;font-size:11px;color:#3f3f46;margin-top:4px}
.ul{color:#3b82f6;cursor:pointer;font-weight:500;text-decoration:none;border:none;background:none;font-size:11px;font-family:inherit;padding:0}
.ul:hover{text-decoration:underline}

/* Keep button (per-tab) */
.kb{margin-left:auto;padding:2px 8px;border:1px solid rgba(255,255,255,.08);border-radius:4px;background:none;color:#71717a;font-size:10px;cursor:pointer;font-family:inherit;flex-shrink:0;transition:all .12s}
.kb:hover{color:#10b981;border-color:rgba(16,185,129,.3)}
.tr.kept{opacity:.35;text-decoration:line-through}
.tr.kept .kb{color:#10b981;border-color:#10b981}
.tr.kept .tx{color:#3f3f46}

/* Dismiss */
.dm{display:block;width:100%;padding:6px;margin-top:6px;border:none;border-radius:6px;background:none;color:#3f3f46;cursor:pointer;font-size:11px;font-family:inherit;transition:color .12s}
.dm:hover{color:#71717a}
</style>

<div class="bk" id="bk"></div>
<div class="pn">
  <button class="xb" id="xb"><svg viewBox="0 0 10 10" fill="none"><path d="M1 1l8 8M9 1l-8 8"/></svg></button>
  <div class="hd">${esc(headline)}</div>
  <div class="ds">${esc(summary)}</div>
  <div class="dv"></div>
  ${closingHtml ? `<div class="sh">Closing ${closeCount} tab${closeCount !== 1 ? "s" : ""}</div>${closingHtml}` : ""}
  ${keepCount > 0 ? `<div class="kn">Keeping <span class="kc">${keepCount}</span> you need</div>` : ""}
  ${errHtml}
  ${stepsHtml ? `<div class="dv"></div>${stepsHtml}` : ""}
  ${recommended.length > 0 ? `<button class="btn" id="cta">${esc(ctaLabel)}</button><div class="ur" id="undo-bar" style="display:none"><span>Done.</span><button class="ul" id="undo-btn">Undo</button></div>` : ""}
  <button class="dm" id="dm">Dismiss</button>
</div>`;

    document.body.appendChild(host);

    let dismissed = false;
    const dismiss = () => {
        if (dismissed) return;
        dismissed = true;
        // Notify background to record cooldown and restore tabs
        chrome.runtime.sendMessage({
            type: "USER_ACTION",
            action: "dismissed",
            intervention_id: payload.intervention_id,
        }).catch(() => {});
        const el = document.getElementById(OID);
        if (el) {
            el.style.transition = "opacity .2s ease";
            el.style.opacity = "0";
            setTimeout(() => el.remove(), 220);
        }
    };
    shadow.getElementById("xb")?.addEventListener("click", dismiss);
    shadow.getElementById("dm")?.addEventListener("click", dismiss);
    shadow.getElementById("bk")?.addEventListener("click", dismiss);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") dismiss();
    }, { once: true });

    // LAYER 5: Per-tab Keep buttons — remove individual tabs from pending closes
    const keptIndices = new Set<number>();
    const keepBtns = shadow.querySelectorAll(".kb");
    const updateCtaLabel = () => {
        const remaining = closeCount - keptIndices.size;
        if (ctaEl) {
            if (remaining <= 0) {
                (ctaEl as HTMLButtonElement).disabled = true;
                ctaEl.textContent = "All tabs kept";
                ctaEl.style.opacity = "0.4";
            } else {
                (ctaEl as HTMLButtonElement).disabled = false;
                ctaEl.textContent = `Close ${remaining} tab${remaining !== 1 ? "s" : ""}`;
                ctaEl.style.opacity = "1";
            }
        }
    };
    keepBtns.forEach(btn => {
        btn.addEventListener("click", (e) => {
            const idx = Number((e.currentTarget as HTMLElement).dataset.keepIdx);
            const row = shadow.getElementById(`tr-${idx}`);
            if (keptIndices.has(idx)) {
                keptIndices.delete(idx);
                row?.classList.remove("kept");
                (e.currentTarget as HTMLElement).textContent = "Keep";
            } else {
                keptIndices.add(idx);
                row?.classList.add("kept");
                (e.currentTarget as HTMLElement).textContent = "Kept";
            }
            updateCtaLabel();
        });
    });

    // CTA
    const ctaEl = shadow.getElementById("cta");
    if (ctaEl) {
        ctaEl.addEventListener("click", () => {
            // Filter out actions for tabs the user chose to keep
            const toExecute = actions.filter((a) => {
                if (a.category !== "recommended") return false;
                // Match kept indices to close actions by their position among recommended close actions
                const closeActions = actions.filter(x =>
                    x.category === "recommended" && (x.action_type === "close_tab" || x.action_type === "bookmark_and_close"));
                const closeIdx = closeActions.indexOf(a);
                if (closeIdx >= 0 && keptIndices.has(closeIdx)) return false;
                return true;
            });
            if (toExecute.length === 0) return;
            (ctaEl as HTMLButtonElement).disabled = true;
            ctaEl.textContent = "Working\u2026";
            ctaEl.style.opacity = "0.5";

            // Build per-tab feedback: which tabs were kept vs closed
            const closeTabs = tabRecs?.tabs?.filter(
                t => t.action === "close" || t.action === "bookmark_and_close"
            ) || [];
            const keptTabData = Array.from(keptIndices).map(i => ({
                url: String(closeTabs[i]?.url || ""),
                title: String(closeTabs[i]?.tab_title || ""),
            })).filter(t => t.url);
            const closedTabData = closeTabs
                .filter((_, i) => !keptIndices.has(i))
                .map(t => ({
                    url: String(t.url || ""),
                    title: String(t.tab_title || ""),
                })).filter(t => t.url);

            chrome.runtime.sendMessage({
                type: "EXECUTE_ALL_RECOMMENDED",
                actions: toExecute,
                intervention_id: payload.intervention_id,
                kept_tabs: keptTabData,
                closed_tabs: closedTabData,
            }, (results: Array<Record<string, unknown>>) => {
                const failCount = Array.isArray(results) ? results.filter(r => !r.success).length : 0;
                const successCount = (Array.isArray(results) ? results.length : 0) - failCount;

                ctaEl.style.opacity = "1";
                ctaEl.classList.add("ok");
                ctaEl.textContent = failCount > 0
                    ? `Done (${failCount} skipped)`
                    : `${successCount} tab${successCount !== 1 ? "s" : ""} closed`;

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

    setTimeout(dismiss, 5 * 60 * 1000);
}


/**
 * Injected into the active tab to show a lockout countdown overlay.
 * Uses the same Shadow DOM pattern as the intervention overlay.
 *
 * payload.duration_s  — lockout duration in seconds
 * payload.reason      — brief message explaining why
 */
function injectLockoutOverlay(payload: Record<string, unknown>): void {
    const OID = "cortex-lockout-overlay";
    document.getElementById(OID)?.remove();

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

    const host = document.createElement("div");
    host.id = OID;
    host.style.cssText =
        "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483647;pointer-events:none;";

    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
<style>
@keyframes panelIn{from{transform:translateY(12px) scale(.99);opacity:0}to{transform:translateY(0) scale(1);opacity:1}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
*{box-sizing:border-box;margin:0;padding:0}
.bk{position:fixed;inset:0;background:rgba(0,0,0,.55);pointer-events:auto;animation:fadeIn .25s ease}
.pn{
  position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:360px;
  pointer-events:auto;
  background:#111113;
  border-radius:14px;
  border:1px solid rgba(255,255,255,.06);
  box-shadow:0 0 0 .5px rgba(0,0,0,.3),0 4px 20px rgba(0,0,0,.4),0 16px 40px rgba(0,0,0,.2);
  font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text',system-ui,sans-serif;
  color:#e4e4e7;padding:28px 24px 22px;text-align:center;
  animation:panelIn .3s cubic-bezier(.16,1,.3,1);
}
.hd{font-size:15px;font-weight:600;color:#e4e4e7;margin-bottom:8px;letter-spacing:-.2px}
.rs{font-size:12px;color:#71717a;line-height:1.5;margin-bottom:20px}
.tm{font-size:40px;font-weight:700;color:#e4e4e7;font-variant-numeric:tabular-nums;margin-bottom:20px;font-family:'SF Mono','Fira Code',ui-monospace,monospace}
.sk{display:inline-block;padding:7px 18px;border:1px solid rgba(255,255,255,.08);border-radius:8px;background:none;color:#71717a;font-size:11px;cursor:pointer;font-family:inherit;transition:all .12s}
.sk:hover{color:#e4e4e7;border-color:rgba(255,255,255,.15)}
</style>
<div class="bk" id="bk"></div>
<div class="pn">
  <div class="hd">Lockout Active</div>
  <div class="rs">\${esc(reason)}</div>
  <div class="tm" id="countdown">\${formatCountdown(durationS)}</div>
  <button class="sk" id="skip">I need to continue</button>
</div>
`;

    document.body.appendChild(host);

    let remaining = durationS;

    function dismiss(): void {
        host.remove();
    }

    const timer = setInterval(() => {
        remaining--;
        const el = shadow.getElementById("countdown");
        if (el) el.textContent = formatCountdown(remaining);
        if (remaining <= 0) {
            clearInterval(timer);
            dismiss();
        }
    }, 1000);

    // Skip button — no penalty, just dismiss. Lives in injected page-context
    // (executeScript), so the service-worker DEBUG flag is out of scope here.
    shadow.getElementById("skip")?.addEventListener("click", () => {
        clearInterval(timer);
        dismiss();
    });

    // Clicking backdrop does NOT dismiss — lockout must be waited out or explicitly skipped
}

function injectLeetCodeCoachOverlay(kind: string, payload: Record<string, unknown>): void {
    const OID = "cortex-leetcode-coach";
    document.getElementById(OID)?.remove();

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

    const host = document.createElement("div");
    host.id = OID;
    host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
<style>
*{box-sizing:border-box}
.card{position:fixed;right:22px;bottom:22px;width:min(380px,calc(100vw - 28px));pointer-events:auto;background:#101112;color:#f3f0e8;border:1px solid rgba(243,240,232,.12);border-radius:18px;box-shadow:0 18px 60px rgba(0,0,0,.35);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;padding:18px;animation:in .22s cubic-bezier(.16,1,.3,1)}
@keyframes in{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
.top{display:flex;align-items:center;gap:10px;margin-bottom:10px}.dot{width:9px;height:9px;border-radius:99px;background:#dfb15b;box-shadow:0 0 18px rgba(223,177,91,.55)}.ttl{font-size:14px;font-weight:700;letter-spacing:-.02em;flex:1}.x{border:0;background:transparent;color:#9b9488;font-size:18px;line-height:1;cursor:pointer}.body{font-size:13px;line-height:1.5;color:#cfc7b7;margin-bottom:13px}textarea{width:100%;height:92px;resize:vertical;background:#18191a;color:#f3f0e8;border:1px solid rgba(243,240,232,.14);border-radius:12px;padding:10px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;outline:none}textarea:focus{border-color:#dfb15b}.tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}.tags span{font-size:11px;color:#dfb15b;border:1px solid rgba(223,177,91,.25);border-radius:99px;padding:4px 8px;background:rgba(223,177,91,.08)}button{border:1px solid rgba(243,240,232,.14);background:#dfb15b;color:#15110a;border-radius:10px;padding:8px 11px;font-size:12px;font-weight:700;cursor:pointer}.hint{margin-top:10px;font-size:12px;line-height:1.45;color:#cfc7b7;background:#18191a;border-radius:10px;padding:10px}label{display:flex;gap:8px;align-items:center;font-size:12px;color:#cfc7b7}
</style>
<div class="card">
  <div class="top"><span class="dot"></span><div class="ttl">${esc(title)}</div><button class="x" id="lc-close">×</button></div>
  <div class="body">${body}</div>
  ${extra}
</div>`;
    document.body.appendChild(host);

    shadow.getElementById("lc-close")?.addEventListener("click", () => host.remove());
    shadow.getElementById("lc-reveal")?.addEventListener("click", () => {
        const hint = shadow.getElementById("lc-hint");
        if (hint) {
            hint.textContent = "Hint 2: define the state transition and one invariant before writing more code.";
        }
    });
}

async function handleIntervention(
    payload: Record<string, unknown>,
): Promise<void> {
    // B.2: track what we actually applied so the ack at the bottom is
    // truthful instead of theatrical. Each successful effect appends
    // a descriptor; failures push to ``errors``.
    const appliedActions: string[] = [];
    const errors: string[] = [];

    // Snapshot tabs so action executor can resolve tab_index → chrome tab ID
    await snapshotTabsForIntervention();

    const uiPlan = payload.ui_plan as Record<string, boolean> | undefined;

    // Directly inject overlay into the active tab via executeScript.
    // This bypasses content script messaging entirely — most reliable approach.
    if (uiPlan?.show_overlay || uiPlan?.dim_background) {
        try {
            const [tab] = await chrome.tabs.query({
                active: true,
                currentWindow: true,
            });
            if (tab?.id) {
                await chrome.scripting.executeScript({
                    target: { tabId: tab.id },
                    func: injectOverlay,
                    args: [payload],
                });
                appliedActions.push("inject_overlay");
            }
        } catch (e) {
            if (DEBUG) console.error("Cortex: failed to inject overlay", e);
            errors.push(`inject_overlay: ${(e as Error)?.message ?? String(e)}`);
        }
    }

    // Hide tabs if simplified workspace — but protect goal-relevant and recently-active tabs
    if (payload.hide_targets) {
        const targets = payload.hide_targets as string[];
        const interventionId = payload.intervention_id;
        if (
            targets.includes("browser_tabs_except_active") &&
            typeof interventionId === "string"
        ) {
            // Build set of protected tab IDs: recently-active tabs + goal-relevant tabs
            const protectedIds = new Set<number>();
            const now = Date.now();
            for (const [tabId, lastActive] of tabLastActivated) {
                if (now - lastActive < RECENTLY_ACTIVE_PROTECTION_MS) {
                    protectedIds.add(tabId);
                }
            }
            // Also protect tabs that match goal keywords
            if (focusSession?.goal) {
                const goalKw = extractGoalKeywords(focusSession.goal);
                if (goalKw.length > 0) {
                    try {
                        const allTabs = await chrome.tabs.query({});
                        for (const tab of allTabs) {
                            if (!tab.id) continue;
                            const titleLower = (tab.title ?? "").toLowerCase();
                            if (goalKw.some(kw => titleLower.includes(kw))) {
                                protectedIds.add(tab.id);
                            }
                        }
                    } catch {}
                }
            }
            try {
                await hideTabsForIntervention(interventionId, protectedIds);
                appliedActions.push("hide_tabs_except_active");
            } catch (e) {
                errors.push(`hide_tabs: ${(e as Error)?.message ?? String(e)}`);
            }
        }
    }

    broadcastToPopup({
        type: "INTERVENTION_TRIGGER",
        payload,
    });

    // B.2: ack the apply so the daemon can replace the optimistic
    // _OptimisticInterventionAdapter mutation tracking with real
    // browser-side outcomes. Without this, InterventionOutcome.workspace_restored
    // is theatrical for tab/overlay mutations (which are the majority).
    const interventionId = payload.intervention_id;
    if (typeof interventionId === "string") {
        sendInterventionApplied(
            interventionId,
            "apply",
            errors.length === 0,
            appliedActions,
            errors,
        );
    }
}

async function handleContextRequest(msg: WSMessage): Promise<void> {
    try {
        const tabs = await collectTabs();
        // Save this tab list so the intervention snapshot uses the same ordering
        // the LLM will see — prevents tab_index misalignment.
        lastContextTabs = tabs;
        lastContextTabsTimestamp = Date.now();
        const activeTab = tabs.find((t) => t.is_active);

        // Get active tab content
        let contentExcerpt = "";
        if (activeTab) {
            try {
                const [tab] = await chrome.tabs.query({
                    active: true,
                    currentWindow: true,
                });
                if (tab?.id) {
                    const results = await chrome.scripting.executeScript({
                        target: { tabId: tab.id },
                        func: extractPageText,
                    });
                    if (results?.[0]?.result) {
                        contentExcerpt = results[0].result as string;
                    }
                }
            } catch {
                // Content extraction failed
            }
        }

        send({
            type: "CONTEXT_RESPONSE",
            payload: {
                browser_context: {
                    active_tab_title: activeTab?.title ?? "",
                    active_tab_url: activeTab?.url ?? "",
                    active_tab_content_excerpt: contentExcerpt,
                    all_tabs: tabs,
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
    // B.2: track real restore outcome so the daemon's
    // InterventionOutcome.workspace_restored reflects truth, not
    // optimistic defaults. Each effect either appends an applied descriptor
    // or pushes an error.
    const appliedActions: string[] = [];
    const errors: string[] = [];

    const interventionId = payload.intervention_id;
    try {
        if (typeof interventionId === "string") {
            await restoreTabsForIntervention(interventionId);
            appliedActions.push("restore_tabs_for_intervention");
        } else {
            await restoreAllTabs();
            appliedActions.push("restore_all_tabs");
        }
    } catch (e) {
        errors.push(`restore_tabs: ${(e as Error)?.message ?? String(e)}`);
    }
    try {
        const tabs = await chrome.tabs.query({});
        await Promise.all(
            tabs
                .filter((tab) => typeof tab.id === "number")
                .map((tab) =>
                    chrome.tabs.sendMessage(tab.id as number, {
                        type: "REMOVE_OVERLAY",
                    }).catch(() => undefined),
                ),
        );
        appliedActions.push("remove_overlay");
    } catch {
        errors.push("remove_overlay");
    }
    activeIntervention = null;
    try { chrome.storage.session.remove(["cortex_active_intervention", "cortex_tab_snapshot", "cortex_tab_mgr_snapshots"]); } catch {}
    broadcastToPopup({ type: "INTERVENTION_RESTORE", payload });

    if (typeof interventionId === "string") {
        sendInterventionApplied(
            interventionId,
            "restore",
            errors.length === 0,
            appliedActions,
            errors,
        );
    }
}

// --- Tab Management ---

interface TabData {
    title: string;
    url: string;
    tab_type: string;
    is_active: boolean;
    tab_id: number;
    topic_hint: string;
    last_activated_ago_seconds: number | null;
}

function extractTopicHint(title: string, url: string, tabType: string): string {
    if (tabType === "ai_assistant") {
        return title.replace(/\s*[-–—]\s*(Gemini|ChatGPT|Claude|Copilot|Perplexity|Phind|Poe).*$/i, "").slice(0, 100);
    }
    if (tabType === "video_platform") {
        return title.replace(/\s*[-–—]\s*(YouTube|Vimeo).*$/i, "").slice(0, 100);
    }
    if (tabType === "search") {
        try { return new URL(url).searchParams.get("q")?.slice(0, 100) || ""; } catch { return ""; }
    }
    if (tabType === "communication") {
        return title.replace(/\s*[-–—]\s*(Slack|Discord|Microsoft Teams).*$/i, "").slice(0, 100);
    }
    return "";
}

async function collectTabs(): Promise<TabData[]> {
    const chromeTabs = await chrome.tabs.query({});
    // LAYER 2: Extract goal keywords for goal-aware classification
    const goalKeywords: string[] = focusSession?.goal
        ? extractGoalKeywords(focusSession.goal)
        : [];

    const now = Date.now();
    return chromeTabs.map((tab) => {
        // Use goal-aware classification when a focus session is active
        const tabType = goalKeywords.length > 0
            ? classifyTabTypeWithGoal(tab.url ?? "", tab.title ?? "", goalKeywords)
            : classifyBrowserTabType(tab.url ?? "");
        const lastActive = tabLastActivated.get(tab.id ?? -1);
        return {
            title: tab.title ?? "",
            url: tab.url ?? "",
            tab_type: tabType,
            is_active: tab.active ?? false,
            tab_id: tab.id ?? -1,
            topic_hint: extractTopicHint(tab.title ?? "", tab.url ?? "", tabType),
            last_activated_ago_seconds: lastActive != null
                ? Math.floor((now - lastActive) / 1000)
                : null,
        };
    });
}

// --- Content extraction function (injected into page) ---

function extractPageText(): string {
    const MAX_CHARS = 8000; // ~2000 tokens
    const walker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
        {
            acceptNode(node: Text): number {
                const parent = node.parentElement;
                if (!parent) return NodeFilter.FILTER_REJECT;
                const tag = parent.tagName.toLowerCase();
                if (
                    ["script", "style", "noscript", "svg", "path"].includes(
                        tag,
                    )
                ) {
                    return NodeFilter.FILTER_REJECT;
                }
                if (parent.offsetWidth === 0 && parent.offsetHeight === 0) {
                    return NodeFilter.FILTER_REJECT;
                }
                const text = node.textContent?.trim();
                if (!text || text.length < 2) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            },
        },
    );

    const chunks: string[] = [];
    let totalLen = 0;
    let node: Text | null;

    while ((node = walker.nextNode() as Text | null)) {
        const text = node.textContent?.trim();
        if (!text) continue;
        if (totalLen + text.length > MAX_CHARS) {
            chunks.push(text.substring(0, MAX_CHARS - totalLen));
            break;
        }
        chunks.push(text);
        totalLen += text.length;
    }

    return chunks.join(" ");
}

// --- Focus Session Logic ---

function startFocusSession(goal: string): void {
    const now = Date.now();
    focusSession = {
        startTime: now,
        totalFocusMs: 0,
        distractionsBlocked: 0,
        lastFocusCheck: now,
        lastStateWasFocus: false,
        longestStreakMs: 0,
        currentStreakStart: 0,
        goal,
    };
    schedulePersist();
    broadcastToPopup({ type: "FOCUS_SESSION_STARTED", goal });
}

function stopFocusSession(): FocusSession | null {
    if (!focusSession) return null;
    const session = { ...focusSession };
    // Save to daily stats
    saveToDailyStats(session);
    focusSession = null;
    schedulePersist();
    broadcastToPopup({ type: "FOCUS_SESSION_ENDED", session });
    return session;
}

function updateFocusSession(payload: Record<string, unknown>): void {
    if (!focusSession) return;
    const now = Date.now();
    const elapsed = now - focusSession.lastFocusCheck;
    const state = payload.state as string;
    const isFocused = state === "FLOW" || state === "RECOVERY";

    if (isFocused) {
        focusSession.totalFocusMs += elapsed;
        if (!focusSession.lastStateWasFocus) {
            focusSession.currentStreakStart = now;
        }
        const currentStreak = now - focusSession.currentStreakStart;
        if (currentStreak > focusSession.longestStreakMs) {
            focusSession.longestStreakMs = currentStreak;
        }
    } else {
        focusSession.currentStreakStart = 0;
    }
    focusSession.lastStateWasFocus = isFocused;
    focusSession.lastFocusCheck = now;
    schedulePersist();
}

function getFocusSessionSnapshot() {
    if (!focusSession) return null;
    const now = Date.now();
    const elapsed = now - focusSession.startTime;
    return {
        elapsedMs: elapsed,
        focusMs: focusSession.totalFocusMs,
        focusPct: elapsed > 0 ? Math.round((focusSession.totalFocusMs / elapsed) * 100) : 0,
        distractionsBlocked: focusSession.distractionsBlocked,
        longestStreakMin: Math.round(focusSession.longestStreakMs / 60000),
        goal: focusSession.goal,
        currentStreakMs: focusSession.lastStateWasFocus && focusSession.currentStreakStart
            ? now - focusSession.currentStreakStart : 0,
    };
}

async function saveToDailyStats(session: FocusSession): Promise<void> {
    const today = new Date().toISOString().slice(0, 10);
    const result = await chrome.storage.local.get("cortex_daily_stats");
    let stats: DailyStats = result.cortex_daily_stats as DailyStats;
    if (!stats || stats.date !== today) {
        stats = {
            date: today,
            totalFocusMin: 0,
            totalSessionMin: 0,
            sessions: 0,
            distractionsBlocked: 0,
            longestStreakMin: 0,
            avgHrDuringFocus: 0,
            hrSamples: 0,
        };
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

// Short but meaningful tech terms that should not be filtered out of goal keywords
const TECH_SHORT_WORDS = new Set([
    "go", "ml", "ai", "css", "sql", "vue", "rx", "aws", "gcp", "api",
    "cli", "gui", "dom", "npm", "pip", "git", "ux", "ui", "db",
    "os", "ci", "cd", "qa", "c++", "c#", "r", "dx", "io", "jwt",
]);

function extractGoalKeywords(goal: string): string[] {
    return goal.toLowerCase().split(/\s+/).filter(
        w => w.length > 1 || TECH_SHORT_WORDS.has(w.toLowerCase())
    );
}

function isDistractionUrl(url: string, title?: string): boolean {
    if (ALWAYS_DISTRACTION.some((p) => p.test(url))) return true;
    if (AI_ASSISTANT_URL_PATTERN.test(url)) return false;
    if (VIDEO_PLATFORM_URL_PATTERN.test(url)) {
        // YouTube: check title for goal-relevant keywords
        if (focusSession?.goal && title) {
            const goalWords = extractGoalKeywords(focusSession.goal);
            const titleLower = title.toLowerCase();
            if (goalWords.some(w => titleLower.includes(w))) return false;
        }
        return true;
    }
    if (CONDITIONAL_DISTRACTION.some((p) => p.test(url))) {
        if (focusSession?.goal && title) {
            const goalWords = extractGoalKeywords(focusSession.goal);
            const titleLower = title.toLowerCase();
            if (goalWords.some(w => titleLower.includes(w))) return false;
        }
        return true;
    }
    return false;
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

interface SuggestedAction {
    action_id: string;
    action_type: string;
    tab_index: number | null;
    target: string;
    label: string;
    reason: string;
    category: "recommended" | "optional" | "informational";
    reversible: boolean;
    group_id?: string;
    metadata: Record<string, unknown>;
}

interface ActionExecuteResult {
    action_id: string;
    success: boolean;
    message: string;
    undo_available: boolean;
}

interface UndoEntry {
    action_id: string;
    action_type: string;
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
    const tabs = lastContextTabs ?? await collectTabs();
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
        const liveTabs = await chrome.tabs.query({});
        const liveIds = new Set(liveTabs.map(t => t.id));
        for (const [idx, entry] of interventionTabSnapshot) {
            if (!liveIds.has(entry.chromeTabId)) {
                interventionTabSnapshot.delete(idx);
                delete snapData[String(idx)];
            }
        }
    } catch {}

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
        const lastActive = tabLastActivated.get(snap.chromeTabId);
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

async function executeAction(action: SuggestedAction): Promise<ActionExecuteResult> {
    try {
        switch (action.action_type) {
            case "close_tab":
                return await executeCloseTab(action);
            case "group_tabs":
                return await executeGroupTabs(action);
            case "bookmark_and_close":
                return await executeBookmarkAndClose(action);
            case "open_url":
                return await executeOpenUrl(action);
            case "search_error":
                return await executeSearchError(action);
            case "highlight_tab":
                return await executeHighlightTab(action);
            case "save_session":
                return await executeSaveSession(action);
            case "copy_to_clipboard":
                return await executeCopyToClipboard(action);
            case "start_timer":
                return await executeStartTimer(action);
            default:
                return { action_id: action.action_id, success: false, message: "Unknown action type", undo_available: false };
        }
    } catch (e) {
        return {
            action_id: action.action_id,
            success: false,
            message: String(e),
            undo_available: false,
        };
    }
}

async function executeCloseTab(action: SuggestedAction): Promise<ActionExecuteResult> {
    const aid = action.action_id || `close_${Date.now()}`;

    // Check if tab closing is disabled by user toggle
    try {
        const { cortex_tab_close_disabled } = await chrome.storage.local.get("cortex_tab_close_disabled");
        if (cortex_tab_close_disabled === true) {
            return { action_id: aid, success: false, message: "Tab closing is disabled", undo_available: false };
        }
    } catch { /* storage read failed — proceed normally */ }
    const tabIndex = typeof action.tab_index === "number" ? action.tab_index : Number(action.tab_index);
    if (isNaN(tabIndex)) {
        return { action_id: aid, success: false, message: "No tab_index provided", undo_available: false };
    }

    // Primary path: use snapshot
    const v = await validateTab(tabIndex);
    let tabId = v.valid ? v.tabId : -1;
    let tabUrl = "";
    let tabTitle = "";

    if (v.valid) {
        const snap = interventionTabSnapshot.get(tabIndex);
        tabUrl = snap?.url || "";
        tabTitle = snap?.title || "";
    } else {
        // Fallback: find the tab by matching title from the action label.
        // This handles cases where the snapshot was lost (SW restart) or stale.
        const targetTitle = action.label?.replace(/^Close\s+/i, "") || "";
        if (targetTitle) {
            try {
                const allTabs = await chrome.tabs.query({});
                const match = allTabs.find(t => t.title?.includes(targetTitle));
                if (match?.id) {
                    tabId = match.id;
                    tabUrl = match.url || "";
                    tabTitle = match.title || "";
                }
            } catch {
                // query failed
            }
        }
        if (tabId === -1) {
            return { action_id: aid, success: false, message: v.message || "Tab not found", undo_available: false };
        }
    }

    // LAYER 0: Minimum tab count — never leave fewer than MIN_TABS_TO_KEEP tabs open
    try {
        const currentWindowTabs = await chrome.tabs.query({ currentWindow: true });
        if (currentWindowTabs.length <= MIN_TABS_TO_KEEP) {
            return { action_id: aid, success: false, message: `Only ${currentWindowTabs.length} tabs open — refusing to close (minimum ${MIN_TABS_TO_KEEP})`, undo_available: false };
        }
    } catch { /* query failed — proceed with other guards */ }

    // LAYER 1 (redundant): Final active-tab guard before close
    try {
        const liveTab = await chrome.tabs.get(tabId);
        if (liveTab.active) {
            return { action_id: aid, success: false, message: "Refusing to close the active tab", undo_available: false };
        }
    } catch {
        return { action_id: aid, success: false, message: "Tab already closed", undo_available: false };
    }

    // LAYER 4: Verify expected title if provided
    if (action.metadata?.expected_title) {
        try {
            const liveTab = await chrome.tabs.get(tabId);
            const expected = String(action.metadata.expected_title).toLowerCase();
            const actual = (liveTab.title || "").toLowerCase();
            if (!actual.includes(expected) && !expected.includes(actual)) {
                return { action_id: aid, success: false, message: "Tab title doesn't match expected — skipping", undo_available: false };
            }
        } catch {
            return { action_id: aid, success: false, message: "Tab already closed", undo_available: false };
        }
    }

    try {
        await chrome.tabs.remove(tabId);
    } catch {
        return { action_id: aid, success: false, message: "Failed to close tab", undo_available: false };
    }
    pushUndo({
        action_id: aid,
        action_type: "close_tab",
        undo_data: { url: tabUrl, title: tabTitle },
        timestamp: Date.now(),
    });
    return { action_id: aid, success: true, message: "Tab closed", undo_available: true };
}

async function executeGroupTabs(action: SuggestedAction): Promise<ActionExecuteResult> {
    const meta = action.metadata || {};
    const tabIndices = (meta.tab_indices as number[]) || [];
    if (action.tab_index !== null && action.tab_index !== undefined) {
        tabIndices.push(action.tab_index);
    }
    const tabIds: number[] = [];
    for (const idx of tabIndices) {
        const v = await validateTab(idx);
        if (v.valid) tabIds.push(v.tabId);
    }
    if (tabIds.length === 0) {
        return { action_id: action.action_id, success: false, message: "No valid tabs to group", undo_available: false };
    }
    const groupName = ((action.metadata || {}).group_name as string) || action.label || "Grouped";
    const groupId = await groupSpecificTabs(tabIds, groupName, "blue");
    pushUndo({
        action_id: action.action_id,
        action_type: "group_tabs",
        undo_data: { tabIds, groupId },
        timestamp: Date.now(),
    });
    return { action_id: action.action_id, success: true, message: `${tabIds.length} tabs grouped`, undo_available: true };
}

async function executeBookmarkAndClose(action: SuggestedAction): Promise<ActionExecuteResult> {
    const aid = action.action_id || `bmc_${Date.now()}`;

    // Check if tab closing is disabled by user toggle
    try {
        const { cortex_tab_close_disabled } = await chrome.storage.local.get("cortex_tab_close_disabled");
        if (cortex_tab_close_disabled === true) {
            return { action_id: aid, success: false, message: "Tab closing is disabled", undo_available: false };
        }
    } catch { /* storage read failed — proceed normally */ }
    const tabIndex = typeof action.tab_index === "number" ? action.tab_index : Number(action.tab_index);
    if (isNaN(tabIndex)) {
        return { action_id: aid, success: false, message: "No tab_index", undo_available: false };
    }

    const v = await validateTab(tabIndex);
    let tabId = v.valid ? v.tabId : -1;
    let tabUrl = "";
    let tabTitle = "";

    if (v.valid) {
        const snap = interventionTabSnapshot.get(tabIndex);
        tabUrl = snap?.url || "";
        tabTitle = snap?.title || "";
    } else {
        const targetTitle = action.label?.replace(/^Close\s+/i, "") || "";
        if (targetTitle) {
            try {
                const allTabs = await chrome.tabs.query({});
                const match = allTabs.find(t => t.title?.includes(targetTitle));
                if (match?.id) {
                    tabId = match.id;
                    tabUrl = match.url || "";
                    tabTitle = match.title || "";
                }
            } catch { /* query failed */ }
        }
        if (tabId === -1) {
            return { action_id: aid, success: false, message: v.message || "Tab not found", undo_available: false };
        }
    }

    // LAYER 1: Final active-tab guard
    try {
        const liveTab = await chrome.tabs.get(tabId);
        if (liveTab.active) {
            return { action_id: aid, success: false, message: "Refusing to close the active tab", undo_available: false };
        }
    } catch {
        return { action_id: aid, success: false, message: "Tab already closed", undo_available: false };
    }

    try {
        await chrome.bookmarks.create({ title: tabTitle || "Cortex bookmark", url: tabUrl });
    } catch {
        // Bookmark permission may not be available
    }
    try {
        await chrome.tabs.remove(tabId);
    } catch {
        return { action_id: aid, success: false, message: "Failed to close tab", undo_available: false };
    }
    pushUndo({
        action_id: aid,
        action_type: "bookmark_and_close",
        undo_data: { url: tabUrl, title: tabTitle },
        timestamp: Date.now(),
    });
    return { action_id: aid, success: true, message: "Bookmarked & closed", undo_available: true };
}

async function executeOpenUrl(action: SuggestedAction): Promise<ActionExecuteResult> {
    if (!action.target) {
        return { action_id: action.action_id, success: false, message: "No URL provided", undo_available: false };
    }
    const tab = await chrome.tabs.create({ url: action.target, active: false });
    pushUndo({
        action_id: action.action_id,
        action_type: "open_url",
        undo_data: { tabId: tab.id },
        timestamp: Date.now(),
    });
    return { action_id: action.action_id, success: true, message: "Opened in background", undo_available: true };
}

async function executeSearchError(action: SuggestedAction): Promise<ActionExecuteResult> {
    const query = ((action.metadata || {}).search_query as string) || action.target || "";
    if (!query) {
        return { action_id: action.action_id, success: false, message: "No search query", undo_available: false };
    }
    const url = `https://www.google.com/search?q=${encodeURIComponent(query)}`;
    const tab = await chrome.tabs.create({ url, active: false });
    pushUndo({
        action_id: action.action_id,
        action_type: "search_error",
        undo_data: { tabId: tab.id },
        timestamp: Date.now(),
    });
    return { action_id: action.action_id, success: true, message: "Search opened", undo_available: true };
}

async function executeHighlightTab(action: SuggestedAction): Promise<ActionExecuteResult> {
    if (action.tab_index === null || action.tab_index === undefined) {
        return { action_id: action.action_id, success: false, message: "No tab_index", undo_available: false };
    }
    const v = await validateTab(action.tab_index);
    if (!v.valid) {
        return { action_id: action.action_id, success: false, message: v.message, undo_available: false };
    }
    await chrome.tabs.update(v.tabId, { active: true });
    return { action_id: action.action_id, success: true, message: "Tab activated", undo_available: false };
}

async function executeSaveSession(action: SuggestedAction): Promise<ActionExecuteResult> {
    const name = action.target || `Session ${new Date().toLocaleTimeString()}`;
    await saveTabSession(name, focusSession?.goal);
    return { action_id: action.action_id, success: true, message: "Session saved", undo_available: false };
}

async function executeCopyToClipboard(action: SuggestedAction): Promise<ActionExecuteResult> {
    const text = action.target || ((action.metadata || {}).text as string) || "";
    if (!text) {
        return { action_id: action.action_id, success: false, message: "Nothing to copy", undo_available: false };
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
        return { action_id: action.action_id, success: false, message: "Clipboard access failed", undo_available: false };
    }
    return { action_id: action.action_id, success: true, message: "Copied to clipboard", undo_available: false };
}

async function executeStartTimer(action: SuggestedAction): Promise<ActionExecuteResult> {
    const minutes = ((action.metadata || {}).minutes as number) || 5;
    await chrome.alarms.create("cortex-break-timer", { delayInMinutes: minutes });
    injectToast("Break timer started", `Timer set for ${minutes} minutes. We'll remind you when it's done.`);
    return { action_id: action.action_id, success: true, message: `${minutes}min timer started`, undo_available: false };
}

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
                try {
                    await chrome.tabs.ungroup(tabIds);
                } catch {
                    // Some tabs may be gone
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
    actions: SuggestedAction[],
): Promise<ActionExecuteResult[]> {
    const results: ActionExecuteResult[] = [];
    for (const action of actions) {
        if (action.category === "recommended") {
            results.push(await executeAction(action));
        }
    }

    // Clear the intervention after execution so popup doesn't show stale data
    const hadIntervention = activeIntervention !== null;
    const interventionId = activeIntervention?.intervention_id;
    activeIntervention = null;
    // Persist cleared state
    try { await chrome.storage.session.remove(["cortex_active_intervention", "cortex_tab_snapshot", "cortex_tab_mgr_snapshots"]); } catch {}

    // Notify daemon that user engaged with the intervention
    if (hadIntervention && interventionId) {
        send({
            type: "USER_ACTION",
            payload: {
                action: "engaged",
                intervention_id: interventionId,
                timestamp: Date.now() / 1000,
            },
            timestamp: Date.now() / 1000,
            sequence: ++sequence,
        });
    }

    // Broadcast to popup so it clears the intervention card
    broadcastToPopup({ type: "INTERVENTION_RESTORE", payload: { intervention_id: interventionId } });

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

// --- Health Alerts (Posture & Eye Strain) ---

function checkHealthAlerts(payload: Record<string, unknown>): void {
    const bio = payload.biometrics as Record<string, number | null> | undefined;
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

    // Forward lean → posture
    const lean = bio.forward_lean;
    if (lean !== null && lean !== undefined && lean > 0.6) {
        if (leaningStart === 0) leaningStart = now;
        if (
            now - leaningStart > POSTURE_ALERT_THRESHOLD &&
            now - lastPostureAlert > HEALTH_ALERT_COOLDOWN
        ) {
            lastPostureAlert = now;
            showHealthNotification(
                "Posture check",
                "You've been leaning forward. Sit back, relax your shoulders, and straighten up.",
            );
            leaningStart = 0;
        }
    } else {
        leaningStart = 0;
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
        if (tab?.id && tab.url && !tab.url.startsWith("chrome://")) {
            await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                func: (t: string, b: string) => {
                    const id = "cortex-toast";
                    document.getElementById(id)?.remove();
                    const el = document.createElement("div");
                    el.id = id;
                    el.style.cssText =
                        "position:fixed;top:16px;right:16px;z-index:2147483647;max-width:300px;" +
                        "padding:12px 14px;border-radius:10px;font-family:-apple-system,BlinkMacSystemFont,'Inter','SF Pro Text',system-ui,sans-serif;" +
                        "background:#111113;color:#e4e4e7;border:1px solid rgba(255,255,255,.06);" +
                        "box-shadow:0 4px 20px rgba(0,0,0,.4);animation:cortexSlideIn .25s ease;font-size:12px;line-height:1.5;" +
                        "cursor:pointer;";
                    const style = document.createElement("style");
                    style.textContent = "@keyframes cortexSlideIn{from{transform:translateY(-12px);opacity:0}to{transform:translateY(0);opacity:1}}";
                    const titleEl = document.createElement("div");
                    titleEl.style.cssText = "font-weight:600;margin-bottom:3px;font-size:12px;color:#e4e4e7";
                    titleEl.textContent = t;
                    const bodyEl = document.createElement("div");
                    bodyEl.style.cssText = "color:#71717a;font-size:11px";
                    bodyEl.textContent = b;
                    el.append(style, titleEl, bodyEl);
                    el.addEventListener("click", () => el.remove());
                    document.body.appendChild(el);
                    setTimeout(() => el.remove(), 8000);
                },
                args: [title, body],
            });
        }
    } catch {
        // Injection failed
    }
}

// --- Smart Break Recommendations ---

function checkBreakNeeded(payload: Record<string, unknown>): void {
    if (!focusSession) return;
    const now = Date.now();
    const state = payload.state as string;
    const bio = payload.biometrics as Record<string, number | null> | undefined;

    // Track consecutive stress signals
    if (state === "HYPER") {
        consecutiveStressUpdates++;
    } else {
        consecutiveStressUpdates = Math.max(0, consecutiveStressUpdates - 1);
    }

    // Suggest break if: stressed for 2+ min OR session > 50 min without break
    const sessionMin = (now - focusSession.startTime) / 60000;
    const shouldSuggestBreak =
        (consecutiveStressUpdates > 12 && now - lastBreakSuggestion > 600_000) || // Stressed 2+ min, max every 10 min
        (sessionMin > 50 && now - lastBreakSuggestion > 1800_000); // 50+ min, max every 30 min

    if (shouldSuggestBreak) {
        lastBreakSuggestion = now;
        consecutiveStressUpdates = 0;

        let reason = "You've been working for a while.";
        if (state === "HYPER" && bio?.heart_rate && bio.heart_rate > 85) {
            reason = `Your heart rate is elevated (${Math.round(bio.heart_rate)} BPM). Your body needs a reset.`;
        } else if (sessionMin > 50) {
            reason = `You've been in this session for ${Math.round(sessionMin)} minutes. A short break boosts retention.`;
        }

        injectToast("Time for a break", reason + " Step away for 5 minutes.");
        broadcastToPopup({ type: "BREAK_SUGGESTED", reason });
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

let lastAmbientBroadcast = 0;
const AMBIENT_THROTTLE_MS = 2000; // Send ambient updates every 2s, not every 500ms

async function broadcastToContentScripts(
    message: Record<string, unknown>,
): Promise<void> {
    const now = Date.now();
    if (now - lastAmbientBroadcast < AMBIENT_THROTTLE_MS) return;
    lastAmbientBroadcast = now;

    try {
        const tabs = await chrome.tabs.query({});
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
        _sender: chrome.runtime.MessageSender,
        sendResponse: (response: unknown) => void,
    ) => {
        switch (message.type) {
            case "GET_STATE":
                // If activeIntervention was lost (SW restart), load from session storage
                if (!activeIntervention) {
                    chrome.storage.session.get("cortex_active_intervention", (data) => {
                        const stored = data?.cortex_active_intervention || null;
                        if (stored) activeIntervention = stored;
                        sendResponse({
                            connected,
                            state: currentState,
                            intervention: activeIntervention,
                            focusSession: focusSession ? getFocusSessionSnapshot() : null,
                        });
                    });
                    return true; // async
                }
                sendResponse({
                    connected,
                    state: currentState,
                    intervention: activeIntervention,
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
                activeIntervention = null;
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

                    // Step 1: Send SHUTDOWN over WebSocket (graceful — triggers daemon stop chain)
                    if (ws && connected) {
                        try {
                            send({
                                type: "SHUTDOWN",
                                payload: {},
                                timestamp: Date.now() / 1000,
                                sequence: ++sequence,
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
                        });
                    } catch { /* daemon may already be dead */ }
                    // Step 4: Wait briefly for graceful shutdown to complete
                    await new Promise((r) => setTimeout(r, 1000));
                    // Step 5: Nuclear kill via native messaging — sends SIGTERM to the
                    // daemon process by PID. This is the most reliable kill mechanism
                    // because it works even when HTTP/WebSocket are unresponsive.
                    try {
                        chrome.runtime.sendNativeMessage(
                            "com.cortex.launcher",
                            { command: "stop" },
                            () => { /* ignore response */ }
                        );
                    } catch { /* native messaging may not be available */ }
                    // Step 6: HTTP stop via launcher agent (port 9471) as final backup
                    try {
                        await fetch(`${LAUNCHER_HTTP_URL}/stop`, {
                            method: "POST",
                            signal: AbortSignal.timeout(3000),
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

            case "LAUNCH_CORTEX":
                // Try three launch paths in order:
                // 1. HTTP launcher agent (port 9471) — works if user started launcher manually
                // 2. Native messaging — Chrome invokes native_host.py directly
                // 3. Direct WebSocket — daemon may already be running
                (async () => {
                    let lastError = "";

                    // Helper: wait for WebSocket connection and enable camera
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
                            const resp = await fetch(`${LAUNCHER_HTTP_URL}/launch`, {
                                method: "POST",
                                signal: AbortSignal.timeout(12000),
                            });
                            const data = await resp.json();
                            if (data.status === "starting" || data.status === "already_running") {
                                // Daemon spawned/running — wait for WebSocket
                                if (await waitAndEnableCamera(16)) {
                                    sendResponse({ ok: true, status: "camera_enabled" });
                                    return;
                                }
                                lastError = "Daemon started via launcher but WebSocket not connected";
                            }
                        } catch {
                            // Launcher not running — try next path
                        }

                        // Path 2: Native messaging
                        const nativeResult = await new Promise<Record<string, string>>((resolve) => {
                            try {
                                chrome.runtime.sendNativeMessage(
                                    "com.cortex.launcher",
                                    { command: "launch" },
                                    (response) => {
                                        if (chrome.runtime.lastError) {
                                            resolve({ status: "native_error", error: chrome.runtime.lastError.message || "Native messaging failed" });
                                        } else {
                                            resolve(response || { status: "no_response" });
                                        }
                                    }
                                );
                            } catch {
                                resolve({ status: "native_unavailable", error: "Native messaging not available" });
                            }
                        });

                        if (nativeResult.status === "launched" || nativeResult.status === "already_running") {
                            if (await waitAndEnableCamera(10)) {
                                sendResponse({ ok: true, status: "camera_enabled" });
                                return;
                            }
                            lastError = "Daemon started via native messaging but WebSocket not connected";
                        } else {
                            lastError = nativeResult.error || "Native messaging failed";
                        }

                        // Path 3: Direct WebSocket — daemon may already be running
                        if (await waitAndEnableCamera(4)) {
                            sendResponse({ ok: true, status: "camera_enabled" });
                            return;
                        }

                        // All paths failed
                        sendResponse({
                            ok: false,
                            status: "not_connected",
                            error: lastError || "Could not start daemon. Run in terminal: python -m cortex.scripts.run_dev",
                        });
                    } catch (e) {
                        sendResponse({ ok: false, status: "error", error: String(e) });
                    }
                })();
                return true; // async

            case "USER_ACTION":
                send({
                    type: "USER_ACTION",
                    payload: {
                        action: message.action,
                        intervention_id: message.intervention_id,
                        timestamp: Date.now() / 1000,
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                });
                if (message.action === "dismissed") {
                    const interventionId =
                        typeof message.intervention_id === "string"
                            ? message.intervention_id
                            : typeof activeIntervention?.intervention_id ===
                                "string"
                              ? (activeIntervention.intervention_id as string)
                              : null;

                    // Record dismissal for cooldown
                    const now = Date.now();
                    if (interventionId) {
                        dismissedInterventions.set(interventionId, now);
                        schedulePersist();
                    }
                    // Also record URL-based cooldown from the active tab
                    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
                        if (tab?.url) {
                            try {
                                dismissedUrlPatterns.set(new URL(tab.url).hostname, now);
                                schedulePersist();
                            } catch {}
                        }
                    }).catch(() => {});
                    // Prune old entries
                    for (const [k, t] of dismissedInterventions) {
                        if (now - t > interventionDismissCooldown) dismissedInterventions.delete(k);
                    }
                    for (const [k, t] of dismissedUrlPatterns) {
                        if (now - t > urlDismissCooldown) dismissedUrlPatterns.delete(k);
                    }
                    schedulePersist();

                    activeIntervention = null;
                    try { chrome.storage.session.remove(["cortex_active_intervention", "cortex_tab_snapshot", "cortex_tab_mgr_snapshots"]); } catch {}
                    if (interventionId) {
                        restoreTabsForIntervention(interventionId);
                    } else {
                        restoreAllTabs();
                    }
                }
                sendResponse({ ok: true });
                break;

            case "RESTORE_TABS":
                restoreAllTabs().then(() => sendResponse({ ok: true }));
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
                send({
                    type: "USER_RATING",
                    payload: {
                        intervention_id: message.intervention_id,
                        rating: message.rating,
                    },
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                });
                sendResponse({ ok: true });
                break;

            case "EXECUTE_ACTION":
                executeAction(message.action as SuggestedAction)
                    .then((result) => {
                        sendResponse(result);
                        // Notify daemon
                        send({
                            type: "ACTION_EXECUTE",
                            payload: {
                                intervention_id: message.intervention_id,
                                action_id: (message.action as SuggestedAction).action_id,
                                action_type: (message.action as SuggestedAction).action_type,
                                result,
                            },
                            timestamp: Date.now() / 1000,
                            sequence: ++sequence,
                        });
                    });
                return true; // async

            case "EXECUTE_ALL_RECOMMENDED":
                executeAllRecommended(message.actions as SuggestedAction[])
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
                    });
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
                send({
                    type: "LEETCODE_CONTEXT_UPDATE",
                    payload,
                    timestamp: Date.now() / 1000,
                    sequence: ++sequence,
                });
                sendResponse({ ok: true });
                break;
            }

            case "ACTIVITY_UPDATE": {
                const record = message.record as ActivityRecord;
                if (record?.content_id) {
                    enrichWithRelatedTabs(record).then(() => upsertActivity(record));
                }
                sendResponse({ ok: true });
                break;
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
        }
        return false;
    },
);

// --- LeetCode → Activity Bridge ---
// Bridges leetcode-observer.ts session data into the unified ActivityRecord format

chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.cortex_leetcode_session) return;
    const session = changes.cortex_leetcode_session.newValue;
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
            code_snapshot: session.code_snapshot,
        },
        content_duration_s: 0,
        duration_spent_s: session.time_elapsed_s || 0,
        session_duration_s: session.time_elapsed_s || 0,
        first_visited: (session.saved_at || Date.now()) - (session.time_elapsed_s || 0) * 1000,
        last_visited: session.saved_at || Date.now(),
        context_snapshot: `${session.difficulty || ""} — ${session.tags?.join(", ") || ""}`,
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
    upsertActivity(record);
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
                }).catch(() => {});
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
            }).catch(() => {});
        }
    });
} catch {
    // webNavigation permission may not be available
}

// --- Keepalive alarm (prevents MV3 service worker from going idle) ---

chrome.alarms.create("cortex-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.create("cortex-activity-cleanup", { periodInMinutes: 1440 }); // Daily

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
    }
});

// --- Auto-connect on install/startup ---

chrome.runtime.onInstalled.addListener((details) => {
    chrome.alarms.create("cortex-keepalive", { periodInMinutes: 0.4 });
    connect();
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
    connect();
});

// Start immediately (service worker activation)
connect();
