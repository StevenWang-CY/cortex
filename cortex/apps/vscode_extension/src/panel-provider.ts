/**
 * Cortex VS Code Extension — Panel Provider
 *
 * Provides a webview-based side panel that displays:
 * - Intervention headline, situation summary, primary focus
 * - Micro-step checklist (1-3 items)
 * - 4-7-8 breathing pacer animation
 * - Dismiss button
 * - Connection status and current state
 */

import * as vscode from "vscode";
import { CortexWSClient } from "./ws-client";
import { PANEL_STATE_HEX_LIGHT } from "./design-tokens";

/**
 * Webview provider for the Cortex intervention side panel.
 *
 * Registered as "cortex.interventionPanel" in the activity bar.
 * Renders LLM-generated intervention content with a calming UI.
 */
export class CortexPanelProvider implements vscode.WebviewViewProvider {
    private _view: vscode.WebviewView | undefined;
    private _extensionUri: vscode.Uri;
    private _wsClient: CortexWSClient;
    private _currentPayload: Record<string, unknown> | null = null;
    private _currentState: Record<string, unknown> = {};

    constructor(extensionUri: vscode.Uri, wsClient: CortexWSClient) {
        this._extensionUri = extensionUri;
        this._wsClient = wsClient;

        // D.1: STATE_UPDATE fires every 500ms. A full HTML rebuild here
        // resets the breathing-pacer canvas's animation start time on
        // every tick, so the pacer never actually animates. Instead push
        // a diff message into the existing webview script, which updates
        // the state label + confidence in place. Full HTML re-render is
        // reserved for showIntervention / clearIntervention where the
        // structural content actually changes.
        wsClient.onStateUpdate((payload) => {
            this._currentState = payload;
            this._postStateToWebview();
        });

        // P1 (audit Phase 4d, Task B): rerender the empty-state region
        // when the connection flips between offline / online so the
        // "Reconnect" button appears or disappears in real time. The
        // full HTML rebuild here is acceptable because it only runs on
        // connection transitions (rare), not on every STATE_UPDATE
        // tick — the breathing-pacer animation is unaffected.
        wsClient.onConnectionChange((_connected) => {
            if (!this._currentPayload) {
                this._updatePanel();
            }
        });
    }

    /**
     * Called by VS Code when the webview view is first shown.
     */
    resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ): void {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri],
        };

        // Handle messages from webview
        webviewView.webview.onDidReceiveMessage((message) => {
            this._handleWebviewMessage(message);
        });

        this._updatePanel();
    }

    /**
     * Show an intervention in the panel.
     */
    showIntervention(payload: Record<string, unknown>): void {
        this._currentPayload = payload;
        this._updatePanel();

        // Ensure panel is visible
        this.showPanel();
    }

    /**
     * Focus/reveal the side panel.
     */
    showPanel(): void {
        if (this._view) {
            this._view.show(true);
        }
    }

    public showMorningBriefing(payload: Record<string, unknown>): void {
        if (this._view) {
            this._view.webview.postMessage({
                type: 'morningBriefing',
                payload,
            });
        }
    }

    /**
     * P0 §3.9: route a WHY_DETAIL response into the webview so the
     * "Why?" drilldown panel can render the structured causal signals.
     */
    public applyWhyDetail(payload: Record<string, unknown>): void {
        if (!this._view) return;
        try {
            this._view.webview.postMessage({
                type: 'whyDetail',
                payload,
            });
        } catch {
            // webview may be tearing down
        }
    }

    /**
     * P0 §3.7: route a BREAK_RECOMMENDATION pulse into the webview so
     * the panel can render a soft pill above the intervention card.
     */
    public applyBreakRecommendation(payload: Record<string, unknown>): void {
        if (!this._view) return;
        try {
            this._view.webview.postMessage({
                type: 'breakRecommendation',
                payload,
            });
        } catch {
            // webview may be tearing down
        }
    }

    /**
     * Clear the current intervention display.
     */
    clearIntervention(): void {
        this._currentPayload = null;
        this._updatePanel();
    }

    // --- Internal ---

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
                // P0 §3.9: forward the "Why?" expansion to the daemon.
                if (!this._currentPayload) break;
                const interventionId = this._currentPayload.intervention_id as string;
                if (!interventionId) break;
                this._wsClient.sendWhyDetailRequest(interventionId);
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
                const newStatus: "pending" | "done" | "skipped" =
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
        const stateStr = (state.state as string) ?? "—";
        const confidence = state.confidence as number | undefined;
        try {
            this._view.webview.postMessage({
                type: "state",
                state: stateStr,
                color: PANEL_STATE_HEX_LIGHT[stateStr] ?? "#888",
                confidence: confidence ?? 0,
            });
        } catch {
            // postMessage can throw briefly during webview teardown
        }
    }

    /**
     * Generate the full HTML content for the webview panel.
     *
     * Includes intervention content (if active), breathing pacer,
     * and current state display.
     */
    private _getWebviewContent(): string {
        const state = this._currentState;
        const payload = this._currentPayload;

        const stateStr = (state.state as string) ?? "—";
        const confidence = state.confidence as number | undefined;
        const confPct =
            confidence !== undefined ? Math.round(confidence * 100) : 0;

        // State color — sourced from emitted design tokens so palette edits in
        // libs/design/tokens.yaml flow through after a sync_design_tokens.py run.
        const stateColor = PANEL_STATE_HEX_LIGHT[stateStr] ?? "#888";

        // Build intervention section
        let interventionHtml = "";
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
            // P0 §3.6: micro_steps may carry either the legacy ``string[]``
            // shape OR the new ``{text, status, …}[]`` shape. Coerce both
            // into a uniform ``{text, status}`` tuple list so the
            // strikethrough styling reflects daemon-authoritative state.
            const stepsRaw = Array.isArray(payload.micro_steps) ? payload.micro_steps : [];
            type StepRow = { text: string; status: "pending" | "done" | "skipped" };
            const steps: StepRow[] = [];
            for (const entry of stepsRaw) {
                if (typeof entry === "string" && entry.length > 0) {
                    steps.push({ text: entry, status: "pending" });
                } else if (entry && typeof entry === "object") {
                    const e = entry as Record<string, unknown>;
                    const text = typeof e.text === "string" ? e.text : "";
                    const rawStatus = typeof e.status === "string" ? e.status : "pending";
                    const status: "pending" | "done" | "skipped" =
                        rawStatus === "done" || rawStatus === "skipped" ? rawStatus : "pending";
                    if (text.length > 0) steps.push({ text, status });
                }
            }

            let stepsHtml = "";
            for (let i = 0; i < steps.length; i++) {
                const step = this._escapeHtml(steps[i].text);
                const isDone = steps[i].status === "done";
                const checkedAttr = isDone ? " checked" : "";
                const struckStyle = isDone
                    ? ' style="text-decoration: line-through; opacity: 0.7;"'
                    : "";
                stepsHtml += `
                    <label class="step">
                        <input type="checkbox" id="step-${i}" data-step-index="${i}"${checkedAttr} />
                        <span${struckStyle}>${step}</span>
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
            const causalSignalsJson = JSON.stringify(causalSignalsRaw);

            interventionHtml = `
                <div class="intervention">
                    <h2 class="headline">${headline}</h2>
                    <p class="summary">${summary}</p>
                    <div class="causal" style="font-size:11px;color:#71717a;margin-top:6px;cursor:pointer;" onclick="this.querySelector('.causal-body').style.display = this.querySelector('.causal-body').style.display === 'none' ? 'block' : 'none'">
                        <span style="font-weight:500;">Why this?</span> ›
                        <div class="causal-body" style="display:none;margin-top:4px;line-height:1.5;">${causalExplanation}</div>
                    </div>
                    <!-- P0 §3.7: BREAK_RECOMMENDATION pill — hidden by
                         default; populated when the daemon emits the
                         pulse. The CTA fires the desktop-side break. -->
                    <div id="break-recommendation" class="break-rec" style="display:none;">
                        <span class="break-rec-text"></span>
                        <button class="break-rec-cta" type="button">Take 4 min</button>
                    </div>
                    <!-- P0 §3.9: structured rationale drilldown. The
                         "Why?" link expands the panel; when no
                         signals were attached the panel issues a
                         WHY_DETAIL_REQUEST to populate them on demand. -->
                    <div class="why-block">
                        <button id="why-toggle" type="button" class="why-toggle">Why?</button>
                        <div id="why-panel" class="why-panel" style="display:none;"></div>
                    </div>
                    <div class="focus">
                        <strong>Focus:</strong> ${focus}
                    </div>
                    <div class="steps">
                        ${stepsHtml}
                    </div>
                    <div class="pacer" id="pacer">
                        <canvas id="pacer-canvas" width="140" height="140"></canvas>
                        <div id="pacer-label">Inhale</div>
                        <div id="pacer-timer">4s</div>
                    </div>
                    <!-- P0 §3.8: 👍 / 👎 row + optional text input. -->
                    <div class="rating-row">
                        <button id="thumbs-up" type="button" class="rating-btn" aria-label="Mark helpful">👍</button>
                        <button id="thumbs-down" type="button" class="rating-btn" aria-label="Mark unhelpful">👎</button>
                    </div>
                    <input id="rating-text" type="text" maxlength="200" placeholder="What would have helped? (Enter to send, Esc to skip)" class="rating-text" style="display:none;" />
                    <button class="dismiss-btn" id="dismiss-btn">Dismiss</button>
                </div>
                <script>
                    window.__CORTEX_INITIAL_CAUSAL_SIGNALS__ = ${causalSignalsJson};
                </script>`;
        }

        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>
        /* VS Code theme variables provide the editor-matched chrome; only
         * the Cortex brand accent (terracotta) is fixed across all themes.
         * The terracotta lives on the intervention card's left border, the
         * focus headline, and the breathing-pacer rings — the same brand
         * mark as the desktop shell + browser extension. */
        :root {
            --cx-bg: var(--vscode-editor-background, #1e1e1e);
            --cx-card: var(--vscode-editorWidget-background,
                        var(--vscode-sideBar-background, #252526));
            --cx-text: var(--vscode-editor-foreground, #e6e6e6);
            --cx-text-secondary: var(--vscode-descriptionForeground,
                                  rgba(204, 204, 204, 0.7));
            --cx-text-tertiary: var(--vscode-disabledForeground,
                                 rgba(204, 204, 204, 0.45));
            --cx-separator: var(--vscode-widget-border, rgba(128, 128, 128, 0.18));
            --cx-focus-ring: var(--vscode-focusBorder, #007fd4);
            --cx-accent: #D97757;          /* brand — preserved */
            --cx-accent-strong: #E08E6F;
            --cx-dismiss-bg: var(--vscode-button-secondaryBackground,
                              rgba(255, 255, 255, 0.06));
            --cx-dismiss-bg-hover: var(--vscode-button-secondaryHoverBackground,
                                    rgba(255, 255, 255, 0.12));
            /* 5-step modular scale matching cortex/libs/design/tokens.yaml */
            --fs-caption: 11px;
            --fs-footnote: 13px;
            --fs-body: 15px;
            --fs-title: 22px;
        }

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
            border: 0.5px solid var(--cx-separator);
            border-radius: 8px;
            margin-bottom: 12px;
        }

        .state-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
        }

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
            border-left: 3px solid var(--cx-accent);
            border-top: 0.5px solid var(--cx-separator);
            border-right: 0.5px solid var(--cx-separator);
            border-bottom: 0.5px solid var(--cx-separator);
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
            color: var(--cx-accent);
            margin-bottom: 12px;
            font-size: var(--fs-footnote);
            font-weight: 600;
        }

        .causal {
            font-size: var(--fs-caption) !important;
            color: var(--cx-text-tertiary) !important;
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
            accent-color: var(--cx-accent);
        }

        .step span {
            line-height: 1.45;
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
            border: 0.5px solid var(--cx-separator);
            border-radius: 6px;
            background: var(--cx-dismiss-bg);
            color: var(--cx-text);
            cursor: pointer;
            font-size: var(--fs-footnote);
            font-weight: 500;
        }

        .dismiss-btn:hover {
            background: var(--cx-dismiss-bg-hover);
        }

        .dismiss-btn:focus-visible {
            outline: 2px solid var(--cx-focus-ring);
            outline-offset: 1px;
        }

        .no-intervention {
            text-align: center;
            padding: 24px 12px;
            color: var(--cx-text-secondary);
        }

        /* P1 (audit Phase 4d, Task B): explicit empty-state for the
           daemon-offline case so the user can tell "no overwhelm yet"
           apart from "client can't reach the daemon". */
        .daemon-offline {
            text-align: center;
            padding: 20px 12px;
            color: var(--cx-text-secondary);
            font-size: var(--fs-caption);
        }

        .daemon-offline button {
            display: inline-block;
            margin-left: 6px;
            padding: 4px 12px;
            border-radius: 5px;
            background: var(--cx-accent);
            color: white;
            border: none;
            cursor: pointer;
            font-size: var(--fs-caption);
            font-weight: 600;
        }

        .daemon-offline button:hover { filter: brightness(1.08); }

        /* P0 §3.7: BREAK_RECOMMENDATION pill */
        .break-rec {
            display: flex;
            align-items: center;
            gap: 8px;
            margin: 8px 0 12px;
            padding: 8px 10px;
            border-radius: 6px;
            background: rgba(217, 119, 87, 0.10);
            border: 1px solid rgba(217, 119, 87, 0.45);
            font-size: var(--fs-caption);
        }

        .break-rec-text { flex: 1; color: var(--cx-text); }

        .break-rec-cta {
            background: var(--cx-accent);
            color: white;
            border: none;
            border-radius: 5px;
            padding: 4px 10px;
            cursor: pointer;
            font-size: var(--fs-caption);
            font-weight: 600;
        }

        /* P0 §3.8: rating row */
        .rating-row {
            display: flex;
            gap: 8px;
            justify-content: center;
            margin: 8px 0;
        }

        .rating-btn {
            background: rgba(255, 255, 255, 0.06);
            color: var(--cx-text);
            border: 0.5px solid var(--cx-separator);
            border-radius: 6px;
            padding: 4px 12px;
            font-size: 14px;
            cursor: pointer;
        }

        .rating-btn:hover { background: rgba(255, 255, 255, 0.12); }
        .rating-btn.selected { background: var(--cx-accent); color: white; }

        .rating-text {
            width: 100%;
            margin-top: 6px;
            padding: 6px 10px;
            background: rgba(255, 255, 255, 0.04);
            color: var(--cx-text);
            border: 1px solid rgba(217, 119, 87, 0.40);
            border-radius: 5px;
            box-sizing: border-box;
            font-size: var(--fs-caption);
        }

        /* P0 §3.9: Why? drilldown */
        .why-block { margin: 6px 0; }

        .why-toggle {
            background: none;
            color: var(--cx-text-secondary);
            border: none;
            padding: 2px 0;
            font-size: var(--fs-caption);
            text-decoration: underline;
            cursor: pointer;
        }

        .why-toggle:hover { color: var(--cx-text); }

        .why-panel {
            margin-top: 4px;
            padding: 6px 10px;
            border-radius: 6px;
            background: rgba(255, 255, 255, 0.03);
        }

        .why-row {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 3px 0;
            font-size: var(--fs-caption);
        }

        .why-name { font-weight: 600; min-width: 96px; }
        .why-value { flex: 1; color: var(--cx-text-secondary); }
        .why-spark { width: 60px; height: 24px; }
        .why-delta-down { color: #E47A6E; font-weight: 600; }
        .why-delta-up { color: var(--cx-accent); font-weight: 600; }
    </style>
</head>
<body>
    <div class="state-bar">
        <div class="state-dot" id="cx-state-dot" style="background: ${stateColor};"></div>
        <span class="state-label" id="cx-state-label">${stateStr}</span>
        <span class="state-conf" id="cx-state-conf">${confPct}%</span>
    </div>

    ${interventionHtml || (this._wsClient.isConnected
        ? '<div class="no-intervention">No active intervention</div>'
        : '<div class="daemon-offline">Cortex daemon offline. <button id="reconnect-btn" type="button">Reconnect</button></div>')}

    <script>
        const vscode = acquireVsCodeApi();

        // D.1: receive STATE_UPDATE diffs from the host and patch the DOM
        // in place. The host posts {type:'state',state,color,confidence}
        // every ~500ms; full HTML rebuild only happens on intervention
        // show/clear so the breathing pacer keeps its animation state.
        window.addEventListener('message', (event) => {
            const msg = event.data || {};
            if (msg.type === 'state') {
                const label = document.getElementById('cx-state-label');
                const dot = document.getElementById('cx-state-dot');
                const conf = document.getElementById('cx-state-conf');
                if (label) label.textContent = msg.state;
                if (dot) dot.style.background = msg.color;
                if (conf) {
                    const pct = Math.round((Number(msg.confidence) || 0) * 100);
                    conf.textContent = pct + '%';
                }
            }
        });

        // Dismiss button
        const dismissBtn = document.getElementById('dismiss-btn');
        if (dismissBtn) {
            dismissBtn.addEventListener('click', () => {
                vscode.postMessage({ command: 'dismiss' });
            });
        }

        // P1 (audit Phase 4d, Task B): reconnect button on the
        // daemon-offline empty state. Asks the host to call
        // ``CortexWSClient.connect()`` via a dedicated command rather
        // than via the activity-bar palette command, so the panel can
        // recover without leaving the webview.
        const reconnectBtn = document.getElementById('reconnect-btn');
        if (reconnectBtn) {
            reconnectBtn.addEventListener('click', () => {
                vscode.postMessage({ command: 'reconnect' });
            });
        }

        // P0 §3.8: rating buttons
        const thumbsUpBtn = document.getElementById('thumbs-up');
        const thumbsDownBtn = document.getElementById('thumbs-down');
        const ratingTextEl = document.getElementById('rating-text');
        if (thumbsUpBtn) {
            thumbsUpBtn.addEventListener('click', () => {
                thumbsUpBtn.classList.add('selected');
                if (thumbsDownBtn) thumbsDownBtn.classList.remove('selected');
                vscode.postMessage({ command: 'userRating', rating: 'thumbs_up' });
            });
        }
        if (thumbsDownBtn) {
            thumbsDownBtn.addEventListener('click', () => {
                thumbsDownBtn.classList.add('selected');
                if (thumbsUpBtn) thumbsUpBtn.classList.remove('selected');
                vscode.postMessage({ command: 'userRating', rating: 'thumbs_down' });
                if (ratingTextEl) {
                    ratingTextEl.style.display = 'block';
                    ratingTextEl.focus();
                }
            });
        }
        if (ratingTextEl) {
            ratingTextEl.addEventListener('keydown', (ev) => {
                if (ev.key === 'Enter') {
                    const text = (ratingTextEl.value || '').trim();
                    if (text) {
                        vscode.postMessage({
                            command: 'userRating',
                            rating: 'thumbs_down',
                            context: text.slice(0, 200),
                        });
                    }
                    ratingTextEl.value = '';
                    ratingTextEl.style.display = 'none';
                } else if (ev.key === 'Escape') {
                    ratingTextEl.value = '';
                    ratingTextEl.style.display = 'none';
                }
            });
        }

        // P0 §3.9: Why? toggle + drilldown render
        const whyToggle = document.getElementById('why-toggle');
        const whyPanel = document.getElementById('why-panel');
        let whyOpen = false;

        function renderCausalSignals(signals) {
            if (!whyPanel) return;
            whyPanel.innerHTML = '';
            if (!Array.isArray(signals) || signals.length === 0) {
                whyPanel.textContent = 'No structured signals available.';
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
                // sparkline
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
                whyPanel.appendChild(row);
            }
        }

        // Initial signals shipped with the trigger payload (if any).
        try {
            renderCausalSignals(window.__CORTEX_INITIAL_CAUSAL_SIGNALS__ || []);
        } catch { /* empty payload */ }

        if (whyToggle) {
            whyToggle.addEventListener('click', () => {
                whyOpen = !whyOpen;
                if (whyPanel) whyPanel.style.display = whyOpen ? 'block' : 'none';
                whyToggle.textContent = whyOpen ? 'Hide why' : 'Why?';
                // If we have no signals cached, ask the daemon for them.
                if (whyOpen && whyPanel && whyPanel.children.length === 0) {
                    vscode.postMessage({ command: 'whyDetailRequest' });
                }
            });
        }

        // P0 §3.7: BREAK_RECOMMENDATION pill listener.
        window.addEventListener('message', (event) => {
            const m = event.data || {};
            if (m.type === 'whyDetail') {
                const sigs = (m.payload && m.payload.causal_signals) || [];
                renderCausalSignals(sigs);
                if (whyPanel) whyPanel.style.display = 'block';
                whyOpen = true;
                if (whyToggle) whyToggle.textContent = 'Hide why';
                return;
            }
            if (m.type === 'breakRecommendation') {
                const p = m.payload || {};
                const durationS = Number(p.duration_seconds || 240);
                const mins = Math.max(1, Math.round(durationS / 60));
                const pill = document.getElementById('break-recommendation');
                if (pill) {
                    pill.style.display = 'flex';
                    const txt = pill.querySelector('.break-rec-text');
                    if (txt) txt.textContent = 'Your HRV has been suppressed — take a ' + mins + '-minute break?';
                    const cta = pill.querySelector('.break-rec-cta');
                    if (cta) {
                        cta.textContent = 'Take ' + mins + ' min';
                        cta.onclick = () => {
                            vscode.postMessage({
                                command: 'userRating',
                                rating: 'thumbs_up',
                            });
                            pill.style.display = 'none';
                        };
                    }
                }
            }
        });

        // P0 §3.6: micro-step checkbox toggle. Each click posts a
        // ``microStepToggled`` message to the extension, which forwards
        // it as MICRO_STEP_TOGGLED via the WS client. The daemon
        // rebroadcasts INTERVENTION_TRIGGER so the strikethrough state
        // converges across every connected surface.
        document.querySelectorAll('.step input').forEach(cb => {
            cb.addEventListener('change', (ev) => {
                const target = ev.target;
                const index = parseInt(target.getAttribute('data-step-index') || '-1', 10);
                if (!Number.isFinite(index) || index < 0) return;
                const newStatus = target.checked ? 'done' : 'pending';
                // Optimistic local strikethrough — daemon will reconcile.
                const span = target.parentElement && target.parentElement.querySelector('span');
                if (span) {
                    if (newStatus === 'done') {
                        span.style.textDecoration = 'line-through';
                        span.style.opacity = '0.7';
                    } else {
                        span.style.textDecoration = 'none';
                        span.style.opacity = '1';
                    }
                }
                vscode.postMessage({
                    command: 'microStepToggled',
                    step_index: index,
                    new_status: newStatus,
                });
            });
        });

        // 4-7-8 Breathing Pacer
        const canvas = document.getElementById('pacer-canvas');
        const labelEl = document.getElementById('pacer-label');
        const timerEl = document.getElementById('pacer-timer');

        if (canvas && labelEl && timerEl) {
            const ctx = canvas.getContext('2d');
            const INHALE = 4, HOLD = 7, EXHALE = 8;
            const CYCLE = INHALE + HOLD + EXHALE;
            let startTime = performance.now();

            function drawPacer() {
                const elapsed = (performance.now() - startTime) / 1000;
                const cyclePos = elapsed % CYCLE;
                const w = canvas.width, h = canvas.height;
                const cx = w / 2, cy = h / 2;
                const maxR = Math.min(w, h) / 2 - 10;

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

                labelEl.textContent = phase;
                timerEl.textContent = Math.ceil(remaining) + 's';

                requestAnimationFrame(drawPacer);
            }

            requestAnimationFrame(drawPacer);
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
