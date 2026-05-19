"""
API Gateway — WebSocket Server

WebSocket server on ws://localhost:9473 for real-time bidirectional
communication between the Cortex daemon and client extensions
(VS Code, Chrome, desktop shell).

Message types (JSON-over-WebSocket):
- STATE_UPDATE (daemon → extension): every 500ms, state + confidence + features
- INTERVENTION_TRIGGER (daemon → extension): intervention type + LLM payload + ID
- USER_ACTION (extension → daemon): dismissed / engaged / snoozed + intervention ID
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from cortex.libs.auth import verify_token
from cortex.libs.config.settings import APIConfig
from cortex.libs.logging.correlation import correlation_scope, get_correlation_id
from cortex.libs.logging.structured import EventType
from cortex.libs.schemas.intervention import InterventionPlan
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.schemas.ws_message import WSMessage as _PydanticWSMessage
from cortex.libs.schemas.ws_message_types import MessageType

logger = logging.getLogger(__name__)


def _auth_ok_frame() -> str:
    """Serialise a minimal ``AUTH_OK`` reply frame (audit Debt-2).

    The Pydantic ``WSMessage`` would also work but is slightly heavier
    than needed for a confirmation that carries no payload data. We hand
    the JSON to ``websocket.send`` directly. ``type`` is the canonical
    ``MessageType.AUTH_OK.value`` so the client side narrows it via the
    same generated TypeScript union.
    """
    return json.dumps({
        "type": MessageType.AUTH_OK.value,
        "payload": {},
        "timestamp": time.monotonic(),
        "sequence": 0,
        "correlation_id": None,
        "target_client_types": None,
        "source_client_type": "daemon",
    })


def _serialize_timestamp(ts: Any) -> Any:
    """``StateEstimate.timestamp`` is typed loosely (float monotonic or
    datetime depending on producer). Return an ISO string for datetimes
    and the raw value for everything else so JSON serialisation works
    consistently across both shapes."""
    if ts is None:
        return None
    iso = getattr(ts, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return ts


@dataclass
class WebSocketClient:
    """Represents a connected WebSocket client.

    ``authenticated`` is False until the client sends a valid ``AUTH``
    frame as its first message (audit Debt-2). Until then the server
    refuses every other ``type`` and closes the socket with code 1011 +
    ``EventType.AUTH_REJECTED``. Setting the flag is intentionally
    one-way per connection — there is no way for a peer to demote
    itself back to ``pending_auth`` mid-session.
    """

    client_id: str
    websocket: Any  # websockets.WebSocketServerProtocol
    connected_at: float = field(default_factory=time.monotonic)
    client_type: str = "unknown"  # "vscode", "chrome", "desktop", "unknown"
    last_message_at: float = 0.0
    authenticated: bool = False


# ─── WSMessage: Pydantic source of truth (Debt-1 closure, Commit 2) ───
#
# ``WSMessage`` is now an alias for the Pydantic model in
# ``cortex.libs.schemas.ws_message``. The model is what the schema
# codegen pipeline (``cortex/scripts/generate_ts_schemas.py``) emits to
# TypeScript, so the extension consumes a generated type rather than a
# hand-written interface (audit Debt-1, closes F45 once the dispatch
# sites in this file route through ``MessageType``).
#
# The legacy dataclass below is preserved unchanged for one release per
# the Debt-1 migration plan; it round-trips structurally with the
# Pydantic model (covered by ``test_ws_message_schema.py``). New code
# should construct ``WSMessage`` directly, which now means the Pydantic
# class.
WSMessage = _PydanticWSMessage


@dataclass
class WSMessageLegacy:
    """Legacy dataclass shape preserved for one-release backwards compat.

    Identical field layout and serialisation contract as the previous
    dataclass-based ``WSMessage``. Kept so external consumers can be
    migrated incrementally; daemon-internal call sites already use the
    Pydantic ``WSMessage`` above.

    Deprecated: this class will be removed in the release after the one
    that ships the codegen pipeline.
    """

    type: str
    payload: dict[str, Any]
    timestamp: float = field(default_factory=time.monotonic)
    sequence: int = 0
    correlation_id: str | None = None
    target_client_types: list[str] | None = None
    source_client_type: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "target_client_types": self.target_client_types,
            "source_client_type": self.source_client_type,
        })

    @classmethod
    def from_json(cls, data: str) -> WSMessageLegacy:
        parsed = json.loads(data)
        return cls(
            type=parsed.get("type", "UNKNOWN"),
            payload=parsed.get("payload", {}),
            timestamp=parsed.get("timestamp", time.monotonic()),
            sequence=parsed.get("sequence", 0),
            correlation_id=parsed.get("correlation_id"),
            target_client_types=parsed.get("target_client_types"),
            source_client_type=parsed.get("source_client_type"),
        )

    def to_pydantic(self) -> _PydanticWSMessage:
        """Convert this dataclass to the canonical Pydantic ``WSMessage``."""
        return _PydanticWSMessage.model_validate(
            {
                "type": self.type,
                "payload": self.payload,
                "timestamp": self.timestamp,
                "sequence": self.sequence,
                "correlation_id": self.correlation_id,
                "target_client_types": self.target_client_types,
                "source_client_type": self.source_client_type,
            }
        )


class WebSocketServer:
    """
    WebSocket server for Cortex daemon ↔ extension communication.

    Manages client connections, broadcasts state updates every 500ms,
    dispatches intervention triggers, and receives user actions.

    Usage:
        server = WebSocketServer()
        await server.start()
        # ... later ...
        await server.broadcast_state(estimate)
        await server.send_intervention(plan)
        await server.stop()
    """

    def __init__(self, config: APIConfig | None = None) -> None:
        self._config = config or APIConfig()
        self._clients: dict[str, WebSocketClient] = {}
        self._server: Any = None  # websockets server
        self._running = False
        self._sequence: int = 0

        # Callbacks for received messages
        self._user_action_callback: Any = None
        self._settings_callback: Any = None
        self._shutdown_callback: Any = None
        self._activity_sync_callback: Any = None
        self._tab_relevance_feedback_callback: Any = None
        self._leetcode_context_callback: Any = None
        self._intervention_applied_callback: Any = None
        # G1 (audit-prod): fired on IDENTIFY + on identified-client disconnect.
        self._client_identified_callback: Any = None

        # Latest state for new connections
        self._latest_state: StateEstimate | None = None
        self._pending_context_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # F23: track which client_id owns each pending correlation_id so we
        # can cancel its futures on disconnect (otherwise the requesting
        # caller hangs until the per-call timeout). One client_id → many
        # correlation_ids; remove the cid from the set as soon as its
        # future resolves so the set never grows past in-flight requests.
        self._pending_cids_by_client: dict[str, set[str]] = {}
        # F04: monotonic settings version last applied. Older payloads are
        # rejected (stale double-click that arrived behind a newer apply).
        self._last_settings_version: int = 0

        # F16-srv: track the cid the daemon stamped on the most recent
        # outbound INTERVENTION_TRIGGER for each intervention_id. A
        # USER_ACTION ACK whose cid does not match is treated as stale
        # (the active plan was superseded on the extension side) and is
        # logged + ignored rather than poisoning the dismissal model.
        self._active_intervention_cid: dict[str, str] = {}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def connected_clients(self) -> list[str]:
        return list(self._clients.keys())

    def connected_client_types(self) -> list[str]:
        """G1 (audit-prod): return the deduped list of IDENTIFY-ed client
        types currently connected (e.g. ``["chrome", "vscode"]``). Each
        type appears exactly once even if multiple browser tabs or VS
        Code windows are connected. ``"unknown"`` and ``"desktop"`` are
        filtered out so the dashboard doesn't see itself.
        """
        seen: set[str] = set()
        for client in self._clients.values():
            ct = client.client_type
            if ct and ct not in ("unknown", "desktop"):
                seen.add(ct)
        return sorted(seen)

    def set_user_action_callback(self, callback: Any) -> None:
        """Set callback for USER_ACTION messages from extensions."""
        self._user_action_callback = callback

    def set_settings_callback(self, callback: Any) -> None:
        """Set callback for SETTINGS_SYNC messages from clients."""
        self._settings_callback = callback

    def set_shutdown_callback(self, callback: Any) -> None:
        """Set callback for SHUTDOWN messages from clients."""
        self._shutdown_callback = callback

    def set_activity_sync_callback(self, callback: Any) -> None:
        """Set callback for ACTIVITY_SYNC messages from browser extension."""
        self._activity_sync_callback = callback

    def set_tab_relevance_feedback_callback(self, callback: Any) -> None:
        """Set callback for TAB_RELEVANCE_FEEDBACK messages from browser extension."""
        self._tab_relevance_feedback_callback = callback

    def set_leetcode_context_callback(self, callback: Any) -> None:
        """Set callback for LEETCODE_CONTEXT_UPDATE messages from browser extension."""
        self._leetcode_context_callback = callback

    def set_intervention_applied_callback(self, callback: Any) -> None:
        """Set callback for ``INTERVENTION_APPLIED`` ack messages.

        Clients send this after attempting to apply or restore an
        intervention, with ``{intervention_id, success, applied_actions,
        errors, phase: "apply"|"restore"}``. The daemon uses the ack to
        replace its optimistic mutation tracking with extension-confirmed
        state, so ``InterventionOutcome.workspace_restored`` reflects the
        real world rather than the assumed default.
        """
        self._intervention_applied_callback = callback

    def set_client_identified_callback(self, callback: Any) -> None:
        """Audit-prod fix (G1): callback fired when a client IDENTIFY frame
        is received OR when a previously-identified client disconnects.

        Signature: ``callback(client_type: str, connected: bool)``.

        Used by the desktop shell to update the Chrome / Edge / Editor
        connection dots on the dashboard. The dot only changes color
        when an IDENTIFY succeeds — the WS ``connected`` flag alone
        is not sufficient because IDENTIFY can fail (wrong client_type
        literal, auth pending, etc.). The disconnect case re-grays the
        dot so the user can see when the extension goes away.
        """
        self._client_identified_callback = callback

    async def start(self) -> bool:
        """
        Start the WebSocket server.

        Returns:
            True if started successfully, False on error.
        """
        try:
            import websockets

            self._server = await websockets.serve(
                self._handle_client,
                self._config.host,
                self._config.ws_port,
            )
            self._running = True
            logger.info(
                f"WebSocket server started on "
                f"ws://{self._config.host}:{self._config.ws_port}"
            )
            return True
        except OSError as e:
            logger.error(f"Failed to start WebSocket server: {e}")
            return False
        except ImportError:
            logger.error("websockets package not installed")
            return False

    async def stop(self) -> None:
        """Stop the WebSocket server and disconnect all clients."""
        self._running = False

        # Close all client connections
        for client in list(self._clients.values()):
            try:
                await client.websocket.close()
            except Exception:
                pass

        self._clients.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        logger.info("WebSocket server stopped")

    async def _handle_client(self, websocket: Any) -> None:
        """Handle a new WebSocket client connection.

        Debt-2 (audit): no outbound frames before the client AUTHs. The
        legacy ``send latest state on connect`` happened unconditionally
        — that leaked the daemon's current STATE_UPDATE to any localhost
        origin that opened a socket. We now defer that send until
        :meth:`_handle_auth` flips ``client.authenticated``.
        """
        client_id = f"client_{id(websocket)}"
        client = WebSocketClient(
            client_id=client_id,
            websocket=websocket,
        )
        self._clients[client_id] = client
        logger.info(f"Client connected: {client_id}")

        try:
            async for raw_message in websocket:
                await self._process_message(client, raw_message)
        except Exception as e:
            logger.debug(f"Client {client_id} disconnected: {e}")
        finally:
            self._clients.pop(client_id, None)
            # F23: cancel any in-flight context-request futures associated
            # with this client so the calling coroutine returns promptly
            # rather than waiting for the per-call timeout.
            self._cancel_pending_for_client(client_id)
            # G1 (audit-prod): if this client previously IDENTIFY-ed, tell
            # the listener it's gone so the dashboard dot re-grays.
            cb = self._client_identified_callback
            if (
                cb is not None
                and client.client_type
                and client.client_type != "unknown"
            ):
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(client.client_type, False)
                    else:
                        cb(client.client_type, False)
                except Exception:
                    logger.debug(
                        "client_identified callback raised (disconnect)",
                        exc_info=True,
                    )
            logger.info(f"Client disconnected: {client_id}")

    async def _process_message(
        self, client: WebSocketClient, raw: str,
    ) -> None:
        """Process an incoming message from a client.

        ``WSMessage`` is the Pydantic model; ``ValidationError`` is the
        new failure mode for unknown ``type`` literals (Debt-1 closure,
        F45). We log + drop the same way the legacy dataclass dropped
        on ``JSONDecodeError`` — clients see no behaviour change, but
        the daemon now refuses to dispatch on types not in the
        ``MessageType`` catalog.
        """
        try:
            msg = WSMessage.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValidationError) as e:
            logger.warning(f"Invalid message from {client.client_id}: {e}")
            return

        client.last_message_at = time.monotonic()

        # F19: every incoming message enters a correlation scope. If the
        # client supplied a correlation id we honour it; otherwise we mint
        # one. The scope ensures every log line emitted by the handlers
        # below — and by any downstream service they call (LLM planner,
        # state engine) — carries the same id.
        with correlation_scope(msg.correlation_id) as cid:
            if msg.correlation_id is None:
                msg.correlation_id = cid
            await self._dispatch_message(client, msg)

    async def _dispatch_message(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """Route a message to the matching handler. Always runs inside a
        correlation scope established by :meth:`_process_message`.
        Type comparison uses ``MessageType`` (Debt-1 codegen) so a typo
        in the dispatch table is a compile-time error instead of a
        silently-unhandled message.

        Debt-2 (audit): the first frame on every connection MUST be
        ``AUTH``. Until ``client.authenticated`` flips True, every other
        ``type`` triggers a close(code=1011, reason="auth required") and
        emits ``EventType.AUTH_REJECTED``. ``AUTH`` itself is a no-op
        once the client is already authenticated (idempotent — a replay
        does not cycle the connection).
        """
        # ─── Debt-2 AUTH-first gate ─────────────────────────────────
        if msg.type == MessageType.AUTH.value:
            await self._handle_auth(client, msg)
            return
        if not client.authenticated:
            logger.warning(
                "%s reason=pre_auth_message type=%s client=%s cid=%s",
                EventType.AUTH_REJECTED.value,
                msg.type,
                client.client_id,
                msg.correlation_id or "-",
            )
            try:
                await client.websocket.close(
                    code=1011, reason="auth required",
                )
            except Exception:
                logger.debug(
                    "close(auth required) on already-dead socket %s",
                    client.client_id,
                    exc_info=True,
                )
            return

        if msg.type == MessageType.USER_ACTION.value:
            await self._handle_user_action(client, msg)
        elif msg.type == MessageType.ACTION_EXECUTE.value:
            await self._handle_user_action(client, msg)
        elif msg.type == MessageType.USER_RATING.value:
            # Route user ratings through the same callback used for user actions.
            await self._handle_user_action(client, msg)
        elif msg.type == MessageType.IDENTIFY.value:
            # Client identifying its type. Audit-prod fix (P1-A): validate
            # against an explicit allowlist. The catalog of legitimate
            # client types is small and stable; an unknown literal becomes
            # ``"unknown"`` so it is filtered from ``connected_client_types``
            # and never reaches the dashboard's dot map.
            _ALLOWED_CLIENT_TYPES = frozenset({
                "chrome", "edge", "vscode", "desktop",
            })
            requested = msg.payload.get("client_type")
            if isinstance(requested, str) and requested in _ALLOWED_CLIENT_TYPES:
                client.client_type = requested
            else:
                logger.warning(
                    "IDENTIFY: rejecting unknown client_type=%r from %s",
                    requested,
                    client.client_id,
                )
                client.client_type = "unknown"
            logger.info(
                f"Client {client.client_id} identified as {client.client_type}"
            )
            # G1 (audit-prod): notify any registered listener (the desktop
            # shell uses this to update the Chrome / Edge / Editor dots).
            cb = self._client_identified_callback
            if cb is not None and client.client_type and client.client_type != "unknown":
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(client.client_type, True)
                    else:
                        cb(client.client_type, True)
                except Exception:
                    logger.debug(
                        "client_identified callback raised (connect)",
                        exc_info=True,
                    )
        elif msg.type == MessageType.CONTEXT_RESPONSE.value:
            self._handle_context_response(msg)
        elif msg.type == MessageType.SETTINGS_SYNC.value:
            await self._handle_settings_sync(client, msg)
        elif msg.type == MessageType.ACTIVITY_SYNC.value:
            await self._handle_activity_sync(client, msg)
        elif msg.type == MessageType.TAB_RELEVANCE_FEEDBACK.value:
            await self._handle_tab_relevance_feedback(client, msg)
        elif msg.type == MessageType.LEETCODE_CONTEXT_UPDATE.value:
            await self._handle_leetcode_context_update(client, msg)
        elif msg.type == MessageType.INTERVENTION_APPLIED.value:
            await self._handle_intervention_applied(client, msg)
        elif msg.type == MessageType.SHUTDOWN.value:
            # F07: require the capability token before honouring a remote
            # SHUTDOWN. Without this gate any localhost origin (malicious
            # webpage in another tab, hostile extension) could reach this
            # path and kill the daemon. The token lives in a mode-0600
            # file legitimate clients (desktop_shell, native_host) can
            # read; cross-origin web pages cannot.
            presented = (msg.payload or {}).get("auth_token")
            if not verify_token(presented):
                logger.warning(
                    "Rejected SHUTDOWN from %s: missing or invalid auth token",
                    client.client_id,
                )
                return
            logger.info("Shutdown requested via WebSocket from %s", client.client_id)
            if self._shutdown_callback is not None:
                try:
                    if asyncio.iscoroutinefunction(self._shutdown_callback):
                        await self._shutdown_callback()
                    else:
                        self._shutdown_callback()
                except Exception as exc:
                    logger.error("Shutdown callback error: %s", exc)
        else:
            logger.debug(f"Unknown message type from {client.client_id}: {msg.type}")

    async def _handle_auth(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """Validate the ``AUTH`` handshake frame (audit Debt-2).

        On success, flips ``client.authenticated`` to True, replies with
        an ``AUTH_OK`` frame so the peer knows the channel is open, and
        — to preserve the legacy "new connection sees the latest state
        on attach" behaviour — sends a fresh STATE_UPDATE if one is
        cached. On failure (no token, wrong token, malformed payload)
        logs ``AUTH_REJECTED`` and closes the socket with code 1011.

        Replay-safe: a second ``AUTH`` on an already-authenticated
        connection short-circuits to a re-ACK with no other side effect.
        That keeps clients that retry on transient WS errors from
        bouncing themselves out of a healthy session.
        """
        if client.authenticated:
            # Idempotent replay — just re-ACK so the peer's promise resolves.
            try:
                await client.websocket.send(_auth_ok_frame())
            except Exception:
                logger.debug(
                    "AUTH_OK replay send failed for %s",
                    client.client_id,
                    exc_info=True,
                )
            return

        presented = (msg.payload or {}).get("auth_token")
        if not isinstance(presented, str) or not verify_token(presented):
            reason = "missing" if not presented else "invalid"
            logger.warning(
                "%s reason=%s_token client=%s cid=%s",
                EventType.AUTH_REJECTED.value,
                reason,
                client.client_id,
                msg.correlation_id or "-",
            )
            try:
                await client.websocket.close(
                    code=1011, reason="invalid auth token",
                )
            except Exception:
                logger.debug(
                    "close(invalid auth) on already-dead socket %s",
                    client.client_id,
                    exc_info=True,
                )
            return

        client.authenticated = True
        try:
            await client.websocket.send(_auth_ok_frame())
        except Exception:
            logger.debug(
                "AUTH_OK send failed for %s",
                client.client_id,
                exc_info=True,
            )
            return

        # Debt-2: legacy behaviour was to push the latest state on every
        # new connection. Defer that send until after AUTH succeeds so
        # an unauthenticated peer never sees STATE_UPDATE.
        if self._latest_state is not None:
            try:
                state_msg = self._make_state_update(self._latest_state)
                await client.websocket.send(state_msg.to_json())
            except Exception:
                logger.debug(
                    "post-AUTH state push failed for %s",
                    client.client_id,
                    exc_info=True,
                )

    async def _handle_user_action(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """Handle USER_ACTION message from extension.

        F16-srv: if the extension's cid does not match the cid the daemon
        stamped on the most recent INTERVENTION_TRIGGER for this
        intervention_id, the ACK belongs to a plan that was superseded
        on the extension side (atomic-swap by latest cid). Log a warning
        and drop the message without invoking the callback so the
        dismissal model is not poisoned by stale ACKs.
        """
        action = msg.payload.get("action")
        intervention_id = msg.payload.get("intervention_id")
        incoming_cid = msg.correlation_id

        if isinstance(intervention_id, str) and intervention_id:
            active_cid = self._active_intervention_cid.get(intervention_id)
            # Only enforce when both sides supplied a cid. A missing
            # incoming cid is treated as a legacy client and honoured;
            # a missing active cid means we never emitted a trigger for
            # this intervention_id (e.g. on test fixtures), also honoured.
            if active_cid and incoming_cid and active_cid != incoming_cid:
                logger.warning(
                    "Dropping stale USER_ACTION action=%s intervention_id=%s "
                    "cid=%s active_cid=%s client=%s",
                    action,
                    intervention_id,
                    incoming_cid,
                    active_cid,
                    client.client_id,
                )
                return

        logger.info(
            f"User action from {client.client_id}: {action} "
            f"(intervention: {intervention_id}, cid: {incoming_cid})"
        )

        if self._user_action_callback is not None:
            try:
                # Audit-prod fix (P1-B confused-deputy): stamp the
                # ``source_client_type`` onto the payload before invoking
                # the callback. The daemon's request-dispatch branch
                # (runtime_daemon._handle_user_action) reads this field
                # and rejects ACTION_DISPATCH requests from anyone other
                # than the desktop shell — otherwise a compromised
                # extension could trigger arbitrary action execution on
                # peer browser clients via the daemon broadcast bus.
                # Underscore prefix marks it as wire-implementation, not
                # user data.
                payload_with_source = dict(msg.payload or {})
                payload_with_source["_source_client_type"] = client.client_type
                if asyncio.iscoroutinefunction(self._user_action_callback):
                    await self._user_action_callback(payload_with_source)
                else:
                    self._user_action_callback(payload_with_source)
            except Exception as e:
                logger.error(f"User action callback error: {e}")

    def _handle_context_response(self, msg: WSMessage) -> None:
        """Resolve a pending context request."""
        correlation_id = msg.correlation_id
        if not correlation_id:
            return
        future = self._pending_context_requests.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(msg.payload)
        # F23: prune the per-client cid tracking so the set only ever
        # contains in-flight cids. If the response races a disconnect we
        # may find no owner — that's fine, the disconnect path already
        # cancelled the future and we'd be a no-op anyway.
        for client_id, owned in list(self._pending_cids_by_client.items()):
            if correlation_id in owned:
                self._drop_pending_cid(client_id, correlation_id)
                break

    async def _handle_settings_sync(self, client: WebSocketClient, msg: WSMessage) -> None:
        """Forward settings updates to the daemon.

        F04: payloads with a ``settings_version`` field are checked against
        the last applied version. Older versions (a stale double-click that
        arrived behind a newer apply) are dropped with a warning so a
        rapid-fire user cannot accidentally rewind their settings.
        """
        if self._settings_callback is None:
            return
        version = msg.payload.get("settings_version")
        if isinstance(version, int):
            if version <= self._last_settings_version:
                logger.warning(
                    "Dropping stale settings sync from %s: version=%d "
                    "(last applied=%d)",
                    client.client_id,
                    version,
                    self._last_settings_version,
                )
                return
            self._last_settings_version = version
        try:
            if asyncio.iscoroutinefunction(self._settings_callback):
                await self._settings_callback(msg.payload)
            else:
                self._settings_callback(msg.payload)
        except Exception as exc:
            logger.error("Settings callback error from %s: %s", client.client_id, exc)

    async def _handle_activity_sync(self, client: WebSocketClient, msg: WSMessage) -> None:
        """Forward activity sync to the daemon for aggregation."""
        callback = getattr(self, "_activity_sync_callback", None)
        if callback is None:
            return
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(msg.payload)
            else:
                callback(msg.payload)
        except Exception as exc:
            logger.error("Activity sync callback error from %s: %s", client.client_id, exc)

    async def _handle_tab_relevance_feedback(self, client: WebSocketClient, msg: WSMessage) -> None:
        """Forward per-tab relevance feedback to the daemon."""
        callback = self._tab_relevance_feedback_callback
        if callback is None:
            return
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(msg.payload)
            else:
                callback(msg.payload)
        except Exception as exc:
            logger.error("Tab relevance feedback error from %s: %s", client.client_id, exc)

    async def _handle_leetcode_context_update(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """Forward LeetCode DOM/code telemetry snapshots to the daemon."""
        callback = self._leetcode_context_callback
        if callback is None:
            return
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(msg.payload)
            else:
                callback(msg.payload)
        except Exception as exc:
            logger.error("LeetCode context callback error from %s: %s", client.client_id, exc)

    async def _handle_intervention_applied(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """Forward an extension-side INTERVENTION_APPLIED ack to the daemon.

        Payload shape::

            {
                "intervention_id": str,
                "phase": "apply" | "restore",
                "success": bool,
                "applied_actions": list[str],
                "errors": list[str],
            }

        The daemon uses this to overwrite the optimistic ``Mutation.success``
        from ``_OptimisticInterventionAdapter`` with the actual extension
        result, so ``InterventionOutcome.workspace_restored`` is truthful.
        """
        callback = self._intervention_applied_callback
        if callback is None:
            return
        try:
            payload = dict(msg.payload or {})
            payload.setdefault("source_client_type", client.client_type)
            if asyncio.iscoroutinefunction(callback):
                await callback(payload)
            else:
                callback(payload)
        except Exception as exc:
            logger.error(
                "intervention_applied callback error from %s: %s",
                client.client_id,
                exc,
            )

    async def broadcast_state(
        self,
        estimate: StateEstimate,
        biometrics: dict[str, float | None] | None = None,
    ) -> int:
        """
        Broadcast STATE_UPDATE to all connected clients.

        Args:
            estimate: Current state estimate.
            biometrics: Optional raw biometric values for ambient UI.

        Returns:
            Number of clients successfully sent to.
        """
        self._latest_state = estimate
        msg = self._make_state_update(estimate, biometrics)
        return await self._broadcast(msg)

    async def send_intervention(self, plan: InterventionPlan) -> int:
        """
        Send INTERVENTION_TRIGGER to all connected clients.

        Args:
            plan: Intervention plan from LLM.

        Returns:
            Number of clients successfully sent to.
        """
        msg = self._make_intervention_trigger(plan)
        return await self._broadcast(msg)

    async def send_restore(self, intervention_id: str, *, user_action: str) -> int:
        """Broadcast an explicit restore event to all clients."""
        self._sequence += 1
        return await self._broadcast(
            WSMessage(
                type=MessageType.INTERVENTION_RESTORE,
                payload={
                    "intervention_id": intervention_id,
                    "user_action": user_action,
                },
                sequence=self._sequence,
            )
        )

    async def broadcast_settings(self, settings: dict[str, Any]) -> int:
        """Broadcast settings to all clients."""
        self._sequence += 1
        return await self._broadcast(
            WSMessage(
                type=MessageType.SETTINGS_SYNC,
                payload=settings,
                sequence=self._sequence,
            )
        )

    async def send_message(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        target_client_types: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> int:
        """Broadcast an arbitrary typed message to connected clients."""
        self._sequence += 1
        return await self._broadcast(
            WSMessage(
                type=message_type,
                payload=payload,
                sequence=self._sequence,
                correlation_id=correlation_id,
                target_client_types=target_client_types,
                source_client_type="daemon",
            )
        )

    async def request_context(
        self,
        client_type: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Request context from the first connected client of a given type."""
        target = next(
            (client for client in self._clients.values() if client.client_type == client_type),
            None,
        )
        if target is None:
            return {}

        self._sequence += 1
        correlation_id = f"ctx_{client_type}_{self._sequence}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_context_requests[correlation_id] = future
        # F23: associate the cid with the requesting client so disconnect
        # can cancel every in-flight future for that client.
        self._pending_cids_by_client.setdefault(target.client_id, set()).add(
            correlation_id,
        )
        message = WSMessage(
            type=MessageType.CONTEXT_REQUEST,
            payload={},
            sequence=self._sequence,
            correlation_id=correlation_id,
            target_client_types=[client_type],
            source_client_type="daemon",
        )
        try:
            await target.websocket.send(message.to_json())
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending_context_requests.pop(correlation_id, None)
            self._drop_pending_cid(target.client_id, correlation_id)
            logger.debug("Context request to %s timed out", client_type)
            return {}
        except asyncio.CancelledError:
            # F23: client disconnected and the disconnect handler cancelled
            # our future. Treat as "no context" rather than propagating
            # the cancellation up into context-loop code that isn't ready
            # for it.
            self._pending_context_requests.pop(correlation_id, None)
            self._drop_pending_cid(target.client_id, correlation_id)
            logger.debug(
                "Context request %s cancelled (client disconnected)",
                correlation_id,
            )
            return {}
        except Exception:
            self._pending_context_requests.pop(correlation_id, None)
            self._drop_pending_cid(target.client_id, correlation_id)
            logger.exception("Context request to %s failed", client_type)
            return {}

    def _drop_pending_cid(self, client_id: str, correlation_id: str) -> None:
        """F23: remove a correlation_id from the per-client tracking set
        once its future has resolved (success / timeout / cancel)."""
        owned = self._pending_cids_by_client.get(client_id)
        if not owned:
            return
        owned.discard(correlation_id)
        if not owned:
            self._pending_cids_by_client.pop(client_id, None)

    def _cancel_pending_for_client(self, client_id: str) -> int:
        """F23: cancel every pending correlation-id future associated with
        ``client_id``. Returns the number of futures cancelled. Called
        from ``_handle_client`` when the client disconnects so the
        requesting coroutine does not hang on a dead client."""
        owned = self._pending_cids_by_client.pop(client_id, None)
        if not owned:
            return 0
        cancelled = 0
        for cid in list(owned):
            future = self._pending_context_requests.pop(cid, None)
            if future is not None and not future.done():
                future.cancel()
                cancelled += 1
        if cancelled:
            logger.debug(
                "Cancelled %d pending correlation futures for %s",
                cancelled,
                client_id,
            )
        return cancelled

    # audit Phase-I: per-send timeout (s) and total broadcast budget (s).
    # The per-send timeout is bumped from 1 s → 2 s so a transient
    # network blip on one client does not get classified as a dead
    # consumer; the hard total budget caps how long any single broadcast
    # can block the loop. A broadcast that exceeds the budget logs a
    # ``WS_BROADCAST_SLOW`` event and counts the clients that did not
    # finish in time as dropped frames for that broadcast — they are
    # not disconnected on the first slow broadcast (only if their
    # individual per-send timeout actually fires).
    _BROADCAST_PER_CLIENT_TIMEOUT_S: float = 2.0
    _BROADCAST_BUDGET_S: float = 0.1

    async def _broadcast(self, msg: WSMessage) -> int:
        """Broadcast a message to all connected clients.

        Combines two correctness/perf wins:
        - F19: stamp outgoing messages with the caller's active correlation
          id so receivers can echo it back on USER_ACTION /
          INTERVENTION_APPLIED replies and the intent-to-effect chain stays
          traceable.
        - F22: when a per-send call times out, the client is presumed a
          "slow consumer" — emit an explicit ``close(code=1011, reason)``
          so the browser-side auto-reconnect sees a clean close rather
          than an EPIPE on the next send, and record a
          ``WS_CLIENT_DISCONNECTED`` event with the client id + reason.
        - audit Phase-I: replace the serial ``for client: await send(...)``
          with ``asyncio.wait`` under a hard total budget. Each send
          runs as an independent Task so a four-client broadcast costs
          ~max(client_latencies) instead of ~sum(client_latencies); a
          slow client that misses the budget logs a ``WS_BROADCAST_SLOW``
          metric but is NOT disconnected on the first miss (only if its
          per-send timeout actually fires).
        """
        if not self._clients:
            return 0

        # F19: stamp the outgoing message with the caller's correlation id
        # so receivers can echo it back on USER_ACTION / INTERVENTION_APPLIED
        # replies and the full intent-to-effect chain stays traceable.
        if msg.correlation_id is None:
            msg.correlation_id = get_correlation_id()

        payload = msg.to_json()
        target_types = set(msg.target_client_types or [])
        # Debt-2 (audit): never broadcast to a peer that has not
        # completed the AUTH handshake. A connection in ``pending_auth``
        # should not see STATE_UPDATE / INTERVENTION frames; the gate in
        # ``_dispatch_message`` already drops non-AUTH inbound frames
        # from such peers, but a connect-and-listen-only client would
        # still receive broadcasts without this filter.
        targets = [
            (client_id, client)
            for client_id, client in self._clients.items()
            if (not target_types or client.client_type in target_types)
            and client.authenticated
        ]
        if not targets:
            return 0

        async def _send_one(client: WebSocketClient) -> str | None:
            """Return ``None`` on success or a disconnect reason string."""
            try:
                await asyncio.wait_for(
                    client.websocket.send(payload),
                    timeout=self._BROADCAST_PER_CLIENT_TIMEOUT_S,
                )
                return None
            except TimeoutError:
                return "slow consumer"
            except Exception:
                return "send error"

        # audit Phase-I: parallel-gather under a hard total budget. Each
        # send is wrapped in its own Task so when the budget elapses we
        # cancel only the unfinished tasks; already-completed tasks keep
        # their results (a plain ``asyncio.gather`` would cancel every
        # inner coroutine when the wrapper is cancelled).
        broadcast_start = time.monotonic()
        send_tasks = [
            asyncio.create_task(_send_one(client)) for _, client in targets
        ]
        done, pending = await asyncio.wait(
            send_tasks, timeout=self._BROADCAST_BUDGET_S,
        )
        budget_exceeded = bool(pending)
        for task in pending:
            task.cancel()
        # Drain cancellations so they don't surface as "task was destroyed
        # but pending" warnings on a busy event loop.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        results: list[Any] = []
        for task in send_tasks:
            if task in pending:
                results.append(asyncio.CancelledError())
            else:
                try:
                    results.append(task.result())
                except BaseException as exc:  # noqa: BLE001
                    results.append(exc)

        elapsed_s = time.monotonic() - broadcast_start
        sent = 0
        # F22: track (client_id, reason) so the post-loop close path can
        # emit the right reason string per disconnect.
        dead_clients: list[tuple[str, str]] = []
        slow_clients: list[str] = []
        for (client_id, _client), outcome in zip(targets, results, strict=False):
            if outcome is None:
                sent += 1
            elif isinstance(outcome, asyncio.CancelledError):
                # Did not finish inside the budget; not a disconnect.
                slow_clients.append(client_id)
            elif isinstance(outcome, str):
                dead_clients.append((client_id, outcome))
            else:  # unexpected exception captured by gather
                dead_clients.append((client_id, "send error"))

        if budget_exceeded or slow_clients:
            try:
                from cortex.libs.logging.structured import EventType, get_logger

                get_logger(__name__).warning(
                    "ws_broadcast_slow",
                    event_type=EventType.WS_BROADCAST_SLOW.value,
                    elapsed_ms=int(elapsed_s * 1000),
                    budget_ms=int(self._BROADCAST_BUDGET_S * 1000),
                    client_count=len(targets),
                    dropped_for_budget=len(slow_clients),
                )
            except Exception:
                # Telemetry must never break the hot path.
                logger.debug("ws_broadcast_slow log failed", exc_info=True)

        # F22: clean up dead connections with explicit close + reason.
        for client_id, reason in dead_clients:
            client = self._clients.pop(client_id, None)
            if client is not None:
                await self._close_slow_consumer(client, reason)
            logger.debug("Removed dead client: %s (%s)", client_id, reason)

        return sent

    async def _close_slow_consumer(
        self, client: WebSocketClient, reason: str,
    ) -> None:
        """F22: send an explicit close frame (code 1011) to a slow client
        before removing it from the registry. Emits
        ``EventType.WS_CLIENT_DISCONNECTED`` with the client id and the
        reason so log aggregators can correlate retries with the cause.

        Closing an already-dead socket must not raise — websockets'
        ``close()`` can throw ``ConnectionClosed`` or ``OSError`` on a
        half-torn-down peer; both are swallowed."""
        try:
            await client.websocket.close(code=1011, reason=reason)
        except Exception:
            # Socket already gone — that's fine; log at debug for
            # completeness but don't surface as an error.
            logger.debug(
                "close(slow consumer) on already-dead socket %s",
                client.client_id,
                exc_info=True,
            )
        try:
            # Structured event so support can grep the launcher log for
            # slow-client disconnects and correlate with extension
            # reconnects in the field.
            from cortex.libs.logging.structured import EventType, get_logger

            get_logger(__name__).info(
                "ws_client_disconnected",
                event_type=EventType.WS_CLIENT_DISCONNECTED.value,
                client_id=client.client_id,
                client_type=client.client_type,
                reason=reason,
            )
        except Exception:
            # Logging must never break the broadcast hot path.
            logger.debug(
                "structured ws_client_disconnected log failed", exc_info=True,
            )

    def _make_state_update(
        self,
        estimate: StateEstimate,
        biometrics: dict[str, float | None] | None = None,
    ) -> WSMessage:
        """Create a STATE_UPDATE message.

        Surfaces every v0.2.0 transparency field consumers may want to
        display: ``stress_integral`` for break-readiness UI,
        ``calibrated_probabilities`` for confidence bars,
        ``classifier_source``/``classifier_alpha`` for debug overlays,
        and ``timestamp`` so clients can detect stale broadcasts.
        """
        self._sequence += 1
        # F18 (audit Wave-2): mirror the ``StateInferResponse`` envelope
        # onto the WS broadcast. The dashboard's "classifier unavailable"
        # banner reads ``payload.get("degraded")`` / ``payload.get("source")``
        # off the STATE_UPDATE payload; before this stamp the banner could
        # never fire through the WS path because the producer omitted the
        # fields. ``degraded`` is True when no real classifier ran
        # (``classifier_source is None``) — that is the same condition the
        # ``/state/infer`` fallback branch uses to flag synthetic
        # confidence. ``source`` is the literal pair ``classifier`` /
        # ``fallback`` so the reader can branch without conflating with
        # the debug-overlay ``classifier_source`` field (``rule`` / ``ml`` /
        # ``ensemble``).
        degraded = estimate.classifier_source is None
        envelope_source = "fallback" if degraded else "classifier"
        payload: dict[str, Any] = {
            "state": estimate.state,
            "confidence": estimate.confidence,
            "scores": {
                "flow": estimate.scores.flow,
                "hypo": estimate.scores.hypo,
                "hyper": estimate.scores.hyper,
                "recovery": estimate.scores.recovery,
            },
            "signal_quality": {
                "physio": estimate.signal_quality.physio,
                "kinematics": estimate.signal_quality.kinematics,
                "telemetry": estimate.signal_quality.telemetry,
                "overall": estimate.signal_quality.overall,
            },
            "dwell_seconds": estimate.dwell_seconds,
            "reasons": estimate.reasons,
            "stress_integral": estimate.stress_integral,
            "calibrated_probabilities": estimate.calibrated_probabilities,
            "classifier_source": estimate.classifier_source,
            "classifier_alpha": estimate.classifier_alpha,
            "source": envelope_source,
            "degraded": degraded,
            "timestamp": _serialize_timestamp(estimate.timestamp),
            # G1 (audit-prod): stamp the deduped list of currently-IDENTIFY-ed
            # client types so consumers (desktop dashboard) can light up the
            # Chrome / Edge / Editor connection dots without subscribing to
            # a separate event stream.
            "connected_clients": self.connected_client_types(),
        }

        # Surface capture status so the consumer dashboard can render
        # "Camera offline" vs "Looking for your face" vs "Reading your
        # pulse" instead of a bare ``--`` while the rPPG window fills.
        # ``latest_frame_meta`` is stamped on every capture tick by
        # ``runtime_daemon._process_capture_output``; absence here means
        # the capture loop hasn't produced a frame yet (camera not open,
        # permission denied, or daemon mid-startup).
        capture_status: dict[str, bool] = {
            "frames_flowing": False,
            "face_detected": False,
        }
        try:
            from cortex.services.api_gateway.app import registry as _registry
            frame_meta = _registry.get("latest_frame_meta")
            if frame_meta is not None:
                fm_ts = float(getattr(frame_meta, "timestamp", 0.0))
                capture_status["frames_flowing"] = (
                    time.monotonic() - fm_ts < 2.0
                )
                capture_status["face_detected"] = bool(
                    getattr(frame_meta, "face_detected", False)
                )
        except Exception:
            # Registry lookup is best-effort; never block a broadcast.
            pass
        payload["capture"] = capture_status

        if biometrics:
            payload["biometrics"] = biometrics
        return WSMessage(
            type=MessageType.STATE_UPDATE,
            payload=payload,
            sequence=self._sequence,
            source_client_type="daemon",
        )

    def _make_intervention_trigger(
        self, plan: InterventionPlan,
    ) -> WSMessage:
        """Create an INTERVENTION_TRIGGER message.

        Surfaces ``causal_explanation`` (so the VS Code "Why this?" panel
        and the popup transparency section can render the grounded
        rationale), ``consent_level`` (the consent gate that produced this
        plan), and ``plan_warnings`` (degradations the planner applied).
        """
        self._sequence += 1
        payload: dict[str, Any] = {
            "intervention_id": plan.intervention_id,
            "level": plan.level,
            "headline": plan.headline,
            "situation_summary": plan.situation_summary,
            "primary_focus": plan.primary_focus,
            "micro_steps": plan.micro_steps,
            "hide_targets": plan.hide_targets,
            "ui_plan": plan.ui_plan.model_dump(),
            "tone": plan.tone,
            "suggested_actions": [a.model_dump() for a in plan.suggested_actions],
            "causal_explanation": getattr(plan, "causal_explanation", None),
            "consent_level": getattr(plan, "consent_level", None),
            "plan_warnings": getattr(plan, "plan_warnings", None) or [],
        }
        if plan.error_analysis is not None:
            payload["error_analysis"] = plan.error_analysis.model_dump()
        if plan.tab_recommendations is not None:
            payload["tab_recommendations"] = plan.tab_recommendations.model_dump()
        # Audit-prod fix (G4 P0): mirror the dashboard's connected-clients
        # snapshot onto the intervention trigger so the WS-mode overlay's
        # action buttons gate on the same authoritative list the
        # STATE_UPDATE flow uses. Without this the WS-mode overlay always
        # renders browser-bound actions disabled.
        payload["connected_clients"] = self.connected_client_types()
        # Audit-2 fix: ship plan.metadata so the F27 fallback hint, F20
        # budget-killed flag, and F29 truncation telemetry reach the
        # overlay. Prior to this fix the WS broadcast omitted the field
        # entirely and only the in-process callback path carried it,
        # silently disabling these UI surfaces in WS-mode.
        if plan.metadata:
            payload["metadata"] = dict(plan.metadata)
        # F16-srv: stamp a deterministic cid per intervention emission so a
        # later USER_ACTION can be matched against the active emission.
        cid = f"iv_{plan.intervention_id}_{self._sequence}"
        if plan.intervention_id:
            self._active_intervention_cid[plan.intervention_id] = cid
        return WSMessage(
            type=MessageType.INTERVENTION_TRIGGER,
            payload=payload,
            sequence=self._sequence,
            correlation_id=cid,
            source_client_type="daemon",
        )

    def reset(self) -> None:
        """Reset server state (does not stop the server)."""
        self._sequence = 0
        self._latest_state = None
        self._pending_context_requests.clear()
        self._active_intervention_cid.clear()
