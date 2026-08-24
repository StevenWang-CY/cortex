import {
    _handleLegacyBreakRecommendationForTest,
    _handleInterventionForTest,
    _setFoldControllerForTest,
} from "../extension";

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
        const vscode = jest.requireMock("vscode") as {
            window: { showInformationMessage: jest.Mock };
        };
        vscode.window.showInformationMessage.mockClear();

        _handleLegacyBreakRecommendationForTest({
            reason: "stress_integral_crossed_threshold",
            duration_seconds: 240,
        });

        expect(vscode.window.showInformationMessage).not.toHaveBeenCalled();
    });
});
