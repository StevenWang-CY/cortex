/**
 * A camera that is still starting is not a camera that is offline.
 *
 * Opening the camera and loading the face model takes about twelve seconds.
 * Through that window the popup said "Camera offline — open Cortex Settings
 * to allow camera access" to a user who had already granted the permission.
 * The daemon now marks the window with `capture.starting`.
 */
import { describe, expect, it } from "vitest";

import { supportStateViewModel, type CortexState } from "../lib/popup-view-model";
import { statePresentationFields } from "../lib/state-presentation";

const OFFLINE = "Camera offline — open Cortex Settings to allow camera access";
const STARTING = "Starting camera…";
const labels = { FLOW: "Steady activity", UNKNOWN: "No estimate" };

const state = (capture: CortexState["capture"]): CortexState => ({
    state: "UNKNOWN", status: "insufficient_evidence", confidence: 0, scores: {},
    signal_quality: {}, dwell_seconds: 0, capture,
});

describe("camera start-up is not camera offline", () => {
    it("says the camera is starting while a start is in flight", () => {
        const view = supportStateViewModel(state({ frames_flowing: false, starting: true }), true, labels);
        expect(view.bioStatusMessage).toBe(STARTING);
        expect(view.captureStale).toBe(false);
    });

    it("keeps the offline guidance when no start is in flight", () => {
        expect(supportStateViewModel(state({ frames_flowing: false, starting: false }), true, labels)
            .bioStatusMessage).toBe(OFFLINE);
        // An older daemon does not send the flag.
        expect(supportStateViewModel(state({ frames_flowing: false }), true, labels)
            .bioStatusMessage).toBe(OFFLINE);
    });

    it("ignores the flag once frames flow", () => {
        expect(supportStateViewModel(
            state({ frames_flowing: true, face_detected: false, starting: true }), true, labels,
        ).bioStatusMessage).toBe("Looking for your face…");
    });

    it("carries the flag through the cached presentation only when it is true", () => {
        expect(statePresentationFields({ capture: { frames_flowing: false, starting: true } })
            .capture?.starting).toBe(true);
        for (const starting of [false, undefined, null, "true", 1]) {
            const fields = statePresentationFields({ capture: { frames_flowing: false, starting } });
            expect(fields.capture && "starting" in fields.capture).toBe(false);
        }
    });
});
