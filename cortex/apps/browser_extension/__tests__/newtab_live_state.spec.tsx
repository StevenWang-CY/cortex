import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fireEvent, within } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PulseRoom, { NEWTAB_DISABLED_KEY, PULSE_MOTION_PAUSED_KEY } from "../newtab";

let clean: (() => Promise<void>) | null = null;
let response: Record<string, unknown>;
let now = 0;
const liveState = { state: "FLOW", status: "estimated", confidence: 0.8, biometrics: { heart_rate: 72 } };
beforeEach(() => {
    vi.useFakeTimers();
    now = 0;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    response = { connected: true, state: liveState, stateAgeMs: 0 };
    globalThis.__cortexChrome.runtime.sendMessage.mockImplementation((msg, callback) => {
        if (msg.type === "GET_STATE") callback?.(response);
        if (msg.type === "GET_RECENT_ACTIVITIES") callback?.([]);
    });
});
afterEach(async () => {
    await clean?.(); clean = null;
    vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
});
async function mount() {
    const element = document.createElement("div"); document.body.appendChild(element);
    const root = createRoot(element);
    await act(async () => root.render(<PulseRoom />));
    clean = async () => { await act(async () => root.unmount()); element.remove(); };
    return element;
}
async function message(msg: Record<string, unknown>) {
    await act(async () => globalThis.__cortexChrome.runtime.onMessage.__dispatch(msg));
}
describe("Pulse Room live state", () => {
    it("clears pulse on null samples and disconnect, then restores only from new evidence", async () => {
        const element = await mount();
        expect(element.textContent).toContain("72 bpm");
        await message({ type: "STATE_UPDATE", payload: { ...liveState, biometrics: { heart_rate: null } } });
        expect(element.textContent).not.toContain("72 bpm");
        await message({ type: "STATE_UPDATE", payload: liveState });
        expect(element.textContent).toContain("72 bpm");
        await message({ type: "CONNECTION_CHANGED", connected: false });
        expect(element.textContent).not.toContain("72 bpm");
        expect(within(element).getByRole("button", { name: "Open Cortex" })).toBeTruthy();
    });
    it("does not rejuvenate stale cached observations on a successful GET_STATE", async () => {
        const element = await mount();
        response = { ...response, stateAgeMs: 11_000 };
        now = 11_000;
        await act(async () => vi.advanceTimersByTime(1000));
        expect(element.textContent).not.toContain("72 bpm");
        expect(element.textContent).toContain("Waiting for fresh signals");
    });
    it("settles a runtime launch failure and keeps retry available", async () => {
        response = { connected: false, state: null };
        const element = await mount();
        globalThis.__cortexChrome.runtime.sendMessage.mockImplementation((msg, callback) => {
            if (msg.type !== "LAUNCH_CORTEX") return;
            globalThis.__cortexChrome.runtime.lastError = { message: "worker gone" };
            callback?.(undefined);
            globalThis.__cortexChrome.runtime.lastError = undefined;
        });
        await act(async () => fireEvent.click(within(element).getByRole("button", { name: "Open Cortex" })));
        expect(element.textContent).toContain("Cortex didn't start");
        expect((within(element).getByRole("button", { name: "Open Cortex" }) as HTMLButtonElement).disabled).toBe(false);
    });
    it("settles a missing launch callback and ignores its late result", async () => {
        response = { connected: false, state: null };
        const element = await mount();
        let late: ((value: unknown) => void) | undefined;
        globalThis.__cortexChrome.runtime.sendMessage.mockImplementation((msg, callback) => {
            if (msg.type === "LAUNCH_CORTEX") late = callback;
        });
        await act(async () => fireEvent.click(within(element).getByRole("button", { name: "Open Cortex" })));
        await act(async () => vi.advanceTimersByTime(45_000));
        expect(element.textContent).toContain("Cortex didn't start");
        await act(async () => late?.({ ok: true }));
        expect(element.textContent).toContain("Cortex didn't start");
    });
    it("honors persisted pause and starts exactly one loop after restoring the room", async () => {
        globalThis.__cortexChrome.storage.local.__reset({ [NEWTAB_DISABLED_KEY]: true });
        const element = await mount();
        expect(requestAnimationFrame).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.runtime.sendMessage).not.toHaveBeenCalled();
        await act(async () => fireEvent.click(within(element).getByTestId("newtab-restore")));
        expect(requestAnimationFrame).toHaveBeenCalledTimes(1);
        await act(async () => fireEvent.click(within(element).getByRole("button", { name: "Pause motion" })));
        expect(cancelAnimationFrame).toHaveBeenCalledTimes(1);
        expect(globalThis.__cortexChrome.storage.local.__peek()[PULSE_MOTION_PAUSED_KEY]).toBe(true);
        await act(async () => fireEvent.click(within(element).getByRole("button", { name: "Resume motion" })));
        expect(requestAnimationFrame).toHaveBeenCalledTimes(2);
    });
});
