/**
 * F54: connectivity panel has four distinct states beyond "ok".
 *
 * - not_installed: native host missing
 * - installed_no_daemon: native host present but daemon WS down
 * - installed_version_mismatch: daemon up, version differs
 * - handshake_failed: WS up, daemon rejected handshake
 */

import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react-dom/test-utils";
import { describe, expect, it } from "vitest";
import CortexPopup, { classifyConnectivity } from "../popup";

describe("F54 classifyConnectivity", () => {
    it("returns ok on the happy path", () => {
        expect(
            classifyConnectivity({
                connected: true,
                nativeHostStatus: "present",
                daemonVersion: "0.2.1",
                expectedVersion: "0.2.1",
                handshakeError: null,
            }),
        ).toBe("ok");
    });

    it("returns not_installed when native host is missing and WS is down", () => {
        expect(
            classifyConnectivity({
                connected: false,
                nativeHostStatus: "missing",
                daemonVersion: null,
                expectedVersion: "0.2.1",
                handshakeError: null,
            }),
        ).toBe("not_installed");
    });

    it("returns installed_no_daemon when host is present but WS is down", () => {
        expect(
            classifyConnectivity({
                connected: false,
                nativeHostStatus: "present",
                daemonVersion: null,
                expectedVersion: "0.2.1",
                handshakeError: null,
            }),
        ).toBe("installed_no_daemon");
    });

    it("returns installed_version_mismatch when versions disagree", () => {
        expect(
            classifyConnectivity({
                connected: true,
                nativeHostStatus: "present",
                daemonVersion: "0.1.0",
                expectedVersion: "0.2.1",
                handshakeError: null,
            }),
        ).toBe("installed_version_mismatch");
    });

    it("returns handshake_failed when daemon rejects after connect", () => {
        expect(
            classifyConnectivity({
                connected: true,
                nativeHostStatus: "present",
                daemonVersion: "0.2.1",
                expectedVersion: "0.2.1",
                handshakeError: "invalid_auth_token",
            }),
        ).toBe("handshake_failed");
    });

    it("defaults to installed_no_daemon when native host status unknown", () => {
        expect(
            classifyConnectivity({
                connected: false,
                nativeHostStatus: "unknown",
                daemonVersion: null,
                expectedVersion: "0.2.1",
                handshakeError: null,
            }),
        ).toBe("installed_no_daemon");
    });
});

describe("F54 popup renders distinct UI per connectivity state", () => {
    type BgListener = (msg: Record<string, unknown>) => void;

    async function renderPopup(): Promise<{
        container: HTMLDivElement;
        listener: BgListener;
        cleanup: () => Promise<void>;
    }> {
        const fake = globalThis.__cortexChrome;
        const container = document.createElement("div");
        document.body.appendChild(container);
        const root = createRoot(container);
        await act(async () => {
            root.render(React.createElement(CortexPopup));
        });
        const calls = fake.runtime.onMessage.addListener.mock.calls;
        const listener = calls[calls.length - 1][0] as BgListener;
        const cleanup = async () => {
            await act(async () => {
                root.unmount();
            });
            container.remove();
        };
        return { container, listener, cleanup };
    }

    it("not_installed shows the native-host diagnostic", async () => {
        const { container, listener, cleanup } = await renderPopup();
        try {
            await act(async () => {
                listener({
                    type: "CONNECTIVITY_DIAGNOSTIC",
                    payload: { native_host_status: "missing" },
                });
            });
            const title = container.querySelector('[data-testid="conn-state-not_installed"]');
            expect(title).not.toBeNull();
            expect(title?.textContent).toContain("Native host not installed");
        } finally {
            await cleanup();
        }
    });

    it("installed_no_daemon is the default disconnected UI", async () => {
        const { container, cleanup } = await renderPopup();
        try {
            const title = container.querySelector('[data-testid="conn-state-installed_no_daemon"]');
            expect(title).not.toBeNull();
            expect(title?.textContent).toContain("Not connected");
        } finally {
            await cleanup();
        }
    });

    it("installed_version_mismatch shows the version diagnostic", async () => {
        const { container, listener, cleanup } = await renderPopup();
        try {
            await act(async () => {
                listener({ type: "CONNECTION_CHANGED", connected: true });
                listener({
                    type: "CONNECTIVITY_DIAGNOSTIC",
                    payload: { native_host_status: "present", daemon_version: "0.1.0" },
                });
            });
            const title = container.querySelector('[data-testid="conn-state-installed_version_mismatch"]');
            expect(title).not.toBeNull();
            expect(title?.textContent).toContain("version mismatch");
        } finally {
            await cleanup();
        }
    });

    it("handshake_failed shows the handshake diagnostic", async () => {
        const { container, listener, cleanup } = await renderPopup();
        try {
            await act(async () => {
                listener({ type: "CONNECTION_CHANGED", connected: true });
                listener({
                    type: "CONNECTIVITY_DIAGNOSTIC",
                    payload: {
                        native_host_status: "present",
                        daemon_version: "0.2.1",
                        handshake_error: "auth_token_invalid",
                    },
                });
            });
            const title = container.querySelector('[data-testid="conn-state-handshake_failed"]');
            expect(title).not.toBeNull();
            expect(title?.textContent).toContain("Handshake failed");
        } finally {
            await cleanup();
        }
    });
});
