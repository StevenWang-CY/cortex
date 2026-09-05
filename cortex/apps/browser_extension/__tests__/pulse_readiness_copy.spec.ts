import { describe, expect, it } from "vitest";

import { pulseUnavailableCopy } from "../lib/popup-view-model";

describe("pulseUnavailableCopy (audit S10)", () => {
    it("keeps the neutral copy when the daemon gives no reason", () => {
        expect(pulseUnavailableCopy(undefined)).toBe("Reading your pulse…");
        expect(pulseUnavailableCopy("garbage")).toBe("Reading your pulse…");
        expect(pulseUnavailableCopy({ code: "something_new" })).toBe("Reading your pulse…");
    });

    it("says how far the window has filled", () => {
        expect(
            pulseUnavailableCopy({ code: "filling", message: "filling 4.2/10 s", observed: 4.2, required: 10 }),
        ).toBe("Reading your pulse… 4 of 10 s");
    });

    it("names the real blocker from the dominant missing reason", () => {
        expect(
            pulseUnavailableCopy({ code: "valid_fraction_below_gate", message: "x", missing_reason: "NO_FACE" }),
        ).toBe("Stay in view for a pulse reading");
        expect(pulseUnavailableCopy({ code: "low_light", message: "x", missing_reason: "LOW_LIGHT" })).toMatch(
            /^Too dark/,
        );
        expect(pulseUnavailableCopy({ code: "motion_fraction_above_cap", message: "x" })).toBe(
            "Hold still for a pulse reading",
        );
        expect(pulseUnavailableCopy({ code: "no_observations", message: "x" })).toBe("Waiting for the camera…");
    });
});
