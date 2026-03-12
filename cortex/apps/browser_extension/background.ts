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
    hideNonActiveTabs as hideTabsForIntervention,
    restoreAllTabs,
    restoreHiddenTabs as restoreTabsForIntervention,
} from "./tab-manager";

// --- Types ---

interface WSMessage {
    type: string;
    payload: Record<string, unknown>;
    timestamp: number;
    sequence: number;
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

// --- Message Handling ---

function handleMessage(raw: string): void {
    let msg: WSMessage;
    try {
        msg = JSON.parse(raw) as WSMessage;
    } catch {
        return;
    }

    switch (msg.type) {
        case "STATE_UPDATE":
            currentState = msg.payload as unknown as CortexState;
            broadcastToPopup({
                type: "STATE_UPDATE",
                payload: msg.payload,
            });
            break;

        case "INTERVENTION_TRIGGER":
            activeIntervention = msg.payload;
            handleIntervention(msg.payload);
            break;

        case "CONTEXT_REQUEST":
            handleContextRequest(msg);
            break;
    }
}

async function handleIntervention(
    payload: Record<string, unknown>,
): Promise<void> {
    const uiPlan = payload.ui_plan as Record<string, boolean> | undefined;

    // Inject content script into active tab for overlay/dimming
    if (uiPlan?.show_overlay || uiPlan?.dim_background) {
        try {
            const [tab] = await chrome.tabs.query({
                active: true,
                currentWindow: true,
            });
            if (tab?.id) {
                await chrome.scripting.executeScript({
                    target: { tabId: tab.id },
                    files: ["content.tsx"],
                });
                // Send intervention data to content script
                await chrome.tabs.sendMessage(tab.id, {
                    type: "SHOW_INTERVENTION",
                    payload,
                });
            }
        } catch {
            // Tab may not be injectable (chrome://, etc.)
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
                active_tab_title: activeTab?.title ?? "",
                active_tab_url: activeTab?.url ?? "",
                active_tab_content_excerpt: contentExcerpt,
                all_tabs: tabs,
            },
            timestamp: Date.now() / 1000,
            sequence: msg.sequence,
        });
    } catch {
        send({
            type: "CONTEXT_RESPONSE",
            payload: { error: "context_gather_failed" },
            timestamp: Date.now() / 1000,
            sequence: msg.sequence,
        });
    }
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

// --- Message Listener (from popup and content scripts) ---

chrome.runtime.onMessage.addListener(
    (
        message: Record<string, unknown>,
        _sender: chrome.runtime.MessageSender,
        sendResponse: (response: unknown) => void,
    ) => {
        switch (message.type) {
            case "GET_STATE":
                sendResponse({
                    connected,
                    state: currentState,
                    intervention: activeIntervention,
                });
                break;

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
                    activeIntervention = null;
                    const interventionId =
                        typeof message.intervention_id === "string"
                            ? message.intervention_id
                            : typeof activeIntervention?.intervention_id ===
                                "string"
                              ? (activeIntervention.intervention_id as string)
                              : null;
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
        }
        return false;
    },
);

// --- Auto-connect on install/startup ---

chrome.runtime.onInstalled.addListener(() => {
    connect();
});

chrome.runtime.onStartup.addListener(() => {
    connect();
});

// Start immediately (service worker activation)
connect();
