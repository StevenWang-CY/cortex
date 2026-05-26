/**
 * Lightweight `chrome.*` fakes for vitest jsdom tests.
 *
 * Each fake records its calls on a `.calls` array and supports
 * `.mockReturnValue` / `.mockImplementation` for canned responses.
 * Listeners registered through `*.onMessage.addListener` (or other
 * event APIs) can be flushed manually via the dispatch helpers exposed
 * on this module so tests can simulate inbound traffic deterministically.
 */

import { vi } from "vitest";

export type StorageBucket = Record<string, unknown>;

function makeStorageArea(initial: StorageBucket = {}) {
    let store: StorageBucket = { ...initial };
    return {
        get: vi.fn(
            (
                keys: string | string[] | StorageBucket | null,
                cb?: (items: StorageBucket) => void,
            ) => {
                let result: StorageBucket = {};
                if (keys === null || keys === undefined) {
                    result = { ...store };
                } else if (typeof keys === "string") {
                    if (keys in store) result[keys] = store[keys];
                } else if (Array.isArray(keys)) {
                    for (const k of keys) {
                        if (k in store) result[k] = store[k];
                    }
                } else if (typeof keys === "object") {
                    for (const [k, fallback] of Object.entries(keys)) {
                        result[k] = k in store ? store[k] : fallback;
                    }
                }
                if (cb) cb(result);
                return Promise.resolve(result);
            },
        ),
        set: vi.fn((items: StorageBucket, cb?: () => void) => {
            store = { ...store, ...items };
            if (cb) cb();
            return Promise.resolve();
        }),
        remove: vi.fn((keys: string | string[], cb?: () => void) => {
            const arr = Array.isArray(keys) ? keys : [keys];
            for (const k of arr) delete store[k];
            if (cb) cb();
            return Promise.resolve();
        }),
        // test-only: clear backing store between cases
        __reset: (next: StorageBucket = {}) => {
            store = { ...next };
        },
        __peek: (): StorageBucket => ({ ...store }),
    };
}

function makeEvent<T extends (...args: unknown[]) => unknown>() {
    const listeners = new Set<T>();
    return {
        addListener: vi.fn((fn: T) => {
            listeners.add(fn);
        }),
        removeListener: vi.fn((fn: T) => {
            listeners.delete(fn);
        }),
        hasListener: vi.fn((fn: T) => listeners.has(fn)),
        // test helpers
        __dispatch: (...args: Parameters<T>) => {
            const results: unknown[] = [];
            for (const fn of listeners) {
                results.push(fn(...args));
            }
            return results;
        },
        __listenerCount: (): number => listeners.size,
        __clear: () => listeners.clear(),
    };
}

export interface ChromeFake {
    runtime: {
        sendMessage: ReturnType<typeof vi.fn>;
        sendNativeMessage: ReturnType<typeof vi.fn>;
        onMessage: ReturnType<typeof makeEvent>;
        onSuspend: ReturnType<typeof makeEvent>;
        onInstalled: ReturnType<typeof makeEvent>;
        onStartup: ReturnType<typeof makeEvent>;
        getURL: ReturnType<typeof vi.fn>;
        lastError: { message: string } | undefined;
        id: string;
    };
    storage: {
        local: ReturnType<typeof makeStorageArea>;
        session: ReturnType<typeof makeStorageArea>;
        sync: ReturnType<typeof makeStorageArea>;
        onChanged: ReturnType<typeof makeEvent>;
    };
    tabs: {
        query: ReturnType<typeof vi.fn>;
        get: ReturnType<typeof vi.fn>;
        create: ReturnType<typeof vi.fn>;
        remove: ReturnType<typeof vi.fn>;
        update: ReturnType<typeof vi.fn>;
        ungroup: ReturnType<typeof vi.fn>;
        sendMessage: ReturnType<typeof vi.fn>;
        onRemoved: ReturnType<typeof makeEvent>;
        onUpdated: ReturnType<typeof makeEvent>;
        onActivated: ReturnType<typeof makeEvent>;
    };
    scripting: {
        executeScript: ReturnType<typeof vi.fn>;
    };
    alarms: {
        create: ReturnType<typeof vi.fn>;
        clear: ReturnType<typeof vi.fn>;
        onAlarm: ReturnType<typeof makeEvent>;
    };
    webNavigation: {
        onCommitted: ReturnType<typeof makeEvent>;
        onCompleted: ReturnType<typeof makeEvent>;
        onHistoryStateUpdated: ReturnType<typeof makeEvent>;
    };
    bookmarks: {
        create: ReturnType<typeof vi.fn>;
    };
    tabGroups: {
        update: ReturnType<typeof vi.fn>;
        query: ReturnType<typeof vi.fn>;
    };
    notifications: {
        create: ReturnType<typeof vi.fn>;
        clear: ReturnType<typeof vi.fn>;
        onClicked: ReturnType<typeof makeEvent>;
        onButtonClicked: ReturnType<typeof makeEvent>;
    };
    action: {
        setBadgeText: ReturnType<typeof vi.fn>;
        setBadgeBackgroundColor: ReturnType<typeof vi.fn>;
    };
}

export function buildChromeFake(): ChromeFake {
    return {
        runtime: {
            sendMessage: vi.fn(() => Promise.resolve(undefined)),
            sendNativeMessage: vi.fn(
                (
                    _app: string,
                    _message: unknown,
                    cb?: (resp: unknown) => void,
                ) => {
                    if (cb) cb({ status: "ok" });
                    return Promise.resolve({ status: "ok" });
                },
            ),
            onMessage: makeEvent(),
            onSuspend: makeEvent(),
            onInstalled: makeEvent(),
            onStartup: makeEvent(),
            getURL: vi.fn((path: string) => `chrome-extension://test/${path}`),
            lastError: undefined,
            id: "test-extension-id",
        },
        storage: {
            local: makeStorageArea(),
            session: makeStorageArea(),
            sync: makeStorageArea(),
            onChanged: makeEvent(),
        },
        tabs: {
            query: vi.fn(() => Promise.resolve([])),
            get: vi.fn(() => Promise.resolve({ id: 1, url: "" })),
            create: vi.fn(() => Promise.resolve({ id: 1 })),
            remove: vi.fn(() => Promise.resolve()),
            update: vi.fn(() => Promise.resolve()),
            ungroup: vi.fn(() => Promise.resolve()),
            sendMessage: vi.fn(() => Promise.resolve(undefined)),
            onRemoved: makeEvent(),
            onUpdated: makeEvent(),
            onActivated: makeEvent(),
        },
        scripting: {
            executeScript: vi.fn(() => Promise.resolve([])),
        },
        alarms: {
            create: vi.fn(),
            clear: vi.fn(),
            onAlarm: makeEvent(),
        },
        webNavigation: {
            onCommitted: makeEvent(),
            onCompleted: makeEvent(),
            onHistoryStateUpdated: makeEvent(),
        },
        bookmarks: {
            create: vi.fn(() => Promise.resolve({ id: "bm1" })),
        },
        tabGroups: {
            update: vi.fn(() => Promise.resolve()),
            query: vi.fn(() => Promise.resolve([])),
        },
        notifications: {
            create: vi.fn(),
            clear: vi.fn(),
            onClicked: makeEvent(),
            onButtonClicked: makeEvent(),
        },
        action: {
            setBadgeText: vi.fn(),
            setBadgeBackgroundColor: vi.fn(),
        },
    };
}

export function installChromeFake(): ChromeFake {
    const fake = buildChromeFake();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).chrome = fake;
    return fake;
}

export function resetChromeFake(fake: ChromeFake): void {
    fake.runtime.sendMessage.mockClear();
    fake.runtime.sendNativeMessage.mockClear();
    fake.runtime.onMessage.__clear();
    fake.runtime.onSuspend.__clear();
    fake.runtime.onInstalled.__clear();
    fake.runtime.onStartup.__clear();
    fake.runtime.getURL.mockClear();
    fake.storage.local.__reset();
    fake.storage.session.__reset();
    fake.storage.sync.__reset();
    fake.storage.onChanged.__clear();
    fake.tabs.query.mockClear();
    fake.tabs.get.mockClear();
    fake.tabs.create.mockClear();
    fake.tabs.remove.mockClear();
    fake.tabs.update.mockClear();
    fake.tabs.ungroup.mockClear();
    fake.tabs.sendMessage.mockClear();
    fake.tabs.onRemoved.__clear();
    fake.tabs.onUpdated.__clear();
    fake.tabs.onActivated.__clear();
    fake.scripting.executeScript.mockClear();
    fake.alarms.create.mockClear();
    fake.alarms.clear.mockClear();
    fake.alarms.onAlarm.__clear();
    fake.webNavigation.onCommitted.__clear();
    fake.webNavigation.onCompleted.__clear();
    fake.webNavigation.onHistoryStateUpdated.__clear();
    fake.bookmarks.create.mockClear();
    fake.tabGroups.update.mockClear();
    fake.tabGroups.query.mockClear();
    fake.notifications.create.mockClear();
    fake.notifications.clear.mockClear();
    fake.notifications.onClicked.__clear();
    fake.notifications.onButtonClicked.__clear();
    fake.action.setBadgeText.mockClear();
    fake.action.setBadgeBackgroundColor.mockClear();
}
