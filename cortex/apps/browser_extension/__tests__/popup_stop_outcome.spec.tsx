import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fireEvent, within } from "@testing-library/dom";
import { afterEach, expect, it, vi } from "vitest";
import CortexPopup from "../popup";
import { StopCortex } from "../popup/components/StopCortex";
import { POPUP_STOP_TIMEOUT_MS } from "../lib/daemon-stop";

let unmount: (() => Promise<void>) | undefined;
afterEach(async () => {
    await unmount?.();
    unmount = undefined;
    vi.useRealTimers();
    document.body.innerHTML = "";
});

async function mount(element: React.ReactElement) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => root.render(element));
    unmount = async () => { await act(async () => root.unmount()); };
    return {
        ...within(host),
        rerender: async (next: React.ReactElement) => { await act(async () => root.render(next)); },
    };
}

it("stop intent alone never claims the daemon is stopped", async () => {
    const view = await mount(<StopCortex stopping={false} stopRequested onStop={() => undefined} />);
    expect(view.queryByTestId("stop-cortex-stopped")).toBeNull();
    expect(view.getByRole("status").textContent).toContain("has not been confirmed");
    expect(view.getByTestId("stop-cortex")).toBeTruthy();
});

it("starts a fresh confirmation after a stopped session is launched again", async () => {
    const onStop = vi.fn();
    const view = await mount(<StopCortex stopping={false} stopRequested={false} onStop={onStop} />);
    await act(async () => fireEvent.click(view.getByTestId("stop-cortex")));
    await act(async () => fireEvent.click(view.getByTestId("stop-cortex-confirm-button")));
    expect(onStop).toHaveBeenCalledOnce();
    await view.rerender(<StopCortex stopping stopRequested onStop={onStop} />);
    await view.rerender(<StopCortex stopping={false} stopRequested confirmed onStop={onStop} />);
    expect(view.getByTestId("stop-cortex-stopped")).toBeTruthy();
    await view.rerender(<StopCortex stopping={false} stopRequested={false} onStop={onStop} />);
    expect(view.queryByTestId("stop-cortex-confirm")).toBeNull();
    expect(view.getByTestId("stop-cortex")).toBeTruthy();
});

it("keeps Stop pending beyond two seconds, reports worker failure, and retries to verified success", async () => {
    vi.useFakeTimers();
    let respond: ((response: unknown) => void) | undefined;
    globalThis.__cortexChrome.runtime.sendMessage.mockImplementation(
        (message: Record<string, unknown>, callback?: (value: unknown) => void) => {
            if (message.type === "GET_STATE") {
                callback?.({ connected: true, state: null, focusSession: null });
            } else if (message.type === "STOP_CORTEX") {
                respond = callback;
            } else { callback?.(undefined); }
            return Promise.resolve(undefined);
        },
    );
    const view = await mount(<CortexPopup />);
    await act(async () => fireEvent.click(view.getByTestId("stop-cortex")));
    await act(async () => fireEvent.click(view.getByTestId("stop-cortex-confirm-button")));
    await act(async () => { await vi.advanceTimersByTimeAsync(2_500); });
    expect(view.queryByTestId("stop-cortex-stopped")).toBeNull();
    expect(view.getByTestId("stop-cortex-confirm-button").getAttribute("aria-busy")).toBe("true");
    await act(async () => respond?.({ ok: false, error: "Process remains alive" }));
    expect(view.getByRole("alert").textContent).toContain("Process remains alive");
    await act(async () => fireEvent.click(view.getByTestId("stop-cortex-retry")));
    await act(async () => respond?.({ ok: true }));
    expect(view.getByTestId("stop-cortex-stopped").textContent).toContain("Cortex is stopped");
});

it("bounds a stop already in progress when the popup is reopened", async () => {
    vi.useFakeTimers();
    globalThis.__cortexChrome.runtime.sendMessage.mockImplementation(
        (message: Record<string, unknown>, callback?: (value: unknown) => void) => {
            callback?.(message.type === "GET_STATE"
                ? { connected: false, stopRequested: true, stopStatus: "stopping", state: null }
                : undefined);
            return Promise.resolve(undefined);
        },
    );
    const view = await mount(<CortexPopup />);
    expect(view.getByTestId("stop-cortex-confirm-button").textContent).toBe("Stopping…");
    await act(async () => { await vi.advanceTimersByTimeAsync(POPUP_STOP_TIMEOUT_MS); });
    expect(view.getByRole("alert").textContent).toContain("could not be confirmed");
    expect(view.getByTestId("stop-cortex-retry")).toBeTruthy();
});
