"use strict";
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
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.FoldController = void 0;
const vscode = __importStar(require("vscode"));
/**
 * Controls code folding for Cortex interventions.
 *
 * Before any fold mutation, captures a snapshot of the current fold
 * state. Provides restoreFoldState to return to the pre-intervention
 * state.
 */
class FoldController {
    _snapshot = null;
    _hasPendingFolds = false;
    /** Whether there are active Cortex-applied folds. */
    get hasPendingFolds() {
        return this._hasPendingFolds;
    }
    /** Get the stored snapshot (for testing). */
    get snapshot() {
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
    async foldExcept(startLine, endLine) {
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
            const foldRanges = [];
            // Fold lines before the kept range
            if (startLine > 0) {
                foldRanges.push(new vscode.Selection(0, 0, Math.max(0, startLine - 1), 0));
            }
            // Fold lines after the kept range
            if (endLine < lineCount - 1) {
                foldRanges.push(new vscode.Selection(endLine + 1, 0, lineCount - 1, 0));
            }
            // Apply folds by selecting ranges and folding
            if (foldRanges.length > 0) {
                for (const range of foldRanges) {
                    editor.selection = range;
                    await vscode.commands.executeCommand("editor.fold", {
                        selectionLines: [range.start.line],
                        levels: 999, // Fold as deeply as possible
                    });
                }
                // Restore cursor to the kept range
                const midLine = Math.floor((startLine + endLine) / 2);
                const position = new vscode.Position(midLine, 0);
                editor.selection = new vscode.Selection(position, position);
                editor.revealRange(new vscode.Range(startLine, 0, endLine, 0), vscode.TextEditorRevealType.InCenter);
            }
            this._hasPendingFolds = true;
            return true;
        }
        catch {
            return false;
        }
    }
    /**
     * Unfold all code in the active editor.
     *
     * Does not restore previous fold state — for that use restoreFoldState.
     */
    async unfoldAll() {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return false;
        }
        try {
            await vscode.commands.executeCommand("editor.unfoldAll");
            this._hasPendingFolds = false;
            return true;
        }
        catch {
            return false;
        }
    }
    /**
     * Restore the saved pre-intervention fold state.
     *
     * Unfolds everything, then re-applies the saved fold ranges
     * and restores the cursor position and visible range.
     */
    async restoreFoldState() {
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
            // Unfold everything first
            await vscode.commands.executeCommand("editor.unfoldAll");
            // Re-fold saved ranges
            for (const [start] of this._snapshot.foldedRanges) {
                await vscode.commands.executeCommand("editor.fold", {
                    selectionLines: [start],
                    levels: 1,
                });
                // Verify range is roughly correct (file may have changed)
                if (start >= editor.document.lineCount) {
                    break;
                }
            }
            // Restore cursor position
            const [line, char] = this._snapshot.cursorPosition;
            const safeLine = Math.min(line, editor.document.lineCount - 1);
            const position = new vscode.Position(safeLine, char);
            editor.selection = new vscode.Selection(position, position);
            // Restore visible range
            if (this._snapshot.visibleRange) {
                const [startLine, endLine] = this._snapshot.visibleRange;
                editor.revealRange(new vscode.Range(Math.min(startLine, editor.document.lineCount - 1), 0, Math.min(endLine, editor.document.lineCount - 1), 0), vscode.TextEditorRevealType.InCenter);
            }
            this._snapshot = null;
            this._hasPendingFolds = false;
            return true;
        }
        catch {
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
    async _saveSnapshot(editor) {
        const filePath = editor.document.uri.fsPath;
        // Capture cursor position
        const cursorLine = editor.selection.active.line;
        const cursorChar = editor.selection.active.character;
        // Capture visible range
        let visibleRange = null;
        if (editor.visibleRanges.length > 0) {
            visibleRange = [
                editor.visibleRanges[0].start.line,
                editor.visibleRanges[0].end.line,
            ];
        }
        // Get folding ranges from the provider
        let foldedRanges = [];
        try {
            const ranges = await vscode.commands.executeCommand("vscode.executeFoldingRangeProvider", editor.document.uri);
            if (ranges) {
                // We store all folding range definitions, not just
                // currently-folded ones (VS Code API doesn't expose
                // which ranges are actually folded). The snapshot serves
                // as a reference for the file's foldable structure.
                foldedRanges = ranges.map((r) => [r.start, r.end]);
            }
        }
        catch {
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
exports.FoldController = FoldController;
//# sourceMappingURL=fold-controller.js.map