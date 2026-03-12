/**
 * Cortex VS Code Extension — Context Provider
 *
 * Extracts workspace context from VS Code for the Cortex daemon:
 * - cortex.getActiveFile: current file path + visible range
 * - cortex.getDiagnostics: all errors/warnings for current file
 * - cortex.getSymbolAtCursor: current function/class/symbol name
 * - gatherFullContext(): combines all three for CONTEXT_RESPONSE
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

/**
 * Provides VS Code editor context to the Cortex daemon.
 */
export class ContextProvider {
    /**
     * Get active file path and visible range.
     *
     * Corresponds to the cortex.getActiveFile command.
     */
    getActiveFile(): ActiveFileInfo | null {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return null;
        }

        const doc = editor.document;
        const visibleRanges = editor.visibleRanges;

        // Use first visible range
        let startLine = 1;
        let endLine = 50;
        if (visibleRanges.length > 0) {
            startLine = visibleRanges[0].start.line + 1; // 1-indexed
            endLine = visibleRanges[0].end.line + 1;
        }

        // Extract visible code (limit to 2000 tokens ≈ 8000 chars)
        const visibleText = doc.getText(
            new vscode.Range(
                Math.max(0, startLine - 1),
                0,
                Math.min(doc.lineCount, endLine),
                0,
            ),
        );
        const visibleCode = visibleText.substring(0, 8000);

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
     * into a single payload matching the EditorContext schema.
     */
    async gatherFullContext(): Promise<Record<string, unknown>> {
        const activeFile = this.getActiveFile();
        if (!activeFile) {
            return {};
        }

        const diagnostics = this.getDiagnostics();
        const symbolAtCursor = await this.getSymbolAtCursor();

        return {
            type: "CONTEXT_RESPONSE",
            file_path: activeFile.file_path,
            visible_range: activeFile.visible_range,
            visible_code: activeFile.visible_code,
            symbol_at_cursor: symbolAtCursor,
            diagnostics: diagnostics,
            recent_edits: [],
        };
    }

    // --- Internal helpers ---

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
