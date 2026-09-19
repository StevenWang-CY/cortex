/**
 * browser-extension:153 — the two halves of a browser context must describe
 * the same page.
 *
 * `BrowserContextCollector.collect` derived `activeTab` from
 * `chrome.tabs.query({})`, which returns the active tab of EVERY window, so
 * `is_active` was true for N entries and `tabs.find((t) => t.is_active)`
 * resolved to whichever window Chrome happened to enumerate first. The page
 * excerpt on the next lines came from a separate query scoped to the focused
 * window. With more than one window open, the daemon could be sent a title
 * and URL from one window and a page excerpt from another, and the prompt
 * builder would present them as one page.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { BrowserContextCollector } from "../lib/context-collector";

interface FakeTab {
    id: number;
    windowId: number;
    active: boolean;
    title: string;
    url: string;
    incognito?: boolean;
}

function installChrome(
    tabs: FakeTab[],
    focusedWindowId: number,
    excerpt: string,
    consentedOrigins: string[] = ["https://example.com/*"],
) {
    const executed: number[] = [];
    (globalThis as Record<string, unknown>).chrome = {
        storage: {
            local: {
                get: async () => ({
                    cortex_page_context_origins: consentedOrigins,
                }),
                set: async () => undefined,
            },
        },
        tabs: {
            query: async (criteria: Record<string, unknown>) => {
                if (criteria.lastFocusedWindow === true && criteria.active === true) {
                    return tabs.filter(
                        (tab) => tab.active && tab.windowId === focusedWindowId,
                    );
                }
                if (criteria.active === true) {
                    return tabs.filter((tab) => tab.active);
                }
                return tabs;
            },
        },
        scripting: {
            executeScript: async ({ target }: { target: { tabId: number } }) => {
                executed.push(target.tabId);
                return [{ result: excerpt }];
            },
        },
        permissions: { contains: async () => true },
    };
    return executed;
}

afterEach(() => {
    delete (globalThis as Record<string, unknown>).chrome;
    vi.restoreAllMocks();
});

describe("BrowserContextCollector.collect", () => {
    it("reports the focused window's tab, not the first window enumerated", async () => {
        // Window 1 is enumerated first but window 2 has focus. Both have an
        // active tab, so `tabs.query({})` marks both `is_active`.
        const executed = installChrome(
            [
                {
                    id: 11,
                    windowId: 1,
                    active: true,
                    title: "Unfocused window",
                    url: "https://example.com/other-window",
                },
                {
                    id: 12,
                    windowId: 1,
                    active: false,
                    title: "Background",
                    url: "https://example.com/background",
                },
                {
                    id: 21,
                    windowId: 2,
                    active: true,
                    title: "Focused window",
                    url: "https://example.com/focused",
                },
            ],
            2,
            "text from the focused page",
        );

        const context = await new BrowserContextCollector().collect();

        expect(context.activeTab?.tab_id).toBe(21);
        expect(context.activeTab?.title).toBe("Focused window");
        // The excerpt and the metadata are now the same tab, by construction.
        expect(executed).toEqual([21]);
        expect(context.contentExcerpt).toBe("text from the focused page");
    });

    it("reports no active tab when the focused one is not reportable", async () => {
        // An incognito tab is filtered out of the reported set; the context
        // must then carry neither its metadata nor its text.
        const executed = installChrome(
            [
                {
                    id: 11,
                    windowId: 1,
                    active: true,
                    title: "Ordinary window",
                    url: "https://example.com/ordinary",
                },
                {
                    id: 99,
                    windowId: 9,
                    active: true,
                    title: "Private",
                    url: "https://example.com/private",
                    incognito: true,
                },
            ],
            9,
            "private page text",
        );

        const context = await new BrowserContextCollector().collect();

        expect(context.activeTab).toBeUndefined();
        expect(context.contentExcerpt).toBe("");
        expect(executed).toEqual([]);
        expect(context.tabs.map((tab) => tab.tab_id)).toEqual([11]);
    });

    it("degrades to metadata-only when the focused window cannot be resolved", async () => {
        installChrome(
            [
                {
                    id: 11,
                    windowId: 1,
                    active: true,
                    title: "Only window",
                    url: "https://example.com/only",
                },
            ],
            404,  // no window matches
            "unreachable",
        );

        const context = await new BrowserContextCollector().collect();

        expect(context.activeTab).toBeUndefined();
        expect(context.contentExcerpt).toBe("");
        expect(context.tabs).toHaveLength(1);
    });
});
