/**
 * Phase 4d Task C — SuggestedAction.tab_index strict typing.
 *
 * Pydantic's schema is ``int | None``. A non-integer payload should be
 * dropped at the extension boundary rather than coerced via Number().
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

async function bootBackground(): Promise<void> {
    vi.resetModules();
    await import("../background");
    await new Promise((r) => setTimeout(r, 0));
}

describe("Phase 4d Task C — strict tab_index coercion", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("drops a close_tab action whose tab_index is a string", async () => {
        await bootBackground();
        const fake = globalThis.__cortexChrome;
        const tabsBeforeRemove = fake.tabs.remove.mock.calls.length;

        // Synthesise an EXECUTE_ACTION inbound message — same shape the
        // popup would send for a click on the "Close tab" CTA.
        const responses: unknown[] = [];
        const sendResponse = (r: unknown) => responses.push(r);
        // The background's onMessage listener iterates registered fns
        // via the chrome mock's __dispatch helper.
        fake.runtime.onMessage.__dispatch(
            {
                type: "EXECUTE_ACTION",
                action: {
                    action_id: "test_a1",
                    action_type: "close_tab",
                    // Intentionally wrong type — the daemon's Pydantic
                    // schema would reject this.
                    tab_index: "3" as unknown as number,
                    target: "",
                    label: "Close",
                    reason: "",
                    category: "recommended",
                    reversible: true,
                    metadata: {},
                },
            },
            { id: "test" },
            sendResponse,
        );

        // Allow the async executor to resolve.
        await new Promise((r) => setTimeout(r, 100));

        // chrome.tabs.remove should NOT have been called — the action
        // was rejected on the strict-type check.
        expect(fake.tabs.remove.mock.calls.length).toBe(tabsBeforeRemove);
    });

    it("drops a close_tab action whose tab_index is negative", async () => {
        await bootBackground();
        const fake = globalThis.__cortexChrome;
        const tabsBeforeRemove = fake.tabs.remove.mock.calls.length;

        const sendResponse = vi.fn();
        fake.runtime.onMessage.__dispatch(
            {
                type: "EXECUTE_ACTION",
                action: {
                    action_id: "test_a2",
                    action_type: "close_tab",
                    tab_index: -1,
                    target: "",
                    label: "Close",
                    reason: "",
                    category: "recommended",
                    reversible: true,
                    metadata: {},
                },
            },
            { id: "test" },
            sendResponse,
        );
        await new Promise((r) => setTimeout(r, 100));
        expect(fake.tabs.remove.mock.calls.length).toBe(tabsBeforeRemove);
    });

    it("drops a bookmark_and_close whose tab_index is non-integer", async () => {
        await bootBackground();
        const fake = globalThis.__cortexChrome;
        const removeBefore = fake.tabs.remove.mock.calls.length;
        const bookmarkBefore = fake.bookmarks.create.mock.calls.length;

        fake.runtime.onMessage.__dispatch(
            {
                type: "EXECUTE_ACTION",
                action: {
                    action_id: "test_a3",
                    action_type: "bookmark_and_close",
                    tab_index: 2.5 as unknown as number,
                    target: "",
                    label: "Bookmark",
                    reason: "",
                    category: "recommended",
                    reversible: true,
                    metadata: {},
                },
            },
            { id: "test" },
            vi.fn(),
        );
        await new Promise((r) => setTimeout(r, 100));
        expect(fake.tabs.remove.mock.calls.length).toBe(removeBefore);
        expect(fake.bookmarks.create.mock.calls.length).toBe(bookmarkBefore);
    });
});
