/**
 * Content-sharing switches must be the user's decision, not a workspace's.
 *
 * `cortex.shareEditorContent` defaults to ON and sends the visible code of the
 * active file to the daemon. It and `cortex.shareUntitledDocuments` were
 * declared with VS Code's default (window) scope and were absent from
 * `capabilities.untrustedWorkspaces.restrictedConfigurations`, so a repository
 * could raise either of them from its own `.vscode/settings.json` — including
 * in a workspace the user had explicitly marked untrusted. `cortex.daemonUrl`
 * was the only setting protected that way.
 *
 * Machine scope means no workspace can override the value at all; listing it
 * as restricted means an untrusted workspace cannot apply it even where scope
 * would otherwise allow. Both are asserted, because they fail independently.
 */

import * as fs from "fs";
import * as path from "path";

interface ConfigurationProperty {
    type?: string;
    scope?: string;
    default?: unknown;
    description?: string;
}

interface PackageManifest {
    capabilities?: {
        untrustedWorkspaces?: {
            supported?: string;
            restrictedConfigurations?: string[];
        };
    };
    contributes?: {
        configuration?: {
            properties?: Record<string, ConfigurationProperty>;
        };
    };
}

// Every setting that governs what leaves the machine, or where it goes.
const PRIVACY_SETTINGS = [
    "cortex.daemonUrl",
    "cortex.shareEditorContent",
    "cortex.shareUntitledDocuments",
];

function manifest(): PackageManifest {
    const file = path.join(__dirname, "..", "..", "package.json");
    return JSON.parse(fs.readFileSync(file, "utf8")) as PackageManifest;
}

describe("privacy-relevant configuration scope", () => {
    it.each(PRIVACY_SETTINGS)("%s is machine-scoped", (name) => {
        const properties = manifest().contributes?.configuration?.properties ?? {};
        const property = properties[name];
        expect(property).toBeDefined();
        expect(property.scope).toBe("machine");
    });

    it.each(PRIVACY_SETTINGS)("%s is restricted in untrusted workspaces", (name) => {
        const restricted =
            manifest().capabilities?.untrustedWorkspaces?.restrictedConfigurations ?? [];
        expect(restricted).toContain(name);
    });

    it("keeps untrusted-workspace support limited rather than full", () => {
        // "supported": true would drop the restriction list entirely.
        expect(manifest().capabilities?.untrustedWorkspaces?.supported).toBe("limited");
    });

    it("shares editor content by default, which is why the scope matters", () => {
        // If this default ever flips to false the test above is less critical,
        // but the scope must still hold — so this documents the coupling
        // rather than relaxing anything.
        const properties = manifest().contributes?.configuration?.properties ?? {};
        expect(properties["cortex.shareEditorContent"]?.default).toBe(true);
    });
});
