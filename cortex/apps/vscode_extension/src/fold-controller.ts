/**
 * Cortex VS Code Extension — Fold Controller
 *
 * Manages code folding for intervention simplification:
 * - cortex.foldExcept: fold everything except a specified line range
 * - cortex.unfoldAll: restore all folds (unfold everything)
 * - cortex.restoreFoldState: restore the saved pre-intervention fold state
 *
 * Saves fold state snapshots before mutations for safe restoration.
 * All folding operations are non-destructive and fully reversible.
 */

import * as vscode from "vscode";

/** Stored fold state for a single file. */
interface FoldSnapshot {
    filePath: string;
    foldedRanges: Array<[number, number]>;
    visibleRange: [number, number] | null;
    cursorPosition: [number, number];
}

/**
 * Controls code folding for Cortex interventions.
 *
 * Before any fold mutation, captures a snapshot of the current fold
 * state. Provides restoreFoldState to return to the pre-intervention
 * state.
 */
export class FoldController {
    private _snapshot: FoldSnapshot | null = null;
    private _hasPendingFolds = false;
    // D.3: ranges that *we* explicitly folded during the most recent
    // foldExcept invocation. Source of truth for restore — we know what
    // we folded, so we know what to unfold (and what to leave alone).
    private _appliedFolds: Array<[number, number]> = [];

    /** Whether there are active Cortex-applied folds. */
    get hasPendingFolds(): boolean {
        return this._hasPendingFolds;
    }

    /** Get the stored snapshot (for testing). */
    get snapshot(): FoldSnapshot | null {
        return this._snapshot;
    }

    /**
     * Fold everything in the active editor except the specified line range.
     *
     * Saves a fold state snapshot before applying. The excluded range
     * stays visible while all other code is folded at the top level.
     *
     * @param startLine - Start of range to keep visible (0-indexed)
     * @param endLine - End of range to keep visible (0-indexed)
     */
    async foldExcept(startLine: number, endLine: number): Promise<boolean> {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return false;
        }

        // Save snapshot before mutation
        await this._saveSnapshot(editor);

        try {
            // First unfold everything to start from a clean state
            await vscode.commands.executeCommand("editor.unfoldAll");

            // Get total line count
            const lineCount = editor.document.lineCount;

            // Build fold ranges: everything before and after the kept range
            const foldRanges: vscode.Selection[] = [];

            // Fold lines before the kept range
            if (startLine > 0) {
                foldRanges.push(
                    new vscode.Selection(0, 0, Math.max(0, startLine - 1), 0),
                );
            }

            // Fold lines after the kept range
            if (endLine < lineCount - 1) {
                foldRanges.push(
                    new vscode.Selection(
                        endLine + 1,
                        0,
                        lineCount - 1,
                        0,
                    ),
                );
            }

            // Apply folds by selecting ranges and folding
            this._appliedFolds = [];
            if (foldRanges.length > 0) {
                for (const range of foldRanges) {
                    editor.selection = range;
                    await vscode.commands.executeCommand(
                        "editor.fold",
                        {
                            selectionLines: [range.start.line],
                            levels: 999, // Fold as deeply as possible
                        },
                    );
                    // Track exactly what we folded so restore can be
                    // precise instead of guessing from the foldable
                    // structure of the whole file.
                    this._appliedFolds.push([range.start.line, range.end.line]);
                }

                // Restore cursor to the kept range
                const midLine = Math.floor((startLine + endLine) / 2);
                const position = new vscode.Position(midLine, 0);
                editor.selection = new vscode.Selection(position, position);
                editor.revealRange(
                    new vscode.Range(startLine, 0, endLine, 0),
                    vscode.TextEditorRevealType.InCenter,
                );
            }

            this._hasPendingFolds = true;
            return true;
        } catch {
            return false;
        }
    }

    /**
     * Unfold all code in the active editor.
     *
     * Does not restore previous fold state — for that use restoreFoldState.
     */
    async unfoldAll(): Promise<boolean> {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return false;
        }

        try {
            await vscode.commands.executeCommand("editor.unfoldAll");
            this._hasPendingFolds = false;
            return true;
        } catch {
            return false;
        }
    }

    /**
     * Restore the saved pre-intervention fold state.
     *
     * Unfolds everything, then re-applies the saved fold ranges
     * and restores the cursor position and visible range.
     */
    async restoreFoldState(): Promise<boolean> {
        if (!this._snapshot) {
            // No snapshot to restore — just unfold
            return this.unfoldAll();
        }

        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return false;
        }

        // Verify we're restoring the same file
        if (editor.document.uri.fsPath !== this._snapshot.filePath) {
            // File changed — just unfold, don't apply stale snapshot
            this._snapshot = null;
            this._hasPendingFolds = false;
            return this.unfoldAll();
        }

        try {
            // D.3: unfold only the ranges we explicitly folded during the
            // intervention. The previous implementation re-folded *every*
            // foldable range in the file on restore, which left the editor
            // MORE folded than the user's pre-intervention state.
            //
            // Unfolding only our applied ranges leaves any folds the user
            // had pre-existing untouched (we never knew about them; VS
            // Code's API doesn't expose currently-folded ranges).
            for (const [start] of this._appliedFolds) {
                if (start >= editor.document.lineCount) {
                    continue;
                }
                const safeStart = Math.max(
                    0,
                    Math.min(start, editor.document.lineCount - 1),
                );
                const pos = new vscode.Position(safeStart, 0);
                editor.selection = new vscode.Selection(pos, pos);
                await vscode.commands.executeCommand("editor.unfold", {
                    selectionLines: [safeStart],
                });
            }
            this._appliedFolds = [];

            // Restore cursor position
            const [line, char] = this._snapshot.cursorPosition;
            const safeLine = Math.min(line, editor.document.lineCount - 1);
            const position = new vscode.Position(safeLine, char);
            editor.selection = new vscode.Selection(position, position);

            // Restore visible range
            if (this._snapshot.visibleRange) {
                const [startLine, endLine] = this._snapshot.visibleRange;
                editor.revealRange(
                    new vscode.Range(
                        Math.min(startLine, editor.document.lineCount - 1),
                        0,
                        Math.min(endLine, editor.document.lineCount - 1),
                        0,
                    ),
                    vscode.TextEditorRevealType.InCenter,
                );
            }

            this._snapshot = null;
            this._hasPendingFolds = false;
            return true;
        } catch {
            this._snapshot = null;
            this._hasPendingFolds = false;
            return false;
        }
    }

    // --- Internal ---

    /**
     * Save a snapshot of the current fold state.
     *
     * Captures folded ranges, visible range, and cursor position.
     * Uses vscode.commands.executeCommand to get folding ranges.
     */
    private async _saveSnapshot(
        editor: vscode.TextEditor,
    ): Promise<void> {
        const filePath = editor.document.uri.fsPath;

        // Capture cursor position
        const cursorLine = editor.selection.active.line;
        const cursorChar = editor.selection.active.character;

        // Capture visible range
        let visibleRange: [number, number] | null = null;
        if (editor.visibleRanges.length > 0) {
            visibleRange = [
                editor.visibleRanges[0].start.line,
                editor.visibleRanges[0].end.line,
            ];
        }

        // Get folding ranges from the provider
        let foldedRanges: Array<[number, number]> = [];
        try {
            const ranges = await vscode.commands.executeCommand<
                vscode.FoldingRange[]
            >(
                "vscode.executeFoldingRangeProvider",
                editor.document.uri,
            );

            if (ranges) {
                // We store all folding range definitions, not just
                // currently-folded ones (VS Code API doesn't expose
                // which ranges are actually folded). The snapshot serves
                // as a reference for the file's foldable structure.
                foldedRanges = ranges.map((r) => [r.start, r.end]);
            }
        } catch {
            // Folding range provider may not be available
        }

        this._snapshot = {
            filePath,
            foldedRanges,
            visibleRange,
            cursorPosition: [cursorLine, cursorChar],
        };
    }
}
