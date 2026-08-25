import { beforeEach, describe, expect, it, vi } from "vitest";

type MediaChange = (event: { matches: boolean }) => void;

function installMotionEnvironment(initialReduced = false) {
    let reduced = initialReduced;
    const listeners = new Set<MediaChange>();
    Object.defineProperty(window, "matchMedia", {
        configurable: true,
        value: vi.fn(() => ({
            get matches() { return reduced; },
            media: "(prefers-reduced-motion: reduce)",
            onchange: null,
            addListener: vi.fn(),
            removeListener: vi.fn(),
            addEventListener: vi.fn((_type: string, listener: MediaChange) => {
                listeners.add(listener);
            }),
            removeEventListener: vi.fn((_type: string, listener: MediaChange) => {
                listeners.delete(listener);
            }),
            dispatchEvent: vi.fn(() => false),
        })),
    });

    const frames: FrameRequestCallback[] = [];
    const request = vi.fn((callback: FrameRequestCallback) => {
        frames.push(callback);
        return frames.length;
    });
    const cancel = vi.fn();
    vi.stubGlobal("requestAnimationFrame", request);
    vi.stubGlobal("cancelAnimationFrame", cancel);

    return {
        cancel,
        frames,
        request,
        setReduced(next: boolean) {
            reduced = next;
            listeners.forEach((listener) => listener({ matches: next }));
        },
    };
}

function dispatchAmbient(payload: Record<string, unknown>): void {
    const event = globalThis.__cortexChrome.runtime.onMessage as unknown as {
        __dispatch: (...args: unknown[]) => unknown[];
    };
    event.__dispatch(
        { type: "AMBIENT_STATE_UPDATE", payload },
        {},
        vi.fn(),
    );
}

async function importAmbient(): Promise<void> {
    await import("../contents/ambient");
    if (!document.getElementById("cortex-ambient-engine")) {
        document.dispatchEvent(new Event("DOMContentLoaded"));
    }
}

const ESTIMATED = {
    state: "FLOW",
    status: "estimated",
    evidence_coverage: 1,
    confidence: 0.9,
    scores: { flow: 0.7, hyper: 0.1 },
    dwell_seconds: 181,
};

describe("ambient motion lifecycle", () => {
    beforeEach(() => {
        vi.resetModules();
        vi.useRealTimers();
        document.body.innerHTML = "";
        Object.defineProperty(document, "visibilityState", {
            configurable: true,
            value: "visible",
        });
    });

    it("does not keep an idle frame loop before evidence is eligible", async () => {
        const motion = installMotionEnvironment();
        await importAmbient();
        expect(motion.request).not.toHaveBeenCalled();
    });

    it("pauses a running loop while hidden", async () => {
        const motion = installMotionEnvironment();
        await importAmbient();
        dispatchAmbient(ESTIMATED);
        expect(motion.request).toHaveBeenCalledTimes(1);

        Object.defineProperty(document, "visibilityState", {
            configurable: true,
            value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
        expect(motion.cancel).toHaveBeenCalledTimes(1);
    });

    it("uses a static color state and no recurring frame under Reduce Motion", async () => {
        const motion = installMotionEnvironment(true);
        await importAmbient();
        dispatchAmbient(ESTIMATED);

        expect(motion.request).not.toHaveBeenCalled();
        const host = document.getElementById("cortex-ambient-engine");
        const aura = host?.children.item(0) as HTMLElement;
        expect(aura.style.transition).toContain("background-color 160ms");
        expect(aura.style.boxShadow).toBe("none");
        expect(aura.style.backgroundColor).not.toBe("");

        motion.setReduced(false);
        expect(motion.request).toHaveBeenCalledTimes(1);
    });

    it("cancels a pending shield restore when flow resumes", async () => {
        vi.useFakeTimers();
        const motion = installMotionEnvironment();
        const distractor = document.createElement("aside");
        distractor.className = "newsletter-panel";
        distractor.style.cssText = "color: red; opacity: 0.8;";
        document.body.appendChild(distractor);
        const originalStyle = distractor.style.cssText;
        await importAmbient();

        let now = 1_000;
        dispatchAmbient(ESTIMATED);
        motion.frames.shift()?.(now);
        expect(distractor.style.opacity).toBe("0.05");

        dispatchAmbient({ ...ESTIMATED, state: "RECOVERY", dwell_seconds: 0 });
        motion.frames.shift()?.(now += 100);
        expect(distractor.style.transition).toContain("200ms");

        dispatchAmbient(ESTIMATED);
        motion.frames.shift()?.(now += 100);
        vi.advanceTimersByTime(300);
        expect(distractor.style.opacity).toBe("0.05");

        dispatchAmbient({ ...ESTIMATED, state: "RECOVERY", dwell_seconds: 0 });
        motion.frames.shift()?.(now += 100);
        vi.advanceTimersByTime(230);
        expect(distractor.style.cssText).toBe(originalStyle);
    });
});
