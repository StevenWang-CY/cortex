/** Privacy-preserving browser context collection boundary. */

import {
    classifyTabType,
    classifyTabTypeWithGoal,
} from "../tab-manager";
import {
    minimizeContextUrl,
    PAGE_EXCERPT_MAX_CHARS,
    sanitizeContextText,
    TAB_TITLE_MAX_CHARS,
} from "./context-privacy";
import { extractGoalKeywords } from "./focus-session";
import { mayExtractPageContent } from "./site-access";

export interface TabData {
    title: string;
    url: string;
    tab_type: string;
    is_active: boolean;
    tab_id: number;
    topic_hint: string;
    last_activated_ago_seconds: number | null;
}

export interface CollectedBrowserContext {
    tabs: TabData[];
    activeTab: TabData | undefined;
    contentExcerpt: string;
}

export interface TabCollectionOptions {
    focusGoal?: string | null;
    lastActivated?: ReadonlyMap<number, number>;
    now?: number;
}

export function extractTopicHint(
    title: string,
    url: string,
    tabType: string,
): string {
    if (tabType === "ai_assistant") {
        return title.replace(
            /\s*[-–—]\s*(Gemini|ChatGPT|Claude|Copilot|Perplexity|Phind|Poe).*$/i,
            "",
        ).slice(0, 100);
    }
    if (tabType === "video" || tabType === "video_platform") {
        return title.replace(/\s*[-–—]\s*(YouTube|Vimeo).*$/i, "").slice(0, 100);
    }
    if (tabType === "reference" || tabType === "search") {
        try {
            return new URL(url).searchParams.get("q")?.slice(0, 100) || "";
        } catch {
            return "";
        }
    }
    if (tabType === "social" || tabType === "communication") {
        return title.replace(
            /\s*[-–—]\s*(Slack|Discord|Microsoft Teams).*$/i,
            "",
        ).slice(0, 100);
    }
    return "";
}

/** Pure normalizer: drops incognito tabs before any content leaves Chrome. */
export function normalizeTabs(
    tabs: readonly chrome.tabs.Tab[],
    options: TabCollectionOptions = {},
): TabData[] {
    const goalKeywords = options.focusGoal
        ? extractGoalKeywords(options.focusGoal)
        : [];
    const now = options.now ?? Date.now();
    const lastActivated = options.lastActivated ?? new Map<number, number>();

    return tabs.filter((tab) => !tab.incognito).map((tab) => {
        const rawTitle = tab.title ?? "";
        const rawUrl = tab.url ?? "";
        const tabType = goalKeywords.length > 0
            ? classifyTabTypeWithGoal(rawUrl, rawTitle, goalKeywords)
            : classifyTabType(rawUrl);
        const tabId = tab.id ?? -1;
        const lastActive = lastActivated.get(tabId);
        return {
            title: sanitizeContextText(rawTitle, TAB_TITLE_MAX_CHARS).value,
            url: minimizeContextUrl(rawUrl),
            tab_type: tabType,
            is_active: tab.active ?? false,
            tab_id: tabId,
            topic_hint: sanitizeContextText(
                extractTopicHint(rawTitle, rawUrl, tabType),
                120,
            ).value,
            last_activated_ago_seconds: lastActive === undefined
                ? null
                : Math.max(0, Math.floor((now - lastActive) / 1_000)),
        };
    });
}

/**
 * Executed inside the inspected page. Keep this function self-contained:
 * Chrome serializes the function body and cannot preserve module closures.
 */
export function extractVisiblePageText(): string {
    const maximumCharacters = 2_000;
    const walker = document.createTreeWalker(
        document.body,
        NodeFilter.SHOW_TEXT,
        {
            acceptNode(node: Text): number {
                const parent = node.parentElement;
                if (!parent) return NodeFilter.FILTER_REJECT;
                const tag = parent.tagName.toLowerCase();
                if (["script", "style", "noscript", "svg", "path"].includes(tag)) {
                    return NodeFilter.FILTER_REJECT;
                }
                if (parent.offsetWidth === 0 && parent.offsetHeight === 0) {
                    return NodeFilter.FILTER_REJECT;
                }
                const text = node.textContent?.trim();
                return text && text.length >= 2
                    ? NodeFilter.FILTER_ACCEPT
                    : NodeFilter.FILTER_REJECT;
            },
        },
    );

    const chunks: string[] = [];
    let length = 0;
    let node: Text | null;
    while ((node = walker.nextNode() as Text | null)) {
        const value = node.textContent?.trim();
        if (!value) continue;
        const remaining = maximumCharacters - length;
        if (value.length > remaining) {
            if (remaining > 0) chunks.push(value.slice(0, remaining));
            break;
        }
        chunks.push(value);
        length += value.length;
    }
    return chunks.join(" ");
}

export class BrowserContextCollector {
    async collect(options: TabCollectionOptions = {}): Promise<CollectedBrowserContext> {
        const rawTabs = await chrome.tabs.query({});
        const tabs = normalizeTabs(rawTabs, options);

        // Resolve the focused tab ONCE and use it for both halves of the
        // context. `chrome.tabs.query({})` returns the active tab of EVERY
        // window, so `is_active` is true for N entries and
        // `tabs.find((t) => t.is_active)` picked whichever window Chrome
        // happened to enumerate first — not the focused one. The excerpt was
        // then taken from a separate, correctly-scoped query, so with more
        // than one window open the daemon could be sent a title and URL from
        // one window and a page excerpt from another, and the prompt builder
        // would describe them as the same page.
        //
        // `lastFocusedWindow` rather than `currentWindow`: this runs in a
        // service worker, which has no window of its own, so `currentWindow`
        // is only defined by fallback.
        let focusedTab: chrome.tabs.Tab | undefined;
        try {
            [focusedTab] = await chrome.tabs.query({
                active: true,
                lastFocusedWindow: true,
            });
        } catch {
            focusedTab = undefined;
        }
        const focusedTabId = focusedTab?.id;
        // An incognito focused tab is filtered out of `tabs` by
        // `normalizeTabs`, so it correctly resolves to no active tab and no
        // excerpt rather than reporting a tab we are not allowed to report.
        const activeTab = focusedTabId === undefined
            ? undefined
            : tabs.find((tab) => tab.tab_id === focusedTabId);
        let contentExcerpt = "";

        if (activeTab && focusedTab && focusedTabId !== undefined) {
            try {
                if (await mayExtractPageContent(focusedTab)) {
                    const results = await chrome.scripting.executeScript({
                        target: { tabId: focusedTabId },
                        func: extractVisiblePageText,
                    });
                    if (results?.[0]?.result) {
                        contentExcerpt = sanitizeContextText(
                            String(results[0].result),
                            PAGE_EXCERPT_MAX_CHARS,
                        ).value;
                    }
                }
            } catch {
                // Missing host access, restricted pages, and tab closure are
                // expected outcomes. Metadata-only context remains useful.
            }
        }

        return { tabs, activeTab, contentExcerpt };
    }
}
