/**
 * CortexPanelProvider.
 *
 *  - P0-4: subscription resilience (a throw inside a handler must not kill
 *    the subscription).
 *  - Reduced-motion pacer lifecycle.
 *  - A2: nonce-based CSP, no inline handlers, script-safe JSON.
 *  - A6: same-id rebroadcast patches micro-steps instead of rebuilding.
 *  - A7: WHY_DETAIL timeout / error copy.
 *  - A8: the view reference is dropped on dispose.
 *  - A11: theme-token colours, no hard-coded white fills.
 *  - A12: workbench.reduceMotion is mirrored into the webview.
 *  - UX: one "Why this?" affordance, one USER_RATING send site.
 */

import * as vscode from "vscode";
import { CortexPanelProvider } from "../panel-provider";
import { WhyDetailTimeoutError } from "../ws-client";

// ── Minimal fake CortexWSClient ──────────────────────────────────────────────

type StateHandler = (p: Record<string, unknown>) => void;
type ConnHandler = (c: boolean) => void;

class FakeWSClient {
    private _stateHandlers: StateHandler[] = [];
    private _connHandlers: ConnHandler[] = [];
    private _connected = false;
    whyDetailImpl: (id: string) => Promise<Record<string, unknown>> =
        () => new Promise(() => { /* never settles by default */ });

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
    connect = jest.fn();
    sendUserAction = jest.fn();
    sendUserRating = jest.fn();
    sendMicroStepToggled = jest.fn();
    sendWhyDetailRequest(id: string): Promise<Record<string, unknown>> {
        return this.whyDetailImpl(id);
    }
}

// ── Minimal fake vscode.Uri ──────────────────────────────────────────────────

const fakeUri = {
    fsPath: "/fake",
    scheme: "file",
    toString: () => "/fake",
    with: () => fakeUri,
};

const mockWorkspace = vscode.workspace as unknown as {
    __setConfig: (key: string, value: unknown) => void;
    __resetConfig: () => void;
    __fireConfigChange: (keys: string[]) => void;
};

// ── Helpers to build the provider ───────────────────────────────────────────

function makeProvider(client: FakeWSClient): CortexPanelProvider {
    // CortexPanelProvider only uses the Uri for localResourceRoots; we
    // never call resolveWebviewView in these unit tests so a dummy suffices.
    return new CortexPanelProvider(fakeUri as never, client as never);
}

/** A fake WebviewView that records html sets, posted messages, show() and dispose hooks. */
function makeView() {
    const posted: Record<string, unknown>[] = [];
    let html = "";
    let htmlSets = 0;
    let messageHandler: ((m: Record<string, unknown>) => void) | undefined;
    const disposeHandlers: Array<() => void> = [];
    const show = jest.fn();
    const view = {
        webview: {
            options: {} as Record<string, unknown>,
            get html(): string { return html; },
            set html(v: string) { html = v; htmlSets += 1; },
            postMessage: jest.fn((m: Record<string, unknown>) => {
                posted.push(m);
                return Promise.resolve(true);
            }),
            onDidReceiveMessage: (h: (m: Record<string, unknown>) => void) => {
                messageHandler = h;
                return { dispose: jest.fn() };
            },
        },
        show,
        onDidDispose: (h: () => void) => {
            disposeHandlers.push(h);
            return { dispose: jest.fn() };
        },
        visible: true,
    };
    return {
        view,
        posted,
        show,
        get html(): string { return html; },
        get htmlSets(): number { return htmlSets; },
        send(m: Record<string, unknown>): void { messageHandler?.(m); },
        triggerDispose(): void { for (const h of disposeHandlers) h(); },
    };
}

function fullHtmlFor(payload: Record<string, unknown> | null, client = new FakeWSClient()): string {
    const provider = makeProvider(client);
    (provider as unknown as Record<string, unknown>)["_currentPayload"] = payload;
    const fn = (provider as unknown as Record<string, () => string>)["_getWebviewContent"];
    return fn.call(provider);
}

async function flushPromises(): Promise<void> {
    for (let i = 0; i < 6; i++) await Promise.resolve();
}

afterEach(() => {
    mockWorkspace.__resetConfig();
});

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
            client.fireState({
                state: "HYPER",
                status: "estimated",
                confidence: 0.9,
                evidence_coverage: 0.8,
            });
        }).not.toThrow();

        // Second fire — postMessage no longer throws; the message must land.
        client.fireState({
            state: "FLOW",
            status: "estimated",
            confidence: 0.7,
            evidence_coverage: 0.8,
        });

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

describe("CortexPanelProvider – reduced-motion pacer lifecycle", () => {
    function getFullHtml(): string {
        return fullHtmlFor({
            intervention_id: "iv-motion",
            headline: "Take a breath",
            situation_summary: "Pause briefly",
            primary_focus: "Breathe",
            micro_steps: [],
        });
    }

    it("owns one cancelable frame loop and stops it while hidden", () => {
        const html = getFullHtml();

        expect(html).toContain("let pacerFrameId = null");
        expect(html).toContain("cancelAnimationFrame(pacerFrameId)");
        expect(html).toContain("document.visibilityState === 'visible'");
        expect(html).toContain(
            "document.addEventListener('visibilitychange', syncPacer)",
        );
        expect(html).not.toMatch(
            /^\s*requestAnimationFrame\(drawPacer\);/m,
        );
    });

    it("renders a static, countdown-free guide under Reduce Motion", () => {
        const html = getFullHtml();

        expect(html).toContain("'(prefers-reduced-motion: reduce)'");
        expect(html).toContain(
            "reducedPacerMotion.addEventListener('change', syncPacer)",
        );
        expect(html).toContain("drawPacerDisc(0.46)");
        expect(html).toContain("Breathe at your pace");
        expect(html).toContain("timerEl.textContent = ''");
        expect(html).toContain('aria-label="Breathing guide"');
    });
});

// ── A2: Content-Security-Policy ─────────────────────────────────────────────

describe("CortexPanelProvider – CSP and script-safe JSON (A2)", () => {
    const hostile = "</script><script>alert(1)</script>";
    const payload = {
        intervention_id: "iv-csp",
        headline: `Head ${hostile}`,
        situation_summary: "Summary",
        primary_focus: "Focus",
        micro_steps: ["Step one"],
        causal_explanation: `Because ${hostile}`,
        causal_signals: [{ name: hostile, current_value: 1, unit: "x" }],
    };

    it("emits a nonce-based CSP and nonces every script", () => {
        const html = fullHtmlFor(payload);
        const meta = html.match(
            /<meta http-equiv="Content-Security-Policy" content="([^"]+)"/,
        );
        expect(meta).not.toBeNull();
        const csp = meta![1];
        expect(csp).toContain("default-src 'none'");
        expect(csp).toContain("style-src 'unsafe-inline'");
        const nonceMatch = csp.match(/script-src 'nonce-([A-Za-z0-9+/=]+)'/);
        expect(nonceMatch).not.toBeNull();
        const nonce = nonceMatch![1];
        expect(nonce.length).toBeGreaterThanOrEqual(16);

        const scriptTags = html.match(/<script\b[^>]*>/g) ?? [];
        expect(scriptTags).toHaveLength(1);
        for (const tag of scriptTags) {
            expect(tag).toContain(`nonce="${nonce}"`);
        }
        // A fresh nonce per render.
        expect(fullHtmlFor(payload)).not.toContain(`nonce="${nonce}"`);
    });

    it("has no inline event handlers", () => {
        const html = fullHtmlFor(payload);
        expect(html).not.toMatch(/\son[a-z]+=/i);
        expect(html).not.toContain('class="causal"');
    });

    it("a </script> payload cannot break out of the nonce script", () => {
        const html = fullHtmlFor(payload);
        expect(html).not.toContain("</script><script>alert(1)");
        expect(html).not.toContain("<script>alert");
        // Exactly one closing tag: the real one.
        expect(html.match(/<\/script>/g)).toHaveLength(1);
        // The signal name survives as JSON with unicode escapes.
        expect(html).toContain("\\u003c/script\\u003e");
        // Text content is HTML-escaped.
        expect(html).toContain("Head &lt;/script&gt;");
        expect(html).toContain("Because &lt;/script&gt;");
    });

    it("safeJsonForScript round-trips through JSON.parse", () => {
        const escaped = CortexPanelProvider.safeJsonForScript([
            { name: hostile, text: "a & b < c > d \u2028 e \u2029 f" },
        ]);
        expect(escaped).not.toContain("<");
        expect(escaped).not.toContain(">");
        expect(escaped).not.toContain("&");
        expect(escaped).not.toContain("\u2028");
        expect(JSON.parse(escaped)).toEqual([
            { name: hostile, text: "a & b < c > d \u2028 e \u2029 f" },
        ]);
    });
});

// ── A8: dispose ─────────────────────────────────────────────────────────────

describe("CortexPanelProvider – view disposal (A8)", () => {
    it("forgets the view on onDidDispose so later calls are no-ops", () => {
        const client = new FakeWSClient();
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        expect(v.htmlSets).toBe(1);

        v.triggerDispose();

        provider.showPanel();
        expect(v.show).not.toHaveBeenCalled();
        expect(() => provider.showIntervention({
            intervention_id: "iv-after-dispose",
            headline: "x",
        })).not.toThrow();
        expect(v.htmlSets).toBe(1);
        expect(provider.currentInterventionId).toBe("iv-after-dispose");

        // Re-resolving a new view renders the retained intervention.
        const v2 = makeView();
        provider.resolveWebviewView(v2.view as never, {} as never, {} as never);
        expect(v2.htmlSets).toBe(1);
        expect(v2.html).toContain("iv-after-dispose".length > 0 ? "class=\"intervention\"" : "");
    });

    it("dispose() releases configuration and view subscriptions", () => {
        const client = new FakeWSClient();
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        provider.dispose();
        provider.showPanel();
        expect(v.show).not.toHaveBeenCalled();
        mockWorkspace.__setConfig("workbench.reduceMotion", "on");
        mockWorkspace.__fireConfigChange(["workbench.reduceMotion"]);
        expect(v.posted.some((m) => m.type === "motion")).toBe(false);
    });
});

// ── A6: same-id rebroadcast patches instead of rebuilding ───────────────────

describe("CortexPanelProvider – micro-step patch on rebroadcast (A6)", () => {
    it("patches steps via postMessage and does not rebuild or re-reveal", () => {
        const client = new FakeWSClient();
        client.setConnected(true);
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        expect(v.htmlSets).toBe(1);

        provider.showIntervention({
            intervention_id: "iv-1",
            headline: "Breathe",
            micro_steps: [{ text: "Stand up", status: "pending" }, "Drink water"],
        });
        expect(v.htmlSets).toBe(2);
        expect(v.show).toHaveBeenCalledTimes(1);
        expect(v.html).toContain('class="step-text" data-step-index="0"');

        // Daemon echo after MICRO_STEP_TOGGLED.
        provider.showIntervention({
            intervention_id: "iv-1",
            headline: "Breathe",
            micro_steps: [{ text: "Stand up", status: "done" }, "Drink water"],
        });
        expect(v.htmlSets).toBe(2);
        expect(v.show).toHaveBeenCalledTimes(1);
        const patch = v.posted.find((m) => m.type === "microSteps");
        expect(patch).toEqual({
            type: "microSteps",
            steps: [
                { text: "Stand up", status: "done" },
                { text: "Drink water", status: "pending" },
            ],
        });

        // A different intervention still rebuilds and reveals.
        provider.showIntervention({ intervention_id: "iv-2", headline: "Next" });
        expect(v.htmlSets).toBe(3);
        expect(v.show).toHaveBeenCalledTimes(2);
    });

    it("the webview script applies microSteps patches in place", () => {
        const html = fullHtmlFor({ intervention_id: "iv", micro_steps: ["a"] });
        expect(html).toContain("case 'microSteps':");
        expect(html).toContain("function applyMicroSteps(steps)");
        expect(html).toContain("span.classList.toggle('is-done', done)");
    });

    it("clearIntervention(id) ignores restores for a different intervention (A14)", () => {
        const client = new FakeWSClient();
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        provider.showIntervention({ intervention_id: "iv-new", headline: "New" });
        const sets = v.htmlSets;

        provider.clearIntervention("iv-old");
        expect(provider.currentInterventionId).toBe("iv-new");
        expect(v.htmlSets).toBe(sets);

        provider.clearIntervention("iv-new");
        expect(provider.currentInterventionId).toBeNull();
        expect(v.htmlSets).toBe(sets + 1);
    });
});

// ── A7: WHY_DETAIL ──────────────────────────────────────────────────────────

describe("CortexPanelProvider – WHY_DETAIL handling (A7)", () => {
    it("renders loading then timeout copy instead of an unhandled rejection", async () => {
        const client = new FakeWSClient();
        client.whyDetailImpl = () =>
            Promise.reject(new WhyDetailTimeoutError("corr", 5000));
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        provider.showIntervention({ intervention_id: "iv-why", headline: "Why" });

        v.send({ command: "whyDetailRequest" });
        await flushPromises();

        const whyMessages = v.posted.filter((m) => m.type === "whyDetail");
        expect(whyMessages).toEqual([
            { type: "whyDetail", status: "loading" },
            { type: "whyDetail", status: "error", error: "timeout" },
        ]);
    });

    it("reports request_failed for non-timeout rejections", async () => {
        const client = new FakeWSClient();
        client.whyDetailImpl = () => Promise.reject(new Error("socket gone"));
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        provider.showIntervention({ intervention_id: "iv-why", headline: "Why" });

        v.send({ command: "whyDetailRequest" });
        await flushPromises();

        expect(v.posted.filter((m) => m.type === "whyDetail").pop()).toEqual({
            type: "whyDetail",
            status: "error",
            error: "request_failed",
        });
    });

    it("delivers the resolved payload and does not report a timeout afterwards", async () => {
        const client = new FakeWSClient();
        client.whyDetailImpl = () =>
            Promise.resolve({ intervention_id: "iv-why", causal_signals: [{ name: "blink" }] });
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        provider.showIntervention({ intervention_id: "iv-why", headline: "Why" });

        v.send({ command: "whyDetailRequest" });
        await flushPromises();

        const last = v.posted.filter((m) => m.type === "whyDetail").pop();
        expect(last).toMatchObject({ type: "whyDetail", status: "ok", error: null });
        expect((last as { payload: Record<string, unknown> }).payload.causal_signals)
            .toEqual([{ name: "blink" }]);
    });

    it("honours payload.error such as handler_not_registered", () => {
        const client = new FakeWSClient();
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);

        provider.applyWhyDetail({
            intervention_id: "iv",
            causal_signals: [],
            error: "handler_not_registered",
        });
        expect(v.posted.pop()).toMatchObject({
            type: "whyDetail",
            status: "error",
            error: "handler_not_registered",
        });

        const html = fullHtmlFor({ intervention_id: "iv", headline: "h" });
        expect(html).toContain("Explanations are not available from this Cortex build.");
        expect(html).toContain("The explanation took too long to arrive.");
        expect(html).toContain("Gathering signals…");
    });
});

// ── A11: theme ──────────────────────────────────────────────────────────────

describe("CortexPanelProvider – theme-aware colours (A11)", () => {
    it("uses VS Code theme tokens and no hard-coded white fills", () => {
        const html = fullHtmlFor({
            intervention_id: "iv-theme",
            headline: "h",
            micro_steps: ["a"],
        });
        const css = html.slice(html.indexOf("<style>"), html.indexOf("</style>"));
        expect(css).not.toMatch(/rgba\(\s*255\s*,\s*255\s*,\s*255/);
        expect(css).not.toMatch(/:\s*white\b/);
        expect(css).not.toMatch(/#fff\b/i);
        expect(css).toContain("--vscode-button-background");
        expect(css).toContain("--vscode-button-foreground");
        expect(css).toContain("--vscode-input-background");
        expect(css).toContain("--vscode-textLink-foreground");
        expect(css).toContain("--vscode-editorWidget-border");
        expect(css).toContain("--vscode-editorWarning-foreground");
        // Terracotta only on the card border and pacer.
        expect(css).toMatch(/\.focus\s*\{[^}]*color:\s*var\(--cx-text\)/);
        expect(css).not.toMatch(/\.why-delta-down\s*\{[^}]*#E47A6E/);
        expect(css).toMatch(/\.reconnect-btn\s*\{[^}]*font-size:\s*var\(--fs-footnote\)/);
    });

    it("renders theme-aware state dots via data-state", () => {
        const html = fullHtmlFor(null);
        expect(html).toContain('id="cx-state-dot" data-state="UNKNOWN"');
        expect(html).not.toMatch(/id="cx-state-dot" style=/);
        expect(html).toContain('.state-dot[data-state="HYPO"]');
        expect(html).toContain('.state-dot[data-state="UNKNOWN"]');
        expect(html).not.toContain("#3C3C432E");
    });
});

// ── A12: workbench.reduceMotion ─────────────────────────────────────────────

describe("CortexPanelProvider – workbench.reduceMotion (A12)", () => {
    it("bakes the initial preference into the HTML and posts changes", () => {
        mockWorkspace.__setConfig("workbench.reduceMotion", "on");
        const client = new FakeWSClient();
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        expect(v.html).toContain('data-reduce-motion="true"');
        expect(v.html).toContain("case 'motion':");
        expect(v.html).toContain("hostReduceMotion");

        mockWorkspace.__setConfig("workbench.reduceMotion", "off");
        mockWorkspace.__fireConfigChange(["workbench.reduceMotion"]);
        expect(v.posted).toContainEqual({ type: "motion", reduce: false });

        mockWorkspace.__setConfig("workbench.reduceMotion", "on");
        mockWorkspace.__fireConfigChange(["workbench.reduceMotion"]);
        expect(v.posted).toContainEqual({ type: "motion", reduce: true });

        // Unrelated settings are ignored.
        const count = v.posted.length;
        mockWorkspace.__fireConfigChange(["editor.fontSize"]);
        expect(v.posted.length).toBe(count);
    });

    it("defaults to following the OS preference", () => {
        const html = fullHtmlFor(null);
        expect(html).toContain('data-reduce-motion="false"');
    });
});

// ── UX polish ───────────────────────────────────────────────────────────────

describe("CortexPanelProvider – UX polish", () => {
    it("has a single Why affordance and a single USER_RATING send site", () => {
        const html = fullHtmlFor({
            intervention_id: "iv-ux",
            headline: "h",
            causal_explanation: "because",
        });
        expect(html.match(/id="why-toggle"/g)).toHaveLength(1);
        expect(html).not.toContain('class="causal"');
        expect(html).toContain('class="why-explanation"');
        expect(html.match(/command: 'userRating'/g)).toHaveLength(1);
        // 👎 defers the send to Enter/Esc; Enter must not send twice.
        expect(html).toContain("if (!downPending) return;");
    });

    it("forwards a single thumbs_down with context from the webview", () => {
        const client = new FakeWSClient();
        const provider = makeProvider(client);
        const v = makeView();
        provider.resolveWebviewView(v.view as never, {} as never, {} as never);
        provider.showIntervention({ intervention_id: "iv-rate", headline: "h" });
        v.send({ command: "userRating", rating: "thumbs_down", context: "More time" });
        expect(client.sendUserRating).toHaveBeenCalledTimes(1);
        expect(client.sendUserRating).toHaveBeenCalledWith("iv-rate", "thumbs_down", "More time");
    });
});
