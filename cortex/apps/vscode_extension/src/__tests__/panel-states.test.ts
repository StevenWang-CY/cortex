/**
 * P2-6 — CortexPanelProvider distinct empty states.
 *
 * Three distinct HTML branches:
 *   (a) connected=false, payload=null  → "Daemon offline" + Reconnect button
 *   (b) connected=true,  payload=null, no state yet → "Connected, awaiting state" spinner
 *   (c) connected=true,  payload=null, state received → "Active" / no intervention
 *
 * Tests feed each combination and assert the correct data-testid appears.
 */

import { CortexPanelProvider } from "../panel-provider";

// ── Minimal fake CortexWSClient ──────────────────────────────────────────────

class FakeWSClient {
    private _stateHandlers: Array<(p: Record<string, unknown>) => void> = [];
    private _connHandlers: Array<(c: boolean) => void> = [];
    private _connected = false;

    get isConnected(): boolean { return this._connected; }
    setConnected(v: boolean): void { this._connected = v; }

    onStateUpdate(h: (p: Record<string, unknown>) => void): void {
        this._stateHandlers.push(h);
    }
    onConnectionChange(h: (c: boolean) => void): void {
        this._connHandlers.push(h);
    }

    connect(): void {}
    sendUserAction(_: string, __: string): void {}
    sendUserRating(_: string, __: string, ___?: string): void {}
    sendWhyDetailRequest(_: string): void {}
    sendMicroStepToggled(_: string, __: number, ___: string): void {}
}

const fakeUri = { fsPath: "/fake", scheme: "file", toString: () => "/fake" };

function makeProvider(client: FakeWSClient): CortexPanelProvider {
    return new CortexPanelProvider(fakeUri as never, client as never);
}

// Capture the HTML the provider would set on the webview.
function captureHtml(provider: CortexPanelProvider): string {
    (provider as unknown as Record<string, unknown>)["_getWebviewContent"] =
        () => {
            // Delegate to the real _getEmptyStateHtml by calling the provider's
            // private method directly.
            const fn = (provider as unknown as Record<string, () => string>)[
                "_getEmptyStateHtml"
            ];
            return fn.call(provider);
        };

    let captured = "";
    const fakeWebview = {
        options: {} as Record<string, unknown>,
        get html(): string { return captured; },
        set html(v: string) { captured = v; },
        postMessage(_msg: unknown): void {},
        onDidReceiveMessage: (_h: unknown) => ({ dispose: () => {} }),
    };
    const fakeView = {
        webview: fakeWebview,
        show: () => {},
        onDidDispose: (_h: unknown) => ({ dispose: () => {} }),
        visible: true,
    };
    provider.resolveWebviewView(fakeView as never, {} as never, {} as never);
    return captured;
}

describe("CortexPanelProvider – P2-6 distinct empty states", () => {
    it("(a) daemon offline → data-testid=cx-state-offline", () => {
        const client = new FakeWSClient();
        client.setConnected(false);
        const provider = makeProvider(client);
        // _currentState is empty (no STATE_UPDATE received)
        const html = captureHtml(provider);
        expect(html).toContain('data-testid="cx-state-offline"');
        expect(html).not.toContain('data-testid="cx-state-awaiting"');
        expect(html).not.toContain('data-testid="cx-state-active"');
    });

    it("(b) connected + no state yet → data-testid=cx-state-awaiting", () => {
        const client = new FakeWSClient();
        client.setConnected(true);
        const provider = makeProvider(client);
        // _currentState defaults to {} — no STATE_UPDATE received yet
        const html = captureHtml(provider);
        expect(html).toContain('data-testid="cx-state-awaiting"');
        expect(html).not.toContain('data-testid="cx-state-offline"');
        expect(html).not.toContain('data-testid="cx-state-active"');
    });

    it("(c) connected + state received → data-testid=cx-state-active", () => {
        const client = new FakeWSClient();
        client.setConnected(true);
        const provider = makeProvider(client);
        // Simulate a STATE_UPDATE arriving by setting _currentState directly.
        (provider as unknown as Record<string, unknown>)["_currentState"] = {
            state: "FLOW",
            confidence: 0.8,
        };
        const html = captureHtml(provider);
        expect(html).toContain('data-testid="cx-state-active"');
        expect(html).not.toContain('data-testid="cx-state-offline"');
        expect(html).not.toContain('data-testid="cx-state-awaiting"');
    });
});
