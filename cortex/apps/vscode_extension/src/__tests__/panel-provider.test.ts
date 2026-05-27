/**
 * P0-4 — CortexPanelProvider subscription resilience.
 *
 * Verifies that a throw inside _postStateToWebview (e.g., the webview
 * stub throws on postMessage) does NOT kill the onStateUpdate subscription.
 * A subsequent valid payload must still be delivered.
 */

import { CortexPanelProvider } from "../panel-provider";

// ── Minimal fake CortexWSClient ──────────────────────────────────────────────

type StateHandler = (p: Record<string, unknown>) => void;
type ConnHandler = (c: boolean) => void;

class FakeWSClient {
    private _stateHandlers: StateHandler[] = [];
    private _connHandlers: ConnHandler[] = [];
    private _connected = false;

    get isConnected(): boolean { return this._connected; }
    setConnected(v: boolean): void { this._connected = v; }

    onStateUpdate(h: StateHandler): void { this._stateHandlers.push(h); }
    onConnectionChange(h: ConnHandler): void { this._connHandlers.push(h); }

    fireState(p: Record<string, unknown>): void {
        for (const h of this._stateHandlers) h(p);
    }
    fireConnection(c: boolean): void {
        for (const h of this._connHandlers) h(c);
    }

    // Other methods that panel-provider may call
    connect(): void {}
    sendUserAction(_: string, __: string): void {}
    sendUserRating(_: string, __: string, ___?: string): void {}
    sendWhyDetailRequest(_: string): void {}
    sendMicroStepToggled(_: string, __: number, ___: string): void {}
}

// ── Minimal fake vscode.Uri ──────────────────────────────────────────────────

const fakeUri = {
    fsPath: "/fake",
    scheme: "file",
    toString: () => "/fake",
    with: () => fakeUri,
};

// ── Helpers to build the provider ───────────────────────────────────────────

function makeProvider(client: FakeWSClient): CortexPanelProvider {
    // CortexPanelProvider only uses the Uri for localResourceRoots; we
    // never call resolveWebviewView in these unit tests so a dummy suffices.
    return new CortexPanelProvider(fakeUri as never, client as never);
}

// ── Test: subscription survives a throw in _postStateToWebview ───────────────

describe("CortexPanelProvider – P0-4 subscription resilience", () => {
    it("onStateUpdate subscription stays alive after _postStateToWebview throws", () => {
        const client = new FakeWSClient();
        const provider = makeProvider(client);

        // Stub _getWebviewContent to return a trivial string so we avoid the
        // giant template literal that exceeds ts-jest's template recursion limit.
        (provider as unknown as Record<string, unknown>)["_getWebviewContent"] =
            () => "<html><body>stub</body></html>";

        // Inject a webview view whose postMessage throws on the first call.
        let callCount = 0;
        const capturedMessages: unknown[] = [];
        const fakeWebview = {
            options: {} as Record<string, unknown>,
            html: "",
            postMessage(msg: unknown): void {
                callCount += 1;
                if (callCount === 1) {
                    throw new Error("simulated postMessage failure");
                }
                capturedMessages.push(msg);
            },
            onDidReceiveMessage: (_handler: unknown) => ({ dispose: () => {} }),
        };
        const fakeView = {
            webview: fakeWebview,
            show: () => {},
            onDidDispose: (_h: unknown) => ({ dispose: () => {} }),
            visible: true,
        };

        // Wire up the view (resolveWebviewView sets this._view).
        provider.resolveWebviewView(
            fakeView as never,
            {} as never,
            {} as never,
        );

        // First fire — postMessage throws internally; the try/catch must
        // swallow the error and keep the subscription alive.
        expect(() => {
            client.fireState({ state: "HYPER", confidence: 0.9 });
        }).not.toThrow();

        // Second fire — postMessage no longer throws; the message must land.
        client.fireState({ state: "FLOW", confidence: 0.7 });

        // The second postMessage (callCount===2) must have reached the stub.
        expect(capturedMessages.length).toBe(1);
        expect((capturedMessages[0] as Record<string, unknown>).type).toBe("state");
        expect((capturedMessages[0] as Record<string, unknown>).state).toBe("FLOW");
    });

    it("onConnectionChange subscription stays alive after _updatePanel throws", () => {
        const client = new FakeWSClient();
        const provider = makeProvider(client);

        // Stub _getWebviewContent to avoid large template literal.
        (provider as unknown as Record<string, unknown>)["_getWebviewContent"] =
            () => "<html><body>stub</body></html>";

        let htmlSetCount = 0;
        const fakeWebview = {
            options: {} as Record<string, unknown>,
            get html(): string { return ""; },
            set html(_v: string) {
                htmlSetCount += 1;
                // Allow the first call (from resolveWebviewView's initial
                // _updatePanel). Throw on the second call (from fireConnection).
                if (htmlSetCount === 2) {
                    throw new Error("simulated html setter failure");
                }
            },
            postMessage(_msg: unknown): void {},
            onDidReceiveMessage: (_handler: unknown) => ({ dispose: () => {} }),
        };
        const fakeView = {
            webview: fakeWebview,
            show: () => {},
            onDidDispose: (_h: unknown) => ({ dispose: () => {} }),
            visible: true,
        };

        provider.resolveWebviewView(fakeView as never, {} as never, {} as never);

        // First connection change — html setter throws; must not propagate.
        expect(() => {
            client.fireConnection(false);
        }).not.toThrow();

        // Second connection change — must not throw either (subscription alive).
        expect(() => {
            client.fireConnection(true);
        }).not.toThrow();

        // htmlSetCount: 1 from resolveWebviewView initial _updatePanel,
        // +1 from first fireConnection (throws at htmlSetCount===2, swallowed),
        // +1 from second fireConnection (htmlSetCount===3, succeeds).
        expect(htmlSetCount).toBeGreaterThanOrEqual(3);
    });
});
