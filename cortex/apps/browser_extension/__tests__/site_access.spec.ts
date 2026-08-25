import { beforeEach, describe, expect, it, vi } from "vitest";

import {
    getSiteAccessState,
    mayExtractPageContent,
    requestSiteAccess,
    revokeSiteAccess,
} from "../lib/site-access";

describe("optional site access", () => {
    beforeEach(() => {
        globalThis.__cortexChrome.storage.local.__reset();
        globalThis.__cortexChrome.tabs.query.mockResolvedValue([{
            id: 1,
            url: "https://example.com/private?q=secret",
            incognito: false,
        }]);
        globalThis.__cortexChrome.permissions.contains.mockResolvedValue(false);
        globalThis.__cortexChrome.permissions.request.mockResolvedValue(false);
        globalThis.__cortexChrome.permissions.remove.mockResolvedValue(false);
    });

    it("is denied by default and always reports incognito collection off", async () => {
        await expect(getSiteAccessState()).resolves.toEqual({
            available: true,
            granted: false,
            origin: "https://example.com/*",
            incognitoCollection: false,
        });
        expect(globalThis.__cortexChrome.permissions.contains)
            .toHaveBeenCalledWith({ origins: ["https://example.com/*"] });
    });

    it("requests and revokes only the manifest-declared optional origins", async () => {
        globalThis.__cortexChrome.permissions.request.mockResolvedValue(true);
        globalThis.__cortexChrome.permissions.remove.mockResolvedValue(true);
        await expect(requestSiteAccess("https://example.com/*")).resolves.toBe(true);
        await expect(revokeSiteAccess("https://example.com/*")).resolves.toBe(true);
        expect(globalThis.__cortexChrome.permissions.request)
            .toHaveBeenCalledWith({ origins: ["https://example.com/*"] });
        expect(globalThis.__cortexChrome.permissions.remove)
            .toHaveBeenCalledWith({ origins: ["https://example.com/*"] });
    });

    it("never extracts incognito or restricted-scheme pages", async () => {
        globalThis.__cortexChrome.permissions.contains.mockResolvedValue(true);
        await expect(mayExtractPageContent({
            url: "https://example.com/private",
            incognito: true,
        })).resolves.toBe(false);
        await expect(mayExtractPageContent({
            url: "chrome://settings",
            incognito: false,
        })).resolves.toBe(false);
        expect(globalThis.__cortexChrome.permissions.contains).not.toHaveBeenCalled();
    });

    it("checks the exact active origin before page extraction", async () => {
        globalThis.__cortexChrome.permissions.contains.mockResolvedValue(true);
        globalThis.__cortexChrome.storage.local.__reset({
            cortex_page_context_origins: ["https://example.com/*"],
        });
        await expect(mayExtractPageContent({
            url: "https://example.com/private?q=secret",
            incognito: false,
        })).resolves.toBe(true);
        expect(globalThis.__cortexChrome.permissions.contains)
            .toHaveBeenCalledWith({ origins: ["https://example.com/*"] });
    });

    it("does not confuse a required content-script host with explicit consent", async () => {
        globalThis.__cortexChrome.permissions.contains.mockResolvedValue(true);
        await expect(mayExtractPageContent({
            url: "https://example.com/private",
            incognito: false,
        })).resolves.toBe(false);
        expect(globalThis.__cortexChrome.permissions.contains).not.toHaveBeenCalled();
    });

    it("marks restricted and incognito active tabs unavailable", async () => {
        globalThis.__cortexChrome.tabs.query.mockResolvedValueOnce([{
            id: 1,
            url: "chrome://settings",
            incognito: false,
        }]);
        await expect(getSiteAccessState()).resolves.toEqual({
            available: false,
            granted: false,
            origin: null,
            incognitoCollection: false,
        });
        globalThis.__cortexChrome.tabs.query.mockResolvedValueOnce([{
            id: 2,
            url: "https://example.com",
            incognito: true,
        }]);
        await expect(getSiteAccessState()).resolves.toMatchObject({
            available: false,
            granted: false,
            incognitoCollection: false,
        });
    });

    it("fails closed when the browser permissions API rejects", async () => {
        globalThis.__cortexChrome.permissions.contains.mockRejectedValue(
            new Error("permissions unavailable"),
        );
        await expect(mayExtractPageContent({
            url: "https://example.com",
            incognito: false,
        })).resolves.toBe(false);
    });
});
