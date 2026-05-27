/**
 * Phase 4d Task F — onboarding daemon health gate.
 *
 * The launch step polls ``GET_STATE`` every 2s and disables Next until
 * the daemon reports connected=true. A "Skip — continue offline" link
 * lets users bypass the gate but surfaces a warning toast.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { fireEvent, waitFor, within } from "@testing-library/dom";

let Onboarding: React.ComponentType;
let activeRoot: Root | null = null;
let activeContainer: HTMLElement | null = null;

async function mount(): Promise<{
    container: HTMLElement;
    root: Root;
}> {
    const mod = await import("../tabs/onboarding");
    Onboarding = (mod.default || (mod as unknown as { Onboarding: React.ComponentType }).Onboarding) as React.ComponentType;
    const container = document.createElement("div");
    document.body.appendChild(container);
    let root!: Root;
    await act(async () => {
        root = createRoot(container);
        root.render(<Onboarding />);
    });
    activeRoot = root;
    activeContainer = container;
    return { container, root };
}

describe("Phase 4d Task F — onboarding daemon health gate", () => {
    beforeEach(() => {
        vi.resetModules();
        document.body.innerHTML = "";
        activeRoot = null;
        activeContainer = null;
    });

    afterEach(() => {
        if (activeRoot) {
            act(() => {
                activeRoot!.unmount();
            });
            activeRoot = null;
        }
        if (activeContainer && activeContainer.parentNode) {
            activeContainer.parentNode.removeChild(activeContainer);
            activeContainer = null;
        }
        document.body.innerHTML = "";
        vi.useRealTimers();
    });

    it("disables Next until the daemon reports connected=true", async () => {
        const fake = globalThis.__cortexChrome;
        // First poll: connected=false.
        fake.runtime.sendMessage.mockImplementation(
            (msg: { type: string }, cb?: (r: unknown) => void) => {
                if (msg.type === "GET_STATE" && cb) {
                    cb({ connected: false, state: null, focusSession: null });
                }
                return Promise.resolve(undefined);
            },
        );

        const { container } = await mount();
        await waitFor(() => {
            const label = within(container).getByTestId(
                "daemon-health-label",
            );
            expect(label.textContent).toMatch(/not detected/i);
        });
        const nextBtn = within(container).getByTestId(
            "onboarding-next-btn",
        );
        expect((nextBtn as HTMLButtonElement).disabled).toBe(true);
    });

    it("enables Next once daemon reports connected=true", async () => {
        const fake = globalThis.__cortexChrome;
        let connected = false;
        fake.runtime.sendMessage.mockImplementation(
            (msg: { type: string }, cb?: (r: unknown) => void) => {
                if (msg.type === "GET_STATE" && cb) {
                    cb({ connected, state: null, focusSession: null });
                }
                return Promise.resolve(undefined);
            },
        );

        const { container } = await mount();
        await waitFor(() => {
            const nextBtn = within(container).getByTestId(
                "onboarding-next-btn",
            );
            expect((nextBtn as HTMLButtonElement).disabled).toBe(true);
        });

        // Flip the mock and wait for the next poll (interval = 2000ms).
        connected = true;
        await new Promise((r) => setTimeout(r, 2100));
        await waitFor(() => {
            const nextBtn = within(container).getByTestId(
                "onboarding-next-btn",
            );
            expect((nextBtn as HTMLButtonElement).disabled).toBe(false);
        });
    }, 5000);

    it("skip-offline link bypasses the gate and surfaces a warning", async () => {
        const fake = globalThis.__cortexChrome;
        fake.runtime.sendMessage.mockImplementation(
            (msg: { type: string }, cb?: (r: unknown) => void) => {
                if (msg.type === "GET_STATE" && cb) {
                    cb({ connected: false, state: null, focusSession: null });
                }
                return Promise.resolve(undefined);
            },
        );

        const { container } = await mount();
        await waitFor(() => {
            within(container).getByTestId("onboarding-skip-offline");
        });
        const skip = within(container).getByTestId(
            "onboarding-skip-offline",
        );
        await act(async () => {
            fireEvent.click(skip);
        });
        const nextBtn = within(container).getByTestId(
            "onboarding-next-btn",
        );
        expect((nextBtn as HTMLButtonElement).disabled).toBe(false);
        within(container).getByTestId("onboarding-offline-toast");
    });
});
