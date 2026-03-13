"use strict";
/**
 * Cortex VS Code Extension — Entry Point
 *
 * Activates on startup, connects to the Cortex daemon via WebSocket,
 * registers all commands, and wires up context provider, fold controller,
 * and intervention panel.
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const ws_client_1 = require("./ws-client");
const context_provider_1 = require("./context-provider");
const fold_controller_1 = require("./fold-controller");
const panel_provider_1 = require("./panel-provider");
let wsClient;
let contextProvider;
let foldController;
let panelProvider;
let statusBarItem;
/**
 * Extension activation — called once on startup.
 */
function activate(context) {
    const config = vscode.workspace.getConfiguration("cortex");
    const daemonUrl = config.get("daemonUrl", "ws://localhost:9473");
    // --- Status bar ---
    if (config.get("showStatusBar", true)) {
        statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
        statusBarItem.text = "$(pulse) Cortex";
        statusBarItem.tooltip = "Cortex — Disconnected";
        statusBarItem.command = "cortex.connect";
        statusBarItem.show();
        context.subscriptions.push(statusBarItem);
    }
    // --- Services ---
    contextProvider = new context_provider_1.ContextProvider();
    foldController = new fold_controller_1.FoldController();
    // --- WebSocket client ---
    wsClient = new ws_client_1.CortexWSClient(daemonUrl);
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
            }
            else {
                statusBarItem.text = "$(debug-disconnect) Cortex";
                statusBarItem.tooltip = "Cortex — Disconnected";
                statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
            }
        }
    });
    wsClient.onRestore((payload) => {
        if (foldController?.hasPendingFolds) {
            void foldController.restoreFoldState();
        }
        panelProvider?.showPanel();
        vscode.window.setStatusBarMessage(`Cortex restored workspace (${String(payload.user_action ?? "done")})`, 3000);
    });
    wsClient.onSettingsSync((payload) => {
        const quietMode = Boolean(payload.quiet_mode);
        if (statusBarItem && quietMode) {
            statusBarItem.tooltip = "Cortex — Quiet mode enabled";
        }
    });
    // --- Panel provider ---
    panelProvider = new panel_provider_1.CortexPanelProvider(context.extensionUri, wsClient);
    context.subscriptions.push(vscode.window.registerWebviewViewProvider("cortex.interventionPanel", panelProvider));
    // --- Register commands ---
    context.subscriptions.push(vscode.commands.registerCommand("cortex.getActiveFile", () => {
        return contextProvider.getActiveFile();
    }), vscode.commands.registerCommand("cortex.getDiagnostics", () => {
        return contextProvider.getDiagnostics();
    }), vscode.commands.registerCommand("cortex.getSymbolAtCursor", () => {
        return contextProvider.getSymbolAtCursor();
    }), vscode.commands.registerCommand("cortex.foldExcept", (startLine, endLine) => {
        return foldController.foldExcept(startLine, endLine);
    }), vscode.commands.registerCommand("cortex.unfoldAll", () => {
        return foldController.unfoldAll();
    }), vscode.commands.registerCommand("cortex.restoreFoldState", () => {
        return foldController.restoreFoldState();
    }), vscode.commands.registerCommand("cortex.showPanel", () => {
        panelProvider?.showPanel();
    }), vscode.commands.registerCommand("cortex.connect", () => {
        wsClient?.connect();
    }), vscode.commands.registerCommand("cortex.disconnect", () => {
        wsClient?.disconnect();
    }));
    // --- Auto-connect ---
    if (config.get("autoConnect", true)) {
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
function deactivate() {
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
function updateStatusBar(payload) {
    if (!statusBarItem) {
        return;
    }
    const state = payload.state;
    const confidence = payload.confidence;
    const stateIcons = {
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
        statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    }
    else if (state === "HYPO") {
        statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
    }
    else {
        statusBarItem.backgroundColor = undefined;
    }
}
/**
 * Handle an INTERVENTION_TRIGGER from the daemon.
 */
function handleIntervention(payload) {
    const uiPlan = payload.ui_plan;
    // Apply fold if requested
    if (uiPlan?.fold_unrelated_code && foldController) {
        const editor = vscode.window.activeTextEditor;
        if (editor) {
            const cursorLine = editor.selection.active.line;
            // Fold everything except ±20 lines around cursor
            foldController.foldExcept(Math.max(0, cursorLine - 20), cursorLine + 20);
        }
    }
    // Show panel with intervention content
    panelProvider?.showIntervention(payload);
    // Show notification for overlay_only
    const level = payload.level;
    const headline = payload.headline;
    if (level === "overlay_only" && headline) {
        vscode.window.showInformationMessage(`Cortex: ${headline}`, "View Details", "Dismiss").then((action) => {
            if (action === "View Details") {
                panelProvider?.showPanel();
            }
            else if (action === "Dismiss") {
                wsClient?.sendUserAction("dismissed", payload.intervention_id);
            }
        });
    }
}
//# sourceMappingURL=extension.js.map