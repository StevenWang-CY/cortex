/**
 * Cortex VS Code Extension — Entry Point
 *
 * Activates on startup, connects to the Cortex daemon via WebSocket,
 * registers all commands, and wires up context provider, fold controller,
 * and intervention panel.
 */

import * as vscode from "vscode";
import { randomUUID } from "crypto";
import { CortexWSClient } from "./ws-client";
import { ContextProvider } from "./context-provider";
import { FoldController } from "./fold-controller";
import { CortexPanelProvider } from "./panel-provider";
import { PANEL_STATE_LABELS } from "./design-tokens";
import { EditorTransactionAdapter } from "./editor-transaction-adapter";

const DEFAULT_DAEMON_URL = "ws://127.0.0.1:9473";

let wsClient: CortexWSClient | undefined;
let contextProvider: ContextProvider | undefined;
let foldController: FoldController | undefined;
let editorTransactionAdapter: EditorTransactionAdapter | undefined;
let panelProvider: CortexPanelProvider | undefined;
let statusBarItem: vscode.StatusBarItem | undefined;
/** Last connection state applied to the status bar (survives re-creation). */
let statusBarConnected = false;
type ExecutionMode = "suggest_only" | "authorized" | "research_autonomous";
let currentExecutionMode: ExecutionMode = "suggest_only";
let editorTransactionChain: Promise<void> = Promise.resolve();

// A6: toast de-dup keyed by intervention_id. The daemon rebroadcasts the
// same INTERVENTION_TRIGGER after every MICRO_STEP_TOGGLED; without this
// each checkbox click re-raised the "Cortex: <headline>" toast.
let lastOverlayToastInterventionId: string | null = null;
let lastOsNotifInterventionId: string | null = null;

function enqueueEditorTransaction(operation: () => Promise<void>): void {
    // Node's EventEmitter does not await async message listeners. Serialize
    // exact apply/restore operations so restore cannot race ahead of the
    // durable editor operation it is meant to reverse.
    editorTransactionChain = editorTransactionChain
        .then(operation)
        .catch((error: unknown) => {
            _logDebug(`transaction command failed closed: ${String(error)}`);
        });
}

function handleLegacyBreakRecommendation(payload: unknown): void {
    // Only the explicit elapsed-focus reminder is product-eligible. Legacy
    // HRV/stress payloads remain a silent decode sink.
    if (typeof payload !== "object" || payload === null) return;
    const p = payload as Record<string, unknown>;
    if (p.basis !== "elapsed_focus") return;
    const reason = typeof p.reason === "string"
        ? p.reason
        : "You've reached your preferred focus interval.";
    void Promise.resolve(vscode.window.showInformationMessage(
        `Cortex · ${reason}`,
        "Open Cortex",
    )).then((choice) => {
        if (choice === "Open Cortex") panelProvider?.showPanel();
    }).catch(() => undefined);
}

/**
 * A13: MORNING_BRIEFING → one notification.
 *
 * ``showInformationMessage`` only honours ``detail`` for modal dialogs,
 * so the action items are inlined into the message itself. Every field
 * is validated: ``action_items`` that is not an array (the previous code
 * threw on ``.map``) degrades to the "left off at" hint.
 */
function handleMorningBriefing(payload: unknown): void {
    const p = typeof payload === "object" && payload !== null
        ? (payload as Record<string, unknown>)
        : {};
    const summary = typeof p.summary === "string" && p.summary.trim().length > 0
        ? p.summary.trim()
        : "Welcome back!";
    const items = Array.isArray(p.action_items)
        ? p.action_items
            .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
            .map((item) => item.trim())
        : [];
    const leftOff = typeof p.left_off_at === "string" ? p.left_off_at.trim() : "";

    let message = `Morning briefing: ${summary}`;
    if (items.length > 0) {
        const shown = items
            .slice(0, 3)
            .map((item, i) => `${i + 1}. ${item}`)
            .join("  ");
        const more = items.length > 3 ? ` (+${items.length - 3} more)` : "";
        message += ` — ${shown}${more}`;
    } else if (leftOff.length > 0) {
        message += ` — You left off at ${leftOff}`;
    }

    void Promise.resolve(vscode.window.showInformationMessage(
        message,
        "Show Details",
    )).then((choice) => {
        if (choice === "Show Details" && panelProvider) {
            panelProvider.showMorningBriefing(p);
        }
    }).catch(() => undefined);
}

function parseExecutionMode(value: unknown): ExecutionMode {
    return value === "authorized" || value === "research_autonomous"
        ? value
        : "suggest_only";
}

function workspaceMutationAllowed(): boolean {
    return currentExecutionMode !== "suggest_only";
}
// Phase-3 P1-N4 / Audit-1.2 F10: cached pulse timeout cleared on
// deactivate so the closure doesn't outlive the disposed status bar.
let osNotifPulseTimeout: ReturnType<typeof setTimeout> | undefined;

// F9 (Phase-4 audit): single OutputChannel shared by every debug-gated
// diagnostic line in the extension. Replaces console.log so messages
// flow into VS Code's Output panel under "Cortex" rather than the
// developer-tools console where the user can't see them. Only created
// once at activation; cleaned up at deactivate.
let outputChannel: vscode.OutputChannel | undefined;

function _logDebug(message: string): void {
    // Gated by ``cortex.debug`` exactly like the prior console.log
    // surface — production installs stay quiet.
    const debug = vscode.workspace
        .getConfiguration("cortex")
        .get<boolean>("debug", false);
    if (!debug) return;
    if (!outputChannel) {
        outputChannel = vscode.window.createOutputChannel("Cortex");
    }
    outputChannel.appendLine(`[debug] ${message}`);
}

/**
 * Paint the status bar item for a connection state.
 *
 * A14: clicking the item reveals the panel while connected (it used to
 * be a no-op ``cortex.connect``) and connects while disconnected.
 * ``initial`` renders the pre-first-attempt look without the warning
 * background.
 */
function applyStatusBarConnection(connected: boolean, initial = false): void {
    statusBarConnected = connected;
    if (!statusBarItem) return;
    if (connected) {
        statusBarItem.text = "$(pulse) Cortex";
        statusBarItem.tooltip = "Cortex — Connected";
        statusBarItem.backgroundColor = undefined;
        statusBarItem.command = "cortex.showPanel";
        return;
    }
    statusBarItem.text = initial ? "$(pulse) Cortex" : "$(debug-disconnect) Cortex";
    statusBarItem.tooltip = "Cortex — Disconnected";
    statusBarItem.backgroundColor = initial
        ? undefined
        : new vscode.ThemeColor("statusBarItem.warningBackground");
    statusBarItem.command = "cortex.connect";
}

/**
 * A14: create or dispose the status bar item to match
 * ``cortex.showStatusBar`` (applied live, no reload required).
 */
function ensureStatusBar(context: vscode.ExtensionContext): void {
    const show = vscode.workspace
        .getConfiguration("cortex")
        .get<boolean>("showStatusBar", true);
    if (show && !statusBarItem) {
        statusBarItem = vscode.window.createStatusBarItem(
            vscode.StatusBarAlignment.Right,
            100,
        );
        applyStatusBarConnection(statusBarConnected, !statusBarConnected);
        statusBarItem.show();
        context.subscriptions.push(statusBarItem);
    } else if (!show && statusBarItem) {
        statusBarItem.dispose();
        statusBarItem = undefined;
    }
}

/**
 * Extension activation — called once on startup.
 */
export async function activate(context: vscode.ExtensionContext): Promise<void> {
    editorTransactionChain = Promise.resolve();
    lastOverlayToastInterventionId = null;
    lastOsNotifInterventionId = null;
    statusBarConnected = false;
    const config = vscode.workspace.getConfiguration("cortex");
    const daemonUrl = config.get<string>("daemonUrl", DEFAULT_DAEMON_URL);

    // --- Status bar ---
    ensureStatusBar(context);

    // --- Services ---
    const services = {
        contextProvider: new ContextProvider(),
        foldController: new FoldController(),
    };
    contextProvider = services.contextProvider;
    foldController = services.foldController;
    // A10: the document-change subscription is released with the extension.
    context.subscriptions.push(services.contextProvider);

    // --- WebSocket client ---
    const instanceKey = "cortex.clientInstanceId.v1";
    const storedInstanceId = context.globalState.get<string>(instanceKey);
    const clientInstanceId = (
        typeof storedInstanceId === "string"
        && /^[A-Za-z0-9._:-]{8,128}$/.test(storedInstanceId)
    )
        ? storedInstanceId
        : `vscode_${randomUUID()}`;
    if (clientInstanceId !== storedInstanceId) {
        // Exact restore routing depends on this surviving extension-host
        // restarts, so complete the durable write before opening the socket.
        await context.globalState.update(instanceKey, clientInstanceId);
    }
    const client = new CortexWSClient(daemonUrl, clientInstanceId);
    wsClient = client;
    editorTransactionAdapter = new EditorTransactionAdapter(
        context.globalState,
        client.clientBootId,
        (batch) => wsClient?.sendInterventionReceipt(batch),
        workspaceMutationAllowed,
        clientInstanceId,
    );

    client.onStateUpdate((payload) => {
        updateStatusBar(payload);
    });

    client.onInterventionTrigger((payload) => {
        handleIntervention(payload);
    });

    client.onConnectionChange((connected) => {
        if (connected) {
            void editorTransactionAdapter?.flushPendingReceipts();
        }
        applyStatusBarConnection(connected);
    });

    client.onRestore((payload) => {
        // Legacy restore frames close presentation only. Exact commands are
        // validated against the durable editor journal and emit typed
        // receipts; neither path calls unfoldAll or steals editor focus.
        enqueueEditorTransaction(async () => {
            await editorTransactionAdapter?.handleRestore(payload);
            // A14: only clear the panel if the restore targets the
            // intervention currently on screen.
            const restoreId = typeof payload.intervention_id === "string"
                ? payload.intervention_id
                : undefined;
            panelProvider?.clearIntervention(restoreId);
        });
    });

    client.onMessage((msg) => {
        if (msg.type === "INTERVENTION_APPLY") {
            enqueueEditorTransaction(async () => {
                await editorTransactionAdapter?.handleApply(msg.payload);
            });
        } else if (msg.type === "INTERVENTION_TRANSACTION_STATE") {
            void editorTransactionAdapter?.acknowledgeTransactionState(
                msg.payload,
            );
        }
    });

    client.onSettingsSync((payload) => {
        currentExecutionMode = parseExecutionMode(payload.execution_mode);
        if (!statusBarItem) return;
        // A14: the quiet-mode tooltip must clear again on quiet_mode:false.
        if (payload.quiet_mode === true) {
            statusBarItem.tooltip = "Cortex — Quiet mode enabled";
        } else if (payload.quiet_mode === false) {
            statusBarItem.tooltip = statusBarConnected
                ? "Cortex — Connected"
                : "Cortex — Disconnected";
        }
    });

    // --- P0 §3.11: QUIET_MODE_STATE — surface mode in status bar ---
    client.onMessage((msg) => {
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
        const endsAt = typeof payload.ends_at_unix_ms === "number"
            ? payload.ends_at_unix_ms / 1000
            : payload.ends_at as number | undefined;
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
    client.onMessage((msg) => {
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
        const interventionId = String(payload.intervention_id || '');
        // A6: de-dup by intervention_id (the daemon echoes the same
        // trigger after every micro-step toggle). Payloads without an id
        // fall back to the headline/10s rule.
        const now = Date.now();
        if (interventionId && interventionId === lastOsNotifInterventionId) {
            return;
        }
        if (
            !interventionId
            && headline === lastOsNotifShownHeadline
            && now - lastOsNotifShownAt < 10_000
        ) {
            return;
        }
        lastOsNotifInterventionId = interventionId || null;
        lastOsNotifShownHeadline = headline;
        lastOsNotifShownAt = now;
        void Promise.resolve(vscode.window.showInformationMessage(
            `Cortex · ${headline}`,
            'Open Dashboard',
            'Snooze',
        )).then((choice) => {
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
        }).catch(() => undefined);
    });

    // --- P0 §3.9: WHY_DETAIL response → forward to panel ---
    client.onMessage((msg) => {
        if (msg.type === 'WHY_DETAIL') {
            const payload = msg.payload as Record<string, unknown> | undefined;
            if (panelProvider && payload) {
                panelProvider.applyWhyDetail(payload);
            }
        }
    });

    // Compatibility-only: reject unsupported HRV/stress-derived claims.
    client.onMessage((msg) => {
        if (msg.type === 'BREAK_RECOMMENDATION') {
            handleLegacyBreakRecommendation(msg.payload);
        }
    });

    // --- v2.0: MORNING_BRIEFING via generic handler ---
    client.onMessage((msg) => {
        if (msg.type === 'MORNING_BRIEFING') {
            handleMorningBriefing(msg.payload);
        }
    });

    // Legacy EXECUTE_ACTION is never editor authority. The exact equivalent
    // arrives only as a manifest-bound INTERVENTION_APPLY above.
    client.onMessage((msg) => {
        if (msg.type !== 'EXECUTE_ACTION') return;
        _logDebug("Rejected legacy EXECUTE_ACTION without exact authorization");
    });

    // B1 (audit-prod): explicit COPILOT_THROTTLE registration.
    // The dedicated dispatch arm in ws-client guarantees the throttle
    // command runs even if generic-handler registration order ever
    // changes; the prior implementation depended on onMessage being
    // bound before the first daemon-pushed throttle frame.
    client.onCopilotThrottle((_payload) => {
        _logDebug("Rejected COPILOT_THROTTLE without exact authorization");
    });

    // --- Panel provider ---
    const panel = new CortexPanelProvider(context.extensionUri, client);
    panelProvider = panel;
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider(
            "cortex.interventionPanel",
            panel,
        ),
        // A10: releases the view/config subscriptions the panel owns.
        panel,
    );

    // --- Settings applied live (A14) ---
    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration((event) => {
            if (event.affectsConfiguration("cortex.showStatusBar")) {
                ensureStatusBar(context);
            }
            if (event.affectsConfiguration("cortex.daemonUrl")) {
                const url = vscode.workspace
                    .getConfiguration("cortex")
                    .get<string>("daemonUrl", DEFAULT_DAEMON_URL);
                wsClient?.setUrl(url);
            }
        }),
    );

    // --- Register commands ---
    context.subscriptions.push(
        vscode.commands.registerCommand("cortex.getActiveFile", () => {
            return contextProvider?.getActiveFile() ?? null;
        }),

        vscode.commands.registerCommand("cortex.getDiagnostics", () => {
            return contextProvider?.getDiagnostics() ?? [];
        }),

        vscode.commands.registerCommand("cortex.getSymbolAtCursor", () => {
            return contextProvider?.getSymbolAtCursor() ?? Promise.resolve(null);
        }),

        vscode.commands.registerCommand(
            "cortex.foldExcept",
            (startLine: unknown, endLine: unknown) => {
                // A9: FoldController validates the range and refuses
                // palette invocations (no arguments) without touching folds.
                return foldController?.foldExcept(
                    startLine as number,
                    endLine as number,
                ) ?? Promise.resolve(false);
            },
        ),

        vscode.commands.registerCommand("cortex.unfoldAll", () => {
            return foldController?.unfoldAll() ?? Promise.resolve(false);
        }),

        vscode.commands.registerCommand("cortex.restoreFoldState", () => {
            return foldController?.restoreFoldState() ?? Promise.resolve(false);
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

    // --- Auto-connect ---
    if (config.get<boolean>("autoConnect", true)) {
        client.connect();
    }

    // --- Handle CONTEXT_REQUEST from daemon ---
    client.onContextRequest(async () => {
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
    // A10: release the subscriptions the providers own. They are also in
    // context.subscriptions; both paths are idempotent.
    contextProvider?.dispose();
    contextProvider = undefined;
    foldController = undefined;
    editorTransactionAdapter = undefined;
    panelProvider?.dispose();
    panelProvider = undefined;
    statusBarItem = undefined;
    // F9: dispose the shared OutputChannel.
    outputChannel?.dispose();
    outputChannel = undefined;
}

// --- Internal helpers ---

/**
 * Update the status bar with current state info.
 */
function updateStatusBar(payload: Record<string, unknown>): void {
    if (!statusBarItem) {
        return;
    }

    const estimateReady = payload.status === "estimated";
    const state = estimateReady
        ? payload.state as string | undefined
        : "UNKNOWN";
    const confidence = payload.confidence as number | undefined;
    const coverage = payload.evidence_coverage as number | undefined;

    const stateIcons: Record<string, string> = {
        FLOW: "$(check)",
        HYPER: "$(flame)",
        HYPO: "$(eye-closed)",
        RECOVERY: "$(sync)",
        UNKNOWN: "$(ellipsis)",
    };

    const icon = stateIcons[state ?? ""] ?? "$(pulse)";
    const confPct = confidence !== undefined ? Math.round(confidence * 100) : 0;

    const label = payload.status === "warming_up"
        ? "Still gathering"
        : payload.status === "insufficient_evidence"
        ? "Not enough evidence"
        : PANEL_STATE_LABELS[state ?? ""] ?? "Status unavailable";
    statusBarItem.text = estimateReady
        ? `${icon} Cortex: ${label} ${confPct}%`
        : `${icon} Cortex: ${label}`;
    const coveragePct = typeof coverage === "number"
        ? Math.round(coverage * 100)
        : 0;
    statusBarItem.tooltip = estimateReady
        ? `Cortex — ${label} (${confPct}% evidence strength; ${coveragePct}% coverage)`
        : `Cortex — ${label}. No actionable estimate.`;

    // Colour coding. VS Code only honours warning/error backgrounds on
    // status bar items; HYPER ("support may help") is the one state that
    // warrants the warning tint. HYPO ("quiet activity") is informational
    // and is distinguished by its icon only — it must not look like an
    // alert (UX polish).
    if (state === "HYPER") {
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
    // A trigger is a proposal, never an apply command. Missing/unknown
    // execution modes fail closed and no editor-folding API is reachable
    // from this handler, including for legacy payloads that request it.
    currentExecutionMode = parseExecutionMode(payload.execution_mode);

    // Show panel with intervention content. A6: the provider patches a
    // same-id rebroadcast in place instead of rebuilding the webview.
    panelProvider?.showIntervention(payload);

    // Show notification for overlay_only
    const level = payload.level as string | undefined;
    const headline = payload.headline as string | undefined;
    const interventionId = typeof payload.intervention_id === "string"
        ? payload.intervention_id
        : "";
    if (level === "overlay_only" && headline) {
        // A6: one toast per intervention id.
        if (interventionId && interventionId === lastOverlayToastInterventionId) {
            return;
        }
        lastOverlayToastInterventionId = interventionId || null;
        void Promise.resolve(vscode.window.showInformationMessage(
            `Cortex: ${headline}`,
            "View Details",
            "Dismiss",
        )).then((action) => {
            if (action === "View Details") {
                panelProvider?.showPanel();
            } else if (action === "Dismiss") {
                wsClient?.sendUserAction(
                    "dismissed",
                    interventionId,
                );
            }
        }).catch(() => undefined);
    }
}

/** Test seam proving that proposal handling cannot reach the fold adapter. */
export function _setFoldControllerForTest(
    controller: Pick<FoldController, "foldExcept"> | undefined,
): void {
    foldController = controller as FoldController | undefined;
}

/** Test seam for the non-mutating INTERVENTION_TRIGGER handler. */
export function _handleInterventionForTest(
    payload: Record<string, unknown>,
): void {
    handleIntervention(payload);
}

/** Test seam for the compatibility sink for unvalidated physiology claims. */
export function _handleLegacyBreakRecommendationForTest(payload: unknown): void {
    handleLegacyBreakRecommendation(payload);
}

/** Test seam for the MORNING_BRIEFING notification (A13). */
export function _handleMorningBriefingForTest(payload: unknown): void {
    handleMorningBriefing(payload);
}

/** Test seam: forget the last toasted intervention ids (A6 de-dup). */
export function _resetToastDedupForTest(): void {
    lastOverlayToastInterventionId = null;
    lastOsNotifInterventionId = null;
}
