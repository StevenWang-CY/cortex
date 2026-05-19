/**
 * Audit-prod G2 — background.ts emits CONNECTIVITY_DIAGNOSTIC.
 *
 * The popup's four-state UI (popup.tsx:423) consumes a payload with
 * ``{ native_host_status, daemon_version, handshake_error }``. Prior
 * to the audit-prod sweep, background.ts never produced this message,
 * so every diagnostic state collapsed to the default "no daemon".
 *
 * These tests pin the contract by stubbing `chrome.runtime.sendNativeMessage`
 * and `fetch`, importing background.ts (which fires `probeConnectivity`
 * on activation), and asserting the right `broadcastToPopup` call.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

describe("G2 CONNECTIVITY_DIAGNOSTIC emission", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("emits with native_host_status='present' when native host responds", async () => {
        const fetchMock = vi.fn().mockResolvedValue({
            ok: true,
            json: () => Promise.resolve({ version: "0.2.1" }),
        });
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (globalThis as any).fetch = fetchMock;

        // Configure the chrome fake to ack the native-host ping.
        const { installChromeFake } = await import("../test/mocks/chrome");
        const fake = installChromeFake();
        fake.runtime.sendNativeMessage = vi.fn(
            (_app: string, _msg: unknown, cb?: (r: unknown) => void) => {
                if (cb) cb({ status: "ok", running: false });
                return Promise.resolve({ status: "ok", running: false });
            },
        );

        await import("../background");
        // Allow the cold-start probeConnectivity microtask to run + the
        // 1.5s native-ping timer to resolve. We poll briefly.
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;
        for (let i = 0; i < 30; i++) {
            const calls = sendMessage.mock.calls.map(
                (c) => c[0] as { type?: string },
            );
            if (calls.some((m) => m?.type === "CONNECTIVITY_DIAGNOSTIC")) break;
            await new Promise((r) => setTimeout(r, 50));
        }
        const calls = sendMessage.mock.calls.map(
            (c) => c[0] as { type?: string; payload?: Record<string, unknown> },
        );
        const diag = calls.find((m) => m?.type === "CONNECTIVITY_DIAGNOSTIC");
        expect(diag).toBeDefined();
        expect(diag!.payload!.native_host_status).toBe("present");
    });

    it("emits with native_host_status='missing' when native host errors", async () => {
        const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (globalThis as any).fetch = fetchMock;

        const { installChromeFake } = await import("../test/mocks/chrome");
        const fake = installChromeFake();
        fake.runtime.sendNativeMessage = vi.fn(
            (_app: string, _msg: unknown, cb?: (r: unknown) => void) => {
                // Simulate the "Specified native messaging host not found" error.
                fake.runtime.lastError = { message: "host not found" };
                if (cb) cb(undefined);
                fake.runtime.lastError = undefined;
                return Promise.reject(new Error("not found"));
            },
        );

        await import("../background");
        const sendMessage = fake.runtime.sendMessage as ReturnType<typeof vi.fn>;
        for (let i = 0; i < 30; i++) {
            const calls = sendMessage.mock.calls.map(
                (c) => c[0] as { type?: string },
            );
            if (calls.some((m) => m?.type === "CONNECTIVITY_DIAGNOSTIC")) break;
            await new Promise((r) => setTimeout(r, 50));
        }
        const calls = sendMessage.mock.calls.map(
            (c) => c[0] as { type?: string; payload?: Record<string, unknown> },
        );
        const diag = calls.find((m) => m?.type === "CONNECTIVITY_DIAGNOSTIC");
        expect(diag).toBeDefined();
        expect(diag!.payload!.native_host_status).toBe("missing");
    });
});
