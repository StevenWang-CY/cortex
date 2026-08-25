import { describe, expect, it } from "vitest";

import {
    minimizeContextUrl,
    sanitizeContextText,
} from "../lib/context-privacy";

describe("browser context privacy", () => {
    it("strips URL userinfo, path, query, and fragment", () => {
        expect(minimizeContextUrl(
            "https://alice:hunter2@example.com/private?q=token#secret",
        )).toBe("https://example.com");
        expect(minimizeContextUrl("javascript:alert(1)"))
            .toBe("[URL OMITTED]");
    });

    it.each([
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "sk-ant-abcdefghijklmnop123456",
        "eyJabcdefgh.ijklmnop.qrstuvwx",
        "password=correct-horse-battery-staple",
        "https://alice:hunter2@example.com/path",
    ])("redacts secret corpus member %s", (secret) => {
        const result = sanitizeContextText(`prefix ${secret} suffix`, 5_000);
        expect(result.value).not.toContain(secret);
        expect(result.value).toContain("[REDACTED:");
        expect(result.redactionCount).toBeGreaterThan(0);
    });

    it("minimizes paths, preserves ordinary Unicode, and removes bidi controls", () => {
        const result = sanitizeContextText(
            "Ｆｏｏ 日本語 \u202e /Users/alice/private/project/main.ts",
            500,
        );
        expect(result.value).toContain("Foo 日本語");
        expect(result.value).not.toContain("\u202e");
        expect(result.value).not.toContain("/Users/alice");
        expect(result.value).toContain("…/main.ts");
    });

    it("bounds CPU input and output for hostile megabyte snippets", () => {
        const started = performance.now();
        const result = sanitizeContextText("x".repeat(1_000_000), 2_000);
        expect(result.value).toHaveLength(2_000);
        expect(performance.now() - started).toBeLessThan(250);
    });
});

