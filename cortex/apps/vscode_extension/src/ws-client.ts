/**
 * Cortex VS Code Extension — WebSocket Client
 *
 * Connects to the Cortex daemon at ws://localhost:9473.
 * Handles STATE_UPDATE and INTERVENTION_TRIGGER messages from daemon,
 * sends IDENTIFY and USER_ACTION messages to daemon.
 * Auto-reconnects on disconnect with exponential backoff.
 */

import * as vscode from "vscode";
import WebSocket from "ws";

/** WebSocket message envelope matching the daemon's WSMessage format. */
interface WSMessage {
    type: string;
    payload: Record<string, unknown>;
    timestamp: number;
    sequence: number;
    correlation_id?: string;
    target_client_types?: string[];
    source_client_type?: string;
}

type StateUpdateHandler = (payload: Record<string, unknown>) => void;
type InterventionHandler = (payload: Record<string, unknown>) => void;
type ConnectionHandler = (connected: boolean) => void;
type ContextRequestHandler = () => Promise<Record<string, unknown>>;
type RestoreHandler = (payload: Record<string, unknown>) => void;
type SettingsHandler = (payload: Record<string, unknown>) => void;
type GenericMessageHandler = (msg: { type: string; payload: Record<string, unknown> }) => void;

/**
 * WebSocket client for communication with the Cortex daemon.
 *
 * Manages connection lifecycle, message routing, and auto-reconnection.
 */
export class CortexWSClient {
    private _url: string;
    private _ws: WebSocket | undefined;
    private _connected = false;
    private _reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    private _reconnectDelay = 3000; // Start at 3s, cap at 30s
    private _maxReconnectDelay = 30000;
    private _intentionalDisconnect = false;
    private _sequence = 0;

    // Event handlers
    private _stateUpdateHandlers: StateUpdateHandler[] = [];
    private _interventionHandlers: InterventionHandler[] = [];
    private _connectionHandlers: ConnectionHandler[] = [];
    private _contextRequestHandler: ContextRequestHandler | undefined;
    private _restoreHandlers: RestoreHandler[] = [];
    private _settingsHandlers: SettingsHandler[] = [];
    private _genericMessageHandlers: GenericMessageHandler[] = [];

    constructor(url: string) {
        this._url = url;
    }

    /** Whether the client is currently connected. */
    get connected(): boolean {
        return this._connected;
    }

    /** Register a handler for STATE_UPDATE messages. */
    onStateUpdate(handler: StateUpdateHandler): void {
        this._stateUpdateHandlers.push(handler);
    }

    /** Register a handler for INTERVENTION_TRIGGER messages. */
    onInterventionTrigger(handler: InterventionHandler): void {
        this._interventionHandlers.push(handler);
    }

    /** Register a handler for connection state changes. */
    onConnectionChange(handler: ConnectionHandler): void {
        this._connectionHandlers.push(handler);
    }

    /** Register a handler for CONTEXT_REQUEST messages from daemon. */
    onContextRequest(handler: ContextRequestHandler): void {
        this._contextRequestHandler = handler;
    }

    onRestore(handler: RestoreHandler): void {
        this._restoreHandlers.push(handler);
    }

    onSettingsSync(handler: SettingsHandler): void {
        this._settingsHandlers.push(handler);
    }

    /** Register a handler for any message type (called for all messages). */
    onMessage(handler: GenericMessageHandler): void {
        this._genericMessageHandlers.push(handler);
    }

    /**
     * Connect to the Cortex daemon WebSocket server.
     */
    connect(): void {
        if (this._connected || this._ws) {
            return;
        }

        this._intentionalDisconnect = false;

        try {
            this._ws = new WebSocket(this._url);

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

                vscode.window.setStatusBarMessage(
                    "Cortex: Connected to daemon",
                    3000,
                );
            });

            this._ws.on("message", (data: WebSocket.RawData) => {
                this._handleMessage(data.toString());
            });

            this._ws.on("close", () => {
                this._handleDisconnect();
            });

            this._ws.on("error", () => {
                // onclose will follow; no extra handling needed
            });
        } catch {
            this._scheduleReconnect();
        }
    }

    /**
     * Disconnect from the daemon (no auto-reconnect).
     */
    disconnect(): void {
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
    sendUserAction(action: string, interventionId: string): void {
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

    private _send(msg: WSMessage): void {
        if (!this._ws || !this._connected) {
            return;
        }
        try {
            this._ws.send(JSON.stringify(msg));
        } catch {
            // Connection may have dropped between check and send
        }
    }

    private _handleMessage(raw: string): void {
        let msg: WSMessage;
        try {
            msg = JSON.parse(raw) as WSMessage;
        } catch {
            return;
        }

        switch (msg.type) {
            case "STATE_UPDATE":
                for (const handler of this._stateUpdateHandlers) {
                    try {
                        handler(msg.payload);
                    } catch {
                        // Handler error should not crash the client
                    }
                }
                break;

            case "INTERVENTION_TRIGGER":
                for (const handler of this._interventionHandlers) {
                    try {
                        handler(msg.payload);
                    } catch {
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
                    } catch {
                        // Ignore handler errors
                    }
                }
                break;

            case "SETTINGS_SYNC":
                for (const handler of this._settingsHandlers) {
                    try {
                        handler(msg.payload);
                    } catch {
                        // Ignore handler errors
                    }
                }
                break;

            default:
                // Forward to generic message handlers
                for (const handler of this._genericMessageHandlers) {
                    try {
                        handler(msg);
                    } catch {
                        // Handler error should not crash the client
                    }
                }
                break;
        }
    }

    private async _handleContextRequest(msg: WSMessage): Promise<void> {
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
        } catch {
            this._send({
                type: "CONTEXT_RESPONSE",
                payload: { error: "context_gather_failed" },
                timestamp: Date.now() / 1000,
                sequence: msg.sequence,
                correlation_id: msg.correlation_id,
            });
        }
    }

    private _handleDisconnect(): void {
        this._ws = undefined;

        if (this._connected) {
            this._connected = false;
            this._notifyConnection(false);
        }

        if (!this._intentionalDisconnect) {
            this._scheduleReconnect();
        }
    }

    private _scheduleReconnect(): void {
        if (this._reconnectTimer || this._intentionalDisconnect) {
            return;
        }

        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = undefined;
            this.connect();
        }, this._reconnectDelay);

        // Exponential backoff (3s, 6s, 12s, 24s, 30s max)
        this._reconnectDelay = Math.min(
            this._reconnectDelay * 2,
            this._maxReconnectDelay,
        );
    }

    private _notifyConnection(connected: boolean): void {
        for (const handler of this._connectionHandlers) {
            try {
                handler(connected);
            } catch {
                // Handler error should not crash the client
            }
        }
    }
}
