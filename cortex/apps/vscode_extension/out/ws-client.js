"use strict";
/**
 * Cortex VS Code Extension — WebSocket Client
 *
 * Connects to the Cortex daemon at ws://localhost:9473.
 * Handles STATE_UPDATE and INTERVENTION_TRIGGER messages from daemon,
 * sends IDENTIFY and USER_ACTION messages to daemon.
 * Auto-reconnects on disconnect with exponential backoff.
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
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.CortexWSClient = void 0;
const vscode = __importStar(require("vscode"));
const ws_1 = __importDefault(require("ws"));
/**
 * WebSocket client for communication with the Cortex daemon.
 *
 * Manages connection lifecycle, message routing, and auto-reconnection.
 */
class CortexWSClient {
    _url;
    _ws;
    _connected = false;
    _reconnectTimer;
    _reconnectDelay = 3000; // Start at 3s, cap at 30s
    _maxReconnectDelay = 30000;
    _intentionalDisconnect = false;
    _sequence = 0;
    // Event handlers
    _stateUpdateHandlers = [];
    _interventionHandlers = [];
    _connectionHandlers = [];
    _contextRequestHandler;
    _restoreHandlers = [];
    _settingsHandlers = [];
    constructor(url) {
        this._url = url;
    }
    /** Whether the client is currently connected. */
    get connected() {
        return this._connected;
    }
    /** Register a handler for STATE_UPDATE messages. */
    onStateUpdate(handler) {
        this._stateUpdateHandlers.push(handler);
    }
    /** Register a handler for INTERVENTION_TRIGGER messages. */
    onInterventionTrigger(handler) {
        this._interventionHandlers.push(handler);
    }
    /** Register a handler for connection state changes. */
    onConnectionChange(handler) {
        this._connectionHandlers.push(handler);
    }
    /** Register a handler for CONTEXT_REQUEST messages from daemon. */
    onContextRequest(handler) {
        this._contextRequestHandler = handler;
    }
    onRestore(handler) {
        this._restoreHandlers.push(handler);
    }
    onSettingsSync(handler) {
        this._settingsHandlers.push(handler);
    }
    /**
     * Connect to the Cortex daemon WebSocket server.
     */
    connect() {
        if (this._connected || this._ws) {
            return;
        }
        this._intentionalDisconnect = false;
        try {
            this._ws = new ws_1.default(this._url);
            this._ws.on("open", () => {
                this._connected = true;
                this._reconnectDelay = 3000; // Reset backoff
                this._notifyConnection(true);
                // Identify as VS Code extension
                this._send({
                    type: "IDENTIFY",
                    payload: { client_type: "vscode" },
                    timestamp: Date.now() / 1000,
                    sequence: ++this._sequence,
                });
                vscode.window.setStatusBarMessage("Cortex: Connected to daemon", 3000);
            });
            this._ws.on("message", (data) => {
                this._handleMessage(data.toString());
            });
            this._ws.on("close", () => {
                this._handleDisconnect();
            });
            this._ws.on("error", () => {
                // onclose will follow; no extra handling needed
            });
        }
        catch {
            this._scheduleReconnect();
        }
    }
    /**
     * Disconnect from the daemon (no auto-reconnect).
     */
    disconnect() {
        this._intentionalDisconnect = true;
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = undefined;
        }
        if (this._ws) {
            this._ws.removeAllListeners("close");
            this._ws.close();
            this._ws = undefined;
        }
        if (this._connected) {
            this._connected = false;
            this._notifyConnection(false);
        }
    }
    /**
     * Send a USER_ACTION message to the daemon.
     *
     * @param action - "dismissed" | "engaged" | "snoozed"
     * @param interventionId - ID of the intervention being acted on
     */
    sendUserAction(action, interventionId) {
        this._send({
            type: "USER_ACTION",
            payload: {
                action,
                intervention_id: interventionId,
                timestamp: Date.now() / 1000,
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
    }
    // --- Internal ---
    _send(msg) {
        if (!this._ws || !this._connected) {
            return;
        }
        try {
            this._ws.send(JSON.stringify(msg));
        }
        catch {
            // Connection may have dropped between check and send
        }
    }
    _handleMessage(raw) {
        let msg;
        try {
            msg = JSON.parse(raw);
        }
        catch {
            return;
        }
        switch (msg.type) {
            case "STATE_UPDATE":
                for (const handler of this._stateUpdateHandlers) {
                    try {
                        handler(msg.payload);
                    }
                    catch {
                        // Handler error should not crash the client
                    }
                }
                break;
            case "INTERVENTION_TRIGGER":
                for (const handler of this._interventionHandlers) {
                    try {
                        handler(msg.payload);
                    }
                    catch {
                        // Handler error should not crash the client
                    }
                }
                break;
            case "CONTEXT_REQUEST":
                this._handleContextRequest(msg);
                break;
            case "INTERVENTION_RESTORE":
                for (const handler of this._restoreHandlers) {
                    try {
                        handler(msg.payload);
                    }
                    catch {
                        // Ignore handler errors
                    }
                }
                break;
            case "SETTINGS_SYNC":
                for (const handler of this._settingsHandlers) {
                    try {
                        handler(msg.payload);
                    }
                    catch {
                        // Ignore handler errors
                    }
                }
                break;
            default:
                // Unknown message types are silently ignored
                break;
        }
    }
    async _handleContextRequest(msg) {
        if (!this._contextRequestHandler) {
            this._send({
                type: "CONTEXT_RESPONSE",
                payload: {},
                timestamp: Date.now() / 1000,
                sequence: ++this._sequence,
                correlation_id: msg.correlation_id,
            });
            return;
        }
        try {
            const context = await this._contextRequestHandler();
            this._send({
                type: "CONTEXT_RESPONSE",
                payload: context,
                timestamp: Date.now() / 1000,
                sequence: msg.sequence, // Echo request sequence
                correlation_id: msg.correlation_id,
            });
        }
        catch {
            this._send({
                type: "CONTEXT_RESPONSE",
                payload: { error: "context_gather_failed" },
                timestamp: Date.now() / 1000,
                sequence: msg.sequence,
                correlation_id: msg.correlation_id,
            });
        }
    }
    _handleDisconnect() {
        this._ws = undefined;
        if (this._connected) {
            this._connected = false;
            this._notifyConnection(false);
        }
        if (!this._intentionalDisconnect) {
            this._scheduleReconnect();
        }
    }
    _scheduleReconnect() {
        if (this._reconnectTimer || this._intentionalDisconnect) {
            return;
        }
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = undefined;
            this.connect();
        }, this._reconnectDelay);
        // Exponential backoff (3s, 6s, 12s, 24s, 30s max)
        this._reconnectDelay = Math.min(this._reconnectDelay * 2, this._maxReconnectDelay);
    }
    _notifyConnection(connected) {
        for (const handler of this._connectionHandlers) {
            try {
                handler(connected);
            }
            catch {
                // Handler error should not crash the client
            }
        }
    }
}
exports.CortexWSClient = CortexWSClient;
//# sourceMappingURL=ws-client.js.map