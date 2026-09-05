/**
 * extension.ts notification seams (audit A6 / A13).
 *
 *  - MORNING_BRIEFING must not throw on a malformed payload and must not
 *    pass the modal-only ``detail`` option to a non-modal notification.
 *  - The overlay_only toast fires once per intervention id even though
 *    the daemon rebroadcasts the same INTERVENTION_TRIGGER after every
 *    micro-step toggle.
 */

import * as vscode from "vscode";
import {
    _handleInterventionForTest,
    _handleMorningBriefingForTest,
    _resetToastDedupForTest,
} from "../extension";

const mockWindow = vscode.window as unknown as {
    showInformationMessage: jest.Mock;
};

beforeEach(() => {
    mockWindow.showInformationMessage.mockReset();
    mockWindow.showInformationMessage.mockImplementation(() => Promise.resolve(undefined));
    _resetToastDedupForTest();
});

describe("MORNING_BRIEFING notification (A13)", () => {
    it("survives a non-array action_items and never passes a modal-only detail option", () => {
        expect(() => _handleMorningBriefingForTest({
            summary: "Rested?",
            action_items: "not-an-array",
            left_off_at: "auth.py:42",
        })).not.toThrow();

        expect(mockWindow.showInformationMessage).toHaveBeenCalledTimes(1);
        const [message, ...rest] = mockWindow.showInformationMessage.mock.calls[0];
        expect(String(message)).toContain("Morning briefing: Rested?");
        expect(String(message)).toContain("auth.py:42");
        // Only string action labels follow the message — no options object.
        expect(rest).toEqual(["Show Details"]);
    });

    it("inlines up to three action items into the message body", () => {
        _handleMorningBriefingForTest({
            summary: "Two things are waiting.",
            action_items: ["Reply to the review", "Run the migration", 42, "Third", "Fourth"],
        });
        const [message] = mockWindow.showInformationMessage.mock.calls[0];
        expect(String(message)).toContain("1. Reply to the review");
        expect(String(message)).toContain("2. Run the migration");
        expect(String(message)).toContain("3. Third");
        expect(String(message)).toContain("(+1 more)");
        expect(String(message)).not.toContain("42");
    });

    it("tolerates non-object payloads", () => {
        expect(() => _handleMorningBriefingForTest(null)).not.toThrow();
        expect(() => _handleMorningBriefingForTest("nope")).not.toThrow();
        expect(mockWindow.showInformationMessage).toHaveBeenCalledTimes(2);
        expect(String(mockWindow.showInformationMessage.mock.calls[0][0]))
            .toBe("Morning briefing: Welcome back!");
    });
});

describe("overlay_only toast de-dup (A6)", () => {
    it("shows one toast per intervention id across daemon rebroadcasts", () => {
        const payload = {
            intervention_id: "iv-1",
            level: "overlay_only",
            headline: "Take a breath",
            micro_steps: [{ text: "Stand up", status: "pending" }],
        };
        _handleInterventionForTest(payload);
        // Daemon echo after MICRO_STEP_TOGGLED — same id, mutated steps.
        _handleInterventionForTest({
            ...payload,
            micro_steps: [{ text: "Stand up", status: "done" }],
        });
        expect(mockWindow.showInformationMessage).toHaveBeenCalledTimes(1);

        _handleInterventionForTest({ ...payload, intervention_id: "iv-2" });
        expect(mockWindow.showInformationMessage).toHaveBeenCalledTimes(2);
    });

    it("does not toast non-overlay levels", () => {
        _handleInterventionForTest({
            intervention_id: "iv-3",
            level: "simplified_workspace",
            headline: "Quiet",
        });
        expect(mockWindow.showInformationMessage).not.toHaveBeenCalled();
    });
});
