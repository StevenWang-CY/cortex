import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLatestSocket } from "../test/mocks/websocket";

const hideTabsSpy = vi.hoisted(() => vi.fn(() => Promise.resolve(null)));

vi.mock("../tab-manager", async (importOriginal) => {
    const actual = await importOriginal<typeof import("../tab-manager")>();
    return {
        ...actual,
        hideNonActiveTabs: hideTabsSpy,
    };
});

type RuntimeListener = (
    message: Record<string, unknown>,
    sender: unknown,
    sendResponse: (response: unknown) => void,
) => unknown;

describe("proposal-only workspace authority", () => {
    beforeEach(() => {
        vi.resetModules();
        hideTabsSpy.mockClear();
    });

    it("never hides tabs for an INTERVENTION_TRIGGER, even in authorized mode", async () => {
        await import("../background");
        await new Promise((resolve) => setTimeout(resolve, 0));
        const socket = getLatestSocket();
        expect(socket).not.toBeNull();

        socket!.__deliver({
            type: "INTERVENTION_TRIGGER",
            payload: {
                intervention_id: "iv-proposal",
                level: "simplified_workspace",
                execution_mode: "authorized",
                headline: "A proposal",
                suggested_actions: [],
                hide_targets: ["browser_tabs_except_active"],
                ui_plan: {
                    show_overlay: false,
                    dim_background: false,
                    fold_unrelated_code: true,
                },
            },
            timestamp: Date.now() / 1000,
            sequence: 1,
        });
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(hideTabsSpy).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.tabs.remove).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.tabs.update).not.toHaveBeenCalled();
    });

    it("rejects runtime action execution by default", async () => {
        const background = await import("../background");
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(background._getExecutionMode()).toBe("suggest_only");

        const listener = globalThis.__cortexChrome.runtime.onMessage.addListener
            .mock.calls[0][0] as RuntimeListener;
        let response: unknown;
        listener(
            {
                type: "EXECUTE_ACTION",
                action: {
                    action_id: "a-close",
                    action_type: "close_tab",
                    tab_index: 0,
                },
            },
            undefined,
            (value) => {
                response = value;
            },
        );

        expect(response).toMatchObject({
            success: false,
            message: "Action unavailable in suggest-only mode",
        });
        expect(globalThis.__cortexChrome.tabs.remove).not.toHaveBeenCalled();
    });

    it("drops legacy physiology-derived prompts without presentation or mutation", async () => {
        await import("../background");
        await new Promise((resolve) => setTimeout(resolve, 0));
        const socket = getLatestSocket();
        expect(socket).not.toBeNull();
        globalThis.__cortexChrome.tabs.query.mockClear();
        globalThis.__cortexChrome.runtime.sendMessage.mockClear();

        for (const type of [
            "BREATHING_OVERLAY",
            "PRE_BREAK_WARNING",
            "BREAK_RECOMMENDATION",
        ]) {
            socket!.__deliver({
                type,
                payload: {
                    headline: "Unsupported claim",
                    reason: "stress_integral_crossed_threshold",
                },
                timestamp: Date.now() / 1000,
                sequence: 10,
            });
        }
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(globalThis.__cortexChrome.tabs.query).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.runtime.sendMessage).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.tabs.remove).not.toHaveBeenCalled();
        expect(globalThis.__cortexChrome.tabs.update).not.toHaveBeenCalled();
    });
});
