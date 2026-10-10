import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { within } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CortexPopup from "../popup";

let cleanup: (() => Promise<void>) | null = null;
let now = 0;
beforeEach(() => {
    vi.useFakeTimers(); now = 0;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    globalThis.__cortexChrome.runtime.sendMessage.mockImplementation((message, callback) => {
        if (message.type === "GET_STATE") callback?.({connected:true, stateAgeMs:0, state:{
            state:"FLOW",status:"estimated",confidence:.8,evidence_coverage:.8,
            biometrics:{heart_rate:72,blink_rate:10},capture:{frames_flowing:true,face_detected:true},
        }});
        else callback?.(undefined);
    });
});
afterEach(async () => { await cleanup?.(); cleanup=null; vi.useRealTimers(); vi.restoreAllMocks(); });
async function mount() {
    const element = document.createElement("div"); document.body.appendChild(element);
    const root = createRoot(element);
    await act(async () => root.render(<CortexPopup />));
    cleanup = async () => { await act(async () => root.unmount()); element.remove(); };
    return element;
}
describe("popup signal freshness", () => {
    it("expires biometrics and evidence coverage when connected updates stop", async () => {
        const element = await mount();
        expect(within(element).getByLabelText("72 beats per minute")).toBeTruthy();
        now = 10_000;
        await act(async () => vi.advanceTimersByTime(10_000));
        expect(within(element).queryByLabelText("72 beats per minute")).toBeNull();
        expect(within(element).getByTestId("status-pill").textContent).toContain("Waiting for fresh signals");
        expect(element.querySelector('[title="Evidence coverage 80%"]')).toBeNull();
    });
    it("drops cached biometrics immediately on disconnect", async () => {
        const element = await mount();
        await act(async () => globalThis.__cortexChrome.runtime.onMessage.__dispatch({type:"CONNECTION_CHANGED",connected:false}));
        expect(within(element).queryByLabelText("72 beats per minute")).toBeNull();
        expect(within(element).getByTestId("status-pill").textContent).toBe("Not running");
    });
});
