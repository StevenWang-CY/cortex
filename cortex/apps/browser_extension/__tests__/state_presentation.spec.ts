import { describe, expect, it } from "vitest";
import { currentSignalView, stateAgeMs, statePresentationFields, STATE_FRESHNESS_MS } from "../lib/state-presentation";
import type { CortexState } from "../lib/popup-view-model";
import { pulseUnavailableCopy } from "../lib/popup-view-model";

const state = (overrides: Partial<CortexState> = {}): CortexState => ({
    state: "FLOW", status: "estimated", confidence: 0.8, scores: {}, signal_quality: {}, dwell_seconds: 2,
    ...statePresentationFields({ biometrics: { heart_rate: 72, blink_rate: 0 } }), ...overrides,
});
const labels = { FLOW: "Steady activity", UNKNOWN: "No estimate" };

describe("live presentation continuity", () => {
    it("preserves camera and storage readiness in cached presentation", () => {
        const fields = statePresentationFields({
            biometrics: { heart_rate: 72, blink_rate: Infinity },
            capture: { frames_flowing: false, face_detected: true, stale: true,
                pulse_unavailable: { code: "filling", message: "filling", observed: 2, required: 10 } },
            store: { degraded: true },
        });
        expect(fields.biometrics?.heart_rate).toBe(72);
        expect(fields.biometrics?.blink_rate).toBeNull();
        expect(fields.capture?.pulse_unavailable?.required).toBe(10);
        expect(fields.capture?.frames_flowing).toBe(false);
        expect(fields.store?.degraded).toBe(true);
    });
    it("clears pulse on every invalidation instead of retaining the last truthy value", () => {
        const valid = state();
        expect(currentSignalView(valid, true, 0, labels).heartRate).toBe(72);
        expect(currentSignalView(valid, true, 0, labels).blinkRate).toBe(0);
        for (const capture of [{ stale: true }, { frames_flowing: false }, { face_detected: false },
            { pulse_unavailable: { code: "filling", message: "not ready" } }]) {
            expect(currentSignalView(state({ capture }), true, 0, labels).heartRate).toBeNull();
        }
        for (const heart_rate of [null, 0, -1, 29, 221, NaN, Infinity]) {
            const invalid = state({ biometrics: { ...valid.biometrics!, heart_rate } });
            expect(currentSignalView(invalid, true, 0, labels).heartRate).toBeNull();
        }
        expect(currentSignalView(valid, false, 0, labels).heartRate).toBeNull();
        expect(currentSignalView(null, true, 0, labels).heartRate).toBeNull();
    });
    it("keeps support readiness separate from the optional camera estimate", () => {
        const warming = currentSignalView(state({ status: "warming_up" }), true, 0, labels);
        expect(warming.stateKey).toBe("UNKNOWN");
        expect(warming.label).toBe("Still gathering");
        expect(warming.heartRate).toBe(72);
    });
    it("expires by sample age without comparing monotonic clocks across contexts", () => {
        expect(stateAgeMs(9000, 20, 1020)).toBe(STATE_FRESHNESS_MS);
        expect(stateAgeMs(null, 20, 1020)).toBe(Infinity);
        const expired = currentSignalView(state(), true, STATE_FRESHNESS_MS, labels);
        expect(expired.heartRate).toBeNull();
        expect(expired.stateKey).toBe("UNKNOWN");
        expect(expired.label).toBe("Waiting for fresh signals");
    });
    it("rejects malformed or regressed age metadata", () => {
        for (const age of [-1, NaN, Infinity]) {
            expect(stateAgeMs(age, 100, 200)).toBe(Infinity);
            expect(currentSignalView(state(), true, age, labels).heartRate).toBeNull();
        }
        expect(stateAgeMs(0, 100, 99)).toBe(Infinity);
        expect(stateAgeMs(0, NaN, 200)).toBe(Infinity);
    });
    it("never renders non-finite or negative warmup progress", () => {
        for (const observed of [NaN, Infinity, -1, null, true]) {
            expect(pulseUnavailableCopy({ code: "filling", observed, required: 10 })).toBe("Reading your pulse…");
        }
        expect(pulseUnavailableCopy({ code: "filling", observed: 2.5, required: 10 })).toBe("Reading your pulse… 3 of 10 s");
    });
});
