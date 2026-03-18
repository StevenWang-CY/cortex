/**
 * Cortex Chrome Extension — Tab Manager
 *
 * Handles tab management for Cortex interventions:
 * - Collect titles/URLs from all open tabs
 * - Classify tabs by type (documentation, stackoverflow, search, etc.)
 * - Temporarily hide/group non-essential tabs
 * - Restore tab visibility after intervention
 *
 * All operations are non-destructive: tabs are grouped and collapsed,
 * never closed or deleted.
 */

// --- Types ---

export interface TabData {
    tabId: number;
    title: string;
    url: string;
    tabType: string;
    isActive: boolean;
    windowId: number;
}

export interface TabSnapshot {
    interventionId: string;
    timestamp: number;
    hiddenTabIds: number[];
    groupId: number | null;
    activeTabId: number | null;
}

// --- Classification ---

const DOC_PATTERNS =
    /docs\.|documentation|\/docs\/|developer\.mozilla|devdocs\.io|readthedocs|sphinx|javadoc|rustdoc|godoc|pkg\.go\.dev|react\.dev|vuejs\.org\/guide|angular\.io\/docs|pytorch\.org\/docs|numpy\.org\/doc|pandas\.pydata\.org\/docs|fastapi\.tiangolo\.com/i;
const PDF_PATTERNS = /(\.pdf(?:$|\?)|arxiv\.org\/pdf\/|openreview\.net\/pdf)/i;
const PAPER_PATTERNS =
    /(arxiv\.org\/abs\/|openreview\.net\/forum|acm\.org\/doi|ieeexplore\.ieee\.org|paperswithcode\.com\/paper)/i;
const REFERENCE_PATTERNS =
    /(wikipedia\.org|scholar\.google\.com|semanticscholar\.org|doi\.org|dblp\.org)/i;
const LEARNING_PLATFORM_PATTERNS =
    /(leetcode\.com|leetcode\.cn|hackerrank\.com|codeforces\.com|codewars\.com|exercism\.org|neetcode\.io|algoexpert\.io|coursera\.org|edx\.org|khanacademy\.org|udemy\.com|brilliant\.org)/i;
const AI_ASSISTANT_PATTERNS =
    /(gemini\.google\.com|chatgpt\.com|chat\.openai\.com|claude\.ai|copilot\.microsoft\.com|perplexity\.ai|phind\.com|you\.com\/chat|poe\.com|bard\.google\.com)/i;
const VIDEO_PLATFORM_PATTERNS =
    /(youtube\.com|youtu\.be|vimeo\.com)/i;
const COMMUNICATION_PATTERNS =
    /(slack\.com|discord\.com|teams\.microsoft\.com)/i;
const SOCIAL_PATTERNS =
    /(twitter\.com|x\.com|reddit\.com|facebook\.com)/i;
const DISTRACTION_PATTERNS =
    /(instagram\.com|tiktok\.com|netflix\.com|twitch\.tv|9gag\.com|buzzfeed\.com|tumblr\.com)/i;

/**
 * Classify a tab by its URL into one of the known categories.
 */
export function classifyTabType(url: string): string {
    const u = url.toLowerCase();

    if (u.includes("stackoverflow.com") || u.includes("stackexchange.com")) {
        return "stackoverflow";
    }
    if (PDF_PATTERNS.test(u)) {
        return "pdf";
    }
    if (PAPER_PATTERNS.test(u)) {
        return "paper";
    }
    if (REFERENCE_PATTERNS.test(u)) {
        return "reference";
    }
    if (DOC_PATTERNS.test(u)) {
        return "documentation";
    }
    if (
        u.includes("google.com/search") ||
        u.includes("bing.com/search") ||
        u.includes("duckduckgo.com")
    ) {
        return "search";
    }
    if (
        u.includes("github.com") ||
        u.includes("gitlab.com") ||
        u.includes("bitbucket.org") ||
        u.includes("codeberg.org")
    ) {
        return "code_host";
    }
    if (LEARNING_PLATFORM_PATTERNS.test(u)) {
        return "learning_platform";
    }
    if (AI_ASSISTANT_PATTERNS.test(u)) {
        return "ai_assistant";
    }
    if (VIDEO_PLATFORM_PATTERNS.test(u)) {
        return "video_platform";
    }
    if (COMMUNICATION_PATTERNS.test(u)) {
        return "communication";
    }
    if (SOCIAL_PATTERNS.test(u)) {
        return "social";
    }
    if (DISTRACTION_PATTERNS.test(u)) {
        return "distraction";
    }
    return "other";
}

/**
 * Goal-aware tab classification. If the tab's title contains keywords from
 * the user's focus goal AND the base type is ambiguous (video, social,
 * communication), reclassify as "goal_relevant" so the LLM never recommends
 * closing it.
 */
export function classifyTabTypeWithGoal(
    url: string,
    title: string,
    goalKeywords: string[],
): string {
    const baseType = classifyTabType(url);
    if (goalKeywords.length === 0) return baseType;

    // Reclassify ambiguous types + AI assistants when they match the goal.
    // AI assistants are tools that could be used for ANY topic — if the user's
    // goal keywords appear in the title, the assistant is actively helping with
    // the goal and should get full goal_relevant protection.
    const ambiguousTypes = new Set([
        "video_platform", "social", "communication", "distraction", "other",
        "ai_assistant",
    ]);
    if (!ambiguousTypes.has(baseType)) return baseType;

    const titleLower = title.toLowerCase();
    for (const kw of goalKeywords) {
        if (titleLower.includes(kw)) {
            return "goal_relevant";
        }
    }
    return baseType;
}

/**
 * Compute tab type classification counts from a list of tabs.
 */
export function computeTypeClassification(
    tabs: TabData[],
): Record<string, number> {
    const counts: Record<string, number> = {};
    for (const tab of tabs) {
        counts[tab.tabType] = (counts[tab.tabType] ?? 0) + 1;
    }
    return counts;
}

// --- Collection ---

/**
 * Collect information about all open tabs across all windows.
 */
export async function collectAllTabs(): Promise<TabData[]> {
    const chromeTabs = await chrome.tabs.query({});
    return chromeTabs.map((tab) => ({
        tabId: tab.id ?? -1,
        title: tab.title ?? "",
        url: tab.url ?? "",
        tabType: classifyTabType(tab.url ?? ""),
        isActive: tab.active ?? false,
        windowId: tab.windowId ?? -1,
    }));
}

/**
 * Get tabs in the current window only.
 */
export async function collectCurrentWindowTabs(): Promise<TabData[]> {
    const chromeTabs = await chrome.tabs.query({ currentWindow: true });
    return chromeTabs.map((tab) => ({
        tabId: tab.id ?? -1,
        title: tab.title ?? "",
        url: tab.url ?? "",
        tabType: classifyTabType(tab.url ?? ""),
        isActive: tab.active ?? false,
        windowId: tab.windowId ?? -1,
    }));
}

// --- Hide/Show ---

/** Active snapshots for restoration. */
const snapshots: Map<string, TabSnapshot> = new Map();

// --- Snapshot Persistence (survives MV3 service worker restarts) ---

let snapshotPersistTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleSnapshotPersist(): void {
    if (snapshotPersistTimer) clearTimeout(snapshotPersistTimer);
    snapshotPersistTimer = setTimeout(async () => {
        try {
            await chrome.storage.session.set({
                cortex_tab_mgr_snapshots: [...snapshots.entries()],
            });
        } catch {
            // storage.session may not be available
        }
    }, 500);
}

async function restoreSnapshots(): Promise<void> {
    try {
        const data = await chrome.storage.session.get("cortex_tab_mgr_snapshots");
        if (data.cortex_tab_mgr_snapshots) {
            snapshots.clear();
            for (const [k, v] of data.cortex_tab_mgr_snapshots) {
                snapshots.set(k, v);
            }
        }
    } catch {
        // storage.session not available
    }
}

// Restore snapshots on module init
restoreSnapshots();

/**
 * Hide non-active tabs in the current window by grouping and collapsing them.
 * Excludes protected tabs (goal-relevant, AI assistants, recently-active, etc.)
 *
 * @param interventionId - ID for tracking this hide operation.
 * @param protectedTabIds - Set of tab IDs that should NOT be hidden.
 * @returns The snapshot for later restoration, or null on failure.
 */
export async function hideNonActiveTabs(
    interventionId: string,
    protectedTabIds?: Set<number>,
): Promise<TabSnapshot | null> {
    try {
        const tabs = await collectCurrentWindowTabs();
        const activeTab = tabs.find((t) => t.isActive);
        if (!activeTab) return null;

        // Safe types that should never be hidden — they are tools the user is actively using
        const safeTypes = new Set([
            "ai_assistant", "learning_platform", "documentation",
            "reference", "code_host", "stackoverflow",
        ]);

        const toHide = tabs
            .filter((t) => {
                if (t.isActive || t.tabId === -1) return false;
                // Don't hide tabs with safe types
                if (safeTypes.has(t.tabType)) return false;
                // Don't hide explicitly protected tabs
                if (protectedTabIds?.has(t.tabId)) return false;
                return true;
            })
            .map((t) => t.tabId);

        if (toHide.length === 0) return null;

        let groupId: number | null = null;
        try {
            groupId = await chrome.tabs.group({ tabIds: toHide });
            await chrome.tabGroups.update(groupId, {
                collapsed: true,
                title: "Cortex: Hidden",
                color: "grey",
            });
        } catch {
            // tabGroups API may not be available
        }

        const snapshot: TabSnapshot = {
            interventionId,
            timestamp: Date.now(),
            hiddenTabIds: toHide,
            groupId,
            activeTabId: activeTab.tabId,
        };

        snapshots.set(interventionId, snapshot);
        scheduleSnapshotPersist();
        return snapshot;
    } catch {
        return null;
    }
}

/**
 * Restore tabs that were hidden by a Cortex intervention.
 *
 * @param interventionId - ID of the intervention to restore.
 * @returns True if restoration succeeded, false otherwise.
 */
export async function restoreHiddenTabs(
    interventionId: string,
): Promise<boolean> {
    const snapshot = snapshots.get(interventionId);
    if (!snapshot) return false;

    try {
        // Ungroup the tabs (restores them to normal state)
        if (snapshot.hiddenTabIds.length > 0) {
            try {
                await chrome.tabs.ungroup(snapshot.hiddenTabIds);
            } catch {
                // Some tabs may have been closed by the user
            }
        }

        snapshots.delete(interventionId);
        scheduleSnapshotPersist();
        return true;
    } catch {
        snapshots.delete(interventionId);
        scheduleSnapshotPersist();
        return false;
    }
}

/**
 * Restore all hidden tabs from all active interventions.
 */
export async function restoreAllTabs(): Promise<void> {
    for (const [interventionId] of snapshots) {
        await restoreHiddenTabs(interventionId);
    }
}

/**
 * Get the snapshot for an intervention.
 */
export function getSnapshot(interventionId: string): TabSnapshot | null {
    return snapshots.get(interventionId) ?? null;
}

/**
 * Check if there are any active tab hiding operations.
 */
export function hasActiveHiding(): boolean {
    return snapshots.size > 0;
}

// --- Targeted tab operations ---

/**
 * Group specific tabs by their IDs into a named, collapsed group.
 */
export async function groupSpecificTabs(
    tabIds: number[],
    groupName: string,
    color: chrome.tabGroups.ColorEnum = "blue",
): Promise<number | null> {
    if (tabIds.length === 0) return null;
    try {
        const groupId = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(groupId, {
            collapsed: true,
            title: groupName,
            color,
        });
        return groupId;
    } catch {
        return null;
    }
}

// --- Session management ---

export interface TabSession {
    name: string;
    tabs: { title: string; url: string }[];
    savedAt: number;
    goal?: string;
}

const MAX_SAVED_SESSIONS = 20;

/**
 * Save current tab set as a named session.
 */
export async function saveTabSession(
    sessionName: string,
    goal?: string,
): Promise<void> {
    const tabs = await collectAllTabs();
    const session: TabSession = {
        name: sessionName,
        tabs: tabs.map((t) => ({ title: t.title, url: t.url })),
        savedAt: Date.now(),
        goal,
    };
    const result = await chrome.storage.local.get("cortex_sessions");
    const sessions: TabSession[] = (result.cortex_sessions as TabSession[]) || [];
    sessions.push(session);
    // Keep last N sessions
    if (sessions.length > MAX_SAVED_SESSIONS) {
        sessions.splice(0, sessions.length - MAX_SAVED_SESSIONS);
    }
    await chrome.storage.local.set({ cortex_sessions: sessions });
}

/**
 * Restore a saved tab session by opening all its tabs.
 */
export async function restoreTabSession(
    sessionName: string,
): Promise<boolean> {
    const result = await chrome.storage.local.get("cortex_sessions");
    const sessions: TabSession[] = (result.cortex_sessions as TabSession[]) || [];
    const session = sessions.find((s) => s.name === sessionName);
    if (!session) return false;
    for (const tab of session.tabs) {
        await chrome.tabs.create({ url: tab.url, active: false });
    }
    return true;
}
