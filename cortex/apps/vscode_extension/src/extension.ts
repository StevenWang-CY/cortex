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
// Phase-3 P1-N4 / Audit-1.2 F10: cached pulse timeout cleared on
// deactivate so the closure doesn't outlive the disposed status bar.
let osNotifPulseTimeout: ReturnType<typeof setTimeout> | undefined;

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
                wsClient?.sendInterventionApplied(
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

    // --- P0 §3.11: QUIET_MODE_STATE — surface mode in status bar ---
    wsClient.onMessage((msg) => {
        if (msg.type !== 'QUIET_MODE_STATE') return;
        const payload = msg.payload as Record<string, unknown> | undefined;
        if (!statusBarItem || !payload) return;
        const kind = (payload.kind as string | undefined) || "off";
        const labels: Record<string, string> = {
            off: "Cortex",
            snooze_15: "Cortex · Snoozed",
            quiet_session: "Cortex · Quiet",
            pause: "Cortex · Paused",
        };
        const label = labels[kind] || "Cortex";
        statusBarItem.text = kind === "off"
            ? "$(pulse) Cortex"
            : `$(circle-slash) ${label}`;
        const endsAt = payload.ends_at as number | undefined;
        if (kind !== "off" && typeof endsAt === "number") {
            const remainingMin = Math.max(
                0,
                Math.round((endsAt * 1000 - Date.now()) / 60000),
            );
            statusBarItem.tooltip = remainingMin > 0
                ? `${label} for ${remainingMin} more min`
                : label;
        } else if (kind === "off") {
            statusBarItem.tooltip = "Cortex — Active";
        }
    });

    // --- P0 §3.12: pulse status bar when desktop not focused ---
    // Phase-3 P1-N4 / Audit-1.2 F10: cache the pulse timeout +
    // dispose it on deactivate, and de-dup ``showInformationMessage``
    // so a burst of interventions within 10s doesn't stack toasts
    // the user has to dismiss one-by-one.
    osNotifPulseTimeout = undefined;
    let lastOsNotifShownHeadline = "";
    let lastOsNotifShownAt = 0;
    wsClient.onMessage((msg) => {
        if (msg.type !== 'INTERVENTION_TRIGGER') return;
        const payload = msg.payload as Record<string, unknown> | undefined;
        if (!payload || payload.desktop_not_focused !== true) return;
        if (!statusBarItem) return;
        const headline = String(payload.headline || 'Cortex');
        statusBarItem.text = `$(pulse) Cortex — ${headline}`.slice(0, 64);
        statusBarItem.backgroundColor = new vscode.ThemeColor(
            "statusBarItem.warningBackground",
        );
        if (osNotifPulseTimeout) clearTimeout(osNotifPulseTimeout);
        osNotifPulseTimeout = setTimeout(() => {
            if (statusBarItem) {
                statusBarItem.text = '$(pulse) Cortex';
                statusBarItem.backgroundColor = undefined;
            }
            osNotifPulseTimeout = undefined;
        }, 5000);
        // De-dup the toast: same headline within 10s collapses to the
        // original popup (the user hasn't dismissed it yet).
        const now = Date.now();
        if (
            headline === lastOsNotifShownHeadline
            && now - lastOsNotifShownAt < 10_000
        ) {
            return;
        }
        lastOsNotifShownHeadline = headline;
        lastOsNotifShownAt = now;
        const interventionId = String(payload.intervention_id || '');
        vscode.window.showInformationMessage(
            `Cortex · ${headline}`,
            'Open Dashboard',
            'Snooze',
        ).then((choice) => {
            if (choice === 'Open Dashboard') {
                panelProvider?.showPanel();
            } else if (choice === 'Snooze') {
                // Phase-3 / Audit-1.2 F11: surface a warning instead
                // of silently dropping when wsClient is undefined.
                if (!wsClient) {
                    void vscode.window.showWarningMessage(
                        "Cortex not connected — open Cortex to snooze.",
                    );
                    return;
                }
                wsClient.sendSnoozeRequest(interventionId, 15);
            }
        });
    });

    // --- P0 §3.9: WHY_DETAIL response → forward to panel ---
    wsClient.onMessage((msg) => {
        if (msg.type === 'WHY_DETAIL') {
            const payload = msg.payload as Record<string, unknown> | undefined;
            if (panelProvider && payload) {
                panelProvider.applyWhyDetail(payload);
            }
        }
    });

    // --- P0 §3.7: BREAK_RECOMMENDATION → status bar pulse + notification ---
    wsClient.onMessage((msg) => {
        if (msg.type === 'BREAK_RECOMMENDATION') {
            const payload = msg.payload as Record<string, unknown> | undefined;
            const reason = (payload?.reason as string | undefined) ?? 'stress_integral_crossed_threshold';
            const urgency = (payload?.urgency as string | undefined) ?? 'medium';
            const durationS = Number(payload?.duration_seconds ?? 240);
            const breathingPattern = (() => {
                const raw = payload?.breathing_pattern;
                return raw === '4-7-8' || raw === 'coherent' || raw === 'box'
                    ? (raw as 'box' | '4-7-8' | 'coherent')
                    : 'box';
            })();
            const mins = Math.max(1, Math.round(durationS / 60));
            try {
                if (statusBarItem) {
                    statusBarItem.text = `$(pulse) Cortex — take ${mins} min`;
                    statusBarItem.tooltip = `Biology break suggested (${urgency} urgency)`;
                    setTimeout(() => {
                        if (statusBarItem) {
                            statusBarItem.text = '$(pulse) Cortex';
                            statusBarItem.tooltip = 'Cortex active';
                        }
                    }, 60 * 1000);
                }
            } catch {/* statusBar may be torn down during reload */ }
            vscode.window.showInformationMessage(
                `Your HRV has been suppressed — take a ${mins}-minute break?`,
                `Take ${mins} min`,
                'Snooze',
            ).then((choice) => {
                if (choice !== `Take ${mins} min`) return;
                // P0 §3.7 audit fix: instead of fabricating a synthetic
                // intervention_id and posting USER_ACTION (which the
                // daemon's helpfulness tracker cannot correlate back to
                // any real intervention), dispatch a proper
                // ``take_biology_break`` ACTION_EXECUTE with the real
                // recommendation's metadata. The daemon routes it
                // through the BiologyBreakController.
                const interventionId = (payload?.intervention_id as string | undefined)
                    || `break_${Date.now()}`;
                wsClient?.sendBiologyBreakRequest(interventionId, {
                    duration_seconds: durationS,
                    breathing_pattern: breathingPattern,
                    audio_cue: true,
                    reason,
                });
            });
            if (panelProvider) {
                panelProvider.applyBreakRecommendation(payload || {});
            }
        }
    });

    // --- v2.0: MORNING_BRIEFING via generic handler ---
    wsClient.onMessage((msg) => {
        if (msg.type === 'MORNING_BRIEFING') {
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
        }
    });

    // --- P0 §3.5: EXECUTE_ACTION (HYPO / RECOVERY catalog) ---
    // The daemon dispatches discrete executable actions to the editor
    // via an ``EXECUTE_ACTION`` message. Today we only need to handle
    // ``resume_last_active_file`` (re-engagement nudge). The browser
    // extension handles the other two (suggest_movement_break and
    // prompt_micro_commit) natively.
    wsClient.onMessage((msg) => {
        if (msg.type !== 'EXECUTE_ACTION') return;
        const payload = msg.payload || {};
        const actionType = (payload.action_type as string | undefined) || '';
        if (actionType !== 'resume_last_active_file') return;

        const target = String(payload.target || '').trim();
        const actionId = (payload.action_id as string | undefined) || '';
        const interventionId = (payload.intervention_id as string | undefined) || '';

        // Parse "file_path:line"; the line is optional and falls back to 1.
        const lastColon = target.lastIndexOf(':');
        let filePath = target;
        let line = 1;
        if (lastColon > 0) {
            const maybeLine = parseInt(target.slice(lastColon + 1), 10);
            if (!Number.isNaN(maybeLine) && maybeLine > 0) {
                filePath = target.slice(0, lastColon);
                line = maybeLine;
            }
        }

        if (!filePath) {
            try {
                wsClient?.sendInterventionApplied(
                    interventionId,
                    'execute_action',
                    false,
                    [],
                    [`resume_last_active_file: empty target`],
                );
            } catch { /* ws may not be ready */ }
            return;
        }

        // Best-effort open with a 0-based selection on the requested line.
        const uri = vscode.Uri.file(filePath);
        const sel = new vscode.Range(line - 1, 0, line - 1, 0);
        vscode.window.showTextDocument(uri, { selection: sel }).then(
            () => {
                try {
                    wsClient?.sendInterventionApplied(
                        interventionId,
                        'execute_action',
                        true,
                        [`resume_last_active_file:${actionId}`],
                        [],
                    );
                } catch { /* ws may not be ready */ }
            },
            (err: unknown) => {
                try {
                    wsClient?.sendInterventionApplied(
                        interventionId,
                        'execute_action',
                        false,
                        [],
                        [`resume_last_active_file: ${String(err)}`],
                    );
                } catch { /* ws may not be ready */ }
            },
        );
    });

    // B1 (audit-prod): explicit COPILOT_THROTTLE registration.
    // The dedicated dispatch arm in ws-client guarantees the throttle
    // command runs even if generic-handler registration order ever
    // changes; the prior implementation depended on onMessage being
    // bound before the first daemon-pushed throttle frame.
    wsClient.onCopilotThrottle((payload) => {
        const action = payload?.action;
        if (action === 'disable') {
            vscode.commands.executeCommand('cortex.disableInlineSuggestions');
            console.log('[cortex] COPILOT_THROTTLE: disabling inline suggestions');
        } else if (action === 'enable') {
            vscode.commands.executeCommand('cortex.enableInlineSuggestions');
            console.log('[cortex] COPILOT_THROTTLE: re-enabling inline suggestions');
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
    // Phase-3 P1-N4 / Audit-1.2 F10: pulse timeout outlives the
    // disposed status bar via the activate-scope closure if not
    // cleared here — the timer fires, the closure tries to mutate a
    // disposed object and VS Code logs an "object has been disposed"
    // warning. Clear it explicitly.
    if (osNotifPulseTimeout) {
        clearTimeout(osNotifPulseTimeout);
        osNotifPulseTimeout = undefined;
    }
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
