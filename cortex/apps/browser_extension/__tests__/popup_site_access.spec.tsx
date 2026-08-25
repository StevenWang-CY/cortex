import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { fireEvent, waitFor, within } from "@testing-library/dom";
import { afterEach, describe, expect, it } from "vitest";

import CortexPopup from "../popup";

let cleanup: (() => Promise<void>) | null = null;

afterEach(async () => {
    if (cleanup) await cleanup();
    cleanup = null;
    document.body.innerHTML = "";
});

async function mountPopup(): Promise<HTMLElement> {
    const fake = globalThis.__cortexChrome;
    fake.tabs.query.mockResolvedValue([{
        id: 1,
        url: "https://example.com/private?q=secret",
        incognito: false,
    }]);
    fake.runtime.sendMessage.mockImplementation(
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
    await act(async () => {
        root.render(<CortexPopup />);
    });
    cleanup = async () => {
        await act(async () => root.unmount());
        container.remove();
    };
    return container;
}

describe("popup optional page context", () => {
    it("shows explicit grant and revoke actions with incognito disclosure", async () => {
        const fake = globalThis.__cortexChrome;
        let granted = false;
        fake.permissions.contains.mockImplementation(
            () => Promise.resolve(granted),
        );
        fake.permissions.request.mockImplementation(() => {
            granted = true;
            return Promise.resolve(true);
        });
        fake.permissions.remove.mockImplementation(() => {
            granted = false;
            return Promise.resolve(true);
        });

        const container = await mountPopup();
        const button = await waitFor(() => {
            const found = within(container).getByTestId("site-access-button");
            expect(found.textContent).toBe("Allow");
            return found as HTMLButtonElement;
        });
        expect(container.textContent).toContain("Off for this site");

        await act(async () => {
            fireEvent.click(button);
        });
        await waitFor(() => expect(button.textContent).toBe("Revoke"));
        expect(container.textContent).toContain("never incognito");
        expect(fake.permissions.request).toHaveBeenCalledWith({
            origins: ["https://example.com/*"],
        });

        await act(async () => {
            fireEvent.click(button);
        });
        await waitFor(() => expect(button.textContent).toBe("Allow"));
        expect(fake.permissions.remove).toHaveBeenCalledWith({
            origins: ["https://example.com/*"],
        });
        expect(fake.runtime.sendMessage).toHaveBeenCalledWith(
            expect.objectContaining({ type: "SITE_ACCESS_REVOKED" }),
            expect.any(Function),
        );
    });
});
