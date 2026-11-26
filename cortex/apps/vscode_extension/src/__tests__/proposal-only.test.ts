import * as vscode from "vscode";
import {
    _handleLegacyBreakRecommendationForTest,
    _handleInterventionForTest,
    _setFoldControllerForTest,
} from "../extension";

// The module-mapped mock is the object the extension itself calls;
// ``jest.requireMock("vscode")`` returns a separate automock, so an assertion
// on it can never fail.
const mockWindow = vscode.window as unknown as {
    showInformationMessage: jest.Mock;
};

describe("proposal-only editor authority", () => {
    it("never folds code for an INTERVENTION_TRIGGER", () => {
        const foldExcept = jest.fn().mockResolvedValue(true);
        _setFoldControllerForTest({ foldExcept });

        _handleInterventionForTest({
            intervention_id: "iv-proposal",
            level: "simplified_workspace",
            execution_mode: "authorized",
            ui_plan: {
                show_overlay: false,
                fold_unrelated_code: true,
                max_visible_lines: 40,
            },
        });

        expect(foldExcept).not.toHaveBeenCalled();
        _setFoldControllerForTest(undefined);
    });

    it("does not present legacy HRV/stress recommendations", () => {
        mockWindow.showInformationMessage.mockReset();

        _handleLegacyBreakRecommendationForTest({
            reason: "stress_integral_crossed_threshold",
            duration_seconds: 240,
        });

        expect(mockWindow.showInformationMessage).not.toHaveBeenCalled();

        _handleLegacyBreakRecommendationForTest({
            basis: "legacy_stress_integral",
            reason: "stress_integral_crossed_threshold",
            duration_seconds: 240,
        });

        expect(mockWindow.showInformationMessage).not.toHaveBeenCalled();
    });

    it("presents a strain-load break suggestion with the daemon's own words", () => {
        mockWindow.showInformationMessage.mockReset();
        mockWindow.showInformationMessage.mockImplementation(() => Promise.resolve(undefined));

        _handleLegacyBreakRecommendationForTest({
            basis: "physiological_strain",
            reason: "Your heart and breathing have run above your usual level for a while.",
            duration_seconds: 180,
        });

        expect(mockWindow.showInformationMessage).toHaveBeenCalledTimes(1);
        expect(mockWindow.showInformationMessage.mock.calls[0][0]).toBe(
            "Cortex · Your heart and breathing have run above your usual level for a while.",
        );
    });
});
