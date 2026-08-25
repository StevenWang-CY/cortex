import { beforeEach, describe, expect, it, vi } from "vitest";

const PAYLOAD = {
    intervention_id: "motion-1",
    headline: "Choose one next step",
    situation_summary: "Support is ready.",
    micro_steps: ["Open the failing test"],
    suggested_actions: [],
    execution_mode: "suggestions_only",
};

describe("injected surface motion", () => {
    beforeEach(() => {
        vi.resetModules();
        vi.useFakeTimers();
        document.body.innerHTML = "";
    });

    it("updates an intervention host without replaying its entrance", async () => {
        const { injectOverlay } = await import("../background");
        injectOverlay(PAYLOAD);
        const original = document.getElementById("cortex-somatic-overlay");
        expect(original).not.toBeNull();
        expect(original?.shadowRoot?.querySelector(".pn.cx-update")).toBeNull();

        injectOverlay({ ...PAYLOAD, headline: "Updated support" });
        const updated = document.getElementById("cortex-somatic-overlay");
        expect(updated).toBe(original);
        expect(updated?.shadowRoot?.querySelector(".pn.cx-update")).not.toBeNull();
        expect(updated?.shadowRoot?.textContent).toContain("Updated support");

        const dismiss = updated?.shadowRoot?.getElementById("dm") as HTMLButtonElement;
        dismiss.click();
        expect(updated?.shadowRoot?.querySelector(".pn.cx-exit")).not.toBeNull();
        vi.advanceTimersByTime(170);
        expect(document.getElementById("cortex-somatic-overlay")).toBeNull();
    });

    it("keeps the lockout centered throughout entry and exit", async () => {
        const { injectLockoutOverlay } = await import("../background");
        injectLockoutOverlay({ duration_s: 10, reason: "Pause briefly." });
        const host = document.getElementById("cortex-lockout-overlay");
        const css = host?.shadowRoot?.querySelector("style")?.textContent ?? "";
        expect(css).toContain("translate(-50%,calc(-50% + 8px))");
        expect(css).toContain("translate(-50%,-50%)");

        injectLockoutOverlay({ duration_s: 9, reason: "Still pausing." });
        expect(document.getElementById("cortex-lockout-overlay")).toBe(host);
        expect(host?.shadowRoot?.querySelector(".pn.cx-update")).not.toBeNull();
        (host?.shadowRoot?.getElementById("skip") as HTMLButtonElement).click();
        expect(host?.shadowRoot?.querySelector(".pn.cx-exit")).not.toBeNull();
        vi.advanceTimersByTime(170);
        expect(document.getElementById("cortex-lockout-overlay")).toBeNull();
    });

    it("gives the coach card a symmetric dismissal and static reduced path", async () => {
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
        const { injectLeetCodeCoachOverlay } = await import("../background");
        injectLeetCodeCoachOverlay("LEETCODE_SHOW_PATTERN_LADDER", {
            tags: ["graph"],
        });
        const host = document.getElementById("cortex-leetcode-coach");
        const css = host?.shadowRoot?.querySelector("style")?.textContent ?? "";
        expect(css).toContain("@keyframes out");
        expect(css).toContain("prefers-reduced-motion");

        (host?.shadowRoot?.getElementById("lc-close") as HTMLButtonElement).click();
        expect(document.getElementById("cortex-leetcode-coach")).toBeNull();
    });
});
