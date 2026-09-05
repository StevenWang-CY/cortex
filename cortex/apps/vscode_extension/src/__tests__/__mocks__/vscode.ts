/**
 * Minimal vscode module mock for Jest tests.
 *
 * Only the members actually imported by the extension source are stubbed.
 * Real VS Code APIs are not available outside the extension host, so this
 * lets the tests run in a plain Node process.
 *
 * Test seams (not part of the real API, all prefixed with ``__``):
 *   - ``workspace.__setConfig("cortex.daemonUrl", value)`` overrides one
 *     configuration key; ``workspace.__resetConfig()`` clears overrides.
 *   - ``workspace.__fireConfigChange(["workbench.reduceMotion"])`` invokes
 *     every ``onDidChangeConfiguration`` listener with an event whose
 *     ``affectsConfiguration`` matches the listed keys (or their prefixes).
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
    contains(position: Position): boolean {
        if (position.line < this.start.line || position.line > this.end.line) {
            return false;
        }
        return true;
    }
}

export class Selection extends Range {
    anchor: Position;
    active: Position;
    constructor(
        anchorLineOrPos: number | Position,
        anchorCharOrActive: number | Position,
        activeLine?: number,
        activeChar?: number,
    ) {
        if (typeof anchorLineOrPos === "number") {
            super(
                anchorLineOrPos,
                anchorCharOrActive as number,
                activeLine ?? anchorLineOrPos,
                activeChar ?? (anchorCharOrActive as number),
            );
        } else {
            const active = anchorCharOrActive as Position;
            super(
                anchorLineOrPos.line,
                anchorLineOrPos.character,
                active.line,
                active.character,
            );
        }
        this.anchor = this.start;
        this.active = this.end;
    }
}

export class ThemeColor {
    constructor(public id: string) {}
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

function makeStatusBarItem() {
    return {
        text: "",
        tooltip: "",
        command: "",
        backgroundColor: undefined as unknown,
        color: undefined as unknown,
        show: jest.fn(),
        hide: jest.fn(),
        dispose: jest.fn(),
    };
}

export const window = {
    activeTextEditor: undefined as unknown,
    activeTerminal: undefined as unknown,
    visibleTextEditors: [] as unknown[],
    createStatusBarItem: jest.fn(() => makeStatusBarItem()),
    // Message boxes return a thenable like the real API so ``.then`` chains
    // in the extension do not throw inside tests.
    showErrorMessage: jest.fn(() => Promise.resolve(undefined)),
    showInformationMessage: jest.fn(() => Promise.resolve(undefined)),
    showWarningMessage: jest.fn(() => Promise.resolve(undefined)),
    showTextDocument: jest.fn(),
    setStatusBarMessage: jest.fn(),
    createOutputChannel: jest.fn(() => ({
        appendLine: jest.fn(),
        dispose: jest.fn(),
    })),
    registerWebviewViewProvider: jest.fn(() => ({ dispose: () => {} })),
};

type ConfigChangeListener = (e: { affectsConfiguration: (k: string) => boolean }) => void;
const _configOverrides = new Map<string, unknown>();
const _configListeners: ConfigChangeListener[] = [];

export const workspace = {
    getConfiguration: (section?: string) => ({
        get: (key: string, defaultValue?: unknown) => {
            const full = section ? `${section}.${key}` : key;
            return _configOverrides.has(full)
                ? _configOverrides.get(full)
                : defaultValue;
        },
        update: jest.fn(),
    }),
    getWorkspaceFolder: jest.fn(),
    onDidChangeTextDocument: jest.fn(() => ({ dispose: jest.fn() })),
    onDidChangeConfiguration: (listener: ConfigChangeListener) => {
        _configListeners.push(listener);
        return {
            dispose: () => {
                const idx = _configListeners.indexOf(listener);
                if (idx >= 0) _configListeners.splice(idx, 1);
            },
        };
    },
    __setConfig: (fullKey: string, value: unknown) => {
        _configOverrides.set(fullKey, value);
    },
    __resetConfig: () => {
        _configOverrides.clear();
    },
    __fireConfigChange: (keys: string[]) => {
        const event = {
            affectsConfiguration: (k: string) =>
                keys.some((key) => key === k || key.startsWith(`${k}.`)),
        };
        for (const listener of [..._configListeners]) listener(event);
    },
    __configListenerCount: () => _configListeners.length,
};

export const commands = {
    registerCommand: jest.fn(() => ({ dispose: () => {} })),
    executeCommand: jest.fn(),
};

export const languages = {
    getDiagnostics: jest.fn(() => [] as unknown[]),
};

export enum StatusBarAlignment {
    Left = 1,
    Right = 2,
}

export enum ConfigurationTarget {
    Global = 1,
    Workspace = 2,
    WorkspaceFolder = 3,
}

export enum DiagnosticSeverity {
    Error = 0,
    Warning = 1,
    Information = 2,
    Hint = 3,
}

export enum TextEditorRevealType {
    Default = 0,
    InCenter = 1,
    InCenterIfOutsideViewport = 2,
    AtTop = 3,
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
    Selection,
    ThemeColor,
    EventEmitter,
    window,
    workspace,
    commands,
    languages,
    StatusBarAlignment,
    ConfigurationTarget,
    DiagnosticSeverity,
    TextEditorRevealType,
    CancellationTokenSource,
};
