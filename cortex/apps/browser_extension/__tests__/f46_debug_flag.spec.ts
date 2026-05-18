/**
 * F46: DEBUG flag from env + runtime override.
 *
 * - When CORTEX_DEBUG=true in the env, DEBUG resolves to true on
 *   module import.
 * - When `chrome.storage.local.cortex_debug` is true at startup,
 *   DEBUG resolves to true.
 * - Flipping `cortex_debug` at runtime updates DEBUG in real time
 *   (via the storage.onChanged listener).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("F46 DEBUG flag", () => {
    const originalEnv = process.env.CORTEX_DEBUG;

    beforeEach(() => {
        vi.resetModules();
        delete process.env.CORTEX_DEBUG;
    });

    afterEach(() => {
        if (originalEnv === undefined) delete process.env.CORTEX_DEBUG;
        else process.env.CORTEX_DEBUG = originalEnv;
    });

    it("defaults to false when no env / no runtime override", async () => {
        const mod = (await import("../background")) as unknown as { _getDebugFlag: () => boolean };
        await new Promise((r) => setTimeout(r, 0));
        expect(mod._getDebugFlag()).toBe(false);
    });

    it("turns on when CORTEX_DEBUG=true is set at import time", async () => {
        process.env.CORTEX_DEBUG = "true";
        const mod = (await import("../background")) as unknown as { _getDebugFlag: () => boolean };
        await new Promise((r) => setTimeout(r, 0));
        expect(mod._getDebugFlag()).toBe(true);
    });

    it("runtime storage override flips on without reload", async () => {
        const fake = globalThis.__cortexChrome;
        const mod = (await import("../background")) as unknown as { _getDebugFlag: () => boolean };
        await new Promise((r) => setTimeout(r, 0));
        expect(mod._getDebugFlag()).toBe(false);

        // Simulate a runtime flip: storage.onChanged fires with cortex_debug=true.
        fake.storage.onChanged.__dispatch(
            { cortex_debug: { newValue: true, oldValue: false } },
            "local",
        );
        expect(mod._getDebugFlag()).toBe(true);

        // Flip off again — falls back to the build-time env (false here).
        fake.storage.onChanged.__dispatch(
            { cortex_debug: { newValue: false, oldValue: true } },
            "local",
        );
        expect(mod._getDebugFlag()).toBe(false);
    });
});
