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
/** A short description of a recent edit. */
interface RecentEdit {
    file_path: string;
    line: number;
    length: number;
    kind: "insert" | "delete" | "replace";
}

export class ContextProvider {
    private _terminalLines: string[] = [];
    private _commandHistory: string[] = [];
    // D.7: maintain a small ring of recent edits so the LLM can see
    // where the user is actively working without needing a full git diff.
    private _recentEdits: RecentEdit[] = [];
    private static readonly _RECENT_EDIT_CAPACITY = 25;

    constructor() {
        // Watch text-document changes — describe each change in a small,
        // privacy-preserving way (no content, only file/line/length).
        try {
            vscode.workspace.onDidChangeTextDocument((event) => {
                if (event.document.uri.scheme !== "file") {
                    return;
                }
                const filePath = event.document.uri.fsPath;
                for (const change of event.contentChanges) {
                    const insertedLength = change.text.length;
                    const replacedLength =
                        change.rangeOffset + change.rangeLength - change.rangeOffset;
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
            });
        } catch {
            // Subscription may fail in test harnesses with stubbed vscode API.
        }

        const windowAny = vscode.window as typeof vscode.window & {
            onDidWriteTerminalData?: (
                listener: (event: { data: string; terminal: vscode.Terminal }) => void,
            ) => vscode.Disposable;
        };

        if (typeof windowAny.onDidWriteTerminalData === "function") {
            windowAny.onDidWriteTerminalData((event) => {
                const chunks = event.data
                    .split(/\r?\n/)
                    .map((line) => line.trimEnd())
                    .filter((line) => line.length > 0);
                for (const chunk of chunks) {
                    this._terminalLines.push(chunk);
                    if (this._terminalLines.length > 200) {
                        this._terminalLines.shift();
                    }
                    if (/^\s*(\$|>|%|#)\s+/.test(chunk)) {
                        this._commandHistory.push(chunk.replace(/^\s*(\$|>|%|#)\s+/, ""));
                        if (this._commandHistory.length > 50) {
                            this._commandHistory.shift();
                        }
                    }
                }
            });
        }
    }

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
            terminal_context: this.getTerminalContext(),
        };
    }

    getTerminalContext(): Record<string, unknown> {
        const lines = this._terminalLines.slice(-50);
        const detectedErrors = lines.filter((line) =>
            /(error:|failed|traceback|exception|command not found|permission denied)/i.test(line),
        );
        const repeatedCommands = Array.from(
            new Set(
                this._commandHistory.filter(
                    (command, index, all) => all.indexOf(command) !== index,
                ),
            ),
        );

        return {
            last_n_lines: lines,
            detected_errors: detectedErrors.slice(-10),
            repeated_commands: repeatedCommands,
            running_command: vscode.window.activeTerminal?.name ?? null,
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
