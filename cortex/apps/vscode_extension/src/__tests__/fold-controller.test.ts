/**
 * FoldController argument validation (audit A9).
 *
 * ``cortex.foldExcept`` is reachable from the command palette with no
 * arguments. Before the fix ``foldExcept(undefined, undefined)`` ran
 * ``editor.unfoldAll`` and then folded nothing — a surprising mutation of
 * the user's fold state. Invalid ranges must now be refused before any
 * editor command runs.
 */

import * as vscode from "vscode";
import { FoldController } from "../fold-controller";

const mockCommands = vscode.commands as unknown as { executeCommand: jest.Mock };
const mockWindow = vscode.window as unknown as { activeTextEditor: unknown };

function fakeEditor(lineCount = 120) {
    const cursor = new vscode.Position(10, 0);
    return {
        document: { uri: { scheme: "file", fsPath: "/ws/a.ts" }, lineCount },
        selection: { active: cursor, anchor: cursor, start: cursor, end: cursor },
        visibleRanges: [new vscode.Range(0, 0, 40, 0)],
        revealRange: jest.fn(),
    };
}

beforeEach(() => {
    mockCommands.executeCommand.mockReset();
    mockWindow.activeTextEditor = fakeEditor();
});

afterEach(() => {
    mockWindow.activeTextEditor = undefined;
});

describe("FoldController.isValidRange", () => {
    it.each([
        [0, 0],
        [0, 10],
        [20, 30],
        [5, 5],
    ])("accepts %p..%p", (start, end) => {
        expect(FoldController.isValidRange(start, end)).toBe(true);
    });

    it.each([
        [undefined, undefined],
        [null, 5],
        [2.5, 10],
        [-1, 5],
        [10, 2],
        ["3", "5"],
        [Number.NaN, 3],
        [Number.POSITIVE_INFINITY, 3],
        [3, Number.NaN],
        [{}, []],
    ])("refuses %p..%p", (start, end) => {
        expect(FoldController.isValidRange(start, end)).toBe(false);
    });
});

describe("FoldController.foldExcept (A9)", () => {
    it.each([
        [undefined, undefined],
        [2.5, 10],
        [-1, 5],
        [10, 2],
        ["3", "5"],
    ])("refuses %p..%p without running any editor command", async (start, end) => {
        const controller = new FoldController();
        await expect(
            controller.foldExcept(start as unknown as number, end as unknown as number),
        ).resolves.toBe(false);
        expect(mockCommands.executeCommand).not.toHaveBeenCalled();
        expect(controller.hasPendingFolds).toBe(false);
        expect(controller.snapshot).toBeNull();
    });

    it("applies a valid integer range: snapshot, unfoldAll, then folds around it", async () => {
        const controller = new FoldController();
        await expect(controller.foldExcept(20, 30)).resolves.toBe(true);

        const commands = mockCommands.executeCommand.mock.calls.map((c) => c[0] as string);
        expect(commands[0]).toBe("vscode.executeFoldingRangeProvider");
        expect(commands[1]).toBe("editor.unfoldAll");
        expect(commands.filter((c) => c === "editor.fold")).toHaveLength(2);
        expect(controller.hasPendingFolds).toBe(true);
        expect(controller.snapshot?.filePath).toBe("/ws/a.ts");
    });

    it("returns false without commands when there is no active editor", async () => {
        mockWindow.activeTextEditor = undefined;
        const controller = new FoldController();
        await expect(controller.foldExcept(1, 2)).resolves.toBe(false);
        expect(mockCommands.executeCommand).not.toHaveBeenCalled();
    });
});
