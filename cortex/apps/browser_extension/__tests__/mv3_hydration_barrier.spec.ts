/**
 * Waking the service worker must not destroy the state it exists to preserve.
 *
 * MV3 suspends the worker aggressively, and `chrome.tabs.onActivated` /
 * `onRemoved` wake it and call `schedulePersist()` directly. That wrote
 * `persistedSessionSnapshot()` — built from module state `restoreState()` had
 * not filled yet, so an EMPTY focus session and undo stack — straight over
 * `chrome.storage.session`. `handleMessage` held the only hydration barrier in
 * the file.
 *
 * Reproducing it needs the hydrating read to still be in flight when the tab
 * event arrives, which is the real ordering on a cold wake but not what the
 * in-memory fake does by default (its `get` settles in a microtask, before the
 * listener can fire). The read is therefore held open explicitly.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const STORED_FOCUS = { goal: "ship the audit", startedAt: 1_700_000_000_000 };

async function settle(ticks = 20): Promise<void> {
    for (let i = 0; i < ticks; i += 1) await new Promise((r) => setTimeout(r, 0));
}

/** Hold the first `storage.session.get` open until the returned release runs. */
function holdHydrationRead(): () => void {
    const fake = globalThis.__cortexChrome;
    const real = fake.storage.session.get.bind(fake.storage.session);
    let release = (): void => undefined;
    const gate = new Promise<void>((resolve) => {
        release = () => resolve();
    });
    // Every session read is held, not just the first: `restoreState` is not
    // guaranteed to be the first caller, and gating the wrong read would let
    // hydration complete and make the assertion below vacuous.
    type SessionGet = typeof fake.storage.session.get;
    fake.storage.session.get = vi.fn(
        async (...args: Parameters<SessionGet>) => {
            await gate;
            return real(...args);
        },
    ) as unknown as SessionGet;
    return release;
}

describe("MV3 hydration barrier", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("a tab event during boot does not overwrite the stored session", async () => {
        const fake = globalThis.__cortexChrome;
        // A previous worker generation left a focus session behind.
        await fake.storage.session.set({ focusSession: STORED_FOCUS, undoStack: [] });
        fake.storage.session.set.mockClear();

        const releaseHydration = holdHydrationRead();
        await import("../background");

        const activated = fake.tabs.onActivated.addListener.mock.calls[0]?.[0] as
            | ((info: { tabId: number }) => void)
            | undefined;
        expect(activated, "onActivated listener must be registered").toBeDefined();

        // The worker is awake and hydration is still in flight.
        activated!({ tabId: 42 });
        // Past the store's 500 ms debounce: the unguarded code writes here.
        await new Promise((r) => setTimeout(r, 700));

        // Assert BEFORE releasing hydration. This is the precise contract: a
        // wake that arrives before the state is loaded must not write at all.
        // Checking only the end state hides the defect, because hydration
        // finishes and the next persist puts the restored value back — the
        // real loss happens when the worker suspends in between, which a test
        // cannot force.
        expect(
            fake.storage.session.set,
            "no session write may happen before hydration",
        ).not.toHaveBeenCalled();

        releaseHydration();
        await settle();
        await new Promise((r) => setTimeout(r, 700));
        await settle();

        // Deferred, not dropped: the restored state is still there afterwards.
        const stored = await fake.storage.session.get(["focusSession"]);
        expect(stored.focusSession).toEqual(STORED_FOCUS);
    });

    it("the deferred write still lands once hydration completes", async () => {
        const fake = globalThis.__cortexChrome;
        await fake.storage.session.set({ focusSession: STORED_FOCUS, undoStack: [] });

        const releaseHydration = holdHydrationRead();
        await import("../background");

        const removed = fake.tabs.onRemoved.addListener.mock.calls[0]?.[0] as
            | ((tabId: number) => void)
            | undefined;
        expect(removed, "onRemoved listener must be registered").toBeDefined();
        removed!(42);

        releaseHydration();
        await settle();
        await new Promise((r) => setTimeout(r, 700));
        await settle();

        // Deferred, not dropped: the snapshot carries the restored state.
        const stored = await fake.storage.session.get(["focusSession"]);
        expect(stored.focusSession).toEqual(STORED_FOCUS);
    });
});
