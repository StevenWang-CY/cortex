/**
 * Cortex VS Code Extension — WebSocket Client
 *
 * Connects to the Cortex daemon at ws://127.0.0.1:9473.
 * Handles STATE_UPDATE and INTERVENTION_TRIGGER messages from daemon,
 * sends IDENTIFY and USER_ACTION messages to daemon.
 * Auto-reconnects on disconnect with exponential backoff.
 */

import * as vscode from "vscode";
import WebSocket from "ws";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/**
 * Audit Debt-2: read the local capability token the daemon mints at
 * ``<config_dir>/auth.token``. The legitimate VS Code extension can
 * read this file because it runs as the same user as the daemon.
 */
function readCapabilityToken(): string | null {
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
        const tokenFile = path.join(configDir, "auth.token");
        if (!fs.existsSync(tokenFile)) {
            return null;
        }
        const raw = fs.readFileSync(tokenFile, "utf-8").trim();
        return raw.length >= 32 ? raw : null;
    } catch {
        return null;
    }
}

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
type CopilotThrottleHandler = (payload: Record<string, unknown>) => void;
type GenericMessageHandler = (msg: { type: string; payload: Record<string, unknown> }) => void;

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

    constructor(url: string) {
        this._url = url;
    }

    /** Whether the client is currently connected. */
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
                // F6 (Phase-4 audit): start the heartbeat the moment
                // we transition to "open". A stale connection where
                // the TCP socket stayed up but the daemon stopped
                // serving will be detected within ~45s instead of
                // waiting for the next inbound message that never
                // arrives.
                this._startHeartbeat();

                // Audit Debt-2: AUTH first. The daemon refuses every other
                // type until this frame validates; without it the server
                // closes the connection with code 1011 ("auth required")
                // before any STATE_UPDATE reaches us. We send the cached
                // capability token synchronously inline so an
                // unauthenticated socket can't be tricked into emitting
                // any other frame.
                const token = readCapabilityToken();
                if (token && this._ws) {
                    try {
                        this._ws.send(
                            JSON.stringify({
                                type: "AUTH",
                                payload: { auth_token: token },
                                timestamp: Date.now() / 1000,
                                sequence: ++this._sequence,
                            }),
                        );
                    } catch {
                        // Will be retried on reconnect
                    }
                }

                // Identify as VS Code extension
                this._send({
                    type: "IDENTIFY",
                    payload: { client_type: "vscode" },
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
                        this._ws?.send(JSON.stringify(msg));
                    } catch {
                        // Connection torn down mid-flush; remaining
                        // messages will be lost. The reconnect handler
                        // re-enters this path on the next open.
                    }
                }

                vscode.window.setStatusBarMessage(
                    "Cortex: Connected to daemon",
                    3000,
                );
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
     * P0 §3.7: ask the daemon to dispatch a biology break.
     *
     * The action runs entirely on the desktop shell (full-screen Qt
     * overlay) — the editor has nothing to do locally, so we send the
     * ACTION_EXECUTE frame with ``request_dispatch=true`` and a fully
     * populated ``action.metadata`` block mirroring the popup CTA
     * shape. The daemon's ``_handle_user_action`` matches on
     * ``action_type == "take_biology_break"`` and routes to the
     * ``BiologyBreakController`` regardless of source client.
     */
    sendBiologyBreakRequest(
        interventionId: string,
        metadata: {
            duration_seconds: number;
            breathing_pattern: string;
            audio_cue: boolean;
            reason: string;
        },
    ): void {
        const actionId = `bk_${Date.now()}`;
        const mins = Math.max(1, Math.round(metadata.duration_seconds / 60));
        this._send({
            type: "ACTION_EXECUTE",
            payload: {
                intervention_id: interventionId,
                action_id: actionId,
                action_type: "take_biology_break",
                request_dispatch: true,
                action: {
                    action_id: actionId,
                    action_type: "take_biology_break",
                    label: `Take ${mins} min`,
                    target: "",
                    metadata,
                },
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
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

    /**
     * P0 §3.11: send a QUIET_MODE_TOGGLE for the kind specified.
     * Kinds: "snooze_15" | "quiet_session" | "pause" | "off".
     */
    sendQuietModeToggle(
        kind: "snooze_15" | "quiet_session" | "pause" | "off",
        durationMinutes?: number,
    ): void {
        this._send({
            type: "QUIET_MODE_TOGGLE",
            payload: {
                kind,
                duration_minutes:
                    typeof durationMinutes === "number"
                        ? durationMinutes
                        : null,
                source: "vscode",
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
    }

    /**
     * Notify the daemon that an intervention was applied (or restored).
     *
     * B.2: the daemon's in-process executor runs an
     * ``_OptimisticInterventionAdapter`` that assumes success for every
     * action; the real workspace effects happen here in the VS Code
     * extension (folds) and in the browser extension (tabs, overlay).
     * The ack lets the daemon overwrite ``Mutation.success`` with the
     * actual client outcome, so ``InterventionOutcome.workspace_restored``
     * is truthful instead of theatrical.
     *
     * ``phase`` values:
     *   - ``"apply"``   — the UIPlan apply ack (folds, overlay). Resolves
     *     the daemon's pending ``await_apply_confirmation`` future.
     *   - ``"restore"`` — the unfold/restore ack.
     *   - ``"execute_action"`` — a discrete EXECUTE_ACTION catalog item
     *     (e.g. ``resume_last_active_file``) the editor ran directly. A
     *     distinct phase so the daemon's ``(intervention_id, phase)`` dedup
     *     key does NOT collapse this ack into the UIPlan ``"apply"`` ack for
     *     the same intervention; the resume outcome is recorded on its own.
     */
    sendInterventionApplied(
        interventionId: string,
        phase: "apply" | "restore" | "execute_action",
        success: boolean,
        appliedActions: string[],
        errors: string[],
    ): void {
        this._send({
            type: "INTERVENTION_APPLIED",
            payload: {
                intervention_id: interventionId,
                phase,
                success,
                applied_actions: appliedActions,
                errors,
            },
            timestamp: Date.now() / 1000,
            sequence: ++this._sequence,
        });
    }

    // --- Internal ---

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
        // F6: kill the heartbeat — a stopped socket cannot send pings.
        this._stopHeartbeat();

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
