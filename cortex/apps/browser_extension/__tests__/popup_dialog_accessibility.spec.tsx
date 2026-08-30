import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fireEvent, within } from "@testing-library/dom";
import { afterEach, describe, expect, it } from "vitest";

import CortexPopup from "../popup";

let cleanup: (() => Promise<void>) | null = null;

afterEach(async () => {
    if (cleanup) await cleanup();
    cleanup = null;
    document.body.innerHTML = "";
});

async function mountPopup(): Promise<HTMLElement> {
    globalThis.__cortexChrome.runtime.sendMessage.mockImplementation(
        (message: Record<string, unknown>, callback?: (value: unknown) => void) => {
            if (message.type === "GET_STATE") {
                callback?.({
                    connected: false,
                    state: null,
                    intervention: null,
                    focusSession: null,
                });
            } else if (message.type === "GET_CACHED_RECAP") {
                callback?.({ recap: null, timestamp: null });
            } else {
                callback?.(undefined);
            }
            return Promise.resolve(undefined);
        },
    );
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => root.render(<CortexPopup />));
    cleanup = async () => {
        await act(async () => root.unmount());
        container.remove();
    };
    return container;
}

describe("popup dialog keyboard behavior", () => {
    it("focuses, traps, dismisses, and restores the bug-report trigger", async () => {
        const container = await mountPopup();
        const trigger = within(container).getByTestId(
            "report-bug-link",
        ) as HTMLButtonElement;
        trigger.focus();
        await act(async () => fireEvent.click(trigger));
        await act(async () => {
            await new Promise<void>((resolve) =>
                window.requestAnimationFrame(() => resolve()),
            );
        });

        const dialog = within(container).getByTestId("bug-report-modal");
        const textarea = within(dialog).getByTestId(
            "bug-report-textarea",
        ) as HTMLTextAreaElement;
        const submit = within(dialog).getByTestId(
            "bug-report-submit",
        ) as HTMLButtonElement;
        expect(document.activeElement).toBe(textarea);

        fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
        expect(document.activeElement).toBe(submit);

        await act(async () => {
            fireEvent.keyDown(document, { key: "Escape" });
        });
        expect(within(container).queryByTestId("bug-report-modal")).toBeNull();
        expect(document.activeElement).toBe(trigger);
    });
});
