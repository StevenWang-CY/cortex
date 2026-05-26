/**
 * P0 §3.6 — browser-extension popup micro-step checkbox toggle.
 *
 * The popup must:
 *   - render an interactive checkbox per ``micro_step`` entry from
 *     the INTERVENTION_TRIGGER payload,
 *   - apply strikethrough styling for entries with status === "done",
 *   - dispatch ``MICRO_STEP_TOGGLED`` via chrome.runtime.sendMessage
 *     with ``{intervention_id, step_index, new_status}`` on click,
 *   - flip the local strikethrough optimistically so the user sees
 *     immediate feedback before the daemon's rebroadcast lands.
 */

import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react-dom/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CortexPopup from "../popup";

type SendMessageResponder = (
    msg: Record<string, unknown>,
    cb?: (resp: unknown) => void,
) => unknown;

interface Harness {
    container: HTMLDivElement;
    sendMessage: ReturnType<typeof vi.fn>;
    cleanup: () => Promise<void>;
}

function installResponder(responder: SendMessageResponder): ReturnType<typeof vi.fn> {
    const fake = globalThis.__cortexChrome;
    const fn = vi.fn(
        (msg: Record<string, unknown>, cb?: (resp: unknown) => void) => {
            const out = responder(msg, cb);
            return Promise.resolve(out);
        },
    );
    fake.runtime.sendMessage = fn as unknown as typeof fake.runtime.sendMessage;
    return fn;
}

const FAKE_INTERVENTION = {
    intervention_id: "int_test_micro",
    headline: "Take a moment",
    situation_summary: "summary",
    primary_focus: "focus",
    micro_steps: [
        { text: "step a", status: "pending" },
        { text: "step b", status: "pending" },
    ],
    suggested_actions: [],
    tab_recommendations: null,
    error_analysis: null,
};

async function renderPopupWithIntervention(): Promise<Harness> {
    installResponder((msg, cb) => {
        if (msg.type === "GET_STATE") {
            cb?.({
                connected: true,
                state: null,
                intervention: FAKE_INTERVENTION,
                focusSession: null,
            });
            return undefined;
        }
        if (msg.type === "GET_DAILY_STATS") {
            cb?.(null);
            return undefined;
        }
        if (msg.type === "GET_CACHED_RECAP") {
            cb?.({ recap: null, timestamp: null });
            return undefined;
        }
        cb?.(undefined);
        return undefined;
    });

    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
        root.render(React.createElement(CortexPopup));
    });
    await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
    });
    const cleanup = async () => {
        await act(async () => {
            root.unmount();
        });
        container.remove();
    };
    return {
        container,
        sendMessage: globalThis.__cortexChrome.runtime.sendMessage as ReturnType<typeof vi.fn>,
        cleanup,
    };
}

describe("popup micro-step checkbox toggle (P0 §3.6)", () => {
    beforeEach(() => {
        globalThis.__cortexChrome.storage.local.__reset({});
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it("renders one checkbox per micro_step entry", async () => {
        const { container, cleanup } = await renderPopupWithIntervention();
        try {
            const list = container.querySelector(
                '[data-testid="micro-step-list"]',
            );
            expect(list).not.toBeNull();
            const row0 = container.querySelector(
                '[data-testid="micro-step-row-0"]',
            );
            const row1 = container.querySelector(
                '[data-testid="micro-step-row-1"]',
            );
            expect(row0?.textContent).toContain("step a");
            expect(row1?.textContent).toContain("step b");
        } finally {
            await cleanup();
        }
    });

    it("dispatches MICRO_STEP_TOGGLED with the correct payload on click", async () => {
        const { container, sendMessage, cleanup } = await renderPopupWithIntervention();
        try {
            const cb0 = container.querySelector(
                '[data-testid="micro-step-checkbox-0"]',
            ) as HTMLInputElement | null;
            expect(cb0).not.toBeNull();

            await act(async () => {
                cb0!.click();
                await new Promise((r) => setTimeout(r, 0));
            });

            const sent = sendMessage.mock.calls.find(
                (c) => (c[0] as { type?: string }).type === "MICRO_STEP_TOGGLED",
            );
            expect(sent).toBeTruthy();
            const payload = sent![0] as {
                type: string;
                intervention_id: string;
                step_index: number;
                new_status: string;
            };
            expect(payload.intervention_id).toBe("int_test_micro");
            expect(payload.step_index).toBe(0);
            expect(payload.new_status).toBe("done");
        } finally {
            await cleanup();
        }
    });

    it("applies strikethrough styling after the optimistic toggle", async () => {
        const { container, cleanup } = await renderPopupWithIntervention();
        try {
            const cb0 = container.querySelector(
                '[data-testid="micro-step-checkbox-0"]',
            ) as HTMLInputElement | null;
            await act(async () => {
                cb0!.click();
                await new Promise((r) => setTimeout(r, 0));
            });
            const row0 = container.querySelector(
                '[data-testid="micro-step-row-0"]',
            ) as HTMLElement | null;
            expect(row0).not.toBeNull();
            expect(row0!.style.textDecoration).toContain("line-through");
        } finally {
            await cleanup();
        }
    });

    it("renders strikethrough for steps already marked done in the wire payload", async () => {
        installResponder((msg, cb) => {
            if (msg.type === "GET_STATE") {
                cb?.({
                    connected: true,
                    state: null,
                    intervention: {
                        ...FAKE_INTERVENTION,
                        micro_steps: [
                            { text: "step a", status: "done" },
                            { text: "step b", status: "pending" },
                        ],
                    },
                    focusSession: null,
                });
                return undefined;
            }
            if (msg.type === "GET_DAILY_STATS") {
                cb?.(null);
                return undefined;
            }
            if (msg.type === "GET_CACHED_RECAP") {
                cb?.({ recap: null, timestamp: null });
                return undefined;
            }
            cb?.(undefined);
            return undefined;
        });

        const container = document.createElement("div");
        document.body.appendChild(container);
        const root = createRoot(container);
        await act(async () => {
            root.render(React.createElement(CortexPopup));
        });
        await act(async () => {
            await new Promise((r) => setTimeout(r, 0));
        });

        try {
            const cb0 = container.querySelector(
                '[data-testid="micro-step-checkbox-0"]',
            ) as HTMLInputElement | null;
            const cb1 = container.querySelector(
                '[data-testid="micro-step-checkbox-1"]',
            ) as HTMLInputElement | null;
            expect(cb0?.checked).toBe(true);
            expect(cb1?.checked).toBe(false);
        } finally {
            await act(async () => {
                root.unmount();
            });
            container.remove();
        }
    });
});
