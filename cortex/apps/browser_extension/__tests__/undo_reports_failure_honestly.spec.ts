/**
 * Undo must report what actually happened.
 *
 * Both undo surfaces previously reported success unconditionally: the page
 * panel discarded `chrome.runtime.lastError` (`void chrome.runtime.lastError`)
 * and ignored the response entirely, so a user whose tabs were never reopened
 * was still told "Changes undone." The background handler could not even
 * report a failure — `undoAllRecent()` had no `.catch`, so a rejection left
 * `sendResponse` uncalled and closed the port, which the surfaces then read as
 * success.
 *
 * These tests pin the honest behaviour at the surface: on any failure the
 * applied state is retained and Undo stays available for a retry.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { InterventionTriggerPayload } from "../types/generated/cortex_schemas";
import {
    buildInterventionPanelModel,
    injectInterventionPanel,
} from "../bg/surfaces/intervention-panel";
import { surfaceCss } from "../bg/surfaces/tokens";

const CSS = surfaceCss();

const PAYLOAD = {
    intervention_id: "undo-1",
    level: "guided_mode",
    situation_summary: "Support is ready.",
    headline: "Choose one next step",
    primary_focus: "The failing test",
    micro_steps: [{ text: "Open the failing test", status: "pending" }],
    ui_plan: { show_overlay: true, dim_background: false },
    suggested_actions: [],
    execution_mode: "suggest_only",
} satisfies InterventionTriggerPayload;

type Listener = (raw: unknown) => void;

function installResponder() {
    const calls: Array<{ message: Record<string, unknown>; respond: Listener }> = [];
    const fake = globalThis.__cortexChrome;
    fake.runtime.sendMessage = vi.fn(
        (message: Record<string, unknown>, cb?: Listener) => {
            calls.push({ message, respond: cb ?? (() => undefined) });
            return Promise.resolve(undefined);
        },
    ) as unknown as typeof fake.runtime.sendMessage;
    return calls;
}

/** Invoke a pending callback with ``chrome.runtime.lastError`` set, the way
 *  the real runtime signals "the service worker never answered". */
function respondWithLastError(respond: Listener, message: string) {
    const fake = globalThis.__cortexChrome as unknown as {
        runtime: { lastError?: { message: string } };
    };
    fake.runtime.lastError = { message };
    try {
        respond(undefined);
    } finally {
        delete fake.runtime.lastError;
    }
}

function executableModel() {
    return buildInterventionPanelModel(
        {
            ...PAYLOAD,
            execution_mode: "authorized",
            suggested_actions: [{
                action_id: "open-1",
                action_type: "open_url",
                target: "https://example.com/reference",
                label: "Open the reference",
                reason: "Keep it nearby",
                category: "recommended",
                reversible: true,
                metadata: {},
            }],
        },
        ["open-1"],
        { autoHideMs: 60_000, undoWindowMs: 1_000 },
    );
}

function host(): HTMLElement | null {
    return document.getElementById("cortex-somatic-overlay");
}

/** Apply the proposal so the Undo affordance is on screen, then click Undo. */
function applyThenUndo() {
    const calls = installResponder();
    injectInterventionPanel(executableModel(), CSS);
    const shadow = host()!.shadowRoot!;
    const cta = shadow.getElementById("cta") as HTMLButtonElement;
    cta.click();
    calls[0].respond({
        ok: true,
        results: [],
        outcome: { phase: "applied", applied: 1, total: 1, reason: null },
    });
    const undo = shadow.getElementById("undo") as HTMLButtonElement;
    expect(undo.hidden).toBe(false);
    undo.click();
    const undoCall = calls.find((c) => c.message.type === "UNDO_ALL_RECENT")!;
    return { shadow, cta, undo, undoCall };
}

function statusText(shadow: ShadowRoot): string {
    return shadow.getElementById("status")?.textContent ?? "";
}

describe("undo reports failure honestly", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        document.body.innerHTML = "";
        Object.defineProperty(document, "hasFocus", { configurable: true, value: () => true });
    });

    it("keeps the applied state when the worker never answers", () => {
        const { shadow, cta, undo, undoCall } = applyThenUndo();

        respondWithLastError(undoCall.respond, "The message port closed");

        expect(cta.textContent).not.toBe("Restored");
        expect(statusText(shadow)).toContain("Couldn't undo");
        // The change is still in place, so Undo must remain offered.
        expect(undo.hidden).toBe(false);
        expect(undo.disabled).toBe(false);
        expect(undo.textContent).toBe("Undo");
    });

    it("keeps the applied state when the background reports a failure", () => {
        const { shadow, cta, undo, undoCall } = applyThenUndo();

        undoCall.respond({
            ok: false,
            attempted: 2,
            undone: 0,
            failed: 2,
            reason: "2 of 2 could not be reversed",
        });

        expect(cta.textContent).not.toBe("Restored");
        expect(statusText(shadow)).toContain("Couldn't undo");
        expect(undo.hidden).toBe(false);
        expect(undo.disabled).toBe(false);
    });

    it("names the partial count rather than claiming a full restore", () => {
        const { shadow, cta, undo, undoCall } = applyThenUndo();

        undoCall.respond({
            ok: false,
            attempted: 3,
            undone: 1,
            failed: 2,
            reason: "2 of 3 could not be reversed",
        });

        expect(cta.textContent).not.toBe("Restored");
        expect(statusText(shadow)).toContain("Undid 1 of 3");
        expect(undo.hidden).toBe(false);
    });

    it("still reports a genuine success", () => {
        const { shadow, cta, undo, undoCall } = applyThenUndo();

        undoCall.respond({ ok: true, attempted: 2, undone: 2, failed: 0, reason: null });

        expect(cta.textContent).toBe("Restored");
        expect(statusText(shadow)).toBe("Changes undone.");
        expect(undo.hidden).toBe(true);
    });
});
