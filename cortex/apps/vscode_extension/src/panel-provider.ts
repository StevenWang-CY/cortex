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

        // Listen for state updates to show in panel
        wsClient.onStateUpdate((payload) => {
            this._currentState = payload;
            this._updatePanel();
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

        // State color
        const stateColors: Record<string, string> = {
            FLOW: "#4CAF50",
            HYPER: "#F44336",
            HYPO: "#6495ED",
            RECOVERY: "#FFC107",
        };
        const stateColor = stateColors[stateStr] ?? "#888";

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
            const microSteps = (payload.micro_steps as string[]) ?? [];

            let stepsHtml = "";
            for (let i = 0; i < microSteps.length; i++) {
                const step = this._escapeHtml(microSteps[i]);
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
        :root {
            --bg: #0e1525;
            --card: #1a2540;
            --accent: #64a0ff;
            --text: #e6f0ff;
            --text-secondary: #a0b4d2;
            --dismiss-bg: rgba(255, 255, 255, 0.08);
        }

        body {
            margin: 0;
            padding: 12px;
            font-family: var(--vscode-font-family, 'Segoe UI', sans-serif);
            font-size: 13px;
            color: var(--text);
            background: var(--bg);
        }

        .state-bar {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            background: var(--card);
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
            color: var(--text-secondary);
            font-size: 12px;
        }

        .intervention {
            background: var(--card);
            border-radius: 12px;
            padding: 16px;
            border-left: 3px solid var(--accent);
        }

        .headline {
            font-size: 16px;
            font-weight: 700;
            margin: 0 0 8px;
            color: var(--text);
        }

        .summary {
            color: var(--text-secondary);
            margin: 0 0 12px;
            line-height: 1.4;
        }

        .focus {
            color: var(--accent);
            margin-bottom: 12px;
            font-size: 13px;
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
            accent-color: var(--accent);
        }

        .step span {
            line-height: 1.4;
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
            font-size: 14px;
            font-weight: 600;
            color: var(--text);
        }

        #pacer-timer {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .dismiss-btn {
            display: block;
            width: 100%;
            padding: 8px;
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 6px;
            background: var(--dismiss-bg);
            color: var(--text);
            cursor: pointer;
            font-size: 13px;
        }

        .dismiss-btn:hover {
            background: rgba(255, 255, 255, 0.15);
        }

        .no-intervention {
            text-align: center;
            padding: 24px 12px;
            color: var(--text-secondary);
        }
    </style>
</head>
<body>
    <div class="state-bar">
        <div class="state-dot" style="background: ${stateColor};"></div>
        <span class="state-label">${stateStr}</span>
        <span class="state-conf">${confPct}%</span>
    </div>

    ${interventionHtml || '<div class="no-intervention">No active intervention</div>'}

    <script>
        const vscode = acquireVsCodeApi();

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

                // Draw circles (gradient effect)
                for (let i = 0; i < 3; i++) {
                    const ri = r - i * 3;
                    if (ri < 5) break;
                    const alpha = 0.5 - i * 0.12;
                    ctx.beginPath();
                    ctx.arc(cx, cy, ri, 0, Math.PI * 2);
                    ctx.fillStyle = 'rgba(100, 160, 255, ' + alpha + ')';
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
