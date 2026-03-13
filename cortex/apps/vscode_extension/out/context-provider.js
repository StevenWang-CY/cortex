"use strict";
/**
 * Cortex VS Code Extension — Context Provider
 *
 * Extracts workspace context from VS Code for the Cortex daemon:
 * - cortex.getActiveFile: current file path + visible range
 * - cortex.getDiagnostics: all errors/warnings for current file
 * - cortex.getSymbolAtCursor: current function/class/symbol name
 * - gatherFullContext(): combines all three for CONTEXT_RESPONSE
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
exports.ContextProvider = void 0;
const vscode = __importStar(require("vscode"));
/**
 * Provides VS Code editor context to the Cortex daemon.
 */
class ContextProvider {
    _terminalLines = [];
    _commandHistory = [];
    constructor() {
        const windowAny = vscode.window;
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
    getActiveFile() {
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
        const visibleText = doc.getText(new vscode.Range(Math.max(0, startLine - 1), 0, Math.min(doc.lineCount, endLine), 0));
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
    getDiagnostics() {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return [];
        }
        const uri = editor.document.uri;
        const diagnostics = vscode.languages.getDiagnostics(uri);
        return diagnostics.map((d) => {
            let severity;
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
            let code = null;
            if (d.code !== undefined) {
                if (typeof d.code === "object" && d.code !== null) {
                    code = String(d.code.value);
                }
                else {
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
    async getSymbolAtCursor() {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            return null;
        }
        const position = editor.selection.active;
        try {
            // Get document symbols
            const symbols = await vscode.commands.executeCommand("vscode.executeDocumentSymbolProvider", editor.document.uri);
            if (!symbols || symbols.length === 0) {
                return null;
            }
            // Find the most specific symbol containing the cursor
            const symbol = this._findEnclosingSymbol(symbols, position);
            return symbol?.name ?? null;
        }
        catch {
            return null;
        }
    }
    /**
     * Gather complete editor context for a CONTEXT_RESPONSE.
     *
     * Combines getActiveFile, getDiagnostics, and getSymbolAtCursor
     * into a single payload matching the EditorContext schema.
     */
    async gatherFullContext() {
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
                recent_edits: [],
            }
            : {};
        return {
            editor_context: editorContext,
            terminal_context: this.getTerminalContext(),
        };
    }
    getTerminalContext() {
        const lines = this._terminalLines.slice(-50);
        const detectedErrors = lines.filter((line) => /(error:|failed|traceback|exception|command not found|permission denied)/i.test(line));
        const repeatedCommands = Array.from(new Set(this._commandHistory.filter((command, index, all) => all.indexOf(command) !== index)));
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
    _findEnclosingSymbol(symbols, position) {
        for (const symbol of symbols) {
            if (symbol.range.contains(position)) {
                // Check children for more specific match
                const child = this._findEnclosingSymbol(symbol.children, position);
                return child ?? symbol;
            }
        }
        return null;
    }
}
exports.ContextProvider = ContextProvider;
//# sourceMappingURL=context-provider.js.map