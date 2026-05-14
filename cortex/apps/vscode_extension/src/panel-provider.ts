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
     * Clear the current intervention display.
     */
    clearIntervention(): void {
        this._currentPayload = null;
        this._updatePanel();
    }

    // --- Internal ---

    private _handleWebviewMessage(message: Record<string, string>): void {
        switch (message.command) {
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
            // Guard against malformed payloads (non-array, mixed types):
            // an unchecked cast would let a stray number/object reach _escapeHtml
            // and stringify into the rendered HTML.
            const steps = Array.isArray(payload.micro_steps)
                ? payload.micro_steps.filter((s): s is string => typeof s === "string")
                : [];

            let stepsHtml = "";
            for (let i = 0; i < steps.length; i++) {
                const step = this._escapeHtml(steps[i]);
                stepsHtml += `
                    <label class="step">
                        <input type="checkbox" id="step-${i}" />
                        <span>${step}</span>
                    </label>`;
            }

            const causalExplanation = this._escapeHtml(
                (payload.causal_explanation as string) ?? "",
            );

            interventionHtml = `
                <div class="intervention">
                    <h2 class="headline">${headline}</h2>
                    <p class="summary">${summary}</p>
                    <div class="causal" style="font-size:11px;color:#71717a;margin-top:6px;cursor:pointer;" onclick="this.querySelector('.causal-body').style.display = this.querySelector('.causal-body').style.display === 'none' ? 'block' : 'none'">
                        <span style="font-weight:500;">Why this?</span> ›
                        <div class="causal-body" style="display:none;margin-top:4px;line-height:1.5;">${causalExplanation}</div>
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
                    <button class="dismiss-btn" id="dismiss-btn">Dismiss</button>
                </div>`;
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
    </style>
</head>
<body>
    <div class="state-bar">
        <div class="state-dot" id="cx-state-dot" style="background: ${stateColor};"></div>
        <span class="state-label" id="cx-state-label">${stateStr}</span>
        <span class="state-conf" id="cx-state-conf">${confPct}%</span>
    </div>

    ${interventionHtml || '<div class="no-intervention">No active intervention</div>'}

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

        // Checkbox engagement tracking
        document.querySelectorAll('.step input').forEach(cb => {
            cb.addEventListener('change', () => {
                vscode.postMessage({ command: 'engage' });
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
