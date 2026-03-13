/**
 * Cortex Chrome Extension — Background Service Worker
 *
 * Maintains a WebSocket connection to the Cortex daemon (ws://localhost:9473).
 * Receives STATE_UPDATE and INTERVENTION_TRIGGER messages.
 * Dispatches content script injection on intervention triggers.
 * Sends IDENTIFY and USER_ACTION messages to the daemon.
 */

import {
    classifyTabType as classifyBrowserTabType,
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

// --- State ---

let ws: WebSocket | null = null;
let connected = false;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = 3000;
const MAX_RECONNECT_DELAY = 30000;
let intentionalDisconnect = false;
let sequence = 0;

let currentState: CortexState | null = null;
let activeIntervention: Record<string, unknown> | null = null;
let quietMode = false;

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

// Distraction site patterns
const DISTRACTION_PATTERNS = [
    /reddit\.com/i, /twitter\.com/i, /x\.com/i,
    /facebook\.com/i, /instagram\.com/i, /tiktok\.com/i,
    /youtube\.com/i, /netflix\.com/i, /twitch\.tv/i,
    /discord\.com/i, /9gag\.com/i, /buzzfeed\.com/i,
    /tumblr\.com/i,
];

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

// --- WebSocket Connection ---

function connect(): void {
    if (connected || ws) {
        return;
    }
    intentionalDisconnect = false;

    try {
        ws = new WebSocket("ws://localhost:9473");

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

        case "INTERVENTION_TRIGGER":
            activeIntervention = msg.payload;
            // Persist so popup can load it after SW restart
            try { chrome.storage.session.set({ cortex_active_intervention: msg.payload }); } catch {}
            handleIntervention(msg.payload);
            break;

        case "CONTEXT_REQUEST":
            handleContextRequest(msg);
            break;

        case "INTERVENTION_RESTORE":
            handleRestore(msg.payload);
            break;

        case "SETTINGS_SYNC":
            quietMode = Boolean(msg.payload.quiet_mode);
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
                    metadata: {},
                });
            }
        }
    }

    const recommended = actions.filter(a => a.category === "recommended");

    // --- Build tab list ---
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
            for (const t of closeTabs) {
                closingHtml += `<div class="tr"><span class="tx">\u00d7</span><span class="tn">${esc(String(t.tab_title || "Untitled"))}</span></div>`;
            }
            closingHtml += `</div>`;
        }
    }

    // --- Error ---
    let errHtml = "";
    if (errA && errA.root_cause) {
        errHtml = `<div class="eb"><div class="eh">Error</div><div class="et">${esc(errA.root_cause)}</div>`;
        if (errA.suggested_fix) {
            errHtml += `<pre class="ec">${esc(errA.suggested_fix)}</pre>`;
        }
        errHtml += `</div>`;
    }

    // --- Steps ---
    let stepsHtml = "";
    if (steps.length > 0) {
        stepsHtml = `<div class="sl">`;
        for (const s of steps) {
            stepsHtml += `<div class="si">${esc(s)}</div>`;
        }
        stepsHtml += `</div>`;
    }

    // --- CTA label ---
    let ctaLabel = "Clean up";
    if (closeCount > 0) {
        ctaLabel = `Close ${closeCount} tab${closeCount !== 1 ? "s" : ""}`;
    } else if (errA && errA.root_cause) {
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

.bk{position:fixed;inset:0;background:rgba(0,0,0,.35);pointer-events:auto;animation:fadeIn .25s ease}

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
.tn{font-size:12px;color:#71717a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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

    const dismiss = () => {
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

    // CTA
    const ctaEl = shadow.getElementById("cta");
    if (ctaEl) {
        ctaEl.addEventListener("click", () => {
            const toExecute = actions.filter(a => a.category === "recommended");
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

async function handleIntervention(
    payload: Record<string, unknown>,
): Promise<void> {
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
            }
        } catch (e) {
            console.error("Cortex: failed to inject overlay", e);
        }
    }

    // Hide tabs if simplified workspace
    if (payload.hide_targets) {
        const targets = payload.hide_targets as string[];
        const interventionId = payload.intervention_id;
        if (
            targets.includes("browser_tabs_except_active") &&
            typeof interventionId === "string"
        ) {
            await hideTabsForIntervention(interventionId);
        }
    }

    broadcastToPopup({
        type: "INTERVENTION_TRIGGER",
        payload,
    });
}

async function handleContextRequest(msg: WSMessage): Promise<void> {
    try {
        const tabs = await collectTabs();
        // Save this tab list so the intervention snapshot uses the same ordering
        // the LLM will see — prevents tab_index misalignment.
        lastContextTabs = tabs;
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
    const interventionId = payload.intervention_id;
    if (typeof interventionId === "string") {
        await restoreTabsForIntervention(interventionId);
    } else {
        await restoreAllTabs();
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
    } catch {
        // Ignore overlay cleanup failures
    }
    activeIntervention = null;
    try { chrome.storage.session.remove(["cortex_active_intervention", "cortex_tab_snapshot"]); } catch {}
    broadcastToPopup({ type: "INTERVENTION_RESTORE", payload });
}

// --- Tab Management ---

interface TabData {
    title: string;
    url: string;
    tab_type: string;
    is_active: boolean;
    tab_id: number;
}

async function collectTabs(): Promise<TabData[]> {
    const chromeTabs = await chrome.tabs.query({});
    return chromeTabs.map((tab) => ({
        title: tab.title ?? "",
        url: tab.url ?? "",
        tab_type: classifyBrowserTabType(tab.url ?? ""),
        is_active: tab.active ?? false,
        tab_id: tab.id ?? -1,
    }));
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
    broadcastToPopup({ type: "FOCUS_SESSION_STARTED", goal });
}

function stopFocusSession(): FocusSession | null {
    if (!focusSession) return null;
    const session = { ...focusSession };
    // Save to daily stats
    saveToDailyStats(session);
    focusSession = null;
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

function isDistractionUrl(url: string): boolean {
    return DISTRACTION_PATTERNS.some((p) => p.test(url));
}

function injectDistractionInterceptor(
    focusMin: number,
    streakMin: number,
    distractionsBlocked: number,
    url: string,
): void {
    const domain = new URL(url).hostname.replace("www.", "");
    document.body.innerHTML = "";
    document.body.style.cssText =
        "margin:0;padding:0;display:flex;align-items:center;justify-content:center;height:100vh;" +
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
    document.body.appendChild(container);

    document.getElementById("cortex-go-back")?.addEventListener("click", () => {
        // Notify background that user resisted distraction
        try { chrome.runtime.sendMessage({ type: "DISTRACTION_BLOCKED" }); } catch {}
        history.back();
    });
    document.getElementById("cortex-continue")?.addEventListener("click", () => {
        location.reload();
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
const undoStack: UndoEntry[] = [];
const MAX_UNDO_ENTRIES = 50;

/**
 * Snapshot tabs for intervention action resolution.
 * Uses the saved context-time tab list (from the last CONTEXT_RESPONSE) to ensure
 * tab_index values from the LLM align with the actual Chrome tab IDs.
 * Falls back to a fresh query if no saved list exists.
 * Persists to chrome.storage.session for service worker restart resilience.
 */
async function snapshotTabsForIntervention(): Promise<void> {
    interventionTabSnapshot = new Map();
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
    try { await chrome.storage.session.remove(["cortex_active_intervention", "cortex_tab_snapshot"]); } catch {}

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
                    el.innerHTML =
                        `<style>@keyframes cortexSlideIn{from{transform:translateY(-12px);opacity:0}to{transform:translateY(0);opacity:1}}</style>` +
                        `<div style="font-weight:600;margin-bottom:3px;font-size:12px;color:#e4e4e7">${t}</div>` +
                        `<div style="color:#71717a;font-size:11px">${b}</div>`;
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
                    activeIntervention = null;
                    try { chrome.storage.session.remove(["cortex_active_intervention", "cortex_tab_snapshot"]); } catch {}
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
                    .then((results) => sendResponse(results));
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
        }
        return false;
    },
);

// --- Distraction Blocking (tab navigation listener) ---

chrome.tabs.onUpdated.addListener((tabId, changeInfo, _tab) => {
    if (!focusSession || !changeInfo.url) return;
    const url = changeInfo.url;
    if (isDistractionUrl(url)) {
        // Don't increment distractionsBlocked here — only when user clicks "Go back"
        const snap = getFocusSessionSnapshot();
        chrome.scripting.executeScript({
            target: { tabId },
            func: injectDistractionInterceptor,
            args: [
                Math.round((snap?.focusMs ?? 0) / 60000),
                snap?.longestStreakMin ?? 0,
                snap?.distractionsBlocked ?? 0,
                url,
            ],
        }).catch(() => {
            // Injection failed
        });
    }
});

// --- Keepalive alarm (prevents MV3 service worker from going idle) ---

chrome.alarms.create("cortex-keepalive", { periodInMinutes: 0.4 });

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "cortex-keepalive") {
        if (!connected) {
            connect();
        }
    } else if (alarm.name === "cortex-break-timer") {
        injectToast("Break's over!", "Time to get back to work. You've got this.");
        broadcastToPopup({ type: "BREAK_TIMER_DONE" });
    }
});

// --- Auto-connect on install/startup ---

chrome.runtime.onInstalled.addListener(() => {
    chrome.alarms.create("cortex-keepalive", { periodInMinutes: 0.4 });
    connect();
});

chrome.runtime.onStartup.addListener(() => {
    connect();
});

// Start immediately (service worker activation)
connect();
