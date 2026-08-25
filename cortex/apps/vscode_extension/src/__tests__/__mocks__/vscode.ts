/**
 * Minimal vscode module mock for Jest tests.
 *
 * Only the members actually imported by the extension source are stubbed.
 * Real VS Code APIs are not available outside the extension host, so this
 * lets the tests run in a plain Node process.
 */

export const Uri = {
    file: (p: string) => ({ fsPath: p, scheme: "file", toString: () => p }),
    parse: (u: string) => ({ fsPath: u, scheme: "vscode-resource", toString: () => u }),
};

export class Position {
    constructor(public line: number, public character: number) {}
}

export class Range {
    start: Position;
    end: Position;
    constructor(
        startLine: number,
        startCharacter: number,
        endLine: number,
        endCharacter: number,
    ) {
        this.start = new Position(startLine, startCharacter);
        this.end = new Position(endLine, endCharacter);
    }
}

export class EventEmitter<T> {
    private _listeners: Array<(e: T) => void> = [];
    event = (listener: (e: T) => void) => {
        this._listeners.push(listener);
        return { dispose: () => {} };
    };
    fire(e: T) {
        this._listeners.forEach(l => l(e));
    }
    dispose() {}
}

export const window = {
    activeTextEditor: undefined as unknown,
    visibleTextEditors: [] as unknown[],
    createStatusBarItem: () => ({
        text: "",
        tooltip: "",
        command: "",
        show: () => {},
        hide: () => {},
        dispose: () => {},
    }),
    showErrorMessage: jest.fn(),
    showInformationMessage: jest.fn(),
    showWarningMessage: jest.fn(),
    showTextDocument: jest.fn(),
    setStatusBarMessage: jest.fn(),
    createOutputChannel: jest.fn(() => ({
        appendLine: jest.fn(),
        dispose: jest.fn(),
    })),
    registerWebviewViewProvider: jest.fn(() => ({ dispose: () => {} })),
};

export const workspace = {
    getConfiguration: (_section?: string) => ({
        get: (_key: string, defaultValue?: unknown) => defaultValue,
        update: jest.fn(),
    }),
    getWorkspaceFolder: jest.fn(),
};

export const commands = {
    registerCommand: jest.fn(() => ({ dispose: () => {} })),
    executeCommand: jest.fn(),
};

export enum StatusBarAlignment {
    Left = 1,
    Right = 2,
}

export const CancellationTokenSource = class {
    token = { isCancellationRequested: false, onCancellationRequested: () => ({dispose: () => {}}) };
    cancel() {}
    dispose() {}
};

export default {
    Uri,
    Position,
    Range,
    EventEmitter,
    window,
    workspace,
    commands,
    StatusBarAlignment,
    CancellationTokenSource,
};
