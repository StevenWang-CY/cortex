import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

describe("manifest privacy posture", () => {
    const testDir = path.dirname(fileURLToPath(import.meta.url));

    it("has no required all-sites host permission", () => {
        const manifestPath = path.resolve(testDir, "../package.json");
        const pkg = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as {
            manifest: {
                host_permissions?: string[];
                optional_host_permissions?: string[];
            };
        };
        expect(pkg.manifest.host_permissions ?? []).not.toContain("<all_urls>");
        expect(pkg.manifest.optional_host_permissions).toEqual([
            "https://*/*",
            "http://*/*",
        ]);
    });

    it("contains no declarative all-sites content script source", () => {
        const contentsDir = path.resolve(testDir, "../contents");
        for (const name of fs.readdirSync(contentsDir)) {
            const source = fs.readFileSync(path.join(contentsDir, name), "utf8");
            expect(source, name).not.toContain('matches: ["<all_urls>"]');
            expect(source, name).not.toContain('matches: ["https://*/*", "http://*/*"]');
        }
    });
});

describe("manifest permissions", () => {
    const testDir = path.dirname(fileURLToPath(import.meta.url));

    it("does not request capabilities with no live consumer", () => {
        const pkg = JSON.parse(fs.readFileSync(path.resolve(testDir, "../package.json"), "utf8")) as {
            manifest: { permissions: string[] };
        };
        expect(pkg.manifest.permissions).not.toContain("bookmarks");
        expect(pkg.manifest.permissions).not.toContain("webNavigation");
        expect(pkg.manifest.permissions).toContain("scripting");
    });

    it("keeps the activity tracker off local dev servers", () => {
        const source = fs.readFileSync(path.resolve(testDir, "../contents/activity-tracker.ts"), "utf8");
        expect(source).not.toContain("http://localhost/*");
    });
});
