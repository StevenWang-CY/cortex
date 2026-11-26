/**
 * extension.ts message wiring, driven through ``activate()``.
 *
 * Each test loads a fresh copy of the extension (module state is
 * per-activation), activates it against a fake context, and drives daemon
 * frames through the fake socket the real ``CortexWSClient`` opened. The
 * socket's ``open`` event is never emitted, so the capability token on disk
 * is never read; ``AUTH_OK`` alone marks the client connected.
 */

import type * as VSCodeModule from "vscode";
import type * as ExtensionModule from "../extension";
import type { CortexPanelProvider } from "../panel-provider";

jest.mock("ws", () => {
    const { EventEmitter } = jest.requireActual("events") as typeof import("events");
    class FakeWebSocket extends EventEmitter {
        static instances: FakeWebSocket[] = [];
        sent: string[] = [];
        constructor(public url: string) {
            super();
            FakeWebSocket.instances.push(this);
        }
        send(raw: string): void { this.sent.push(raw); }
        close(): void { /* the real close event arrives later, if at all */ }
        terminate(): void { /* no-op */ }
        ping(): void { /* no-op */ }
    }
    return { __esModule: true, default: FakeWebSocket, __fake: FakeWebSocket };
});

interface FakeSocket {
    sent: string[];
    emit(event: string, ...args: unknown[]): boolean;
}

interface StatusBarItemShape {
    text: string;
    tooltip: string;
    backgroundColor: unknown;
}

interface Harness {
    ext: typeof ExtensionModule;
    vscode: typeof VSCodeModule;
    socket: FakeSocket;
    panel: CortexPanelProvider;
    statusBar: () => StatusBarItemShape;
    deliver(type: string, payload: Record<string, unknown>, extra?: Record<string, unknown>): void;
}

let sequence = 0;
let active: Harness | undefined;

function makeContext() {
    const workspace = new Map<string, unknown>();
    const global = new Map<string, unknown>();
    return {
        subscriptions: [] as Array<{ dispose(): unknown }>,
        workspaceState: {
            get: (key: string) => workspace.get(key),
            update: async (key: string, value: unknown) => { workspace.set(key, value); },
        },
        globalState: {
            get: (key: string) => global.get(key),
            update: async (key: string, value: unknown) => { global.set(key, value); },
        },
        extensionUri: { fsPath: "/ext", scheme: "file", toString: () => "/ext" },
    };
}

async function activateHarness(
    config: Record<string, unknown> = {},
): Promise<Harness> {
    let ext!: typeof ExtensionModule;
    let vscode!: typeof VSCodeModule;
    let fake!: { instances: FakeSocket[] };
    jest.isolateModules(() => {
        /* eslint-disable @typescript-eslint/no-require-imports */
        vscode = require("vscode");
        fake = require("ws").__fake;
        ext = require("../extension");
        /* eslint-enable @typescript-eslint/no-require-imports */
    });
    const mockWorkspace = vscode.workspace as unknown as {
        __setConfig(key: string, value: unknown): void;
    };
    for (const [key, value] of Object.entries(config)) {
        mockWorkspace.__setConfig(key, value);
    }
    await ext.activate(makeContext() as never);
    const socket = fake.instances[fake.instances.length - 1];
    const register = vscode.window.registerWebviewViewProvider as unknown as jest.Mock;
    const panel = register.mock.calls[0][1] as CortexPanelProvider;
    const createStatusBarItem = vscode.window.createStatusBarItem as unknown as jest.Mock;
    const harness: Harness = {
        ext,
        vscode,
        socket,
        panel,
        statusBar: () => {
            const results = createStatusBarItem.mock.results;
            return results[results.length - 1].value as StatusBarItemShape;
        },
        deliver(type, payload, extra = {}) {
            sequence += 1;
            socket.emit("message", Buffer.from(JSON.stringify({
                type,
                payload,
                schema_version: "2.0",
                protocol_version: "2.0",
                event_id: `evt-${sequence}`,
                sequence,
                timestamp: 1,
                ...extra,
            })));
        },
    };
    active = harness;
    return harness;
}

function connect(h: Harness): void {
    h.deliver("AUTH_OK", { selected_protocol_version: "2.0" });
}

function makeView() {
    let html = "";
    return {
        view: {
            webview: {
                options: {} as Record<string, unknown>,
                get html(): string { return html; },
                set html(value: string) { html = value; },
                postMessage: jest.fn(() => Promise.resolve(true)),
                onDidReceiveMessage: () => ({ dispose: jest.fn() }),
            },
            show: jest.fn(),
            onDidDispose: () => ({ dispose: jest.fn() }),
            visible: true,
        },
        get html(): string { return html; },
    };
}

beforeEach(() => {
    jest.useFakeTimers({ doNotFake: ["nextTick", "queueMicrotask", "setImmediate"] });
});

afterEach(() => {
    active?.ext.deactivate();
    active = undefined;
    jest.clearAllTimers();
    jest.useRealTimers();
});

const FLOW_ESTIMATE = {
    state: "FLOW",
    status: "estimated",
    confidence: 0.84,
    evidence_coverage: 0.9,
};

describe("status bar quiet mode lifetime", () => {
    it("stops masking the estimate once a timed mode's end time has passed", async () => {
        const h = await activateHarness();
        connect(h);
        h.deliver("QUIET_MODE_STATE", {
            kind: "snooze_15",
            duration_minutes: 1,
            ends_at_unix_ms: Date.now() + 60_000,
            source: "vscode",
        });
        h.deliver("STATE_UPDATE", FLOW_ESTIMATE);
        expect(h.statusBar().text).toContain("Snoozed");

        // The daemon lapses a timed snooze on restart without broadcasting
        // "off", and a decay frame sent while this window was disconnected is
        // never replayed. The daemon itself treats a passed end time as off.
        jest.setSystemTime(Date.now() + 61_000);
        h.deliver("STATE_UPDATE", FLOW_ESTIMATE);
        expect(h.statusBar().text).not.toContain("Snoozed");
        expect(h.statusBar().text).toContain("Steady activity");
    });

    it("an indefinite pause stays until the daemon says otherwise", async () => {
        const h = await activateHarness();
        connect(h);
        h.deliver("QUIET_MODE_STATE", { kind: "pause", source: "dashboard" });
        jest.setSystemTime(Date.now() + 6 * 60 * 60 * 1000);
        h.deliver("STATE_UPDATE", FLOW_ESTIMATE);
        expect(h.statusBar().text).toContain("Paused");
    });

    it("remembers a mode that arrived while the status bar was hidden", async () => {
        const h = await activateHarness({ "cortex.showStatusBar": false });
        connect(h);
        h.deliver("QUIET_MODE_STATE", { kind: "pause", source: "dashboard" });

        const mockWorkspace = h.vscode.workspace as unknown as {
            __setConfig(key: string, value: unknown): void;
            __fireConfigChange(keys: string[]): void;
        };
        mockWorkspace.__setConfig("cortex.showStatusBar", true);
        mockWorkspace.__fireConfigChange(["cortex.showStatusBar"]);
        expect(h.statusBar().text).toContain("Paused");
    });
});

describe("DISMISS_OVERLAY (P0 §3.21 cross-surface dismiss)", () => {
    it("clears the intervention the daemon dismissed from the panel", async () => {
        const h = await activateHarness();
        connect(h);
        const v = makeView();
        h.panel.resolveWebviewView(v.view as never, {} as never, {} as never);

        h.deliver("INTERVENTION_TRIGGER", {
            intervention_id: "iv-dismiss",
            level: "overlay_only",
            headline: "Take a breath",
        });
        expect(h.panel.currentInterventionId).toBe("iv-dismiss");
        expect(v.html).toContain('class="intervention"');

        // Cmd+Shift+D on the desktop or in the browser dismisses every
        // surface; the daemon broadcasts this cue and nothing else.
        h.deliver("DISMISS_OVERLAY", {
            intervention_id: "iv-dismiss",
            reason: "user_shortcut",
        });
        expect(h.panel.currentInterventionId).toBeNull();
        expect(v.html).not.toContain('class="intervention"');
    });

    it("keeps a newer intervention when the cue names an older one", async () => {
        const h = await activateHarness();
        connect(h);
        h.deliver("INTERVENTION_TRIGGER", { intervention_id: "iv-new", headline: "New" });
        h.deliver("DISMISS_OVERLAY", { intervention_id: "iv-old", reason: "user_shortcut" });
        expect(h.panel.currentInterventionId).toBe("iv-new");
    });

    it("clears whatever is shown when the cue carries no id", async () => {
        const h = await activateHarness();
        connect(h);
        h.deliver("INTERVENTION_TRIGGER", { intervention_id: "iv-any", headline: "Any" });
        h.deliver("DISMISS_OVERLAY", { intervention_id: null, reason: "user_shortcut" });
        expect(h.panel.currentInterventionId).toBeNull();
    });
});
