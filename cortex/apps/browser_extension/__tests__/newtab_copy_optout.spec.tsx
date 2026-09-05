/**
 * New tab: consumer copy (no raw state enums, no "engine offline"), the
 * pre-rendered glow animated by opacity/transform only, and the
 * "use the browser's default new tab" opt-out.
 */

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { within } from "@testing-library/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import PulseRoom, { NEWTAB_DISABLED_KEY } from "../newtab";

let cleanup: (() => Promise<void>) | null = null;

afterEach(async () => {
    if (cleanup) await cleanup();
    cleanup = null;
    vi.unstubAllGlobals();
    document.body.innerHTML = "";
});

function stubState(response: Record<string, unknown>): void {
    globalThis.__cortexChrome.runtime.sendMessage.mockImplementation(
        (msg: Record<string, unknown>, cb?: (r: unknown) => void) => {
            if (msg.type === "GET_STATE") cb?.(response);
            else if (msg.type === "GET_RECENT_ACTIVITIES") cb?.([]);
            else cb?.(undefined);
            return Promise.resolve(undefined);
        },
    );
}

async function render(): Promise<HTMLElement> {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => root.render(<PulseRoom />));
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    cleanup = async () => {
        await act(async () => root.unmount());
        container.remove();
    };
    return container;
}

describe("new tab copy and opt-out", () => {
    it("names the state in consumer language and never prints the enum", async () => {
        stubState({
            connected: true,
            state: { state: "HYPER", confidence: 0.8, biometrics: { heart_rate: 72 } },
        });
        const container = await render();
        const line = within(container).getByTestId("newtab-state-line");
        expect(line.textContent).toBe("72 bpm · Support may help");
        expect(container.textContent).not.toContain("HYPER");
        expect(container.textContent).not.toContain("BIOFEEDBACK");
    });

    it("says Cortex is resting when nothing is running", async () => {
        stubState({ connected: false, state: null });
        const container = await render();
        expect(within(container).getByTestId("newtab-state-line").textContent).toBe("Cortex is resting");
        expect(container.textContent).not.toContain("ENGINE");
        expect(within(container).getByRole("button", { name: "Open Cortex" })).toBeTruthy();
    });

    it("animates the pre-rendered glow with opacity and transform only", async () => {
        const frames: FrameRequestCallback[] = [];
        vi.stubGlobal("requestAnimationFrame", vi.fn((cb: FrameRequestCallback) => { frames.push(cb); return frames.length; }));
        vi.stubGlobal("cancelAnimationFrame", vi.fn());
        stubState({ connected: true, state: { state: "FLOW", confidence: 0.9, biometrics: { heart_rate: 60 } } });
        const container = await render();
        const glow = within(container).getByTestId("pulse-glow");
        const filterBefore = glow.style.filter;
        expect(filterBefore).toContain("blur");
        expect(frames.length).toBeGreaterThan(0);
        await act(async () => { frames[0](1_000); });
        expect(glow.style.filter).toBe(filterBefore);
        expect(glow.style.opacity).not.toBe("");
        expect(glow.style.transform).toContain("scale(");
    });

    it("honours the default-new-tab preference with a minimal page", async () => {
        stubState({ connected: false, state: null });
        globalThis.__cortexChrome.storage.local.__reset({ [NEWTAB_DISABLED_KEY]: true });
        const container = await render();
        expect(within(container).getByTestId("newtab-default-notice")).toBeTruthy();
        expect(container.querySelector("canvas")).toBeNull();
        expect(within(container).getByTestId("newtab-open-default").textContent).toContain("default new tab");
        await act(async () => {
            within(container).getByTestId("newtab-restore").click();
        });
        expect(globalThis.__cortexChrome.storage.local.__peek()[NEWTAB_DISABLED_KEY]).toBe(false);
        expect(container.querySelector("canvas")).not.toBeNull();
    });
});
