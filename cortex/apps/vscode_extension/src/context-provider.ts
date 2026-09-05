/**
 * Cortex VS Code Extension — Context Provider
 *
 * Extracts workspace context from VS Code for the Cortex daemon:
 * - cortex.getActiveFile: current file path + visible range
 * - cortex.getDiagnostics: all errors/warnings for current file
 * - cortex.getSymbolAtCursor: current function/class/symbol name
 * - gatherFullContext(): combines all three for CONTEXT_RESPONSE
 *
 * Privacy contract (docs/data-flow.md, "Editor" row):
 * - Only ``file`` documents are described. ``untitled`` documents are
 *   included only when ``cortex.shareUntitledDocuments`` is on. Output
 *   panels, settings editors, diff views, git/virtual documents and
 *   every other scheme are never shared (A16).
 * - ``cortex.shareEditorContent`` (default on) gates the visible-code
 *   excerpt. When off, only path, visible range, symbol and diagnostics
 *   are shared.
 * - Terminal content is never collected. The former terminal-capture
 *   path depended on a proposed API the extension never declared and
 *   would have shipped raw terminal lines + command history; it has
 *   been removed entirely (A3).
 */

import * as vscode from "vscode";

/** Diagnostic info matching the Cortex Diagnostic schema. */
interface CortexDiagnostic {
    severity: "error" | "warning" | "info" | "hint";
    message: string;
    line: number;
    column: number;
    source: string | null;
    code: string | null;
}

/** Active file info matching EditorContext fields. */
interface ActiveFileInfo {
    file_path: string;
    visible_range: [number, number];
    visible_code: string;
}

/** A short description of a recent edit. */
interface RecentEdit {
    file_path: string;
    line: number;
    length: number;
    kind: "insert" | "delete" | "replace";
}

/**
 * Provides VS Code editor context to the Cortex daemon.
 */
export class ContextProvider implements vscode.Disposable {
    // D.7: maintain a small ring of recent edits so the LLM can see
    // where the user is actively working without needing a full git diff.
    private _recentEdits: RecentEdit[] = [];
    private static readonly _RECENT_EDIT_CAPACITY = 25;
    private readonly _disposables: vscode.Disposable[] = [];

    constructor() {
        // Watch text-document changes — describe each change in a small,
        // privacy-preserving way (no content, only file/line/length).
        try {
            this._disposables.push(
                vscode.workspace.onDidChangeTextDocument((event) => {
                    if (!this._isShareableScheme(event.document.uri.scheme)) {
                        return;
                    }
                    const filePath = event.document.uri.fsPath;
                    for (const change of event.contentChanges) {
                        const insertedLength = change.text.length;
                        const replacedLength = change.rangeLength;
                        let kind: RecentEdit["kind"];
                        if (insertedLength > 0 && change.rangeLength > 0) {
                            kind = "replace";
                        } else if (insertedLength > 0) {
                            kind = "insert";
                        } else {
                            kind = "delete";
                        }
                        this._recentEdits.push({
                            file_path: filePath,
                            line: change.range.start.line + 1,
                            length: Math.max(insertedLength, replacedLength),
                            kind,
                        });
                        if (this._recentEdits.length > ContextProvider._RECENT_EDIT_CAPACITY) {
                            this._recentEdits.shift();
                        }
                    }
                }),
            );
        } catch {
            // Subscription may fail in test harnesses with stubbed vscode API.
        }
    }

    /** A10: release the document-change subscription. */
    dispose(): void {
        for (const d of this._disposables.splice(0)) {
            try {
                d.dispose();
            } catch {
                // already disposed
            }
        }
    }

    /**
     * Get active file path and visible range.
     *
     * Corresponds to the cortex.getActiveFile command. Returns ``null``
     * when there is no active editor or its document scheme is not
     * shareable (A16).
     */
    getActiveFile(): ActiveFileInfo | null {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return null;
        }

        const doc = editor.document;
        if (!this._isShareableScheme(doc.uri.scheme)) {
            return null;
        }
        const visibleRanges = editor.visibleRanges;

        // Use first visible range
        let startLine = 1;
        let endLine = 50;
        if (visibleRanges.length > 0) {
            startLine = visibleRanges[0].start.line + 1; // 1-indexed
            endLine = visibleRanges[0].end.line + 1;
        }

        let visibleCode = "";
        if (this._shareEditorContent()) {
            // Extract visible code (limit to 2000 tokens ≈ 8000 chars)
            const visibleText = doc.getText(
                new vscode.Range(
                    Math.max(0, startLine - 1),
                    0,
                    Math.min(doc.lineCount, endLine),
                    0,
                ),
            );
            visibleCode = visibleText.substring(0, 8000);
        }

        return {
            file_path: doc.uri.fsPath,
            visible_range: [startLine, endLine],
            visible_code: visibleCode,
        };
    }

    /**
     * Get diagnostics (errors/warnings) for the current file.
     *
     * Corresponds to the cortex.getDiagnostics command.
     */
    getDiagnostics(): CortexDiagnostic[] {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return [];
        }
        if (!this._isShareableScheme(editor.document.uri.scheme)) {
            return [];
        }

        const uri = editor.document.uri;
        const diagnostics = vscode.languages.getDiagnostics(uri);

        return diagnostics.map((d) => {
            let severity: CortexDiagnostic["severity"];
            switch (d.severity) {
                case vscode.DiagnosticSeverity.Error:
                    severity = "error";
                    break;
                case vscode.DiagnosticSeverity.Warning:
                    severity = "warning";
                    break;
                case vscode.DiagnosticSeverity.Information:
                    severity = "info";
                    break;
                case vscode.DiagnosticSeverity.Hint:
                    severity = "hint";
                    break;
            }

            // Extract code as string
            let code: string | null = null;
            if (d.code !== undefined) {
                if (typeof d.code === "object" && d.code !== null) {
                    code = String(
                        (d.code as { value: string | number }).value,
                    );
                } else {
                    code = String(d.code);
                }
            }

            return {
                severity,
                message: d.message,
                line: d.range.start.line + 1, // 1-indexed
                column: d.range.start.character,
                source: d.source ?? null,
                code,
            };
        });
    }

    /**
     * Get the symbol (function/class/variable) at the cursor position.
     *
     * Corresponds to the cortex.getSymbolAtCursor command.
     * Uses VS Code's document symbol provider to find the enclosing symbol.
     */
    async getSymbolAtCursor(): Promise<string | null> {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return null;
        }
        if (!this._isShareableScheme(editor.document.uri.scheme)) {
            return null;
        }

        const position = editor.selection.active;

        try {
            // Get document symbols
            const symbols = await vscode.commands.executeCommand<
                vscode.DocumentSymbol[]
            >("vscode.executeDocumentSymbolProvider", editor.document.uri);

            if (!symbols || symbols.length === 0) {
                return null;
            }

            // Find the most specific symbol containing the cursor
            const symbol = this._findEnclosingSymbol(symbols, position);
            return symbol?.name ?? null;
        } catch {
            return null;
        }
    }

    /**
     * Gather complete editor context for a CONTEXT_RESPONSE.
     *
     * Combines getActiveFile, getDiagnostics, and getSymbolAtCursor
     * into a single payload matching the EditorContext schema. No
     * ``terminal_context`` is ever produced (A3); the daemon schema
     * treats it as optional.
     */
    async gatherFullContext(): Promise<Record<string, unknown>> {
        const activeFile = this.getActiveFile();
        const diagnostics = this.getDiagnostics();
        const symbolAtCursor = await this.getSymbolAtCursor();

        const editorContext = activeFile
            ? {
                  file_path: activeFile.file_path,
                  visible_range: activeFile.visible_range,
                  visible_code: activeFile.visible_code,
                  symbol_at_cursor: symbolAtCursor,
                  diagnostics: diagnostics,
                  // D.7: actual edits collected by onDidChangeTextDocument.
                  // Format is privacy-preserving (file/line/length/kind only).
                  recent_edits: this._recentEdits.slice(-10).map(
                      (e) =>
                          `${e.kind} at ${e.file_path}:${e.line} (${e.length} chars)`,
                  ),
              }
            : {};

        return {
            editor_context: editorContext,
        };
    }

    // --- Internal helpers ---

    /** A16: ``file`` always; ``untitled`` only by explicit opt-in. */
    private _isShareableScheme(scheme: string): boolean {
        if (scheme === "file") return true;
        if (scheme === "untitled") {
            return this._config().get<boolean>("shareUntitledDocuments", false) === true;
        }
        return false;
    }

    private _shareEditorContent(): boolean {
        return this._config().get<boolean>("shareEditorContent", true) !== false;
    }

    private _config(): vscode.WorkspaceConfiguration {
        return vscode.workspace.getConfiguration("cortex");
    }

    /**
     * Find the most specific (deepest) document symbol containing a position.
     */
    private _findEnclosingSymbol(
        symbols: vscode.DocumentSymbol[],
        position: vscode.Position,
    ): vscode.DocumentSymbol | null {
        for (const symbol of symbols) {
            if (symbol.range.contains(position)) {
                // Check children for more specific match
                const child = this._findEnclosingSymbol(
                    symbol.children,
                    position,
                );
                return child ?? symbol;
            }
        }
        return null;
    }
}
