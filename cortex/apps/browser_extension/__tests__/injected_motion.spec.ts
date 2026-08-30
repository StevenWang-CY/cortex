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
        expect(original?.shadowRoot?.querySelector('.pn[role="region"]')).not.toBeNull();
        expect(
            original?.shadowRoot?.getElementById("cortex-intervention-summary")
                ?.getAttribute("aria-live"),
        ).toBe("polite");
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

    it("treats lockout as a keyboard modal and restores prior focus", async () => {
        const trigger = document.createElement("button");
        trigger.textContent = "Continue reading";
        document.body.appendChild(trigger);
        trigger.focus();

        const { injectLockoutOverlay } = await import("../background");
        injectLockoutOverlay({ duration_s: 10, reason: "Pause briefly." });

        const host = document.getElementById("cortex-lockout-overlay");
        const dialog = host?.shadowRoot?.querySelector('[role="dialog"]');
        const skip = host?.shadowRoot?.getElementById("skip") as HTMLButtonElement;
        const timer = host?.shadowRoot?.querySelector('[role="timer"]');
        expect(dialog?.getAttribute("aria-modal")).toBe("true");
        expect(dialog?.getAttribute("aria-describedby")).toContain("cortex-lockout-countdown");
        expect(timer).not.toBeNull();
        expect(timer?.textContent).toBe("0:10");
        expect(host?.shadowRoot?.activeElement).toBe(skip);

        vi.advanceTimersByTime(1000);
        expect(timer?.textContent).toBe("0:09");

        document.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Tab",
            bubbles: true,
        }));
        expect(host?.shadowRoot?.activeElement).toBe(skip);

        document.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Escape",
            bubbles: true,
        }));
        vi.advanceTimersByTime(170);
        expect(document.getElementById("cortex-lockout-overlay")).toBeNull();
        expect(document.activeElement).toBe(trigger);
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
        expect(host?.shadowRoot?.querySelector('.card[role="region"]')).not.toBeNull();
        expect(host?.shadowRoot?.querySelector('.card[role="dialog"]')).toBeNull();

        (host?.shadowRoot?.getElementById("lc-close") as HTMLButtonElement).click();
        expect(document.getElementById("cortex-leetcode-coach")).toBeNull();
    });
});
