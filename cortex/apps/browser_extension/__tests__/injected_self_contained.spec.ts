/**
 * Every function handed to `chrome.scripting.executeScript` must be
 * self-contained.
 *
 * Chrome serialises only the function's own source and evaluates it in the
 * page's isolated world, where nothing from the service worker's module scope
 * exists. Each of these modules says so in prose — "no imports and no outer
 * closures", "Self-contained by construction — do not reference anything
 * outside this function body" — and each also imports helpers at module level
 * for its *worker-side* builder. Today the injected halves genuinely touch
 * none of them, but nothing enforced that: a future edit reaching for one of
 * those helpers would type-check, pass every existing spec (they call the
 * function directly, with module scope intact), and then throw
 * `ReferenceError` in the page on the next real intervention.
 *
 * Rebuilding each function through `new Function` reproduces the serialization
 * boundary: the rebuilt copy evaluates in global scope, so any surviving
 * module-scope reference throws where the current tests cannot see it.
 */

import { beforeEach, describe, expect, it } from "vitest";
import type { InterventionTriggerPayload } from "../types/generated/cortex_schemas";
import {
    buildInterventionPanelModel,
    injectInterventionPanel,
} from "../bg/surfaces/intervention-panel";
import { buildCoachPanelModel, injectCoachPanel } from "../bg/surfaces/coach-panel";
import { injectDistractionInterceptor } from "../bg/surfaces/interceptor";
import { removeCortexOverlay } from "../bg/surfaces/remove-overlay";
import { injectCortexToast } from "../bg/surfaces/toast";
import { surfaceCss } from "../bg/surfaces/tokens";

const CSS = surfaceCss();

const PAYLOAD = {
    intervention_id: "selfcontained-1",
    level: "guided_mode",
    situation_summary: "Support is ready.",
    headline: "Choose one next step",
    primary_focus: "The failing test",
    micro_steps: [{ text: "Open the failing test", status: "pending" }],
    ui_plan: { show_overlay: true, dim_background: false },
    suggested_actions: [],
    execution_mode: "suggest_only",
} satisfies InterventionTriggerPayload;

/** Re-evaluate `fn` in global scope, exactly as the page receives it. */
function rebuild<T extends (...args: never[]) => unknown>(fn: T): T {
    // eslint-disable-next-line @typescript-eslint/no-implied-eval
    return new Function(`return (${fn.toString()})`)() as T;
}

describe("injected page surfaces are self-contained", () => {
    beforeEach(() => {
        document.body.innerHTML = "";
        Object.defineProperty(document, "hasFocus", {
            configurable: true,
            value: () => true,
        });
    });

    it("injectInterventionPanel survives the serialization boundary", () => {
        const model = buildInterventionPanelModel(PAYLOAD, []);
        expect(() => rebuild(injectInterventionPanel)(model, CSS)).not.toThrow();
        expect(document.getElementById("cortex-somatic-overlay")).not.toBeNull();
    });

    it("injectCoachPanel survives the serialization boundary", () => {
        const model = buildCoachPanelModel("recap", {
            headline: "Take one step",
            body: "You have been on this for a while.",
            tags: ["focus"],
        });
        expect(() => rebuild(injectCoachPanel)(model, CSS)).not.toThrow();
    });

    it("injectCortexToast survives the serialization boundary", () => {
        expect(() =>
            rebuild(injectCortexToast)("Saved", "Session stored", CSS),
        ).not.toThrow();
    });

    it("injectDistractionInterceptor survives the serialization boundary", () => {
        expect(() =>
            rebuild(injectDistractionInterceptor)(
                {
                    focusMin: 25,
                    streakMin: 12,
                    distractionsBlocked: 3,
                    domain: "example.com",
                },
                CSS,
            ),
        ).not.toThrow();
    });

    it("removeCortexOverlay survives the serialization boundary", () => {
        expect(() => rebuild(removeCortexOverlay)()).not.toThrow();
    });
});
