/**
 * Popup controls: the apply button renders every phase of the shared
 * machine, quiet mode is one segmented control sending one canonical
 * message, Stop Cortex is two-step and names its consequence, the header
 * carries one status pill (no duplicate Connect), and a version skew is a
 * dismissible banner over the live session rather than a wall.
 */

import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { fireEvent, within } from "@testing-library/dom";
import { afterEach, describe, expect, it } from "vitest";
import CortexPopup from "../popup";
import { ApplyButton, IDLE_APPLY_STATE } from "../popup/components/InterventionCard";

let cleanup: (() => Promise<void>) | null = null;

async function unmountCurrent(): Promise<void> {
    if (cleanup) await cleanup();
    cleanup = null;
}

afterEach(async () => {
    await unmountCurrent();
    document.body.innerHTML = "";
});

async function mount(element: React.ReactElement): Promise<HTMLElement> {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => root.render(element));
    cleanup = async () => {
        await act(async () => root.unmount());
        container.remove();
    };
    return container;
}

type BgListener = (msg: Record<string, unknown>) => void;

async function mountPopup(connected = false): Promise<{ container: HTMLElement; listener: BgListener }> {
    const fake = globalThis.__cortexChrome;
    fake.runtime.sendMessage.mockImplementation(
        (message: Record<string, unknown>, callback?: (value: unknown) => void) => {
            if (message.type === "GET_STATE") {
                callback?.({ connected, state: null, intervention: null, focusSession: null });
            } else if (message.type === "GET_CACHED_RECAP") {
                callback?.({ recap: null, timestamp: null });
            } else {
                callback?.(undefined);
            }
            return Promise.resolve(undefined);
        },
    );
    const container = await mount(<CortexPopup />);
    const calls = fake.runtime.onMessage.addListener.mock.calls;
    const listener = calls[calls.length - 1][0] as BgListener;
    return { container, listener };
}

function sent(type: string): Array<Record<string, unknown>> {
    return globalThis.__cortexChrome.runtime.sendMessage.mock.calls
        .map((call) => call[0] as Record<string, unknown>)
        .filter((m) => m.type === type);
}

describe("ApplyButton phases", () => {
    const noop = () => undefined;

    it("idle is the only clickable phase", async () => {
        let clicks = 0;
        const container = await mount(
            <ApplyButton apply={IDLE_APPLY_STATE} label="Apply 2 changes" onApply={() => { clicks++; }} onUndo={noop} />,
        );
        const button = within(container).getByTestId("intervention-primary-action") as HTMLButtonElement;
        expect(button.textContent).toBe("Apply 2 changes");
        expect(button.disabled).toBe(false);
        fireEvent.click(button);
        expect(clicks).toBe(1);
    });

    it("pending disables and announces busy", async () => {
        const container = await mount(
            <ApplyButton apply={{ ...IDLE_APPLY_STATE, phase: "pending" }} label="Apply" onApply={noop} onUndo={noop} />,
        );
        const button = within(container).getByTestId("intervention-primary-action") as HTMLButtonElement;
        expect(button.disabled).toBe(true);
        expect(button.getAttribute("aria-busy")).toBe("true");
        expect(button.textContent).toBe("Applying…");
        expect(within(container).queryByTestId("intervention-undo")).toBeNull();
    });

    it("applied and partial offer Undo; failed explains and offers none", async () => {
        let undos = 0;
        const applied = await mount(
            <ApplyButton
                apply={{ phase: "applied", outcome: { phase: "applied", applied: 1, total: 1, reason: null }, undone: false, undoBusy: false }}
                label="Apply" onApply={noop} onUndo={() => { undos++; }}
            />,
        );
        expect(within(applied).getByTestId("intervention-primary-action").textContent).toBe("Applied");
        fireEvent.click(within(applied).getByTestId("intervention-undo"));
        expect(undos).toBe(1);
        await unmountCurrent();

        const partial = await mount(
            <ApplyButton
                apply={{ phase: "partial", outcome: { phase: "partial", applied: 2, total: 3, reason: "one tab moved" }, undone: false, undoBusy: false }}
                label="Apply" onApply={noop} onUndo={noop}
            />,
        );
        expect(within(partial).getByTestId("intervention-primary-action").textContent).toBe("2 of 3 applied");
        expect(within(partial).queryByTestId("intervention-undo")).not.toBeNull();
        await unmountCurrent();

        const failed = await mount(
            <ApplyButton
                apply={{ phase: "failed", outcome: { phase: "failed", applied: 0, total: 1, reason: "workspace changes are off" }, undone: false, undoBusy: false }}
                label="Apply" onApply={noop} onUndo={noop}
            />,
        );
        const button = within(failed).getByTestId("intervention-primary-action") as HTMLButtonElement;
        expect(button.textContent).toBe("Nothing changed — workspace changes are off");
        expect(button.disabled).toBe(true);
        expect(button.getAttribute("data-phase")).toBe("failed");
        expect(within(failed).queryByTestId("intervention-undo")).toBeNull();
    });
});

describe("popup controls", () => {
    it("shows one status pill and no duplicate Connect control", async () => {
        const { container } = await mountPopup(false);
        expect(within(container).getByTestId("status-pill").textContent).toContain("Not running");
        expect(within(container).queryByText("Connect")).toBeNull();
        expect(within(container).getByTestId("conn-state-installed_no_daemon").textContent)
            .toBe("Cortex isn't running");
        expect(within(container).getByTestId("connection-cta").textContent).toBe("Open Cortex");
    });

    it("quiet mode is one segmented control sending one canonical message", async () => {
        const { container } = await mountPopup(true);
        const group = within(container).getByTestId("quiet-mode-control");
        expect(group.getAttribute("role")).toBe("radiogroup");
        expect(within(group).getAllByRole("radio")).toHaveLength(4);
        await act(async () => fireEvent.click(within(group).getByTestId("quiet-mode-snooze_15")));
        expect(sent("QUIET_MODE_TOGGLE")).toHaveLength(1);
        expect(sent("QUIET_MODE_TOGGLE")[0]).toMatchObject({ kind: "snooze_15", duration_minutes: 15 });
        expect(sent("TOGGLE_QUIET_MODE")).toHaveLength(0);
        expect(within(container).getByTestId("quiet-mode-status").textContent).toContain("Snoozed");
        expect(within(group).getByTestId("quiet-mode-snooze_15").getAttribute("aria-checked")).toBe("true");
    });

    it("Stop Cortex asks once, names the consequence, and can be cancelled", async () => {
        const { container } = await mountPopup(true);
        await act(async () => fireEvent.click(within(container).getByTestId("stop-cortex")));
        const confirm = within(container).getByTestId("stop-cortex-confirm");
        expect(confirm.textContent).toContain("camera");
        expect(sent("STOP_CORTEX")).toHaveLength(0);
        await act(async () => fireEvent.click(within(confirm).getByTestId("stop-cortex-cancel")));
        expect(within(container).queryByTestId("stop-cortex-confirm")).toBeNull();
        await act(async () => fireEvent.click(within(container).getByTestId("stop-cortex")));
        await act(async () => fireEvent.click(within(container).getByTestId("stop-cortex-confirm-button")));
        expect(sent("STOP_CORTEX")).toHaveLength(1);
    });

    it("treats a patch skew as compatible and a minor skew as a dismissible banner", async () => {
        const { container, listener } = await mountPopup(false);
        await act(async () => {
            listener({ type: "CONNECTION_CHANGED", connected: true });
            listener({
                type: "CONNECTIVITY_DIAGNOSTIC",
                payload: { native_host_status: "present", native_host_error: null, daemon_version: "0.2.9", handshake_error: null },
            });
        });
        expect(within(container).queryByTestId("conn-state-installed_version_mismatch")).toBeNull();
        expect(within(container).queryByTestId("connection-panel")).toBeNull();

        await act(async () => {
            listener({
                type: "CONNECTIVITY_DIAGNOSTIC",
                payload: { native_host_status: "present", native_host_error: null, daemon_version: "0.1.0", handshake_error: null },
            });
        });
        const banner = within(container).getByTestId("conn-state-installed_version_mismatch");
        expect(banner.textContent).toContain("Cortex needs an update");
        expect(within(banner).getByTestId("version-banner-open").textContent).toBe("Open Cortex");
        expect(within(container).queryByTestId("connection-panel")).toBeNull();
        await act(async () => fireEvent.click(within(banner).getByTestId("version-banner-dismiss")));
        expect(within(container).queryByTestId("conn-state-installed_version_mismatch")).toBeNull();
    });

    it("a healthy connection clears a stale handshake verdict", async () => {
        const { container, listener } = await mountPopup(false);
        await act(async () => {
            listener({
                type: "CONNECTIVITY_DIAGNOSTIC",
                payload: { native_host_status: "present", native_host_error: null, daemon_version: "0.2.1", handshake_error: "handshake_rejected" },
            });
            listener({ type: "CONNECTION_CHANGED", connected: true });
        });
        expect(within(container).queryByTestId("conn-state-handshake_failed")).toBeNull();
        expect(within(container).queryByTestId("connection-panel")).toBeNull();
        expect(container.textContent).not.toContain("handshake_rejected");
    });
});
