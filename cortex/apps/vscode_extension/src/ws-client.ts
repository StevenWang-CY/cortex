/**
 * Cortex VS Code Extension — WebSocket Client
 *
 * Connects to the Cortex daemon at ws://127.0.0.1:9473.
 * Handles STATE_UPDATE and INTERVENTION_TRIGGER messages from daemon,
 * sends IDENTIFY and USER_ACTION messages to daemon.
 * Auto-reconnects on disconnect with exponential backoff.
 *
 * Connection contract (audit A1/A4/A5):
 *   - Only loopback daemon URLs are accepted; anything else is refused
 *     before a socket is opened because the channel carries the local
 *     capability token and editor content.
 *   - ``AUTH`` is the first frame on every socket. The client is only
 *     reported as *connected* after the daemon answers ``AUTH_OK``;
 *     ``IDENTIFY`` and the offline outbox are flushed after that.
 *   - A missing capability token surfaces one warning naming the token
 *     path; the socket is closed and the backoff cycle re-checks the
 *     file later instead of tripping the daemon's 1011 auth gate.
 *   - Daemon close code 1011 and ``PROTOCOL_ERROR`` are surfaced to the
 *     user instead of being logged to a console nobody reads.
 */

import * as vscode from "vscode";
import WebSocket from "ws";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { randomUUID } from "crypto";
import type {
    InterventionReceiptBatch,
    WSMessage as WSMessageSchema,
} from "./generated/cortex_schemas";

/** Result of looking up the daemon-minted capability token. */
export interface CapabilityTokenLookup {
    /** The token, or ``null`` when the file is absent/unreadable/too short. */
    token: string | null;
    /** Absolute path that was checked (``null`` only if it cannot be resolved). */
    path: string | null;
}

/**
 * Resolve ``<config_dir>/auth.token`` for the current platform. Mirrors the
 * daemon's config-dir rules so the warning shown when the file is missing
 * names the exact path the user can inspect.
 */
export function resolveCapabilityTokenPath(): string | null {
    try {
        let configDir: string;
        const platform = process.platform;
        if (platform === "darwin") {
            configDir = path.join(
                os.homedir(),
                "Library",
                "Application Support",
                "Cortex",
            );
        } else if (platform === "win32") {
            const appData = process.env.APPDATA;
            if (!appData) return null;
            configDir = path.join(appData, "Cortex");
        } else {
            const xdg = process.env.XDG_CONFIG_HOME;
            configDir = xdg
                ? path.join(xdg, "cortex")
                : path.join(os.homedir(), ".config", "cortex");
        }
        return path.join(configDir, "auth.token");
    } catch {
        return null;
    }
}

/**
 * Audit Debt-2: read the local capability token the daemon mints at
 * ``<config_dir>/auth.token``. The legitimate VS Code extension can
 * read this file because it runs as the same user as the daemon.
 */
export function readCapabilityToken(): CapabilityTokenLookup {
    const tokenFile = resolveCapabilityTokenPath();
    if (!tokenFile) return { token: null, path: null };
    try {
        if (!fs.existsSync(tokenFile)) {
            return { token: null, path: tokenFile };
        }
        const raw = fs.readFileSync(tokenFile, "utf-8").trim();
        return { token: raw.length >= 32 ? raw : null, path: tokenFile };
    } catch {
        return { token: null, path: tokenFile };
    }
}

/**
 * A1: only loopback hosts may receive the auth token + editor content.
 * Accepts ``ws://127.0.0.1``, ``ws://localhost`` and ``ws://[::1]`` (any
 * port, ``wss`` allowed for the same hosts). Everything else — other
 * hosts, other schemes, unparsable strings — is refused.
 */
export function isLoopbackDaemonUrl(url: string): boolean {
    let parsed: URL;
    try {
        parsed = new URL(url);
    } catch {
        return false;
    }
    if (parsed.protocol !== "ws:" && parsed.protocol !== "wss:") {
        return false;
    }
    if (parsed.username || parsed.password) {
        return false;
    }
    const host = parsed.hostname.toLowerCase();
    if (host === "localhost" || host === "[::1]" || host === "::1") {
        return true;
    }
    // 127.0.0.0/8 — the whole IPv4 loopback block.
    return /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host);
}

/** Generated wire envelope with metadata optional only before send stamping. */
type WSMetadataKeys =
    | "schema_version"
    | "protocol_version"
    | "event_id"
    | "sent_at_unix_ms"
    | "sent_at_mono_ns"
    | "boot_id";
type WSMessage = Omit<WSMessageSchema, "payload" | WSMetadataKeys> &
    Partial<Pick<WSMessageSchema, WSMetadataKeys>> & {
    payload: Record<string, unknown>;
};

const WIRE_SCHEMA_VERSION = "2.0";
const PROTOCOL_VERSION = "2.0";
const SUPPORTED_PROTOCOL_VERSIONS = ["2.0", "1.0"] as const;
const CLIENT_BOOT_ID = randomUUID();

function withWireMetadata(
    msg: WSMessage,
    protocolVersion = PROTOCOL_VERSION,
): WSMessage {
    const sentAtUnixMs = Date.now();
    return {
        ...msg,
        schema_version: WIRE_SCHEMA_VERSION,
        protocol_version: protocolVersion,
        event_id: randomUUID(),
        sent_at_unix_ms: sentAtUnixMs,
        sent_at_mono_ns: Math.max(0, Math.round(performance.now() * 1_000_000)),
        boot_id: CLIENT_BOOT_ID,
        timestamp: sentAtUnixMs / 1000,
    } as WSMessage;
}

type StateUpdateHandler = (payload: Record<string, unknown>) => void;
type InterventionHandler = (payload: Record<string, unknown>) => void;
type ConnectionHandler = (connected: boolean) => void;
type ContextRequestHandler = () => Promise<Record<string, unknown>>;
type RestoreHandler = (payload: Record<string, unknown>) => void;
type SettingsHandler = (payload: Record<string, unknown>) => void;
type CopilotThrottleHandler = (payload: Record<string, unknown>) => void;
type GenericMessageHandler = (msg: { type: string; payload: Record<string, unknown> }) => void;

/** Injectable collaborators (test seam; production uses the defaults). */
export interface CortexWSClientDeps {
    readToken?: () => CapabilityTokenLookup;
}

/**
 * Typed rejection for ``sendWhyDetailRequest`` timeouts.
 *
 * F11 (Phase-4 audit): the WHY_DETAIL promise previously rejected with
 * a plain ``new Error("…timed out…")`` which forced every caller to
 * pattern-match the error message. Callers that want to render a
 * specific "explanation took too long" UI can now ``instanceof``-check
 * this class. Plain-Error catches keep working for backwards compat.
 */
export class WhyDetailTimeoutError extends Error {
    /** The correlation_id of the request that timed out. */
    readonly correlationId: string;
    /** Timeout window in milliseconds. */
    readonly timeoutMs: number;
    constructor(correlationId: string, timeoutMs: number) {
        super(`WHY_DETAIL request timed out after ${timeoutMs}ms`);
        this.name = "WhyDetailTimeoutError";
        this.correlationId = correlationId;
        this.timeoutMs = timeoutMs;
    }
}

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
    private _negotiatedProtocolVersion = PROTOCOL_VERSION;
    private readonly _lastInboundSequenceByType = new Map<string, number>();
    private readonly _seenInboundEventIds = new Set<string>();
    private readonly _seenInboundEventOrder: string[] = [];
    private static readonly _MAX_SEEN_EVENT_IDS = 512;
    private readonly _readToken: () => CapabilityTokenLookup;

    /**
     * A4: the daemon must answer ``AUTH`` with ``AUTH_OK`` before the
     * client counts as connected. If nothing arrives within this window
     * the socket is torn down so the backoff cycle can retry instead of
     * sitting in a half-open limbo where the heartbeat (frame-level
     * pongs are answered by the transport, not the daemon) never fires.
     */
    private static readonly _AUTH_TIMEOUT_MS = 10_000;
    private _authTimer: ReturnType<typeof setTimeout> | undefined;

    /**
     * F6 (Phase-4 audit): WebSocket frame-level ping/pong heartbeat.
     *
     * The `websockets` Python library (daemon) does NOT auto-emit
     * application-layer pings unless `ping_interval` is set; it DOES
     * auto-respond to inbound frame-level pings with pongs. So the
     * client sends ``ws.ping()`` every 30s and waits for the
     * frame-level pong reply. If no pong arrives within 45s
     * (``HEARTBEAT_TIMEOUT_MS``), we consider the connection stale
     * and force a reconnect.
     *
     * This is the WS-protocol-level mechanism (RFC 6455 §5.5.2) — not
     * an application-layer message — so it costs nothing on the
     * daemon side and stays out of the WSMessage dispatch path.
     */
    private static readonly _HEARTBEAT_INTERVAL_MS = 30_000;
    private static readonly _HEARTBEAT_TIMEOUT_MS = 45_000;
    private _heartbeatTimer: ReturnType<typeof setInterval> | undefined;
    private _lastPongAt = 0;
    // swift-concurrency-pro rule (transferred to TS): the reconnect timer
    // should be propagation-aware. ``disconnect()`` aborts this controller
    // so the queued reconnect doesn't fire after teardown.
    private _reconnectAbort: AbortController | undefined;

    // One-shot user-facing notices. Each is reset when the condition it
    // describes clears so a *new* episode warns again, but a reconnect
    // storm never stacks toasts.
    private _hasConnectedOnce = false;
    private _tokenWarningShown = false;
    private _urlRefusalShown = false;
    private _protocolErrorPromptOpen = false;
    private _lastCloseWarningReason: string | null = null;

    // Event handlers
    private _stateUpdateHandlers: StateUpdateHandler[] = [];
    private _interventionHandlers: InterventionHandler[] = [];
    private _connectionHandlers: ConnectionHandler[] = [];
    private _contextRequestHandler: ContextRequestHandler | undefined;
    private _restoreHandlers: RestoreHandler[] = [];
    private _settingsHandlers: SettingsHandler[] = [];
    // B1 (audit-prod): explicit handler list for COPILOT_THROTTLE so the
    // message is dispatched through a dedicated arm of the switch rather
    // than falling through to the generic-default. The generic arm
    // silently drops the message if no listener happens to be
    // registered at the time the frame arrives; the explicit arm makes
    // the contract visible at the dispatch site.
    private _copilotThrottleHandlers: CopilotThrottleHandler[] = [];
    private _genericMessageHandlers: GenericMessageHandler[] = [];

    // P1 (audit Phase 4d): bounded outbox so messages sent while
    // disconnected are queued (up to 16 entries) and flushed on the
    // next successful connection. Without this, any USER_ACTION fired
    // during the 3-30s reconnect backoff was silently dropped, which
    // made the panel feel "dead" after a daemon restart even though the
    // user clicked buttons.
    private static readonly _OUTBOX_MAX = 16;
    private _outbox: WSMessage[] = [];
    private _overflowWarned = false;

    // P1 (audit Phase 4d, Task C): correlation_id-keyed pending
    // WHY_DETAIL_REQUEST resolvers. Each request generates a UUID,
    // resolves on a matching WHY_DETAIL reply, and times out after 5s.
    private _pendingWhyDetail: Map<
        string,
        {
            resolve: (payload: Record<string, unknown>) => void;
            reject: (err: Error) => void;
            timer: ReturnType<typeof setTimeout>;
        }
    > = new Map();

    constructor(
        url: string,
        private readonly _clientInstanceId = `vscode_${CLIENT_BOOT_ID}`,
        deps: CortexWSClientDeps = {},
    ) {
        this._url = url;
        this._readToken = deps.readToken ?? readCapabilityToken;
    }

    /** Whether the client is currently connected (i.e. past ``AUTH_OK``). */
    get connected(): boolean {
        return this._connected;
    }

    /**
     * P1 (audit Phase 4d, Task B): public connection-state predicate
     * used by ``CortexPanelProvider`` to branch the empty-state UI
     * between "no active intervention" and "daemon offline / reconnect".
     */
    get isConnected(): boolean {
        return this._connected;
    }

    /** Per-process identity used to bind editor-origin authorizations. */
    get clientBootId(): string {
        return CLIENT_BOOT_ID;
    }

    /** The daemon URL this client targets. */
    get url(): string {
        return this._url;
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

    /** B1 (audit-prod): register a handler for COPILOT_THROTTLE
     * directives from the daemon. The handler is invoked from the
     * explicit ``case "COPILOT_THROTTLE"`` arm rather than via the
     * generic-default fallback. */
    onCopilotThrottle(handler: CopilotThrottleHandler): void {
        this._copilotThrottleHandlers.push(handler);
    }

    /** Register a handler for any message type (called for all messages). */
    onMessage(handler: GenericMessageHandler): void {
        this._genericMessageHandlers.push(handler);
    }

    /**
     * A14: switch the daemon URL at runtime (``cortex.daemonUrl`` changed
     * in settings). Non-loopback URLs are refused and the previous URL
     * stays in effect. If a connection or reconnect cycle is active it
     * is restarted against the new URL.
     */
    setUrl(url: string): void {
        if (url === this._url) return;
        if (!isLoopbackDaemonUrl(url)) {
            this._refuseUrl(url);
            return;
        }
        this._url = url;
        this._urlRefusalShown = false;
        const active = Boolean(this._ws) || this._connected || Boolean(this._reconnectTimer);
        if (!active) return;
        this.disconnect();
        this.connect();
    }

    /**
     * Connect to the Cortex daemon WebSocket server.
     */
    connect(): void {
        if (this._connected || this._ws) {
            return;
        }

        if (!isLoopbackDaemonUrl(this._url)) {
            // A1: never open a socket to a non-loopback host. No reconnect
            // is scheduled — a bad URL cannot fix itself.
            this._refuseUrl(this._url);
            return;
        }

        this._intentionalDisconnect = false;

        try {
            this._ws = new WebSocket(this._url);

            this._ws.on("open", () => {
                this._lastInboundSequenceByType.clear();
                this._seenInboundEventIds.clear();
                this._seenInboundEventOrder.length = 0;
                this._negotiatedProtocolVersion = PROTOCOL_VERSION;
                this._onSocketOpen();
            });

            this._ws.on("message", (data: WebSocket.RawData) => {
                this._handleMessage(data.toString());
            });

            // F6: WS-protocol-level pong handler. The daemon's
            // ``websockets`` server auto-pongs every inbound frame-level
            // ping; the timestamp lets ``_checkHeartbeatHealth`` decide
            // whether the connection is alive.
            this._ws.on("pong", () => {
                this._lastPongAt = Date.now();
            });

            this._ws.on("close", (code: number, reason: Buffer | string) => {
                const reasonText = typeof reason === "string"
                    ? reason
                    : (reason ? reason.toString() : "");
                this._handleDisconnect(code, reasonText);
            });

            this._ws.on("error", () => {
                // onclose will follow; no extra handling needed
            });
        } catch {
            this._ws = undefined;
            this._scheduleReconnect();
        }
    }

    /**
     * Disconnect from the daemon (no auto-reconnect).
     */
    disconnect(): void {
        this._intentionalDisconnect = true;

        // Cancel any pending reconnect attempt — both the legacy
        // ``setTimeout`` cleanup and the AbortController signal listener.
        this._reconnectAbort?.abort();
        this._reconnectAbort = undefined;
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = undefined;
        }

        // F6: stop the heartbeat before tearing down the socket so we
        // don't spuriously trigger reconnect on the in-flight ping.
        this._stopHeartbeat();
        this._clearAuthTimer();

        if (this._ws) {
            this._ws.removeAllListeners("close");
            try {
                this._ws.close();
            } catch {
                // Socket may already be dead.
            }
            this._ws = undefined;
        }

        if (this._connected) {
            this._connected = false;
            this._notifyConnection(false);
        }
    }

    /**
     * Socket opened: send AUTH (or, without a token, warn once and back
     * off). Nothing else is emitted until ``AUTH_OK`` arrives.
     */
    private _onSocketOpen(): void {
        const lookup = this._readToken();
        if (!lookup.token) {
            this._warnMissingToken(lookup.path);
            // Close *our* side so ``_handleDisconnect`` re-arms the
            // backoff cycle and re-reads the token file later. Sending
            // any other frame first would make the daemon close with
            // 1011 and produce the same loop with an extra warning.
            try {
                this._ws?.close(1000, "no capability token");
            } catch {
                // close event follows regardless
            }
            return;
        }
        this._tokenWarningShown = false;

        // Audit Debt-2: AUTH first. The daemon refuses every other
        // type until this frame validates; without it the server
        // closes the connection with code 1011 ("auth required")
        // before any STATE_UPDATE reaches us. The frame bypasses the
        // outbox because we are deliberately not "connected" yet.
        this._sendRaw({
            type: "AUTH",
            payload: {
                auth_token: lookup.token,
                protocol_version: PROTOCOL_VERSION,
                supported_protocol_versions: [
                    ...SUPPORTED_PROTOCOL_VERSIONS,
                ],
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        } as WSMessage);

        this._clearAuthTimer();
        this._authTimer = setTimeout(() => {
            this._authTimer = undefined;
            if (this._connected || !this._ws) return;
            console.warn("[Cortex] no AUTH_OK within timeout — reconnecting");
            try {
                this._ws.terminate();
            } catch {
                // close event follows
            }
        }, CortexWSClient._AUTH_TIMEOUT_MS);
    }

    /** ``AUTH_OK`` received: the channel is open for real now. */
    private _onAuthenticated(selectedProtocol: unknown): void {
        if (selectedProtocol === "1.0" || selectedProtocol === "2.0") {
            this._negotiatedProtocolVersion = selectedProtocol;
        }
        if (selectedProtocol !== PROTOCOL_VERSION) {
            console.warn(
                `[Cortex] daemon selected protocol ${String(selectedProtocol)}; expected ${PROTOCOL_VERSION}`,
            );
        }
        if (this._connected) {
            // Idempotent replay ACK — nothing else to do.
            return;
        }
        this._clearAuthTimer();
        this._connected = true;
        this._reconnectDelay = 3000; // Reset backoff
        this._lastCloseWarningReason = null;
        // F6 (Phase-4 audit): start the heartbeat the moment the channel
        // is live. A stale connection where the TCP socket stayed up but
        // the daemon stopped serving is detected within ~45s.
        this._startHeartbeat();

        // Identify as VS Code extension (only after AUTH_OK — A4).
        this._send({
            type: "IDENTIFY",
            payload: {
                client_type: "vscode",
                client_instance_id: this._clientInstanceId,
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });

        // P1 (Task A): flush the bounded outbox now that we've
        // reattached. Drain in FIFO order; do NOT re-queue on
        // failure — a transient send error during flush is
        // logged but not retried (the next disconnect/connect
        // cycle would re-queue infinitely otherwise).
        const queued = this._outbox;
        this._outbox = [];
        this._overflowWarned = false;
        for (const msg of queued) {
            try {
                this._ws?.send(JSON.stringify(withWireMetadata(
                    msg,
                    this._negotiatedProtocolVersion,
                )));
            } catch {
                // Connection torn down mid-flush; remaining
                // messages will be lost. The reconnect handler
                // re-enters this path on the next open.
            }
        }

        this._notifyConnection(true);

        // UX: the status-bar item already reflects reconnects; only the
        // first successful connection of this extension host gets a toast.
        if (!this._hasConnectedOnce) {
            this._hasConnectedOnce = true;
            try {
                vscode.window.setStatusBarMessage(
                    "Cortex: Connected to daemon",
                    3000,
                );
            } catch {
                // Host without a status bar (tests).
            }
        }
    }

    private _warnMissingToken(tokenPath: string | null): void {
        if (this._tokenWarningShown) return;
        this._tokenWarningShown = true;
        const where = tokenPath ?? "the Cortex config directory";
        const message =
            "Cortex isn't running or hasn't created its local auth token yet "
            + `(expected at ${where}). Start the Cortex app, then reconnect.`;
        try {
            void Promise.resolve(
                vscode.window.showWarningMessage(message, "Open Cortex"),
            ).then((choice) => {
                if (choice === "Open Cortex") {
                    void vscode.commands.executeCommand("cortex.showPanel");
                }
            }).catch(() => undefined);
        } catch {
            console.warn(`[Cortex] ${message}`);
        }
    }

    private _refuseUrl(url: string): void {
        console.error(`[Cortex] refusing non-loopback daemon URL: ${url}`);
        if (this._urlRefusalShown) return;
        this._urlRefusalShown = true;
        try {
            void vscode.window.showErrorMessage(
                `Cortex refused to connect to ${url}: only loopback daemon URLs `
                + "are allowed (ws://127.0.0.1:9473, ws://localhost, ws://[::1]). "
                + "Check the cortex.daemonUrl setting.",
            );
        } catch {
            // Host without message boxes (tests).
        }
    }

    private _showProtocolErrorOnce(payload: Record<string, unknown>): void {
        if (this._protocolErrorPromptOpen) return;
        this._protocolErrorPromptOpen = true;
        const code = typeof payload.code === "string" ? payload.code : "protocol_error";
        const message =
            `Cortex daemon rejected the connection (${code}). The daemon and `
            + "this extension speak different protocol versions — update both "
            + "to matching releases, then retry.";
        try {
            void Promise.resolve(
                vscode.window.showErrorMessage(message, "Retry"),
            ).then((choice) => {
                this._protocolErrorPromptOpen = false;
                if (choice === "Retry") {
                    this._forceReconnect();
                }
            }).catch(() => {
                this._protocolErrorPromptOpen = false;
            });
        } catch {
            this._protocolErrorPromptOpen = false;
        }
    }

    /** Tear down whatever socket exists and dial again immediately. */
    private _forceReconnect(): void {
        this._reconnectAbort?.abort();
        this._reconnectAbort = undefined;
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = undefined;
        }
        this._stopHeartbeat();
        this._clearAuthTimer();
        if (this._ws) {
            this._ws.removeAllListeners("close");
            try {
                this._ws.terminate();
            } catch {
                // already dead
            }
            this._ws = undefined;
        }
        if (this._connected) {
            this._connected = false;
            this._notifyConnection(false);
        }
        this.connect();
    }

    private _clearAuthTimer(): void {
        if (this._authTimer) {
            clearTimeout(this._authTimer);
            this._authTimer = undefined;
        }
    }

    /**
     * F6 (Phase-4 audit): start the WS-protocol-level heartbeat.
     *
     * Sends ``ws.ping()`` every ``_HEARTBEAT_INTERVAL_MS`` and checks
     * ``_lastPongAt`` against ``_HEARTBEAT_TIMEOUT_MS``. The first
     * pong is seeded to ``Date.now()`` at start so a freshly-opened
     * connection has a full window before the first stale check.
     */
    private _startHeartbeat(): void {
        this._stopHeartbeat();
        this._lastPongAt = Date.now();
        this._heartbeatTimer = setInterval(() => {
            this._checkHeartbeatHealth();
        }, CortexWSClient._HEARTBEAT_INTERVAL_MS);
    }

    /** F6: clear the heartbeat interval. Safe to call when no timer is armed. */
    private _stopHeartbeat(): void {
        if (this._heartbeatTimer) {
            clearInterval(this._heartbeatTimer);
            this._heartbeatTimer = undefined;
        }
    }

    /**
     * F6: one heartbeat tick. If no pong arrived within the timeout
     * window, force a reconnect; otherwise emit a fresh ping.
     */
    private _checkHeartbeatHealth(): void {
        if (!this._ws || !this._connected) {
            this._stopHeartbeat();
            return;
        }
        const sincePong = Date.now() - this._lastPongAt;
        if (sincePong > CortexWSClient._HEARTBEAT_TIMEOUT_MS) {
            // Stale — force a reconnect. ``_handleDisconnect`` re-arms
            // the backoff cycle and ``connect()`` will restart the
            // heartbeat on the next ``open``.
            console.warn(
                `[Cortex] ws heartbeat timeout (${sincePong}ms since pong) — reconnecting`,
            );
            this._stopHeartbeat();
            try {
                this._ws.terminate();
            } catch {
                // Already closing; ``close`` event will follow.
            }
            return;
        }
        try {
            this._ws.ping();
        } catch {
            // Send failure → close event will follow.
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

    /** Send typed per-action apply/compensation/restore receipts. */
    sendInterventionReceipt(batch: InterventionReceiptBatch): void {
        this._send({
            type: "INTERVENTION_RECEIPT",
            payload: batch as unknown as Record<string, unknown>,
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
    }

    /**
     * P0 §3.6: send a MICRO_STEP_TOGGLED message to the daemon.
     *
     * @param interventionId - id of the active intervention
     * @param stepIndex - zero-based index into ``micro_steps``
     * @param newStatus - "pending" | "done" | "skipped"
     */
    sendMicroStepToggled(
        interventionId: string,
        stepIndex: number,
        newStatus: "pending" | "done" | "skipped",
    ): void {
        this._send({
            type: "MICRO_STEP_TOGGLED",
            payload: {
                intervention_id: interventionId,
                step_index: stepIndex,
                new_status: newStatus,
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
    }

    /**
     * P0 §3.8: send a USER_RATING message to the daemon.
     *
     * @param interventionId - id of the active intervention
     * @param rating - "thumbs_up" | "thumbs_down"
     * @param context - optional one-line free-text comment (≤200 chars)
     */
    sendUserRating(
        interventionId: string,
        rating: "thumbs_up" | "thumbs_down",
        context?: string,
    ): void {
        const payload: Record<string, unknown> = {
            intervention_id: interventionId,
            rating,
        };
        if (context && context.length > 0) {
            payload.context = context.slice(0, 200);
        }
        this._send({
            type: "USER_RATING",
            payload,
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
    }

    /**
     * P0 §3.9: request the structured causal rationale.
     *
     * P1 (audit Phase 4d, Task C): now correlation-id keyed. Each call
     * generates a fresh ``correlation_id`` (via ``crypto.randomUUID()``)
     * and returns a Promise that resolves when the daemon's WHY_DETAIL
     * reply carries the same id, or rejects after 5 s without a match.
     * Older callers that ignore the return value still get the legacy
     * fire-and-forget side effect: the frame is sent unchanged.
     *
     * @param interventionId - id of the active intervention
     * @returns Promise resolving to the daemon's WHY_DETAIL payload.
     */
    sendWhyDetailRequest(
        interventionId: string,
    ): Promise<Record<string, unknown>> {
        const correlationId = (() => {
            // ``crypto.randomUUID()`` is available on Node >= 16.7.
            // Cast to ``any`` keeps the fallback path narrow without
            // requiring a polyfill import for ancient runtimes.
            const c: { randomUUID?: () => string } =
                (globalThis as { crypto?: { randomUUID?: () => string } })
                    .crypto ?? {};
            if (typeof c.randomUUID === "function") {
                return c.randomUUID();
            }
            // Fallback: non-cryptographic UUID-shaped string so the
            // correlation table still works on hosts that lack
            // ``crypto.randomUUID``.
            return `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`.replace(
                /[xy]/g,
                (ch) => {
                    const r = (Math.random() * 16) | 0;
                    const v = ch === "x" ? r : (r & 0x3) | 0x8;
                    return v.toString(16);
                },
            );
        })();

        const WHY_DETAIL_TIMEOUT_MS = 5000;
        const promise = new Promise<Record<string, unknown>>(
            (resolve, reject) => {
                const timer = setTimeout(() => {
                    // F11 (Phase-4 audit): reject with a typed error so
                    // callers can ``instanceof WhyDetailTimeoutError``
                    // instead of string-matching the message.
                    this._pendingWhyDetail.delete(correlationId);
                    reject(
                        new WhyDetailTimeoutError(
                            correlationId,
                            WHY_DETAIL_TIMEOUT_MS,
                        ),
                    );
                }, WHY_DETAIL_TIMEOUT_MS);
                this._pendingWhyDetail.set(correlationId, {
                    resolve,
                    reject,
                    timer,
                });
            },
        );

        this._send({
            type: "WHY_DETAIL_REQUEST",
            payload: {
                intervention_id: interventionId,
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
            correlation_id: correlationId,
        });

        return promise;
    }

    /**
     * P0 §3.11 / §3.12: send a SNOOZE_REQUEST for an intervention.
     *
     * VS Code uses this from the OS-notification fallback path when
     * the desktop dashboard isn't focused and the user clicks the
     * "Snooze" toast button. The daemon unifies snooze requests
     * (regardless of source) through ``set_quiet_mode`` and
     * broadcasts QUIET_MODE_STATE so every surface mirrors.
     */
    sendSnoozeRequest(interventionId: string, durationMinutes: number = 15): void {
        this._send({
            type: "SNOOZE_REQUEST",
            payload: {
                intervention_id: interventionId,
                duration_minutes: durationMinutes,
                source: "vscode",
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
    }

    // --- Internal ---

    /** Write straight to the socket, bypassing the connected-gate/outbox. */
    private _sendRaw(msg: WSMessage): void {
        if (!this._ws) return;
        try {
            this._ws.send(JSON.stringify(withWireMetadata(
                msg,
                this._negotiatedProtocolVersion,
            )));
        } catch {
            // Will be retried on reconnect
        }
    }

    private _send(msg: WSMessage): void {
        // P1 (Task A): when disconnected, queue into the bounded outbox
        // instead of silently dropping the frame. The next successful
        // open flushes the queue in FIFO order.
        if (!this._ws || !this._connected) {
            if (this._outbox.length >= CortexWSClient._OUTBOX_MAX) {
                // Drop oldest to make room for the new entry.
                this._outbox.shift();
                console.warn(
                    "[Cortex] ws-client outbox overflow, dropping oldest",
                );
                if (!this._overflowWarned) {
                    this._overflowWarned = true;
                    try {
                        vscode.window.showWarningMessage(
                            "Cortex offline — action queued; some actions may be lost on reconnect",
                        );
                    } catch {
                        // showWarningMessage may not be available in
                        // some host contexts (tests); the warn line
                        // above is the durable signal.
                    }
                }
            }
            this._outbox.push(msg);
            return;
        }
        try {
            this._ws.send(JSON.stringify(withWireMetadata(
                msg,
                this._negotiatedProtocolVersion,
            )));
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
        if (!msg || typeof msg !== "object") return;
        if (!msg.payload || typeof msg.payload !== "object") {
            msg.payload = {};
        }

        if (!this._acceptIncomingMessage(msg)) return;

        switch (msg.type) {
            case "AUTH_OK":
                this._onAuthenticated(msg.payload.selected_protocol_version);
                break;

            case "PROTOCOL_ERROR":
                console.error(
                    `[Cortex] protocol negotiation failed: ${JSON.stringify(msg.payload)}`,
                );
                this._intentionalDisconnect = true;
                try {
                    this._ws?.close(1002, "unsupported Cortex protocol");
                } catch {
                    // Server may already have closed after the error frame.
                }
                // A5: a permanent silent disconnect is not acceptable —
                // tell the user and offer a retry.
                this._showProtocolErrorOnce(msg.payload);
                break;

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

            case "WHY_DETAIL": {
                // P1 (audit Phase 4d, Task C): resolve the pending
                // promise matching ``correlation_id``. The generic
                // ``onMessage`` fan-out still runs below so the
                // existing extension.ts ``WHY_DETAIL`` listener
                // (forwarding to the panel) keeps working unchanged.
                const correlationId = msg.correlation_id;
                if (correlationId) {
                    const pending = this._pendingWhyDetail.get(correlationId);
                    if (pending) {
                        clearTimeout(pending.timer);
                        this._pendingWhyDetail.delete(correlationId);
                        try {
                            pending.resolve(msg.payload);
                        } catch {
                            // Resolver throwing should not crash the
                            // client; pending map is already cleaned.
                        }
                    }
                }
                for (const handler of this._genericMessageHandlers) {
                    try {
                        handler(msg);
                    } catch {
                        // Handler error should not crash the client
                    }
                }
                break;
            }

            case "COPILOT_THROTTLE":
                // B1 (audit-prod): explicit arm. Previously the message
                // dropped to the generic-default and worked only as long
                // as the extension.ts ``onMessage`` listener was
                // registered before the first frame arrived. The
                // dedicated handler list makes the contract visible at
                // the dispatch site.
                for (const handler of this._copilotThrottleHandlers) {
                    try {
                        handler(msg.payload);
                    } catch {
                        // Handler error should not crash the client
                    }
                }
                // Also forward to generic handlers for backwards-compat
                // with existing extension.ts that listens via onMessage.
                for (const handler of this._genericMessageHandlers) {
                    try {
                        handler(msg);
                    } catch {
                        // Handler error should not crash the client
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

    /** Drop duplicate event identities and stale per-type sequences.
     *
     * Event identity is authoritative for v2 replays. Sequence ordering is
     * scoped by message type so a high-rate STATE_UPDATE cannot suppress a
     * lower-frequency intervention frame. Sequence zero remains the v1
     * compatibility sentinel and bypasses ordering.
     */
    private _acceptIncomingMessage(msg: WSMessage): boolean {
        if (typeof msg.event_id === "string" && msg.event_id.length > 0) {
            if (this._seenInboundEventIds.has(msg.event_id)) return false;
            this._seenInboundEventIds.add(msg.event_id);
            this._seenInboundEventOrder.push(msg.event_id);
            if (
                this._seenInboundEventOrder.length >
                CortexWSClient._MAX_SEEN_EVENT_IDS
            ) {
                const evicted = this._seenInboundEventOrder.shift();
                if (evicted) this._seenInboundEventIds.delete(evicted);
            }
        }

        const sequence = typeof msg.sequence === "number" ? msg.sequence : 0;
        if (sequence <= 0 || !msg.type) return true;
        const last = this._lastInboundSequenceByType.get(msg.type) ?? 0;
        if (sequence <= last) return false;
        this._lastInboundSequenceByType.set(msg.type, sequence);
        return true;
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

    private _handleDisconnect(code?: number, reason?: string): void {
        this._ws = undefined;
        // F6: kill the heartbeat — a stopped socket cannot send pings.
        this._stopHeartbeat();
        this._clearAuthTimer();

        if (code === 1011) {
            // A4: the daemon actively rejected us (auth required /
            // invalid token / slow consumer). Say so — once per reason —
            // instead of flashing "Connected" and silently re-dialling.
            this._warnDaemonClose(code, reason ?? "");
        }

        if (this._connected) {
            this._connected = false;
            this._notifyConnection(false);
        }

        if (!this._intentionalDisconnect) {
            this._scheduleReconnect();
        }
    }

    private _warnDaemonClose(code: number, reason: string): void {
        const key = `${code}:${reason}`;
        if (this._lastCloseWarningReason === key) return;
        this._lastCloseWarningReason = key;
        const detail = reason.length > 0 ? reason : "auth required";
        const hint = /auth|token/i.test(detail)
            ? "Restart the Cortex app so it re-mints the local auth token, then reconnect."
            : "Cortex will keep retrying in the background.";
        try {
            void vscode.window.showWarningMessage(
                `Cortex daemon closed the connection (${code}: ${detail}). ${hint}`,
            );
        } catch {
            console.warn(`[Cortex] daemon closed the connection (${code}: ${detail})`);
        }
    }

    private _scheduleReconnect(): void {
        if (this._reconnectTimer || this._intentionalDisconnect) {
            return;
        }

        // Re-arm the abort controller for this attempt. If ``disconnect()``
        // fires after the timer is set but before it runs, the abort handler
        // clears the pending callback so we don't reconnect against a
        // torn-down client.
        this._reconnectAbort?.abort();
        const controller = new AbortController();
        this._reconnectAbort = controller;

        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = undefined;
            if (controller.signal.aborted) {
                return;
            }
            this.connect();
        }, this._reconnectDelay);

        controller.signal.addEventListener("abort", () => {
            if (this._reconnectTimer) {
                clearTimeout(this._reconnectTimer);
                this._reconnectTimer = undefined;
            }
        });

        // Exponential backoff (3s, 6s, 12s, 24s, 30s max).
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
