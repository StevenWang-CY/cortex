/**
 * ContextProvider privacy contract (audit A3 / A10 / A16).
 *
 *  - Only ``file`` documents are described; ``untitled`` needs an explicit
 *    opt-in; output panels, settings, diffs and other virtual schemes are
 *    never shared.
 *  - ``cortex.shareEditorContent`` gates the visible-code excerpt on both
 *    the command path (getActiveFile) and the CONTEXT_REQUEST path
 *    (gatherFullContext).
 *  - Terminal content is never collected — ``terminal_context`` is gone.
 *  - dispose() releases the document-change subscription.
 */

import * as vscode from "vscode";
import { ContextProvider } from "../context-provider";

const mockWorkspace = vscode.workspace as unknown as {
    __setConfig: (key: string, value: unknown) => void;
    __resetConfig: () => void;
    onDidChangeTextDocument: jest.Mock;
};
const mockWindow = vscode.window as unknown as { activeTextEditor: unknown };

function editorFor(scheme: string, text = "const a = 1;\nconst b = 2;\n") {
    return {
        document: {
            uri: {
                scheme,
                fsPath: `/ws/${scheme}-doc.ts`,
                toString: () => `${scheme}:///ws/${scheme}-doc.ts`,
            },
            lineCount: 2,
            getText: jest.fn(() => text),
        },
        visibleRanges: [new vscode.Range(0, 0, 1, 0)],
        selection: { active: new vscode.Position(0, 0) },
    };
}

afterEach(() => {
    mockWorkspace.__resetConfig();
    mockWindow.activeTextEditor = undefined;
});

describe("ContextProvider – document scheme filtering (A16)", () => {
    it.each([
        "output",
        "vscode-settings",
        "vscode-userdata",
        "git",
        "debug",
        "walkThrough",
        "vscode-notebook-cell",
    ])("never describes %s documents", (scheme) => {
        mockWindow.activeTextEditor = editorFor(scheme);
        const provider = new ContextProvider();
        expect(provider.getActiveFile()).toBeNull();
        expect(provider.getDiagnostics()).toEqual([]);
    });

    it("describes file documents, including visible code by default", () => {
        const editor = editorFor("file");
        mockWindow.activeTextEditor = editor;
        const provider = new ContextProvider();
        const info = provider.getActiveFile();
        expect(info).not.toBeNull();
        expect(info?.file_path).toBe("/ws/file-doc.ts");
        expect(info?.visible_range).toEqual([1, 2]);
        expect(info?.visible_code).toContain("const a = 1;");
        expect(editor.document.getText).toHaveBeenCalled();
    });

    it("hides untitled documents unless cortex.shareUntitledDocuments is on", () => {
        mockWindow.activeTextEditor = editorFor("untitled");
        const provider = new ContextProvider();
        expect(provider.getActiveFile()).toBeNull();

        mockWorkspace.__setConfig("cortex.shareUntitledDocuments", true);
        expect(provider.getActiveFile()?.file_path).toBe("/ws/untitled-doc.ts");
    });
});

describe("ContextProvider – cortex.shareEditorContent (A16)", () => {
    it("omits the code excerpt but keeps path and range when off", async () => {
        const editor = editorFor("file");
        mockWindow.activeTextEditor = editor;
        mockWorkspace.__setConfig("cortex.shareEditorContent", false);
        const provider = new ContextProvider();

        const info = provider.getActiveFile();
        expect(info?.file_path).toBe("/ws/file-doc.ts");
        expect(info?.visible_range).toEqual([1, 2]);
        expect(info?.visible_code).toBe("");
        expect(editor.document.getText).not.toHaveBeenCalled();

        // The CONTEXT_REQUEST path honours the same setting.
        const full = await provider.gatherFullContext();
        const editorContext = full.editor_context as Record<string, unknown>;
        expect(editorContext.file_path).toBe("/ws/file-doc.ts");
        expect(editorContext.visible_code).toBe("");
    });
});

describe("ContextProvider – no terminal capture (A3)", () => {
    it("gatherFullContext never emits terminal_context", async () => {
        mockWindow.activeTextEditor = editorFor("file");
        const provider = new ContextProvider();
        const full = await provider.gatherFullContext();
        expect(Object.keys(full)).toEqual(["editor_context"]);
        expect(full).not.toHaveProperty("terminal_context");
        expect(provider).not.toHaveProperty("getTerminalContext");
    });

    it("yields an empty editor_context for a non-shareable document", async () => {
        mockWindow.activeTextEditor = editorFor("output");
        const provider = new ContextProvider();
        const full = await provider.gatherFullContext();
        expect(full).toEqual({ editor_context: {} });
    });
});

describe("ContextProvider – subscription lifecycle (A10)", () => {
    it("dispose() releases the onDidChangeTextDocument subscription", () => {
        const before = mockWorkspace.onDidChangeTextDocument.mock.results.length;
        const provider = new ContextProvider();
        const results = mockWorkspace.onDidChangeTextDocument.mock.results;
        expect(results.length).toBe(before + 1);
        const subscription = results[results.length - 1].value as { dispose: jest.Mock };
        expect(subscription.dispose).not.toHaveBeenCalled();
        provider.dispose();
        expect(subscription.dispose).toHaveBeenCalledTimes(1);
        // Idempotent.
        provider.dispose();
        expect(subscription.dispose).toHaveBeenCalledTimes(1);
    });
});
