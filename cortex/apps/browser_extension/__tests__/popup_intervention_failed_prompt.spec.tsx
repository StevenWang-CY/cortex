/**
 * P1-FC-INTERVENTION-FAILED / P1-FC-INTERVENTION-PROMPT — popup consumers.
 *
 * The popup must:
 *   - On INTERVENTION_FAILED: flip the intervention card to an error
 *     state (render the failure banner with the daemon's reason) and
 *     DISABLE the apply CTA so the user isn't told to engage a plan that
 *     never actually changed the workspace.
 *   - On INTERVENTION_PROMPT: render the prompt text inline (above the
 *     action card) so a popup-open user has awareness of the active
 *     cross-surface prompt.
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

function installResponder(responder: SendMessageResponder): void {
    const fake = globalThis.__cortexChrome;
    const fn = vi.fn(
        (msg: Record<string, unknown>, cb?: (resp: unknown) => void) => {
            const out = responder(msg, cb);
            return Promise.resolve(out);
        },
    );
    fake.runtime.sendMessage = fn as unknown as typeof fake.runtime.sendMessage;
}

const FAKE_INTERVENTION = {
    intervention_id: "iv-fc-1",
    headline: "Take a moment",
    situation_summary: "summary",
    primary_focus: "focus",
    micro_steps: [],
    suggested_actions: [
        {
            action_id: "a1",
            action_type: "close_tab",
            label: "Close noisy tab",
            category: "recommended",
            tab_index: 2,
        },
    ],
    tab_recommendations: null,
    error_analysis: null,
    level: "guided_mode",
};

function defaultResponder(msg: Record<string, unknown>, cb?: (resp: unknown) => void) {
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
}

async function renderPopup() {
    installResponder(defaultResponder);
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
    return { container, cleanup };
}

function dispatchToPopup(message: Record<string, unknown>): void {
    const fake = globalThis.__cortexChrome;
    fake.runtime.onMessage.__dispatch(message, {}, () => undefined);
}

describe("popup INTERVENTION_FAILED / INTERVENTION_PROMPT consumers", () => {
    beforeEach(() => {
        globalThis.__cortexChrome.storage.local.__reset({});
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it("renders an error banner and disables the CTA on INTERVENTION_FAILED", async () => {
        const { container, cleanup } = await renderPopup();
        try {
            // Establish the active intervention via the trigger first.
            await act(async () => {
                dispatchToPopup({
                    type: "INTERVENTION_TRIGGER",
                    payload: FAKE_INTERVENTION,
                });
                await new Promise((r) => setTimeout(r, 0));
            });

            await act(async () => {
                dispatchToPopup({
                    type: "INTERVENTION_FAILED",
                    payload: {
                        intervention_id: "iv-fc-1",
                        error_reason: "Extension lacks tab permission",
                        failed_action_types: ["close_tab"],
                    },
                });
                await new Promise((r) => setTimeout(r, 0));
            });

            const banner = container.querySelector(
                '[data-testid="intervention-error-banner"]',
            );
            expect(banner).not.toBeNull();
            expect(banner!.textContent).toContain("Extension lacks tab permission");

            // The primary apply CTA must be disabled.
            const buttons = Array.from(
                container.querySelectorAll("button"),
            ) as HTMLButtonElement[];
            const cta = buttons.find((b) =>
                /Couldn't apply/i.test(b.textContent || ""),
            );
            expect(cta).toBeDefined();
            expect(cta!.disabled).toBe(true);
        } finally {
            await cleanup();
        }
    });

    it("synthesizes a banner body from failed_action_types when no reason given", async () => {
        const { container, cleanup } = await renderPopup();
        try {
            await act(async () => {
                dispatchToPopup({
                    type: "INTERVENTION_TRIGGER",
                    payload: FAKE_INTERVENTION,
                });
                await new Promise((r) => setTimeout(r, 0));
            });
            await act(async () => {
                dispatchToPopup({
                    type: "INTERVENTION_FAILED",
                    payload: {
                        intervention_id: "iv-fc-1",
                        failed_action_types: ["mute_tab"],
                    },
                });
                await new Promise((r) => setTimeout(r, 0));
            });

            const banner = container.querySelector(
                '[data-testid="intervention-error-banner"]',
            );
            expect(banner).not.toBeNull();
            expect(banner!.textContent?.toLowerCase()).toContain("mute tab");
        } finally {
            await cleanup();
        }
    });

    it("renders the prompt text inline on INTERVENTION_PROMPT", async () => {
        const { container, cleanup } = await renderPopup();
        try {
            await act(async () => {
                dispatchToPopup({
                    type: "INTERVENTION_PROMPT",
                    payload: {
                        action_type: "prompt_micro_commit",
                        prompt: "Commit the one line you just changed?",
                        timeout_seconds: 120,
                        metadata: {},
                    },
                });
                await new Promise((r) => setTimeout(r, 0));
            });

            const prompt = container.querySelector(
                '[data-testid="intervention-prompt"]',
            );
            expect(prompt).not.toBeNull();
            expect(prompt!.textContent).toContain(
                "Commit the one line you just changed?",
            );
        } finally {
            await cleanup();
        }
    });
});
