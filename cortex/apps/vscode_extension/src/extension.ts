/**
 * Cortex VS Code Extension — Entry Point
 *
 * Activates on startup, connects to the Cortex daemon via WebSocket,
 * registers all commands, and wires up context provider, fold controller,
 * and intervention panel.
 */

import * as vscode from "vscode";
import { CortexWSClient } from "./ws-client";
import { ContextProvider } from "./context-provider";
import { FoldController } from "./fold-controller";
import { CortexPanelProvider } from "./panel-provider";

let wsClient: CortexWSClient | undefined;
let contextProvider: ContextProvider | undefined;
let foldController: FoldController | undefined;
let panelProvider: CortexPanelProvider | undefined;
let statusBarItem: vscode.StatusBarItem | undefined;

/**
 * Extension activation — called once on startup.
 */
export function activate(context: vscode.ExtensionContext): void {
    const config = vscode.workspace.getConfiguration("cortex");
    const daemonUrl = config.get<string>("daemonUrl", "ws://127.0.0.1:9473");

    // --- Status bar ---
    if (config.get<boolean>("showStatusBar", true)) {
        statusBarItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Right,
            100,
        );
        statusBarItem.text = "$(pulse) Cortex";
        statusBarItem.tooltip = "Cortex — Disconnected";
        statusBarItem.command = "cortex.connect";
        statusBarItem.show();
        context.subscriptions.push(statusBarItem);
    }

    // --- Services ---
    contextProvider = new ContextProvider();
    foldController = new FoldController();

    // --- WebSocket client ---
    wsClient = new CortexWSClient(daemonUrl);

    wsClient.onStateUpdate((payload) => {
        updateStatusBar(payload);
    });

    wsClient.onInterventionTrigger((payload) => {
        handleIntervention(payload);
    });

    wsClient.onConnectionChange((connected) => {
        if (statusBarItem) {
            if (connected) {
                statusBarItem.text = "$(pulse) Cortex";
                statusBarItem.tooltip = "Cortex — Connected";
                statusBarItem.backgroundColor = undefined;
            } else {
                statusBarItem.text = "$(debug-disconnect) Cortex";
                statusBarItem.tooltip = "Cortex — Disconnected";
                statusBarItem.backgroundColor = new vscode.ThemeColor(
                    "statusBarItem.warningBackground",
                );
            }
        }
    });

    wsClient.onRestore((payload) => {
        const applied: string[] = [];
        const errors: string[] = [];
        let restoreOk = true;
        if (foldController?.hasPendingFolds) {
            try {
                void foldController.restoreFoldState();
                applied.push("restoreFoldState");
            } catch (e) {
                restoreOk = false;
                errors.push(`restoreFoldState: ${(e as Error)?.message ?? String(e)}`);
            }
        }
        panelProvider?.showPanel();
        vscode.window.setStatusBarMessage(
            `Cortex restored workspace (${String(payload.user_action ?? "done")})`,
            3000,
        );
        // B.2 (restore-side ack): tell the daemon the unfold completed so
        // InterventionOutcome.workspace_restored reflects reality.
        const interventionId = payload?.intervention_id;
        if (typeof interventionId === "string") {
            try {
                wsClient.sendInterventionApplied(
                    interventionId,
                    "restore",
                    restoreOk,
                    applied,
                    errors,
                );
            } catch {
                // ws may be closing — never crash the handler
            }
        }
    });

    wsClient.onSettingsSync((payload) => {
        const quietMode = Boolean(payload.quiet_mode);
        if (statusBarItem && quietMode) {
            statusBarItem.tooltip = "Cortex — Quiet mode enabled";
        }
    });

    // --- v2.0: Handle MORNING_BRIEFING and COPILOT_THROTTLE messages ---
    wsClient.onMessage((msg) => {
        switch (msg.type) {
            case 'MORNING_BRIEFING': {
                const summary = msg.payload?.summary || 'Welcome back!';
                const items = msg.payload?.action_items || [];
                const leftOff = msg.payload?.left_off_at || '';
                const detail = (items as string[]).length > 0
                    ? (items as string[]).map((item: string, i: number) => `${i + 1}. ${item}`).join('\n')
                    : leftOff as string;
                vscode.window.showInformationMessage(
                    `☀️ ${summary}`,
                    { modal: false, detail },
                    'Show Details',
                ).then(choice => {
                    if (choice === 'Show Details' && panelProvider) {
                        panelProvider.showMorningBriefing(msg.payload);
                    }
                });
                break;
            }
            case 'COPILOT_THROTTLE': {
                const action = msg.payload?.action;
                if (action === 'disable') {
                    vscode.commands.executeCommand('cortex.disableInlineSuggestions');
                } else if (action === 'enable') {
                    vscode.commands.executeCommand('cortex.enableInlineSuggestions');
                }
                break;
            }
        }
    });

    // --- Panel provider ---
    panelProvider = new CortexPanelProvider(context.extensionUri, wsClient);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "cortex.interventionPanel",
            panelProvider,
        ),
    );

    // --- Register commands ---
    context.subscriptions.push(
        vscode.commands.registerCommand("cortex.getActiveFile", () => {
            return contextProvider!.getActiveFile();
        }),

        vscode.commands.registerCommand("cortex.getDiagnostics", () => {
            return contextProvider!.getDiagnostics();
        }),

        vscode.commands.registerCommand("cortex.getSymbolAtCursor", () => {
            return contextProvider!.getSymbolAtCursor();
        }),

        vscode.commands.registerCommand(
            "cortex.foldExcept",
            (startLine: number, endLine: number) => {
                return foldController!.foldExcept(startLine, endLine);
            },
        ),

        vscode.commands.registerCommand("cortex.unfoldAll", () => {
            return foldController!.unfoldAll();
        }),

        vscode.commands.registerCommand("cortex.restoreFoldState", () => {
            return foldController!.restoreFoldState();
        }),

        vscode.commands.registerCommand("cortex.showPanel", () => {
            panelProvider?.showPanel();
        }),

        vscode.commands.registerCommand("cortex.connect", () => {
            wsClient?.connect();
        }),

        vscode.commands.registerCommand("cortex.disconnect", () => {
            wsClient?.disconnect();
        }),
    );

    // v2.0: Copilot throttle commands
    context.subscriptions.push(
        vscode.commands.registerCommand('cortex.disableInlineSuggestions', async () => {
            const config = vscode.workspace.getConfiguration();
            await config.update('editor.inlineSuggest.enabled', false, vscode.ConfigurationTarget.Global);
            // Also try to disable GitHub Copilot if available
            try {
                await config.update('github.copilot.enable', { '*': false }, vscode.ConfigurationTarget.Global);
            } catch {
                // Copilot not installed
            }
        }),
        vscode.commands.registerCommand('cortex.enableInlineSuggestions', async () => {
            const config = vscode.workspace.getConfiguration();
            await config.update('editor.inlineSuggest.enabled', true, vscode.ConfigurationTarget.Global);
            try {
                await config.update('github.copilot.enable', { '*': true }, vscode.ConfigurationTarget.Global);
            } catch {
                // Copilot not installed
            }
        }),
    );

    // --- Auto-connect ---
    if (config.get<boolean>("autoConnect", true)) {
        wsClient.connect();
    }

    // --- Handle CONTEXT_REQUEST from daemon ---
    wsClient.onContextRequest(async () => {
        if (!contextProvider) {
            return {};
        }
        return contextProvider.gatherFullContext();
    });
}

/**
 * Extension deactivation — cleanup.
 */
export function deactivate(): void {
    wsClient?.disconnect();
    wsClient = undefined;
    contextProvider = undefined;
    foldController = undefined;
    panelProvider = undefined;
}

// --- Internal helpers ---

/**
 * Update the status bar with current state info.
 */
function updateStatusBar(payload: Record<string, unknown>): void {
    if (!statusBarItem) {
        return;
    }

    const state = payload.state as string | undefined;
    const confidence = payload.confidence as number | undefined;

    const stateIcons: Record<string, string> = {
        FLOW: "$(check)",
        HYPER: "$(flame)",
        HYPO: "$(eye-closed)",
        RECOVERY: "$(sync)",
    };

    const icon = stateIcons[state ?? ""] ?? "$(pulse)";
    const confPct = confidence !== undefined ? Math.round(confidence * 100) : 0;

    statusBarItem.text = `${icon} Cortex: ${state ?? "—"} ${confPct}%`;
    statusBarItem.tooltip = `Cortex — ${state ?? "Unknown"} (${confPct}% confidence)`;

    // Color coding
    if (state === "HYPER") {
        statusBarItem.backgroundColor = new vscode.ThemeColor(
            "statusBarItem.errorBackground",
        );
    } else if (state === "HYPO") {
        statusBarItem.backgroundColor = new vscode.ThemeColor(
            "statusBarItem.warningBackground",
        );
    } else {
        statusBarItem.backgroundColor = undefined;
    }
}

/**
 * Handle an INTERVENTION_TRIGGER from the daemon.
 */
function handleIntervention(payload: Record<string, unknown>): void {
    const uiPlan = payload.ui_plan as Record<string, unknown> | undefined;

    // D.6: respect the LLM-supplied max_visible_lines constraint instead
    // of hard-coding ±20. The daemon's SimplificationConstraints flow
    // into UIPlan.max_visible_lines (default 40 = ±20 either side of
    // cursor) — see libs/schemas/intervention.py.
    const rawMaxVisible =
        (uiPlan?.max_visible_lines as number | undefined) ??
        ((payload.constraints as Record<string, unknown> | undefined)
            ?.max_visible_lines as number | undefined);
    const halfWindow = Math.max(
        5,
        Math.floor(((typeof rawMaxVisible === "number" ? rawMaxVisible : 40)) / 2),
    );

    // Apply fold if requested
    if (uiPlan?.fold_unrelated_code && foldController) {
        const editor = vscode.window.activeTextEditor;
        if (editor) {
            const cursorLine = editor.selection.active.line;
            foldController.foldExcept(
                Math.max(0, cursorLine - halfWindow),
                cursorLine + halfWindow,
            );
        }
    }

    // Show panel with intervention content
    panelProvider?.showIntervention(payload);

    // D.4 / B.2: ack the apply so the daemon can replace optimistic
    // mutation tracking with real client outcome. We don't have detailed
    // success/failure per action here; report overall success and the
    // current state of the fold controller.
    try {
        wsClient?.sendInterventionApplied(
            payload.intervention_id as string,
            "apply",
            true,
            uiPlan?.fold_unrelated_code ? ["foldExcept"] : [],
            [],
        );
    } catch {
        // wsClient may not be ready in tests — never crash the handler
    }

    // Show notification for overlay_only
    const level = payload.level as string | undefined;
    const headline = payload.headline as string | undefined;
    if (level === "overlay_only" && headline) {
        vscode.window.showInformationMessage(
            `Cortex: ${headline}`,
            "View Details",
            "Dismiss",
        ).then((action) => {
            if (action === "View Details") {
                panelProvider?.showPanel();
            } else if (action === "Dismiss") {
                wsClient?.sendUserAction(
                    "dismissed",
                    payload.intervention_id as string,
                );
            }
        });
    }
}
