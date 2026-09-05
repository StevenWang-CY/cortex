/**
 * Cortex VS Code Extension — Panel Provider
 *
 * Provides a webview-based side panel that displays:
 * - Intervention headline, situation summary, primary focus
 * - Micro-step checklist (1-3 items)
 * - 4-7-8 breathing pacer animation
 * - Dismiss button
 * - Connection status and current state
 *
 * Security/lifecycle contract (audit A2/A6/A7/A8/A10/A11/A12):
 * - Every render carries a nonce-based Content-Security-Policy; there
 *   are no inline event handlers and daemon-provided JSON is escaped
 *   before it is embedded in the nonce script.
 * - A rebroadcast of the *same* intervention (daemon echo after
 *   MICRO_STEP_TOGGLED) patches the micro-steps via postMessage instead
 *   of rebuilding the DOM, so the pacer, "Why this?" state and rating
 *   survive.
 * - The WHY_DETAIL promise is always consumed; loading/timeout/error
 *   copy is rendered instead of leaving an empty panel.
 * - The view reference is dropped on ``onDidDispose``; every
 *   subscription is disposable through ``dispose()``.
 * - Colours come from VS Code theme variables; terracotta is reserved
 *   for the card border and the breathing pacer.
 * - ``workbench.reduceMotion`` is honoured alongside the OS preference.
 */

import * as vscode from "vscode";
import { randomBytes } from "crypto";
import { CortexWSClient, WhyDetailTimeoutError } from "./ws-client";
import { PANEL_STATE_LABELS } from "./design-tokens";

type StepStatus = "pending" | "done" | "skipped";
interface StepRow {
    text: string;
    status: StepStatus;
}

/**
 * Webview provider for the Cortex intervention side panel.
 *
 * Registered as "cortex.interventionPanel" in the activity bar.
 * Renders LLM-generated intervention content with a calming UI.
 */
export class CortexPanelProvider
    implements vscode.WebviewViewProvider, vscode.Disposable
{
    private _view: vscode.WebviewView | undefined;
    private _extensionUri: vscode.Uri;
    private _wsClient: CortexWSClient;
    private _currentPayload: Record<string, unknown> | null = null;
    private _currentState: Record<string, unknown> = {};
    private readonly _disposables: vscode.Disposable[] = [];
    private _viewDisposables: vscode.Disposable[] = [];
    /** intervention_id of the WHY_DETAIL request currently awaiting a reply. */
    private _whyDetailInFlight: string | null = null;
    /** A12: mirror of ``workbench.reduceMotion === "on"``. */
    private _reduceMotion = false;

    constructor(extensionUri: vscode.Uri, wsClient: CortexWSClient) {
        this._extensionUri = extensionUri;
        this._wsClient = wsClient;
        this._reduceMotion = CortexPanelProvider._readReduceMotion();

        // D.1: STATE_UPDATE fires every 500ms. A full HTML rebuild here
        // resets the breathing-pacer canvas's animation start time on
        // every tick, so the pacer never actually animates. Instead push
        // a diff message into the existing webview script, which updates
        // the state label + confidence in place. Full HTML re-render is
        // reserved for showIntervention / clearIntervention where the
        // structural content actually changes.
        //
        // P0-4: wrap in try/catch so a throw inside _postStateToWebview
        // (e.g. a bad payload crashing postMessage) does not silently
        // kill the subscription — the onStateUpdate stream stays alive
        // and the next valid payload will still be delivered.
        wsClient.onStateUpdate((payload) => {
            try {
                this._currentState = payload;
                this._postStateToWebview();
            } catch (err) {
                console.error("[CortexPanel] onStateUpdate handler threw:", err);
            }
        });

        // P1 (audit Phase 4d, Task B): rerender the empty-state region
        // when the connection flips between offline / online so the
        // "Reconnect" button appears or disappears in real time. The
        // full HTML rebuild here is acceptable because it only runs on
        // connection transitions (rare), not on every STATE_UPDATE
        // tick — the breathing-pacer animation is unaffected.
        //
        // P0-4: same guard — _updatePanel should not take down the
        // subscription if the webview is mid-teardown.
        wsClient.onConnectionChange((_connected) => {
            try {
                if (!this._currentPayload) {
                    this._updatePanel();
                }
            } catch (err) {
                console.error("[CortexPanel] onConnectionChange handler threw:", err);
            }
        });

        // A12: follow ``workbench.reduceMotion`` live. The initial value
        // is baked into the HTML (``data-reduce-motion``); changes are
        // pushed into the running webview so the pacer can stop or
        // resume without a rebuild.
        try {
            this._disposables.push(
                vscode.workspace.onDidChangeConfiguration((event) => {
                    if (!event.affectsConfiguration("workbench.reduceMotion")) {
                        return;
                    }
                    this._reduceMotion = CortexPanelProvider._readReduceMotion();
                    this._postMotionPreference();
                }),
            );
        } catch {
            // Stubbed host without configuration events (tests).
        }
    }

    /** A8/A10: drop the view and every subscription this provider owns. */
    dispose(): void {
        this._disposeViewSubscriptions();
        for (const d of this._disposables.splice(0)) {
            try {
                d.dispose();
            } catch {
                // already disposed
            }
        }
        this._view = undefined;
    }

    /**
     * Called by VS Code when the webview view is first shown.
     */
    resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ): void {
        this._disposeViewSubscriptions();
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri],
        };

        // Handle messages from webview
        // P0-4: wrap so a throw inside _handleWebviewMessage (bad message
        // shape, null intervention_id, etc.) cannot kill the subscription.
        this._track(
            webviewView.webview.onDidReceiveMessage((message) => {
                try {
                    this._handleWebviewMessage(message);
                } catch (err) {
                    console.error("[CortexPanel] onDidReceiveMessage handler threw:", err);
                }
            }),
        );

        // A8: once VS Code disposes the view (hidden without
        // retainContextWhenHidden, sidebar closed, extension host
        // reload) ``show()`` and the ``html`` setter throw. Forget the
        // reference so later showIntervention/showPanel calls become
        // no-ops until the view is resolved again.
        this._track(
            webviewView.onDidDispose(() => {
                if (this._view === webviewView) {
                    this._view = undefined;
                }
                this._disposeViewSubscriptions();
            }),
        );

        this._updatePanel();
    }

    /** intervention_id of the intervention currently on screen, if any. */
    get currentInterventionId(): string | null {
        const id = this._currentPayload?.intervention_id;
        return typeof id === "string" && id.length > 0 ? id : null;
    }

    /**
     * Show an intervention in the panel.
     *
     * A6: when the daemon rebroadcasts the intervention that is already
     * displayed (it does so after every MICRO_STEP_TOGGLED so peer
     * surfaces converge) only the micro-step rows are patched in place.
     * A full rebuild would restart the pacer, collapse "Why this?",
     * drop a pending rating and re-reveal the panel.
     */
    showIntervention(payload: Record<string, unknown>): void {
        const incomingId = typeof payload.intervention_id === "string"
            ? payload.intervention_id
            : null;
        const sameIntervention =
            incomingId !== null && incomingId === this.currentInterventionId;
        this._currentPayload = payload;

        if (sameIntervention && this._view) {
            this._postMicroSteps();
            return;
        }

        this._whyDetailInFlight = null;
        this._updatePanel();

        // Ensure panel is visible
        this.showPanel();
    }

    /**
     * Focus/reveal the side panel.
     */
    showPanel(): void {
        if (!this._view) return;
        try {
            this._view.show(true);
        } catch {
            // View disposed between the null-check and the call.
        }
    }

    public showMorningBriefing(payload: Record<string, unknown>): void {
        this.showPanel();
        this._post({
            type: "morningBriefing",
            payload,
        });
    }

    /**
     * P0 §3.9: route a WHY_DETAIL response into the webview so the
     * "Why this?" drilldown can render the structured causal signals.
     *
     * A7: ``payload.error`` (e.g. ``handler_not_registered``) is honoured
     * and rendered as an error state rather than as "no signals".
     */
    public applyWhyDetail(payload: Record<string, unknown>): void {
        this._whyDetailInFlight = null;
        const error = typeof payload.error === "string" && payload.error.length > 0
            ? payload.error
            : null;
        this._post({
            type: "whyDetail",
            status: error ? "error" : "ok",
            error,
            payload,
        });
    }

    /**
     * Clear the current intervention display.
     *
     * A14: when ``interventionId`` is given (INTERVENTION_RESTORE carries
     * one) the panel is only cleared if that is the intervention on
     * screen — a late restore for a previous intervention must not wipe
     * a newer one.
     */
    clearIntervention(interventionId?: string): void {
        if (interventionId && this._currentPayload) {
            const currentId = this.currentInterventionId;
            if (currentId && currentId !== interventionId) {
                return;
            }
        }
        this._currentPayload = null;
        this._whyDetailInFlight = null;
        this._updatePanel();
    }

    // --- Internal ---

    private _track(disposable: vscode.Disposable | undefined): void {
        if (disposable && typeof disposable.dispose === "function") {
            this._viewDisposables.push(disposable);
        }
    }

    private _disposeViewSubscriptions(): void {
        for (const d of this._viewDisposables.splice(0)) {
            try {
                d.dispose();
            } catch {
                // already disposed
            }
        }
    }

    private static _readReduceMotion(): boolean {
        try {
            return vscode.workspace
                .getConfiguration("workbench")
                .get<string>("reduceMotion", "auto") === "on";
        } catch {
            return false;
        }
    }

    /** postMessage that tolerates a webview mid-teardown. */
    private _post(message: Record<string, unknown>): boolean {
        if (!this._view) return false;
        try {
            void this._view.webview.postMessage(message);
            return true;
        } catch {
            // postMessage can throw briefly during webview teardown
            return false;
        }
    }

    private _postMotionPreference(): void {
        this._post({ type: "motion", reduce: this._reduceMotion });
    }

    private _postMicroSteps(): void {
        if (!this._currentPayload) return;
        this._post({
            type: "microSteps",
            steps: CortexPanelProvider._normalizeSteps(this._currentPayload.micro_steps),
        });
    }

    /**
     * P0 §3.6: micro_steps may carry either the legacy ``string[]`` shape
     * OR the ``{text, status, …}[]`` shape. Coerce both into a uniform
     * ``{text, status}`` list so the strikethrough styling reflects
     * daemon-authoritative state.
     */
    private static _normalizeSteps(raw: unknown): StepRow[] {
        const stepsRaw = Array.isArray(raw) ? raw : [];
        const steps: StepRow[] = [];
        for (const entry of stepsRaw) {
            if (typeof entry === "string" && entry.length > 0) {
                steps.push({ text: entry, status: "pending" });
            } else if (entry && typeof entry === "object") {
                const e = entry as Record<string, unknown>;
                const text = typeof e.text === "string" ? e.text : "";
                const rawStatus = typeof e.status === "string" ? e.status : "pending";
                const status: StepStatus =
                    rawStatus === "done" || rawStatus === "skipped" ? rawStatus : "pending";
                if (text.length > 0) steps.push({ text, status });
            }
        }
        return steps;
    }

    private _handleWebviewMessage(message: Record<string, unknown>): void {
        const command = String(message.command || "");
        switch (command) {
            case "dismiss":
                if (this._currentPayload) {
                    this._wsClient.sendUserAction(
                        "dismissed",
                        this._currentPayload.intervention_id as string,
                    );
                    this.clearIntervention();
                }
                break;

            case "engage":
                if (this._currentPayload) {
                    this._wsClient.sendUserAction(
                        "engaged",
                        this._currentPayload.intervention_id as string,
                    );
                }
                break;

            case "userRating": {
                // P0 §3.8: forward 👍/👎 to the daemon via the WS client.
                if (!this._currentPayload) break;
                const interventionId = this._currentPayload.intervention_id as string;
                if (!interventionId) break;
                const ratingRaw = String(message.rating || "");
                if (ratingRaw !== "thumbs_up" && ratingRaw !== "thumbs_down") break;
                const ctxRaw = message.context;
                const context = typeof ctxRaw === "string" ? ctxRaw.slice(0, 200) : undefined;
                this._wsClient.sendUserRating(
                    interventionId,
                    ratingRaw,
                    context,
                );
                break;
            }

            case "whyDetailRequest": {
                // P0 §3.9: forward the "Why this?" expansion to the daemon.
                if (!this._currentPayload) break;
                const interventionId = this._currentPayload.intervention_id as string;
                if (!interventionId) break;
                this._whyDetailInFlight = interventionId;
                this._post({ type: "whyDetail", status: "loading" });
                // A7: the promise rejects with WhyDetailTimeoutError after
                // 5 s; consuming it here avoids an unhandled rejection and
                // lets the panel show timeout copy instead of staying empty.
                Promise.resolve(this._wsClient.sendWhyDetailRequest(interventionId))
                    .then((payload) => {
                        if (payload && typeof payload === "object") {
                            this.applyWhyDetail(payload);
                        }
                    })
                    .catch((err: unknown) => {
                        // The generic WHY_DETAIL listener in extension.ts
                        // may already have delivered a reply that lacked a
                        // correlation id (older daemons); only report when
                        // nothing arrived at all.
                        if (this._whyDetailInFlight !== interventionId) return;
                        this._whyDetailInFlight = null;
                        const timedOut = err instanceof WhyDetailTimeoutError;
                        this._post({
                            type: "whyDetail",
                            status: "error",
                            error: timedOut ? "timeout" : "request_failed",
                        });
                    });
                break;
            }

            case "reconnect":
                // P1 (audit Phase 4d, Task B): the webview's
                // "Reconnect" button on the daemon-offline empty state
                // tells the host to retry the WS handshake. We call
                // ``connect()`` directly; the ws-client guards against
                // double-connect when one is already in flight.
                try {
                    this._wsClient.connect();
                } catch {
                    // connect() is no-op on the happy path; the catch
                    // is here only so a host-side throw never crashes
                    // the message-pump.
                }
                break;

            case "microStepToggled": {
                // P0 §3.6: forward the webview's micro-step toggle to
                // the daemon via the existing WS client. The daemon
                // mutates the active plan and rebroadcasts
                // INTERVENTION_TRIGGER so peer surfaces sync.
                if (!this._currentPayload) break;
                const interventionId = this._currentPayload.intervention_id as string;
                if (!interventionId) break;
                const stepIndex = Number(message.step_index);
                const rawStatus = String(message.new_status || "");
                if (!Number.isFinite(stepIndex) || stepIndex < 0) break;
                const newStatus: StepStatus =
                    rawStatus === "done" || rawStatus === "skipped" || rawStatus === "pending"
                        ? rawStatus
                        : "pending";
                this._wsClient.sendMicroStepToggled(
                    interventionId,
                    stepIndex,
                    newStatus,
                );
                break;
            }
        }
    }

    private _updatePanel(): void {
        if (!this._view) {
            return;
        }

        this._view.webview.html = this._getWebviewContent();
    }

    /**
     * P2-6: return the correct empty-state HTML snippet for the three
     * distinct panel states:
     *
     *   (a) Daemon offline — client is not connected. Shows a
     *       "Daemon offline" message with a Reconnect button.
     *   (b) Connected, awaiting state — client is connected but
     *       _currentState has not yet been populated by a STATE_UPDATE.
     *       Shows a subtle spinner so the user knows data is on the way.
     *   (c) Active — client is connected and state is live. Shows the
     *       standard "No active intervention" message.
     */
    private _getEmptyStateHtml(): string {
        if (!this._wsClient.isConnected) {
            // (a) Daemon offline.
            return '<div class="daemon-offline" data-testid="cx-state-offline">'
                + 'Cortex daemon offline. '
                + '<button id="reconnect-btn" type="button" class="reconnect-btn">Reconnect</button>'
                + '</div>';
        }
        const hasState = Object.keys(this._currentState).length > 0;
        if (!hasState) {
            // (b) Connected, awaiting first STATE_UPDATE.
            return '<div class="cx-awaiting-state" data-testid="cx-state-awaiting">'
                + '<span class="cx-spinner" aria-hidden="true"></span>'
                + ' Connecting…'
                + '</div>';
        }
        // (c) Active — connected and state received, no intervention right now.
        return '<div class="no-intervention" data-testid="cx-state-active">'
            + 'No active intervention'
            + '</div>';
    }

    /**
     * Push the current state into the existing webview without rebuilding
     * the DOM. The webview script (see _getWebviewContent) listens for
     * messages with type === 'state' and updates the state label, dot, and
     * confidence in place.
     *
     * D.1 fix: keeps the breathing-pacer animation running across the
     * 500ms STATE_UPDATE stream that previously reset it every tick.
     */
    private _postStateToWebview(): void {
        if (!this._view) {
            return;
        }
        const state = this._currentState;
        const ready = state.status === "estimated";
        const stateStr = ready ? ((state.state as string) ?? "UNKNOWN") : "UNKNOWN";
        const label = state.status === "warming_up"
            ? "Still gathering"
            : state.status === "insufficient_evidence"
            ? "Not enough evidence"
            : PANEL_STATE_LABELS[stateStr] ?? "Status unavailable";
        const confidence = state.confidence as number | undefined;
        this._view.webview.postMessage({
            type: "state",
            state: stateStr,
            label,
            status: state.status,
            confidence: confidence ?? 0,
        });
    }

    /**
     * A2: JSON that is safe to embed inside a ``<script>`` body.
     * ``JSON.stringify`` does not escape ``</script>``; a daemon (or a
     * poisoned LLM output) could otherwise close the nonce script and
     * open a new one — the CSP would block it, but the panel would be
     * blank. ``<``, ``>``, ``&`` and the JS line terminators U+2028/2029
     * are emitted as ``\uXXXX`` escapes, which JSON/JS decode back to
     * the original characters.
     */
    static safeJsonForScript(value: unknown): string {
        return JSON.stringify(value ?? null)
            .replace(/</g, "\\u003c")
            .replace(/>/g, "\\u003e")
            .replace(/&/g, "\\u0026")
            .replace(/\u2028/g, "\\u2028")
            .replace(/\u2029/g, "\\u2029");
    }

    /**
     * Generate the full HTML content for the webview panel.
     *
     * Includes intervention content (if active), breathing pacer,
     * and current state display.
     */
    private _getWebviewContent(): string {
        const nonce = randomBytes(16).toString("base64");
        const state = this._currentState;
        const payload = this._currentPayload;

        const ready = state.status === "estimated";
        const stateStr = ready ? ((state.state as string) ?? "UNKNOWN") : "UNKNOWN";
        const stateLabel = state.status === "warming_up"
            ? "Still gathering"
            : state.status === "insufficient_evidence"
            ? "Not enough evidence"
            : PANEL_STATE_LABELS[stateStr] ?? "Status unavailable";
        const confidence = state.confidence as number | undefined;
        const confPct =
            confidence !== undefined ? Math.round(confidence * 100) : 0;
        const stateAttr = this._escapeHtml(String(stateStr));

        // Build intervention section
        let interventionHtml = "";
        let initialSignalsJson = "[]";
        if (payload) {
            const headline = this._escapeHtml(
                (payload.headline as string) ?? "Take a moment",
            );
            const summary = this._escapeHtml(
                (payload.situation_summary as string) ?? "",
            );
            const focus = this._escapeHtml(
                (payload.primary_focus as string) ?? "",
            );
            const steps = CortexPanelProvider._normalizeSteps(payload.micro_steps);

            let stepsHtml = "";
            for (let i = 0; i < steps.length; i++) {
                const step = this._escapeHtml(steps[i].text);
                const isDone = steps[i].status === "done";
                stepsHtml += `
                    <label class="step">
                        <input type="checkbox" id="step-${i}" data-step-index="${i}"${isDone ? " checked" : ""} />
                        <span class="step-text${isDone ? " is-done" : ""}" data-step-index="${i}">${step}</span>
                    </label>`;
            }

            const causalExplanation = this._escapeHtml(
                (payload.causal_explanation as string) ?? "",
            );

            // P0 §3.9: serialise structured causal signals (initial
            // payload) so the panel can render the drilldown rows on
            // first paint without waiting for the on-demand WHY_DETAIL.
            const causalSignalsRaw = Array.isArray(payload.causal_signals)
                ? (payload.causal_signals as Record<string, unknown>[])
                : [];
            initialSignalsJson = CortexPanelProvider.safeJsonForScript(causalSignalsRaw);

            interventionHtml = `
                <div class="intervention">
                    <h2 class="headline">${headline}</h2>
                    <p class="summary">${summary}</p>
                    <!-- UX: one "Why this?" affordance. Expands the free-text
                         rationale (when the trigger carried one) and the
                         structured signal rows; issues a WHY_DETAIL_REQUEST
                         on first open when no signals were attached. -->
                    <div class="why-block">
                        <button id="why-toggle" type="button" class="why-toggle" aria-expanded="false" aria-controls="why-panel">Why this?</button>
                        <div id="why-panel" class="why-panel" hidden>
                            ${causalExplanation ? `<p class="why-explanation">${causalExplanation}</p>` : ""}
                            <div id="why-signals" class="why-signals" aria-live="polite"></div>
                        </div>
                    </div>
                    <div class="focus">
                        <strong>Focus:</strong> ${focus}
                    </div>
                    <div class="steps" id="steps">
                        ${stepsHtml}
                    </div>
                    <div class="pacer" id="pacer" role="group" aria-label="Breathing guide">
                        <canvas id="pacer-canvas" width="140" height="140" aria-hidden="true"></canvas>
                        <div id="pacer-label">Inhale</div>
                        <div id="pacer-timer">4s</div>
                    </div>
                    <!-- P0 §3.8: 👍 / 👎 row + optional text input. -->
                    <div class="rating-row">
                        <button id="thumbs-up" type="button" class="rating-btn" aria-label="Mark helpful">👍</button>
                        <button id="thumbs-down" type="button" class="rating-btn" aria-label="Mark unhelpful">👎</button>
                    </div>
                    <input id="rating-text" type="text" maxlength="200" placeholder="What would have helped? (Enter to send, Esc to skip)" aria-label="What would have helped?" class="rating-text" hidden />
                    <button class="dismiss-btn" id="dismiss-btn" type="button">Dismiss</button>
                </div>`;
        }

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>
        /* A11: every colour is a VS Code theme variable so the panel
         * reads correctly on light, dark and high-contrast themes. The
         * Cortex brand accent (terracotta) is the only fixed colour and
         * is used exclusively on the intervention card's left border and
         * the breathing-pacer rings — never for text. */
        :root {
            --cx-bg: var(--vscode-editor-background, #1e1e1e);
            --cx-card: var(--vscode-editorWidget-background,
                        var(--vscode-sideBar-background, #252526));
            --cx-text: var(--vscode-editor-foreground, #e6e6e6);
            --cx-text-secondary: var(--vscode-descriptionForeground, #9d9d9d);
            --cx-text-tertiary: var(--vscode-disabledForeground, #8b8b8b);
            --cx-separator: var(--vscode-editorWidget-border,
                              var(--vscode-widget-border, #3c3c3c));
            --cx-focus-ring: var(--vscode-focusBorder, #007fd4);
            --cx-link: var(--vscode-textLink-foreground, #3794ff);
            --cx-warn: var(--vscode-editorWarning-foreground, #cca700);
            --cx-button-bg: var(--vscode-button-background, #0e639c);
            --cx-button-fg: var(--vscode-button-foreground, #f4f4f4);
            --cx-button-hover: var(--vscode-button-hoverBackground, #1177bb);
            --cx-button2-bg: var(--vscode-button-secondaryBackground, #3a3d41);
            --cx-button2-fg: var(--vscode-button-secondaryForeground, #f4f4f4);
            --cx-button2-hover: var(--vscode-button-secondaryHoverBackground, #45494e);
            --cx-input-bg: var(--vscode-input-background, #3c3c3c);
            --cx-input-fg: var(--vscode-input-foreground, #cccccc);
            --cx-input-border: var(--vscode-input-border, var(--cx-separator));
            --cx-accent: #D97757;          /* brand — border + pacer only */
            --cx-accent-strong: #E08E6F;
            /* 5-step modular scale matching cortex/libs/design/tokens.yaml */
            --fs-caption: 11px;
            --fs-footnote: 13px;
            --fs-body: 15px;
            --fs-title: 22px;
        }

        [hidden] { display: none !important; }

        body {
            margin: 0;
            padding: 12px;
            font-family: var(--vscode-font-family,
                -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif);
            font-size: var(--fs-footnote);
            color: var(--cx-text);
            background: var(--cx-bg);
        }

        .state-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            background: var(--cx-card);
            border: 1px solid var(--cx-separator);
            border-radius: 8px;
            margin-bottom: 12px;
        }

        /* Theme-aware state dots. UNKNOWN is an outlined ring so it stays
           visible on both light and dark backgrounds. */
        .state-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            box-sizing: border-box;
            background: var(--cx-text-tertiary);
        }
        .state-dot[data-state="UNKNOWN"] {
            background: transparent;
            border: 2px solid var(--cx-text-tertiary);
        }
        .state-dot[data-state="FLOW"] { background: var(--cx-accent); }
        .state-dot[data-state="HYPER"] { background: var(--cx-warn); }
        .state-dot[data-state="HYPO"] { background: var(--cx-text-secondary); }
        .state-dot[data-state="RECOVERY"] { background: var(--cx-link); }

        .state-label {
            font-weight: 600;
            flex: 1;
        }

        .state-conf {
            color: var(--cx-text-secondary);
            font-size: var(--fs-caption);
        }

        .intervention {
            background: var(--cx-card);
            border-radius: 10px;
            padding: 16px;
            border: 1px solid var(--cx-separator);
            border-left: 3px solid var(--cx-accent);
        }

        .headline {
            font-size: var(--fs-body);
            font-weight: 600;
            margin: 0 0 8px;
            color: var(--cx-text);
        }

        .summary {
            color: var(--cx-text-secondary);
            margin: 0 0 12px;
            line-height: 1.45;
        }

        .focus {
            color: var(--cx-text);
            margin-bottom: 12px;
            font-size: var(--fs-footnote);
            font-weight: 600;
        }

        .focus strong {
            font-weight: 500;
            color: var(--cx-text-secondary);
        }

        .steps {
            margin-bottom: 16px;
        }

        .step {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            padding: 6px 0;
            cursor: pointer;
        }

        .step input[type="checkbox"] {
            margin-top: 2px;
            accent-color: var(--cx-button-bg);
        }

        .step-text {
            line-height: 1.45;
        }

        .step-text.is-done {
            text-decoration: line-through;
            opacity: 0.7;
        }

        .pacer {
            text-align: center;
            margin: 16px 0;
        }

        #pacer-canvas {
            display: block;
            margin: 0 auto 8px;
        }

        #pacer-label {
            font-size: var(--fs-body);
            font-weight: 600;
            color: var(--cx-text);
        }

        #pacer-timer {
            font-size: var(--fs-caption);
            color: var(--cx-text-secondary);
        }

        .dismiss-btn {
            display: block;
            width: 100%;
            padding: 8px;
            border: 1px solid var(--cx-separator);
            border-radius: 6px;
            background: var(--cx-button2-bg);
            color: var(--cx-button2-fg);
            cursor: pointer;
            font-size: var(--fs-footnote);
            font-weight: 500;
            transition: background-color 120ms cubic-bezier(.23, 1, .32, 1),
                border-color 120ms cubic-bezier(.23, 1, .32, 1),
                transform 120ms cubic-bezier(.23, 1, .32, 1);
        }

        button:focus-visible,
        input:focus-visible {
            outline: 2px solid var(--cx-focus-ring);
            outline-offset: 1px;
        }

        .no-intervention {
            text-align: center;
            padding: 24px 12px;
            color: var(--cx-text-secondary);
        }

        /* Explicit empty state: daemon offline is distinct from an
           evidence-aware UNKNOWN/quiet support state. */
        .daemon-offline {
            text-align: center;
            padding: 20px 12px;
            color: var(--cx-text-secondary);
            font-size: var(--fs-footnote);
        }

        .reconnect-btn {
            display: inline-block;
            margin-left: 6px;
            padding: 5px 14px;
            border-radius: 5px;
            background: var(--cx-button-bg);
            color: var(--cx-button-fg);
            border: none;
            cursor: pointer;
            font-size: var(--fs-footnote);
            font-weight: 600;
            transition: background-color 120ms cubic-bezier(.23, 1, .32, 1),
                transform 120ms cubic-bezier(.23, 1, .32, 1);
        }

        /* P2-6: "Connected, awaiting state" empty state */
        .cx-awaiting-state {
            text-align: center;
            padding: 20px 12px;
            color: var(--cx-text-secondary);
            font-size: var(--fs-footnote);
        }

        /* Subtle CSS-only pulsing dot spinner — no external assets. */
        .cx-spinner {
            display: inline-block;
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: var(--cx-text-tertiary);
            animation: cx-pulse 1.2s cubic-bezier(.77, 0, .175, 1) infinite;
            vertical-align: middle;
            margin-right: 4px;
        }

        @keyframes cx-pulse {
            0%, 100% { opacity: 0.3; transform: scale(0.85); }
            50%       { opacity: 1.0; transform: scale(1.15); }
        }

        /* Morning briefing card (rendered on demand via postMessage). */
        .briefing {
            background: var(--cx-card);
            border: 1px solid var(--cx-separator);
            border-radius: 8px;
            padding: 12px 14px;
            margin-bottom: 12px;
        }
        .briefing-title { font-weight: 600; margin-bottom: 4px; }
        .briefing ol { margin: 6px 0 0; padding-left: 20px; }
        .briefing p { margin: 4px 0; color: var(--cx-text-secondary); }

        /* P0 §3.8: rating row */
        .rating-row {
            display: flex;
            gap: 8px;
            justify-content: center;
            margin: 8px 0;
        }

        .rating-btn {
            background: var(--cx-button2-bg);
            color: var(--cx-button2-fg);
            border: 1px solid var(--cx-separator);
            border-radius: 6px;
            padding: 4px 12px;
            font-size: 14px;
            cursor: pointer;
            transition: background-color 120ms cubic-bezier(.23, 1, .32, 1),
                border-color 120ms cubic-bezier(.23, 1, .32, 1),
                transform 120ms cubic-bezier(.23, 1, .32, 1);
        }

        .rating-btn.selected {
            background: var(--cx-button-bg);
            color: var(--cx-button-fg);
            border-color: transparent;
        }

        .rating-text {
            width: 100%;
            margin-top: 6px;
            padding: 6px 10px;
            background: var(--cx-input-bg);
            color: var(--cx-input-fg);
            border: 1px solid var(--cx-input-border);
            border-radius: 5px;
            box-sizing: border-box;
            font-size: var(--fs-footnote);
        }

        /* P0 §3.9: Why this? drilldown */
        .why-block { margin: 6px 0; }

        .why-toggle {
            background: none;
            color: var(--cx-link);
            border: none;
            padding: 2px 0;
            font-size: var(--fs-caption);
            text-decoration: underline;
            cursor: pointer;
            transition: color 120ms cubic-bezier(.23, 1, .32, 1),
                transform 120ms cubic-bezier(.23, 1, .32, 1);
        }

        .why-panel {
            margin-top: 4px;
            padding: 6px 10px;
            border-radius: 6px;
            background: var(--cx-card);
            border: 1px solid var(--cx-separator);
        }

        .why-explanation {
            margin: 0 0 6px;
            font-size: var(--fs-caption);
            line-height: 1.5;
            color: var(--cx-text-secondary);
        }

        .why-signals {
            font-size: var(--fs-caption);
            color: var(--cx-text-secondary);
        }

        .why-retry {
            margin-left: 8px;
            padding: 2px 8px;
            border-radius: 4px;
            border: 1px solid var(--cx-separator);
            background: var(--cx-button2-bg);
            color: var(--cx-button2-fg);
            font-size: var(--fs-caption);
            cursor: pointer;
        }

        .why-row {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 3px 0;
            font-size: var(--fs-caption);
        }

        .why-name { font-weight: 600; min-width: 96px; color: var(--cx-text); }
        .why-value { flex: 1; color: var(--cx-text-secondary); }
        .why-spark { width: 60px; height: 24px; }
        .why-delta-down { color: var(--cx-warn); font-weight: 600; }
        .why-delta-up { color: var(--cx-link); font-weight: 600; }

        .dismiss-btn:active,
        .reconnect-btn:active,
        .rating-btn:active,
        .why-toggle:active { transform: scale(.97); }

        @media (hover: hover) and (pointer: fine) {
            .dismiss-btn:hover { background: var(--cx-button2-hover); }
            .reconnect-btn:hover { background: var(--cx-button-hover); }
            .rating-btn:hover { background: var(--cx-button2-hover); }
            .rating-btn.selected:hover { background: var(--cx-button-hover); }
            .why-toggle:hover { color: var(--cx-text); }
        }

        @media (prefers-reduced-motion: reduce) {
            .cx-spinner {
                animation: none;
                opacity: .75;
                transform: none;
                transition: opacity 160ms cubic-bezier(.23, 1, .32, 1),
                    background-color 160ms cubic-bezier(.23, 1, .32, 1);
            }
            .dismiss-btn,
            .reconnect-btn,
            .rating-btn,
            .why-toggle { transition-property: background-color, border-color, color; }
            .dismiss-btn:active,
            .reconnect-btn:active,
            .rating-btn:active,
            .why-toggle:active { transform: none; }
        }

        body[data-reduce-motion="true"] .cx-spinner {
            animation: none;
            opacity: .75;
            transform: none;
        }
    </style>
</head>
<body data-reduce-motion="${this._reduceMotion ? "true" : "false"}">
    <div class="state-bar">
        <div class="state-dot" id="cx-state-dot" data-state="${stateAttr}"></div>
        <span class="state-label" id="cx-state-label">${stateLabel}</span>
        <span class="state-conf" id="cx-state-conf">${ready ? `Evidence ${confPct}%` : "No estimate"}</span>
    </div>

    <div id="cx-briefing" class="briefing" hidden></div>

    ${interventionHtml || this._getEmptyStateHtml()}

    <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();
        // A2: escaped server-side (see safeJsonForScript) so a closing script tag
        // inside a signal name cannot terminate this script element.
        const INITIAL_CAUSAL_SIGNALS = ${initialSignalsJson};
        // A12: host-side workbench.reduceMotion mirror; updated by
        // {type:'motion'} messages.
        let hostReduceMotion = document.body.dataset.reduceMotion === 'true';
        let syncPacerFn = null;

        // ---- Morning briefing (on demand) ----
        function renderBriefing(p) {
            const el = document.getElementById('cx-briefing');
            if (!el || !p || typeof p !== 'object') return;
            el.innerHTML = '';
            const title = document.createElement('div');
            title.className = 'briefing-title';
            title.textContent = 'Morning briefing';
            el.appendChild(title);
            if (p.summary) {
                const s = document.createElement('p');
                s.textContent = String(p.summary);
                el.appendChild(s);
            }
            const items = Array.isArray(p.action_items) ? p.action_items : [];
            if (items.length > 0) {
                const ol = document.createElement('ol');
                for (const item of items) {
                    const li = document.createElement('li');
                    li.textContent = String(item);
                    ol.appendChild(li);
                }
                el.appendChild(ol);
            }
            if (p.left_off_at) {
                const l = document.createElement('p');
                l.textContent = 'You left off at ' + String(p.left_off_at);
                el.appendChild(l);
            }
            el.hidden = false;
        }

        // ---- Why this? drilldown (P0 §3.9 / A7) ----
        const whyToggle = document.getElementById('why-toggle');
        const whyPanel = document.getElementById('why-panel');
        const whySignals = document.getElementById('why-signals');
        let whyOpen = false;
        let whyLoaded = false;
        let whyLoading = false;
        const WHY_ERROR_COPY = {
            timeout: 'The explanation took too long to arrive.',
            handler_not_registered: 'Explanations are not available from this Cortex build.',
            request_failed: 'Could not load the explanation.'
        };

        function setWhyStatus(text) {
            if (!whySignals) return;
            whySignals.innerHTML = '';
            whySignals.textContent = text;
        }

        function openWhy() {
            whyOpen = true;
            if (whyPanel) whyPanel.hidden = false;
            if (whyToggle) {
                whyToggle.textContent = 'Hide why';
                whyToggle.setAttribute('aria-expanded', 'true');
            }
        }

        function closeWhy() {
            whyOpen = false;
            if (whyPanel) whyPanel.hidden = true;
            if (whyToggle) {
                whyToggle.textContent = 'Why this?';
                whyToggle.setAttribute('aria-expanded', 'false');
            }
        }

        function requestWhyDetail() {
            if (whyLoaded || whyLoading) return;
            whyLoading = true;
            setWhyStatus('Gathering signals…');
            vscode.postMessage({ command: 'whyDetailRequest' });
        }

        function showWhyError(code) {
            if (!whySignals) return;
            whySignals.innerHTML = '';
            const text = document.createElement('span');
            text.textContent = WHY_ERROR_COPY[code] || WHY_ERROR_COPY.request_failed;
            whySignals.appendChild(text);
            if (code !== 'handler_not_registered') {
                const retry = document.createElement('button');
                retry.type = 'button';
                retry.className = 'why-retry';
                retry.textContent = 'Retry';
                retry.addEventListener('click', requestWhyDetail);
                whySignals.appendChild(retry);
            }
        }

        function renderCausalSignals(signals) {
            if (!whySignals) return;
            whySignals.innerHTML = '';
            if (!Array.isArray(signals) || signals.length === 0) {
                whySignals.textContent = 'No structured signals available.';
                return;
            }
            for (const sig of signals) {
                if (!sig || typeof sig !== 'object') continue;
                const row = document.createElement('div');
                row.className = 'why-row';
                const nameEl = document.createElement('span');
                nameEl.className = 'why-name';
                nameEl.textContent = String(sig.name || '');
                row.appendChild(nameEl);
                const valEl = document.createElement('span');
                valEl.className = 'why-value';
                const unit = String(sig.unit || '');
                let vtext = (Number(sig.current_value) || 0).toFixed(1) + unit;
                if (sig.baseline_value != null && !Number.isNaN(Number(sig.baseline_value))) {
                    vtext += ' (baseline ' + Number(sig.baseline_value).toFixed(1) + unit + ')';
                }
                valEl.textContent = vtext;
                row.appendChild(valEl);
                // sparkline — terracotta is a graphic accent here, not text
                const canvas = document.createElement('canvas');
                canvas.className = 'why-spark';
                canvas.width = 60;
                canvas.height = 24;
                const samples = Array.isArray(sig.samples_60s) ? sig.samples_60s : [];
                const cx2 = canvas.getContext('2d');
                if (cx2 && samples.length > 1) {
                    cx2.strokeStyle = 'rgba(217, 119, 87, 0.86)';
                    cx2.lineWidth = 1;
                    let lo = Infinity, hi = -Infinity;
                    for (const v of samples) { if (v < lo) lo = v; if (v > hi) hi = v; }
                    if (hi <= lo) { hi = lo + 1; }
                    const w = canvas.width - 2;
                    const h = canvas.height - 4;
                    const step = w / (samples.length - 1);
                    cx2.beginPath();
                    for (let i = 0; i < samples.length; i++) {
                        const x = 1 + i * step;
                        const y = canvas.height - 2 - ((samples[i] - lo) / (hi - lo)) * h;
                        if (i === 0) cx2.moveTo(x, y); else cx2.lineTo(x, y);
                    }
                    cx2.stroke();
                }
                row.appendChild(canvas);
                if (sig.delta_pct != null && !Number.isNaN(Number(sig.delta_pct))) {
                    const delta = Number(sig.delta_pct);
                    const pill = document.createElement('span');
                    pill.className = delta < 0 ? 'why-delta-down' : 'why-delta-up';
                    const arrow = delta < 0 ? '↓' : '↑';
                    pill.textContent = arrow + Math.abs(delta).toFixed(0) + '%';
                    row.appendChild(pill);
                }
                whySignals.appendChild(row);
            }
        }

        function applyWhyDetail(m) {
            if (m.status === 'loading') {
                whyLoading = true;
                setWhyStatus('Gathering signals…');
                return;
            }
            whyLoading = false;
            if (m.status === 'error' || m.error) {
                whyLoaded = false;
                showWhyError(String(m.error || 'request_failed'));
                openWhy();
                return;
            }
            const sigs = (m.payload && m.payload.causal_signals) || [];
            renderCausalSignals(sigs);
            whyLoaded = true;
            openWhy();
        }

        // Initial signals shipped with the trigger payload (if any).
        if (Array.isArray(INITIAL_CAUSAL_SIGNALS) && INITIAL_CAUSAL_SIGNALS.length > 0) {
            renderCausalSignals(INITIAL_CAUSAL_SIGNALS);
            whyLoaded = true;
        }

        if (whyToggle) {
            whyToggle.addEventListener('click', () => {
                if (whyOpen) {
                    closeWhy();
                    return;
                }
                openWhy();
                requestWhyDetail();
            });
        }

        // ---- Rating (P0 §3.8) ----
        // Exactly one USER_RATING per 👎 selection: the click only opens
        // the comment field; Enter/Esc (or Dismiss) sends the rating once,
        // with or without the comment.
        const thumbsUpBtn = document.getElementById('thumbs-up');
        const thumbsDownBtn = document.getElementById('thumbs-down');
        const ratingTextEl = document.getElementById('rating-text');
        let downPending = false;

        function sendRating(rating, context) {
            const msg = { command: 'userRating', rating: rating };
            const text = (context || '').trim();
            if (text) msg.context = text.slice(0, 200);
            vscode.postMessage(msg);
        }

        function hideRatingText() {
            if (!ratingTextEl) return;
            ratingTextEl.value = '';
            ratingTextEl.hidden = true;
        }

        function finishThumbsDown(context) {
            if (!downPending) return;
            downPending = false;
            const text = context !== undefined
                ? context
                : (ratingTextEl ? ratingTextEl.value : '');
            hideRatingText();
            sendRating('thumbs_down', text);
        }

        if (thumbsUpBtn) {
            thumbsUpBtn.addEventListener('click', () => {
                if (thumbsUpBtn.classList.contains('selected')) return;
                thumbsUpBtn.classList.add('selected');
                if (thumbsDownBtn) thumbsDownBtn.classList.remove('selected');
                downPending = false;
                hideRatingText();
                sendRating('thumbs_up');
            });
        }
        if (thumbsDownBtn) {
            thumbsDownBtn.addEventListener('click', () => {
                if (thumbsDownBtn.classList.contains('selected')) return;
                thumbsDownBtn.classList.add('selected');
                if (thumbsUpBtn) thumbsUpBtn.classList.remove('selected');
                downPending = true;
                if (ratingTextEl) {
                    ratingTextEl.hidden = false;
                    ratingTextEl.focus();
                } else {
                    finishThumbsDown('');
                }
            });
        }
        if (ratingTextEl) {
            ratingTextEl.addEventListener('keydown', (ev) => {
                if (ev.key === 'Enter') {
                    ev.preventDefault();
                    finishThumbsDown();
                } else if (ev.key === 'Escape') {
                    finishThumbsDown('');
                }
            });
        }

        // ---- Dismiss ----
        const dismissBtn = document.getElementById('dismiss-btn');
        if (dismissBtn) {
            dismissBtn.addEventListener('click', () => {
                finishThumbsDown();
                vscode.postMessage({ command: 'dismiss' });
            });
        }

        // P1 (audit Phase 4d, Task B): reconnect button on the
        // daemon-offline empty state. Asks the host to call
        // 'CortexWSClient.connect()' via a dedicated command rather
        // than via the activity-bar palette command, so the panel can
        // recover without leaving the webview.
        const reconnectBtn = document.getElementById('reconnect-btn');
        if (reconnectBtn) {
            reconnectBtn.addEventListener('click', () => {
                vscode.postMessage({ command: 'reconnect' });
            });
        }

        // ---- Micro-steps (P0 §3.6 / A6) ----
        function applyMicroSteps(steps) {
            if (!Array.isArray(steps)) return;
            steps.forEach((s, i) => {
                const cb = document.getElementById('step-' + i);
                const span = document.querySelector('.step-text[data-step-index="' + i + '"]');
                const done = Boolean(s && s.status === 'done');
                if (cb) cb.checked = done;
                if (span) {
                    span.classList.toggle('is-done', done);
                    if (s && typeof s.text === 'string' && s.text) span.textContent = s.text;
                }
            });
        }

        document.querySelectorAll('.step input').forEach(cb => {
            cb.addEventListener('change', (ev) => {
                const target = ev.target;
                const index = parseInt(target.getAttribute('data-step-index') || '-1', 10);
                if (!Number.isFinite(index) || index < 0) return;
                const newStatus = target.checked ? 'done' : 'pending';
                // Optimistic local strikethrough — daemon will reconcile
                // via a same-id INTERVENTION_TRIGGER that the host turns
                // into a {type:'microSteps'} patch.
                const span = target.parentElement && target.parentElement.querySelector('.step-text');
                if (span) span.classList.toggle('is-done', newStatus === 'done');
                vscode.postMessage({
                    command: 'microStepToggled',
                    step_index: index,
                    new_status: newStatus,
                });
            });
        });

        // ---- Host → webview messages ----
        // D.1: receive STATE_UPDATE diffs from the host and patch the DOM
        // in place. The host posts {type:'state',state,label,confidence}
        // every ~500ms; full HTML rebuild only happens on intervention
        // show/clear so the breathing pacer keeps its animation state.
        window.addEventListener('message', (event) => {
            const msg = event.data || {};
            switch (msg.type) {
                case 'state': {
                    const label = document.getElementById('cx-state-label');
                    const dot = document.getElementById('cx-state-dot');
                    const conf = document.getElementById('cx-state-conf');
                    if (label) label.textContent = msg.label || msg.state;
                    if (dot) dot.dataset.state = String(msg.state || 'UNKNOWN');
                    if (conf) {
                        const pct = Math.round((Number(msg.confidence) || 0) * 100);
                        conf.textContent = msg.status === 'estimated'
                            ? 'Evidence ' + pct + '%'
                            : 'No estimate';
                    }
                    break;
                }
                case 'motion':
                    hostReduceMotion = Boolean(msg.reduce);
                    document.body.dataset.reduceMotion = hostReduceMotion ? 'true' : 'false';
                    if (syncPacerFn) syncPacerFn();
                    break;
                case 'microSteps':
                    applyMicroSteps(msg.steps);
                    break;
                case 'whyDetail':
                    applyWhyDetail(msg);
                    break;
                case 'morningBriefing':
                    renderBriefing(msg.payload);
                    break;
                default:
                    break;
            }
        });

        // ---- 4-7-8 Breathing Pacer ----
        const canvas = document.getElementById('pacer-canvas');
        const labelEl = document.getElementById('pacer-label');
        const timerEl = document.getElementById('pacer-timer');

        if (canvas && labelEl && timerEl) {
            const ctx = canvas.getContext('2d');
            const INHALE = 4, HOLD = 7, EXHALE = 8;
            const CYCLE = INHALE + HOLD + EXHALE;
            let startTime = performance.now();
            let pacerFrameId = null;
            const reducedPacerMotion = window.matchMedia(
                '(prefers-reduced-motion: reduce)'
            );

            function drawPacerDisc(scale) {
                const w = canvas.width, h = canvas.height;
                const cx = w / 2, cy = h / 2;
                const maxR = Math.min(w, h) / 2 - 10;
                const r = maxR * scale;

                ctx.clearRect(0, 0, w, h);

                // Brand accent (terracotta) — RGB matches CX.accent so the
                // pacer reads as Cortex on any VS Code theme.
                for (let i = 0; i < 3; i++) {
                    const ri = r - i * 3;
                    if (ri < 5) break;
                    const alpha = 0.5 - i * 0.12;
                    ctx.beginPath();
                    ctx.arc(cx, cy, ri, 0, Math.PI * 2);
                    ctx.fillStyle = 'rgba(217, 119, 87, ' + alpha + ')';
                    ctx.fill();
                }
            }

            function renderStaticPacer() {
                drawPacerDisc(0.46);
                labelEl.textContent = 'Breathe at your pace';
                timerEl.textContent = '';
            }

            function motionReduced() {
                return reducedPacerMotion.matches || hostReduceMotion;
            }

            function shouldRunPacer() {
                return !motionReduced()
                    && document.visibilityState === 'visible';
            }

            function stopPacer() {
                if (pacerFrameId !== null) {
                    cancelAnimationFrame(pacerFrameId);
                    pacerFrameId = null;
                }
            }

            function drawPacer() {
                pacerFrameId = null;
                if (!shouldRunPacer()) return;
                const elapsed = (performance.now() - startTime) / 1000;
                const cyclePos = elapsed % CYCLE;

                let phase, remaining, scale;
                if (cyclePos < INHALE) {
                    phase = 'Inhale';
                    remaining = INHALE - cyclePos;
                    scale = 0.3 + 0.7 * (cyclePos / INHALE);
                } else if (cyclePos < INHALE + HOLD) {
                    phase = 'Hold';
                    remaining = INHALE + HOLD - cyclePos;
                    scale = 1.0;
                } else {
                    phase = 'Exhale';
                    const exhalePos = cyclePos - INHALE - HOLD;
                    remaining = EXHALE - exhalePos;
                    scale = 1.0 - 0.7 * (exhalePos / EXHALE);
                }

                drawPacerDisc(scale);

                labelEl.textContent = phase;
                timerEl.textContent = Math.ceil(remaining) + 's';

                pacerFrameId = requestAnimationFrame(drawPacer);
            }

            function startPacer() {
                if (pacerFrameId !== null || !shouldRunPacer()) return;
                startTime = performance.now();
                pacerFrameId = requestAnimationFrame(drawPacer);
            }

            function syncPacer() {
                stopPacer();
                if (motionReduced()) {
                    renderStaticPacer();
                } else if (document.visibilityState === 'visible') {
                    startPacer();
                }
            }

            syncPacerFn = syncPacer;
            reducedPacerMotion.addEventListener('change', syncPacer);
            document.addEventListener('visibilitychange', syncPacer);
            syncPacer();
        }
    </script>
</body>
</html>`;
    }

    private _escapeHtml(text: string): string {
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }
}
