/**
 * Injected page surfaces: wire-shape fidelity, interruptible motion, focus
 * contract, Escape ownership, expiry semantics, and the shared apply
 * machine — exercised directly on the self-contained functions the worker
 * hands to ``chrome.scripting.executeScript``.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { InterventionTriggerPayload } from "../types/generated/cortex_schemas";
import {
    buildInterventionPanelModel,
    injectInterventionPanel,
} from "../bg/surfaces/intervention-panel";
import { buildCoachPanelModel, injectCoachPanel } from "../bg/surfaces/coach-panel";
import { injectDistractionInterceptor } from "../bg/surfaces/interceptor";
import { removeCortexOverlay } from "../bg/surfaces/remove-overlay";
import { surfaceCss } from "../bg/surfaces/tokens";

// Schema-shaped fixture: ``micro_steps`` are MicroStep objects on the wire
// (1–3 of them); a drift in the generated payload type fails this file.
const PAYLOAD = {
    intervention_id: "motion-1",
    level: "guided_mode",
    situation_summary: "Support is ready.",
    headline: "Choose one next step",
    primary_focus: "The failing test",
    micro_steps: [
        { text: "Open the failing test", status: "pending" },
        { text: "Take a deep breath", status: "pending" },
    ],
    ui_plan: { show_overlay: true, dim_background: false },
    suggested_actions: [],
    execution_mode: "suggest_only",
} satisfies InterventionTriggerPayload;

const CSS = surfaceCss();

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

function host(id = "cortex-somatic-overlay"): HTMLElement | null {
    return document.getElementById(id);
}

function executableModel() {
    const suggested = {
        action_id: "open-1",
        action_type: "open_url",
        target: "https://example.com/reference",
        label: "Open the reference",
        reason: "Keep it nearby",
        category: "recommended",
        reversible: true,
        metadata: {},
    };
    return buildInterventionPanelModel(
        { ...PAYLOAD, execution_mode: "authorized", suggested_actions: [suggested] },
        ["open-1"],
        { autoHideMs: 60_000, undoWindowMs: 1_000 },
    );
}

describe("intervention panel", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        document.body.innerHTML = "";
        Object.defineProperty(document, "hasFocus", { configurable: true, value: () => true });
    });

    it("renders MicroStep objects by their text and drops placeholder steps", () => {
        const model = buildInterventionPanelModel(PAYLOAD, []);
        expect(model.steps).toEqual([{ text: "Open the failing test", done: false }]);
        injectInterventionPanel(model, CSS);
        const text = host()?.shadowRoot?.textContent ?? "";
        expect(text).toContain("Open the failing test");
        expect(text).not.toContain("Take a deep breath");
        expect(text).not.toContain("[object Object]");
        expect(model.ctaLabel).toBeNull();
    });

    it("updates in place without replaying the entrance and exits symmetrically", () => {
        const calls = installResponder();
        injectInterventionPanel(buildInterventionPanelModel(PAYLOAD, []), CSS);
        const original = host();
        const panel = () => original?.shadowRoot?.getElementById("panel");
        expect(panel()?.getAttribute("role")).toBe("region");
        expect(panel()?.getAttribute("data-state")).toBe("open");

        injectInterventionPanel(
            buildInterventionPanelModel({ ...PAYLOAD, headline: "Updated support" }, []),
            CSS,
        );
        expect(host()).toBe(original);
        expect(panel()?.getAttribute("data-state")).toBe("open");
        expect(original?.shadowRoot?.textContent).toContain("Updated support");

        (original?.shadowRoot?.getElementById("dismiss") as HTMLButtonElement).click();
        expect(panel()?.getAttribute("data-state")).toBe("exit");
        vi.advanceTimersByTime(170);
        expect(host()).toBeNull();
        const action = calls.find((c) => c.message.type === "USER_ACTION");
        expect(action?.message.action).toBe("dismissed");
    });

    it("moves focus to the heading on mount and restores it on dismiss", () => {
        const trigger = document.createElement("button");
        document.body.appendChild(trigger);
        trigger.focus();
        injectInterventionPanel(buildInterventionPanelModel(PAYLOAD, []), CSS);
        const shadow = host()?.shadowRoot;
        expect(shadow?.activeElement?.id).toBe("cortex-intervention-title");
        (shadow?.getElementById("close") as HTMLButtonElement).click();
        expect(document.activeElement).toBe(trigger);
    });

    it("never steals focus from a field the user is typing in", () => {
        const input = document.createElement("input");
        document.body.appendChild(input);
        input.focus();
        injectInterventionPanel(buildInterventionPanelModel(PAYLOAD, []), CSS);
        expect(document.activeElement).toBe(input);
        expect(host()?.shadowRoot?.activeElement).toBeNull();
    });

    it("lets Escape belong to the page unless the panel owns focus or the pointer", () => {
        const calls = installResponder();
        const input = document.createElement("input");
        document.body.appendChild(input);
        input.focus();
        injectInterventionPanel(buildInterventionPanelModel(PAYLOAD, []), CSS);

        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        expect(host()).not.toBeNull();
        expect(calls.some((c) => c.message.type === "USER_ACTION")).toBe(false);

        host()?.shadowRoot?.getElementById("panel")?.dispatchEvent(new Event("mouseenter"));
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        vi.advanceTimersByTime(170);
        expect(host()).toBeNull();
        expect(calls.find((c) => c.message.type === "USER_ACTION")?.message.action).toBe("dismissed");
    });

    it("reports an untouched panel as expired, never as dismissed", () => {
        const calls = installResponder();
        injectInterventionPanel(
            buildInterventionPanelModel(PAYLOAD, [], { autoHideMs: 1_000 }),
            CSS,
        );
        vi.advanceTimersByTime(1_000);
        const action = calls.find((c) => c.message.type === "USER_ACTION");
        expect(action?.message.action).toBe("expired");
        vi.advanceTimersByTime(170);
        expect(host()).toBeNull();
    });

    it("runs idle → pending → applied → undo with a single request", () => {
        const calls = installResponder();
        injectInterventionPanel(executableModel(), CSS);
        const shadow = host()!.shadowRoot!;
        const cta = shadow.getElementById("cta") as HTMLButtonElement;
        expect(cta.textContent).toBe("Open recommended page");

        cta.click();
        cta.click();
        const executes = calls.filter((c) => c.message.type === "EXECUTE_ALL_RECOMMENDED");
        expect(executes).toHaveLength(1);
        expect(cta.disabled).toBe(true);
        expect(cta.getAttribute("data-phase")).toBe("pending");
        expect(cta.textContent).toBe("Applying…");

        executes[0].respond({
            ok: true,
            results: [{ action_id: "open-1", success: true, message: "ok", reversible: true }],
            outcome: { phase: "applied", applied: 1, total: 1, reason: null },
        });
        expect(cta.getAttribute("data-phase")).toBe("applied");
        expect(cta.textContent).toBe("Applied");
        const undo = shadow.getElementById("undo") as HTMLButtonElement;
        expect(undo.hidden).toBe(false);
        expect(shadow.getElementById("dismiss")?.textContent).toBe("Done");

        // The undo window keeps the panel on screen.
        vi.advanceTimersByTime(900);
        expect(host()).not.toBeNull();

        undo.click();
        const undoCall = calls.find((c) => c.message.type === "UNDO_ALL_RECENT");
        expect(undoCall?.message.intervention_id).toBe("motion-1");
        undoCall!.respond({ ok: true });
        expect(cta.textContent).toBe("Restored");
        expect(undo.hidden).toBe(true);
        expect(shadow.getElementById("dismiss")?.textContent).toBe("Dismiss");
        expect(calls.some((c) => c.message.type === "USER_ACTION")).toBe(false);
    });

    it("renders partial and failed outcomes honestly", () => {
        const calls = installResponder();
        injectInterventionPanel(executableModel(), CSS);
        const shadow = host()!.shadowRoot!;
        const cta = shadow.getElementById("cta") as HTMLButtonElement;
        cta.click();
        calls[0].respond({
            ok: true,
            results: [],
            outcome: { phase: "partial", applied: 2, total: 3, reason: "one tab moved" },
        });
        expect(cta.textContent).toBe("2 of 3 applied");
        expect((shadow.getElementById("undo") as HTMLButtonElement).hidden).toBe(false);

        document.body.innerHTML = "";
        const failedCalls = installResponder();
        injectInterventionPanel(executableModel(), CSS);
        const failedCta = host()!.shadowRoot!.getElementById("cta") as HTMLButtonElement;
        failedCta.click();
        failedCalls[0].respond({
            ok: false,
            results: [],
            outcome: { phase: "failed", applied: 0, total: 1, reason: "this suggestion has expired" },
        });
        expect(failedCta.getAttribute("data-phase")).toBe("failed");
        expect(failedCta.textContent).toBe("Nothing changed — this suggestion has expired");
        expect(failedCta.disabled).toBe(true);
        expect((host()!.shadowRoot!.getElementById("undo") as HTMLButtonElement).hidden).toBe(true);
    });
});

describe("coach panel", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        document.body.innerHTML = "";
        Object.defineProperty(document, "hasFocus", { configurable: true, value: () => true });
    });

    it("indexes into an honest hint ladder and keeps no dead-end fields", () => {
        const model = buildCoachPanelModel("LEETCODE_SHOW_PATTERN_LADDER", { tags: ["graph"] });
        expect(model.hints).toHaveLength(3);
        injectCoachPanel(model, CSS);
        const shadow = host("cortex-leetcode-coach")!.shadowRoot!;
        expect(shadow.querySelector("textarea")).toBeNull();
        expect(shadow.querySelector('.cx-panel[role="region"]')).not.toBeNull();
        const reveal = shadow.getElementById("reveal") as HTMLButtonElement;
        const hint = shadow.getElementById("hint") as HTMLElement;
        expect(reveal.textContent).toBe("Reveal hint 1 of 3");
        reveal.click();
        expect(hint.textContent).toContain("Hint 1 of 3");
        expect(hint.textContent).toContain("graph");
        reveal.click();
        expect(hint.textContent).toContain("Hint 2 of 3");
        expect(reveal.textContent).toBe("Reveal hint 3 of 3");
        reveal.click();
        expect(hint.textContent).toContain("Hint 3 of 3");
        expect(reveal.disabled).toBe(true);
        expect(reveal.textContent).toBe("All hints shown");
    });

    it("focuses its heading, honours Escape while focused, and restores focus", () => {
        const trigger = document.createElement("button");
        document.body.appendChild(trigger);
        trigger.focus();
        injectCoachPanel(buildCoachPanelModel("LEETCODE_SHOW_SCRATCHPAD", {}), CSS);
        const shadow = host("cortex-leetcode-coach")!.shadowRoot!;
        expect(shadow.activeElement?.id).toBe("cortex-coach-title");
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        vi.advanceTimersByTime(170);
        expect(host("cortex-leetcode-coach")).toBeNull();
        expect(document.activeElement).toBe(trigger);
    });

    it("removes immediately under Reduce Motion", () => {
        Object.defineProperty(window, "matchMedia", {
            configurable: true,
            value: vi.fn(() => ({
                matches: true,
                media: "(prefers-reduced-motion: reduce)",
                onchange: null,
                addListener: vi.fn(),
                removeListener: vi.fn(),
                addEventListener: vi.fn(),
                removeEventListener: vi.fn(),
                dispatchEvent: vi.fn(() => false),
            })),
        });
        injectCoachPanel(buildCoachPanelModel("LEETCODE_SHOW_CONSOLIDATION", {}), CSS);
        const shadow = host("cortex-leetcode-coach")!.shadowRoot!;
        expect(shadow.querySelector("style")?.textContent).toContain("prefers-reduced-motion");
        (shadow.getElementById("close") as HTMLButtonElement).click();
        expect(host("cortex-leetcode-coach")).toBeNull();
    });
});

describe("distraction interceptor", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        document.body.innerHTML = "";
    });

    const model = { focusMin: 12, streakMin: 5, distractionsBlocked: 3, domain: "reddit.com" };

    it("is a labelled keyboard modal that closes a fresh tab instead of leaving it blank", () => {
        const calls = installResponder();
        const trigger = document.createElement("button");
        document.body.appendChild(trigger);
        trigger.focus();
        injectDistractionInterceptor(model, CSS);
        const shadow = host("cortex-distraction-interceptor")!.shadowRoot!;
        const dialog = shadow.querySelector('[role="dialog"]');
        expect(dialog?.getAttribute("aria-modal")).toBe("true");
        expect(dialog?.getAttribute("data-state")).toBe("open");
        expect(shadow.activeElement?.id).toBe("back");

        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
        expect(shadow.activeElement?.id).toBe("continue");

        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        const blocked = calls.find((c) => c.message.type === "DISTRACTION_BLOCKED");
        expect(blocked?.message.leave).toBe("close");
        vi.advanceTimersByTime(170);
        expect(host("cortex-distraction-interceptor")).toBeNull();
        expect(document.activeElement).toBe(trigger);
    });

    it("goes back when there is history to return to", () => {
        const calls = installResponder();
        window.history.pushState({}, "", "#reddit");
        const back = vi.spyOn(window.history, "back").mockImplementation(() => undefined);
        injectDistractionInterceptor(model, CSS);
        (host("cortex-distraction-interceptor")!.shadowRoot!.getElementById("back") as HTMLButtonElement).click();
        expect(calls.find((c) => c.message.type === "DISTRACTION_BLOCKED")?.message.leave).toBe("back");
        expect(back).toHaveBeenCalledTimes(1);
        back.mockRestore();
    });
});

describe("removeCortexOverlay", () => {
    beforeEach(() => {
        vi.useFakeTimers();
        document.body.innerHTML = "";
    });

    it("tears down every Cortex surface and reports whether anything was removed", () => {
        injectInterventionPanel(buildInterventionPanelModel(PAYLOAD, []), CSS);
        injectCoachPanel(buildCoachPanelModel("LEETCODE_SHOW_SCRATCHPAD", {}), CSS);
        expect(removeCortexOverlay()).toBe(true);
        expect(host()).toBeNull();
        expect(host("cortex-leetcode-coach")).toBeNull();
        expect(removeCortexOverlay()).toBe(false);
    });
});
