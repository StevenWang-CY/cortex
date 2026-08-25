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
import inspect
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from cortex.application.clock import (
    SYSTEM_CLOCK,
    Clock,
    monotonic_seconds,
    unix_seconds,
)
from cortex.application.events import (
    ApplicationEventHub,
    OutboundTransportEvent,
    Subscription,
)
from cortex.application.gateway import WebSocketCommandHandlers
from cortex.application.runtime_status import (
    RuntimeStatusReader,
    RuntimeStatusSnapshot,
)
from cortex.application.services import ServiceProvider
from cortex.libs.auth import verify_token
from cortex.libs.config.settings import APIConfig
from cortex.libs.logging.correlation import correlation_scope, get_correlation_id
from cortex.libs.logging.structured import EventType
from cortex.libs.schemas.intervention import InterventionPlan
from cortex.libs.schemas.intervention_transaction import (
    ActionManifest,
    AuthorizationDenied,
    InterventionApplyCommand,
    InterventionAuthorizationRequest,
    InterventionLifecycleState,
    InterventionReceiptBatch,
    InterventionRestoreCommand,
    ReceiptPhase,
)
from cortex.libs.schemas.protocol import (
    AuthOkPayload,
    AuthRequestPayload,
    ProtocolErrorPayload,
    negotiate_protocol,
)
from cortex.libs.schemas.realtime import (
    BiometricsSummary,
    CaptureStatus,
    InterventionTriggerPayload,
    StateUpdatePayload,
    StoreHealth,
)
from cortex.libs.schemas.session_history import (
    SessionDetailResponse,
    SessionListResponse,
    TrendsResponse,
)
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.schemas.ws_message import WSMessage as _PydanticWSMessage
from cortex.libs.schemas.ws_message_types import MessageType

logger = logging.getLogger(__name__)


def _receipt_has_restorable_effect(receipt: Any) -> bool:
    """Project only verified, non-empty effects as user-restorable.

    Merely carrying an inverse JSON object is insufficient: failed adapters
    deliberately return ``{}`` or conservative recovery evidence, and a
    verified no-op carries ``{"noEffect": true}``. Neither may light an Undo
    control. The transaction coordinator has already authenticated provenance
    before this privacy-minimal UI projection is built.
    """

    status = str(getattr(receipt.status, "value", receipt.status))
    verification = str(
        getattr(receipt.verification, "value", receipt.verification)
    )
    inverse_json = receipt.inverse_payload_json
    if (
        status not in {"succeeded", "already_complete"}
        or verification != "verified"
        or inverse_json is None
    ):
        return False
    try:
        inverse = json.loads(inverse_json)
    except (TypeError, ValueError):
        return False
    return isinstance(inverse, dict) and inverse.get("noEffect") is not True


def _auth_ok_frame(clock: Clock, selected_protocol_version: str) -> str:
    """Serialise a minimal ``AUTH_OK`` reply frame (audit Debt-2).

    The reply includes the selected protocol and full v2 event metadata.
    """
    payload = AuthOkPayload.model_validate(
        {"selected_protocol_version": selected_protocol_version}
    )
    return _PydanticWSMessage.from_clock(
        clock=clock,
        type=MessageType.AUTH_OK,
        payload=payload.model_dump(mode="json"),
        protocol_version=selected_protocol_version,
        source_client_type="daemon",
    ).to_json()


def _serialize_timestamp(ts: Any) -> Any:
    """``StateEstimate.timestamp`` is typed loosely (float monotonic or
    datetime depending on producer). Return an ISO string for datetimes
    and the raw value for everything else so JSON serialisation works
    consistently across both shapes.

    P2-2: the previously bare ``except Exception: pass`` silently
    swallowed isoformat errors making them invisible in production logs.
    Replaced with a ``logger.debug`` that includes ``exc_info=True`` so
    the root cause is visible in debug logging while the fall-through
    behaviour (return the raw value) is preserved.
    """
    if ts is None:
        return None
    iso = getattr(ts, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            logger.debug(
                "timestamp ISO serialize failed for %r", ts, exc_info=True
            )
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

    Phase-4b TASK I: ``coalesce_queue`` + ``coalesce_task`` implement
    newest-wins per-client coalescing for the high-frequency broadcast
    types (STATE_UPDATE, PHYSIO_DATA, KINEMATICS_UPDATE,
    TELEMETRY_UPDATE). A slow client cannot accumulate a backlog — the
    queue is capped at depth=1 and the producer drops the old frame
    before inserting the new one.
    """

    client_id: str
    websocket: Any  # websockets.WebSocketServerProtocol
    connected_at: float = 0.0
    client_type: str = "unknown"  # "vscode", "chrome", "desktop", "unknown"
    # Stable per extension installation/profile. Socket ``client_id`` values
    # are connection-local and cannot safely own a crash-recoverable effect.
    client_instance_id: str | None = None
    last_message_at: float = 0.0
    authenticated: bool = False
    protocol_version: str = "1.0"
    coalesce_queue: Any | None = None  # asyncio.Queue[str] | None
    coalesce_task: Any | None = None  # asyncio.Task | None
    seen_event_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=512),
        repr=False,
    )
    seen_event_id_set: set[str] = field(default_factory=set, repr=False)


@dataclass(frozen=True)
class ExactDispatchReport:
    """Observed transport boundary for one exact apply command.

    A failed socket write is delivery-ambiguous: the peer may have received
    the frame before the local exception. Keeping ``attempted`` separate from
    ``delivered`` lets the coordinator compensate instead of falsely treating
    every zero-success send as a preflight-safe failure.
    """

    expected_targets: int
    attempted_targets: int
    delivered_targets: int


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
    # Wall-clock seconds matching the Pydantic ``WSMessage`` contract
    # (cortex/libs/schemas/ws_message.py:70-78). The legacy dataclass
    # previously seeded with a process-local monotonic reading
    # and not comparable to a JS client clock — fixed here so the
    # round-trip ``WSMessageLegacy → WSMessage → JSON`` produces a
    # uniform wire format regardless of construction path.
    timestamp: float = field(default_factory=lambda: unix_seconds(SYSTEM_CLOCK))
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
            # Same wall-clock contract as the field default above.
            timestamp=parsed.get("timestamp", unix_seconds(SYSTEM_CLOCK)),
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

    def __init__(
        self,
        config: APIConfig | None = None,
        *,
        clock: Clock | None = None,
        services: ServiceProvider | None = None,
        events: ApplicationEventHub | None = None,
        runtime_status: RuntimeStatusReader | None = None,
    ) -> None:
        self._config = config or APIConfig()
        self._clock = clock or SYSTEM_CLOCK
        self._services = services
        self._events = events or ApplicationEventHub()
        self._runtime_status = runtime_status
        self._clients: dict[str, WebSocketClient] = {}
        self._server: Any = None  # websockets server
        self._running = False
        self._sequence: int = 0

        # Callbacks for received messages
        self._user_action_callback: Any = None
        self._settings_callback: Any = None
        self._calibration_reload_callback: Any = None
        self._shutdown_callback: Any = None
        self._activity_sync_callback: Any = None
        self._tab_relevance_feedback_callback: Any = None
        self._leetcode_context_callback: Any = None
        self._intervention_applied_callback: Any = None
        self._intervention_authorize_callback: Any = None
        self._intervention_receipt_callback: Any = None
        self._intervention_dispatch_failure_callback: Any = None
        self._intervention_partial_dispatch_callback: Any = None
        self._intervention_dispatch_binding_callback: Any = None
        # G1 (audit-prod): fired on IDENTIFY + on identified-client disconnect.
        self._client_identified_callback: Any = None
        self._published_client_connectivity: dict[str, bool] = {}
        # P0 §3.1 / §3.2 / §3.3: handlers for the new history / trends /
        # recap request types. Each callback returns a Pydantic envelope
        # (or a plain dict / None) that the dispatcher serialises and
        # sends back to the requesting client. Typed at the field level
        # (P0 §3.1 fix #27) so a typo at the call site is a type error
        # rather than a runtime AttributeError.
        self._session_list_callback: (
            Callable[[float | None, int], Awaitable[SessionListResponse]] | None
        ) = None
        self._session_detail_callback: (
            Callable[[str], Awaitable[SessionDetailResponse]] | None
        ) = None
        self._trends_callback: (
            Callable[..., Awaitable[TrendsResponse]] | None
        ) = None
        self._session_recap_cache_callback: (
            Callable[[], dict[str, Any] | None] | None
        ) = None
        # P0 §3.3 (Wave-2 P1): callback fired when the UI confirms the
        # user dismissed the SESSION_RECAP card. Signature:
        # ``async (session_id: str | None) -> None``. The daemon's
        # ``stop()`` awaits an event this callback sets so the WS
        # server is not torn down mid-recap.
        self._session_recap_acknowledged_callback: Any = None
        # P0 §3.6: callback fired when a peer surface toggles a
        # micro-step checkbox. Signature:
        # ``async (intervention_id: str, step_index: int, new_status: str)``.
        # The dispatcher validates payload shape + value bounds before
        # invoking; the daemon's ``toggle_micro_step`` does the active-
        # plan lookup and rebroadcast.
        self._micro_step_toggled_callback: Any = None
        # P0 §3.9: callback that resolves a WHY_DETAIL_REQUEST into a
        # list of CausalSignal dicts. Signature:
        # ``async (intervention_id: str) -> list[dict] | None``.
        # ``None`` means "no signals available" — the server still
        # replies with WHY_DETAIL so the client clears its loading
        # state.
        self._why_detail_callback: Any = None
        # P0 §3.11: callback fired when a peer surface (dashboard menu,
        # overlay footer, tray menu, browser popup, VS Code panel)
        # asks the daemon to enter / leave a quiet or pause mode.
        # Signature:
        #   ``async (kind: str, duration_minutes: int | None,
        #            source: str) -> None``
        # The daemon serializes the resulting state through
        # :attr:`MessageType.QUIET_MODE_STATE` so every surface
        # reflects the same truth.
        self._quiet_mode_toggle_callback: Any = None
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
        # A successful socket write is not an application receipt. Keep one
        # bounded watchdog per consumed authorization so a client crash after
        # delivery enters exact compensation instead of stranding APPLYING.
        self._intervention_receipt_watchdogs: dict[
            str, asyncio.Task[None]
        ] = {}
        # Serialize forward command writes against exact inverse writes. A
        # consent reset/emergency restore may race an authorization after its
        # durable consumption but before socket delivery. Binding revalidates
        # the consent revision; once binding succeeds, this barrier guarantees
        # the forward write finishes before a concurrently requested inverse
        # can reach the same owner.
        self._intervention_wire_dispatch_lock = asyncio.Lock()

    def bind_command_handlers(self, handlers: WebSocketCommandHandlers) -> None:
        """Bind one immutable application command surface.

        This is the production composition API. The individual ``set_*``
        methods below remain temporary compatibility facades for isolated
        tests and external integrations.
        """

        for attribute, handler in handlers.as_callback_attributes().items():
            if not hasattr(self, attribute):
                raise AttributeError(f"unknown WebSocket handler slot: {attribute}")
            setattr(self, attribute, handler)
        self._published_client_connectivity.clear()

    def subscribe_outbound(
        self,
        listener: Callable[[OutboundTransportEvent], None],
    ) -> Subscription:
        """Observe transport-neutral outbound events without monkey-patching."""

        return self._events.outbound_transport.subscribe(listener)

    def _service_provider(self) -> ServiceProvider:
        """Return injected services, falling back only for compatibility."""

        if self._services is not None:
            return self._services
        from cortex.services.api_gateway.app import registry

        return registry

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def authenticated_client_count(self) -> int:
        """Number of peers eligible to receive protected broadcasts."""

        return sum(1 for client in self._clients.values() if client.authenticated)

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

    def set_calibration_reload_callback(self, callback: Any) -> None:
        """Set the measured-profile live reload callback."""

        self._calibration_reload_callback = callback

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

    def set_intervention_authorize_callback(self, callback: Any) -> None:
        """Bind exact manifest authorization handling.

        Signature: ``async (request, client_id, client_type) ->
        InterventionApplyCommand | AuthorizationDenied``.
        """

        self._intervention_authorize_callback = callback

    def set_intervention_receipt_callback(self, callback: Any) -> None:
        """Bind typed action receipt handling.

        Signature: ``async (batch, client_id, client_type) ->
        tuple[state, optional_restore_command]``.
        """

        self._intervention_receipt_callback = callback

    def set_intervention_dispatch_failure_callback(self, callback: Any) -> None:
        """Bind fail-closed handling for a consumed but undispatchable grant."""

        self._intervention_dispatch_failure_callback = callback

    def set_intervention_partial_dispatch_callback(self, callback: Any) -> None:
        """Bind compensation for a command that reached only some targets.

        Signature: ``async (authorization_id, reason) ->
        InterventionRestoreCommand | None``.
        """

        self._intervention_partial_dispatch_callback = callback

    def set_intervention_dispatch_binding_callback(self, callback: Any) -> None:
        """Bind durable action→client-instance ownership before dispatch.

        Signature: ``async (command_id, action_client_instance_ids) -> None``.
        Exact transaction commands fail closed when this callback is absent
        or its persistence step raises.
        """

        self._intervention_dispatch_binding_callback = callback

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
        self._published_client_connectivity.clear()

    async def _notify_client_type_connectivity(self, client_type: str) -> None:
        """Publish aggregate connectivity for one surface type.

        Reconnects briefly overlap old and new sockets. Reporting the raw
        disconnect event would gray the UI even though the replacement is
        already live, so callbacks always receive the current aggregate
        truth after registry mutation.
        """

        if client_type in {"", "unknown"}:
            return
        callback = self._client_identified_callback
        if callback is None:
            return
        connected = any(
            peer.client_type == client_type
            for peer in self._clients.values()
        )
        if self._published_client_connectivity.get(client_type) == connected:
            return
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(client_type, connected)
            else:
                callback(client_type, connected)
            self._published_client_connectivity[client_type] = connected
        except Exception:
            logger.debug(
                "client_identified callback raised for %s=%s",
                client_type,
                connected,
                exc_info=True,
            )

    # ── P0 §3.1 / §3.2 / §3.3: history / trends / recap callbacks ────

    def set_session_list_callback(
        self,
        callback: Callable[[float | None, int], Awaitable[SessionListResponse]] | None,
    ) -> None:
        """P0 §3.1: handler for ``REQUEST_SESSION_LIST`` messages.

        Signature: ``async (since: float | None, limit: int) ->
        SessionListResponse``. The dispatcher serialises the response
        and routes it back to the requesting client only.
        """
        self._session_list_callback = callback

    def set_session_detail_callback(
        self,
        callback: Callable[[str], Awaitable[SessionDetailResponse]] | None,
    ) -> None:
        """P0 §3.1: handler for ``REQUEST_SESSION_DETAIL`` messages.

        Signature: ``async (session_id: str) -> SessionDetailResponse``.
        """
        self._session_detail_callback = callback

    def set_trends_callback(
        self,
        callback: Callable[..., Awaitable[TrendsResponse]] | None,
    ) -> None:
        """P0 §3.2: handler for ``REQUEST_TRENDS`` messages.

        Signature: ``async (window: str, *, refresh: bool) ->
        TrendsResponse``.
        """
        self._trends_callback = callback

    def set_micro_step_toggled_callback(self, callback: Any) -> None:
        """P0 §3.6: handler for ``MICRO_STEP_TOGGLED`` messages.

        Signature: ``async (intervention_id: str, step_index: int,
        new_status: str)``. The dispatcher validates the payload
        shape + value bounds before invoking; the daemon's
        ``toggle_micro_step`` does the active-plan lookup and
        rebroadcast.
        """
        self._micro_step_toggled_callback = callback

    def set_why_detail_callback(
        self,
        callback: Callable[[str], Awaitable[list[dict[str, Any]] | None]] | None,
    ) -> None:
        """P0 §3.9: handler for ``WHY_DETAIL_REQUEST`` messages.

        Signature: ``async (intervention_id: str) -> list[dict] |
        None``. The dispatcher serialises the response as a
        ``WHY_DETAIL`` frame and routes it back to the requesting
        client only.
        """
        self._why_detail_callback = callback

    def set_session_recap_cache_callback(
        self,
        callback: Callable[[], dict[str, Any] | None] | None,
    ) -> None:
        """P0 §3.3: handler for ``REQUEST_SESSION_RECAP`` messages.

        Signature: ``() -> dict | None``. Returns the most-recent
        SESSION_RECAP payload emitted this process lifetime so that
        late-joining clients (e.g. the browser extension popup that
        opened after a recap broadcast) can still see the recap.
        """
        self._session_recap_cache_callback = callback

    def set_session_recap_acknowledged_callback(self, callback: Any) -> None:
        """P0 §3.3 (Wave-2 P1): handler for ``SESSION_RECAP_ACKNOWLEDGED``.

        Signature: ``async (session_id: str | None) -> None``. The
        daemon's ``stop()`` awaits an :class:`asyncio.Event` this
        callback flips so a fast UI hide doesn't race the WS teardown.
        """
        self._session_recap_acknowledged_callback = callback

    def set_quiet_mode_toggle_callback(self, callback: Any) -> None:
        """P0 §3.11: handler for ``QUIET_MODE_TOGGLE`` / ``SNOOZE_REQUEST``.

        Signature: ``async (kind: str, duration_minutes: int | None,
        source: str) -> None``. The dispatcher validates payload
        shape + value bounds before invoking; the daemon's
        :meth:`set_quiet_mode` does the activation and broadcasts the
        resulting :attr:`MessageType.QUIET_MODE_STATE` so every surface
        re-renders consistently.
        """
        self._quiet_mode_toggle_callback = callback

    async def start(self) -> bool:
        """
        Start the WebSocket server.

        Phase-4b TASK J: pin an explicit ``process_request`` Origin
        allowlist so a malicious page on http://localhost can't open a
        cross-origin WebSocket to the daemon. The capability-token AUTH
        handshake remains the primary defense; the Origin filter is
        defense-in-depth so a browser that auto-sends ``Origin`` for an
        attacker page is rejected at the TCP layer before the token
        gate even sees the connection.

        Returns:
            True if started successfully, False on error.
        """
        try:
            import re

            import websockets

            # Phase-4b TASK J: pass a regex-based origins allowlist when
            # supported (websockets >= 14 accepts ``re.Pattern`` entries
            # in the ``origins`` sequence). Older versions silently
            # ignore unknown kwargs via ``**kwargs`` so this stays
            # forward-compatible. ``None`` in the sequence accepts a
            # request that omits the Origin header entirely (native
            # clients such as the desktop shell).
            origin_allowlist = [
                None,
                re.compile(r"^chrome-extension://.*$"),
                re.compile(r"^moz-extension://.*$"),
                re.compile(r"^vscode-webview://.*$"),
            ]
            self._server = await websockets.serve(
                self._handle_client,
                self._config.host,
                self._config.ws_port,
                origins=origin_allowlist,
                process_request=self._origin_gate,
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

    @staticmethod
    def _is_allowed_origin(origin: str | None) -> bool:
        """Phase-4b TASK J: accept only the Origin headers Cortex
        legitimately serves: extension origins, vscode-webview, and
        clients that send no Origin (Python ``websockets`` clients,
        the desktop shell). Reject everything else."""
        if origin is None:
            # Native clients (desktop shell, python websockets) do not
            # set Origin. Accept; the AUTH token gate covers them.
            return True
        if origin.startswith("chrome-extension://"):
            return True
        if origin.startswith("moz-extension://"):
            return True
        if origin.startswith("vscode-webview://"):
            return True
        return False

    async def _origin_gate(
        self, connection: Any, request: Any,
    ) -> Any:
        """Phase-4b TASK J: ``process_request`` hook for ``websockets``.

        Reject the upgrade with a 403 if the Origin header is not in
        our allowlist. Returning ``None`` lets the upgrade proceed.
        The websockets library signature differs between major
        versions; we duck-type the Origin header extraction so both
        old (``request.headers.get``) and new (``request.headers["Origin"]``)
        shapes are tolerated.
        """
        try:
            headers = getattr(request, "headers", None) or {}
            origin: str | None
            if hasattr(headers, "get"):
                origin = headers.get("Origin") or headers.get("origin")
            else:
                origin = None
        except Exception:
            origin = None
        if self._is_allowed_origin(origin):
            return None
        logger.warning(
            "Rejecting WS upgrade: disallowed Origin=%r", origin,
        )
        # Try the new-style websockets API first, falling back to a
        # best-effort plain response builder.
        try:
            import http

            from websockets.datastructures import Headers
            from websockets.http11 import Response

            return Response(
                http.HTTPStatus.FORBIDDEN.value,
                "Forbidden",
                Headers(),
                b"",
            )
        except Exception:
            # Old websockets API returns (status, headers, body) tuple.
            return (403, [], b"")

    async def stop(self) -> None:
        """Stop the WebSocket server and disconnect all clients."""
        self._running = False

        if self._intervention_receipt_watchdogs:
            watchdogs = list(self._intervention_receipt_watchdogs.values())
            for task in watchdogs:
                task.cancel()
            await asyncio.gather(*watchdogs, return_exceptions=True)
            self._intervention_receipt_watchdogs.clear()

        # Close all client connections
        for client in list(self._clients.values()):
            # Phase-4b TASK I: cancel the per-client coalesce drain task
            # so it doesn't survive the WS teardown waiting on a queue
            # that will never receive another frame.
            if client.coalesce_task is not None:
                try:
                    client.coalesce_task.cancel()
                except Exception:
                    # P2-2: previously ``pass`` swallowed cancellation
                    # failures silently. ``logger.debug`` with
                    # ``exc_info=True`` matches the pattern used in
                    # ``_close_slow_consumer`` so root causes are
                    # visible without changing behaviour.
                    logger.debug(
                        "coalesce task cancel failed during stop() for %s",
                        client.client_id,
                        exc_info=True,
                    )
                client.coalesce_task = None
            client.coalesce_queue = None
            try:
                await client.websocket.close()
            except Exception:
                # P2-2: surface close failures in debug logs (e.g.
                # already-closed sockets, broken transports).
                logger.debug(
                    "websocket close failed during stop() for %s",
                    client.client_id,
                    exc_info=True,
                )

        self._clients.clear()
        self._published_client_connectivity.clear()

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
            connected_at=monotonic_seconds(self._clock),
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
            # Phase-4b TASK I: dispose of the per-client coalesce queue
            # + drain task so a dropped client doesn't leak a Task
            # awaiting forever on a queue nobody will fill.
            if client.coalesce_task is not None:
                try:
                    client.coalesce_task.cancel()
                except Exception:
                    logger.debug(
                        "coalesce task cancel failed for %s",
                        client.client_id,
                        exc_info=True,
                    )
            client.coalesce_queue = None
            client.coalesce_task = None
            # F23: cancel any in-flight context-request futures associated
            # with this client so the calling coroutine returns promptly
            # rather than waiting for the per-call timeout.
            self._cancel_pending_for_client(client_id)
            # G1 (audit-prod): if this client previously IDENTIFY-ed, tell
            # the listener it's gone so the dashboard dot re-grays.
            if client.client_type and client.client_type != "unknown":
                await self._notify_client_type_connectivity(
                    client.client_type
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

        client.last_message_at = monotonic_seconds(self._clock)

        # v2 event IDs make mutating command delivery idempotent even when a
        # reconnecting client replays an already-sent frame. Keep a bounded
        # per-connection set; legacy 1.0 frames retain their historical
        # at-least-once behavior because they did not carry stable identity.
        if client.authenticated and client.protocol_version != "1.0":
            event_id = str(msg.event_id)
            if event_id in client.seen_event_id_set:
                logger.debug(
                    "Dropping duplicate event_id=%s from %s",
                    event_id,
                    client.client_id,
                )
                return
            if len(client.seen_event_ids) == client.seen_event_ids.maxlen:
                evicted = client.seen_event_ids.popleft()
                client.seen_event_id_set.discard(evicted)
            client.seen_event_ids.append(event_id)
            client.seen_event_id_set.add(event_id)

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
            prior_client_type = client.client_type
            _ALLOWED_CLIENT_TYPES = frozenset({
                "chrome", "edge", "vscode", "desktop",
            })
            requested = msg.payload.get("client_type")
            requested_instance = msg.payload.get("client_instance_id")
            valid_instance = (
                isinstance(requested_instance, str)
                and 8 <= len(requested_instance) <= 128
                and all(
                    character.isalnum() or character in "._:-"
                    for character in requested_instance
                )
            )
            if isinstance(requested, str) and requested in _ALLOWED_CLIENT_TYPES:
                client.client_type = requested
                client.client_instance_id = (
                    requested_instance if valid_instance else None
                )
                if client.client_instance_id is not None:
                    # A reconnect supersedes an older socket for the same
                    # durable extension instance. Remove and de-authenticate
                    # the stale socket before its close handshake so it can
                    # receive neither proposals nor exact commands during the
                    # overlap window.
                    superseded: list[WebSocketClient] = []
                    for peer in list(self._clients.values()):
                        if (
                            peer is not client
                            and peer.client_instance_id
                            == client.client_instance_id
                        ):
                            peer.client_instance_id = None
                            peer.authenticated = False
                            self._clients.pop(peer.client_id, None)
                            self._cancel_pending_for_client(peer.client_id)
                            if peer.coalesce_task is not None:
                                peer.coalesce_task.cancel()
                                peer.coalesce_task = None
                            peer.coalesce_queue = None
                            superseded.append(peer)
                    for peer in superseded:
                        try:
                            await peer.websocket.close(
                                code=1000,
                                reason="superseded by stable-instance reconnect",
                            )
                        except Exception:
                            logger.debug(
                                "superseded socket close failed for %s",
                                peer.client_id,
                                exc_info=True,
                            )
            else:
                logger.warning(
                    "IDENTIFY: rejecting unknown client_type=%r from %s",
                    requested,
                    client.client_id,
                )
                client.client_type = "unknown"
                client.client_instance_id = None
            if client.client_type != "unknown" and not valid_instance:
                logger.warning(
                    "IDENTIFY from %s lacks a durable client_instance_id; "
                    "exact workspace commands are disabled for this socket",
                    client.client_id,
                )
            logger.info(
                f"Client {client.client_id} identified as {client.client_type}"
            )
            # G1 (audit-prod): publish aggregate type connectivity. This
            # remains true when a reconnect replaces an older socket.
            if (
                prior_client_type
                and prior_client_type != "unknown"
                and prior_client_type != client.client_type
            ):
                await self._notify_client_type_connectivity(
                    prior_client_type
                )
            if client.client_type and client.client_type != "unknown":
                await self._notify_client_type_connectivity(
                    client.client_type
                )
        elif msg.type == MessageType.CONTEXT_RESPONSE.value:
            self._handle_context_response(msg)
        elif msg.type == MessageType.SETTINGS_SYNC.value:
            await self._handle_settings_sync(client, msg)
        elif msg.type == MessageType.CALIBRATION_RELOAD.value:
            await self._handle_calibration_reload(client, msg)
        elif msg.type == MessageType.ACTIVITY_SYNC.value:
            await self._handle_activity_sync(client, msg)
        elif msg.type == MessageType.TAB_RELEVANCE_FEEDBACK.value:
            await self._handle_tab_relevance_feedback(client, msg)
        elif msg.type == MessageType.LEETCODE_CONTEXT_UPDATE.value:
            await self._handle_leetcode_context_update(client, msg)
        elif msg.type == MessageType.INTERVENTION_APPLIED.value:
            await self._handle_intervention_applied(client, msg)
        elif msg.type == MessageType.INTERVENTION_AUTHORIZE.value:
            await self._handle_intervention_authorize(client, msg)
        elif msg.type == MessageType.INTERVENTION_RECEIPT.value:
            await self._handle_intervention_receipt(client, msg)
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
        elif msg.type == MessageType.REQUEST_SESSION_LIST.value:
            await self._handle_request_session_list(client, msg)
        elif msg.type == MessageType.REQUEST_SESSION_DETAIL.value:
            await self._handle_request_session_detail(client, msg)
        elif msg.type == MessageType.REQUEST_TRENDS.value:
            await self._handle_request_trends(client, msg)
        elif msg.type == MessageType.REQUEST_SESSION_RECAP.value:
            await self._handle_request_session_recap(client, msg)
        elif msg.type == MessageType.SESSION_RECAP_ACKNOWLEDGED.value:
            # P0 §3.3 (Wave-2 P1): the UI confirmed the user dismissed
            # the recap card. The daemon's ``stop()`` is awaiting this
            # acknowledgement (or its 5 s timeout) before tearing down.
            await self._handle_session_recap_acknowledged(client, msg)
        elif msg.type == MessageType.WHY_DETAIL_REQUEST.value:
            await self._handle_why_detail_request(client, msg)
        elif msg.type == MessageType.MICRO_STEP_TOGGLED.value:
            # P0 §3.6: a peer surface (browser popup, VS Code panel,
            # WS-mode overlay) clicked a micro-step checkbox. Validate
            # the payload at the daemon boundary and forward to the
            # daemon's ``toggle_micro_step`` via the same callback
            # pipeline used for USER_ACTION; a malformed frame is
            # logged and dropped without closing the socket so a
            # buggy/older client does not bounce itself off.
            await self._handle_micro_step_toggled(client, msg)
        elif msg.type == MessageType.QUIET_MODE_TOGGLE.value:
            # P0 §3.11: the dashboard menu, overlay footer, tray
            # checkmark, or VS Code/browser surface asked the daemon
            # to enter or leave a quiet/pause mode. Validate at the
            # boundary so an older client cannot smuggle an invalid
            # ``kind`` into the trigger policy or the pause path.
            await self._handle_quiet_mode_toggle(client, msg)
        elif msg.type == MessageType.SNOOZE_REQUEST.value:
            # P0 §3.11: alias for ``QUIET_MODE_TOGGLE`` with kind=
            # ``"snooze_15"``. Carried separately so the source-of-
            # truth in the helpfulness log can attribute "overlay
            # snooze click" vs. "dashboard menu pick" later.
            await self._handle_snooze_request(client, msg)
        elif msg.type == MessageType.COST_REQUEST.value:
            # P0 §3.15: client (desktop shell on a ~10 s poll) asked
            # for a snapshot of today's LLM spend.
            await self._handle_cost_request(client, msg)
        elif msg.type == MessageType.TEST_PROVIDER.value:
            # P0 §3.19: client asked the daemon to probe a specific LLM
            # provider and return latency / error.
            await self._handle_test_provider(client, msg)
        elif msg.type == MessageType.GOAL_SET.value:
            # P0 §3.13: client announced the user-provided session goal.
            await self._handle_goal_set(client, msg)
        elif msg.type == MessageType.FORCE_RECAP.value:
            # P0 §3.21: developer-keyboard-shortcut request to emit a
            # SESSION_RECAP for the in-progress session.
            await self._handle_force_recap(client, msg)
        elif msg.type == MessageType.DISMISS_OVERLAY.value:
            # P0 §3.21: developer-keyboard-shortcut request to dismiss
            # the active overlay across every surface.
            await self._handle_dismiss_overlay(client, msg)
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
                await client.websocket.send(
                    _auth_ok_frame(self._clock, client.protocol_version)
                )
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

        try:
            auth_payload = AuthRequestPayload.model_validate(msg.payload or {})
        except ValidationError:
            error = ProtocolErrorPayload(
                code="malformed_protocol",
                offered_protocol_versions=[],
            )
            await client.websocket.send(
                WSMessage.from_clock(
                    clock=self._clock,
                    type=MessageType.PROTOCOL_ERROR,
                    payload=error.model_dump(mode="json"),
                    source_client_type="daemon",
                ).to_json()
            )
            await client.websocket.close(code=1002, reason="malformed protocol offer")
            return

        offers = auth_payload.offers()
        selected_protocol = negotiate_protocol(offers)
        if selected_protocol is None:
            error = ProtocolErrorPayload(
                code="unsupported_protocol",
                offered_protocol_versions=offers,
            )
            await client.websocket.send(
                WSMessage.from_clock(
                    clock=self._clock,
                    type=MessageType.PROTOCOL_ERROR,
                    payload=error.model_dump(mode="json"),
                    source_client_type="daemon",
                ).to_json()
            )
            await client.websocket.close(code=1002, reason="unsupported protocol")
            return

        client.authenticated = True
        client.protocol_version = selected_protocol
        try:
            await client.websocket.send(
                _auth_ok_frame(self._clock, selected_protocol)
            )
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

    async def _handle_calibration_reload(
        self,
        client: WebSocketClient,
        msg: WSMessage,
    ) -> None:
        """Apply a committed profile; only the desktop authority may request it."""

        async def _reject(code: str, message: str, profile_id: object = None) -> None:
            await self._send_to(
                client,
                MessageType.CALIBRATION_UPDATE_FAILED.value,
                {
                    "code": code,
                    "message": message,
                    "profile_id": (
                        str(profile_id) if isinstance(profile_id, str) else None
                    ),
                    "previous_calibration_unchanged": True,
                },
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )

        if client.client_type != "desktop":
            logger.warning(
                "Rejected calibration reload from client type %s",
                client.client_type,
            )
            await _reject(
                "calibration_authority_required",
                "Only the desktop application may apply calibration.",
            )
            return
        callback = self._calibration_reload_callback
        profile_id = msg.payload.get("profile_id")
        profile_sha256 = msg.payload.get("profile_sha256")
        if callback is None:
            await _reject(
                "calibration_service_unavailable",
                "Calibration could not be applied; the previous profile remains active.",
                profile_id,
            )
            return
        if not isinstance(profile_id, str) or not profile_id:
            await _reject(
                "invalid_calibration_profile_id",
                "The calibration profile identifier is missing or invalid.",
            )
            return
        if not (
            isinstance(profile_sha256, str)
            and len(profile_sha256) == 64
            and all(char in "0123456789abcdef" for char in profile_sha256)
        ):
            await _reject(
                "invalid_calibration_checksum",
                "The calibration integrity value is missing or invalid.",
                profile_id,
            )
            return
        try:
            result = callback(
                profile_id,
                expected_sha256=profile_sha256,
            )
            if asyncio.iscoroutine(result):
                result = await result
            payload = (
                result.model_dump(mode="json")
                if hasattr(result, "model_dump")
                else dict(result)
            )
            await self.send_message(
                MessageType.CALIBRATION_UPDATED.value,
                payload,
                correlation_id=msg.correlation_id,
            )
        except Exception as exc:
            logger.error(
                "Calibration reload failed for %s: %s",
                profile_id,
                exc,
            )
            await _reject(
                "calibration_apply_failed",
                "Calibration could not be validated or applied; the previous profile remains active.",
                profile_id,
            )

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

    async def _handle_micro_step_toggled(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.6: forward a MICRO_STEP_TOGGLED frame to the daemon.

        Validation:
          * ``intervention_id`` must be a non-empty string.
          * ``step_index`` must be an int in ``[0, 2]`` (the schema
            constrains ``InterventionPlan.micro_steps`` to ``max_length=3``).
          * ``new_status`` must be one of ``"pending"`` / ``"done"`` /
            ``"skipped"``.

        Any validation failure is logged and dropped — we do NOT
        close the socket, because a buggy or stale client should not
        be able to disconnect itself; the daemon's
        ``toggle_micro_step`` is itself idempotent under stale ids.
        """
        callback = self._micro_step_toggled_callback
        if callback is None:
            # Wave-2 P1 (audit-cross-pipeline): MICRO_STEP_TOGGLED is
            # fire-and-forget — the client doesn't wait on a typed
            # reply, but the rebroadcast loop (INTERVENTION_TRIGGER
            # echoes back the mutated plan) will never fire so the
            # checkbox UI stays optimistic forever. Log loudly so
            # operators see the broken wiring.
            logger.warning(
                "MICRO_STEP_TOGGLED received but no callback wired (handler_not_registered)",
            )
            return
        payload = msg.payload or {}
        intervention_id = payload.get("intervention_id")
        step_index = payload.get("step_index")
        new_status = payload.get("new_status")
        if not isinstance(intervention_id, str) or not intervention_id:
            logger.warning(
                "MICRO_STEP_TOGGLED from %s: missing/invalid intervention_id=%r",
                client.client_id,
                intervention_id,
            )
            return
        if not isinstance(step_index, int) or not (0 <= step_index <= 2):
            logger.warning(
                "MICRO_STEP_TOGGLED from %s: invalid step_index=%r",
                client.client_id,
                step_index,
            )
            return
        if new_status not in ("pending", "done", "skipped"):
            logger.warning(
                "MICRO_STEP_TOGGLED from %s: invalid new_status=%r",
                client.client_id,
                new_status,
            )
            return
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(intervention_id, step_index, new_status)
            else:
                callback(intervention_id, step_index, new_status)
        except Exception as exc:
            logger.error(
                "micro_step_toggled callback error from %s: %s",
                client.client_id,
                exc,
            )

    async def _handle_quiet_mode_toggle(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.11: validate + forward a ``QUIET_MODE_TOGGLE`` frame.

        Payload:
          * ``kind``: ``"snooze_15"`` | ``"quiet_session"`` | ``"pause"`` | ``"off"``
          * ``duration_minutes``: int | None (only meaningful for
            ``"snooze_15"`` / ``"quiet_session"`` — server clamps to
            [1, 240])
          * ``source``: ``"dashboard"`` | ``"overlay"`` | ``"tray"`` |
            ``"shortcut"`` | ``"popup"`` | ``"vscode"`` — best-effort
            attribution; ``"unknown"`` when omitted.

        Any validation failure is logged and dropped — we never close
        the socket here for the same reason as MICRO_STEP_TOGGLED.
        """
        callback = self._quiet_mode_toggle_callback
        if callback is None:
            # Wave-2 P1: clients expect a QUIET_MODE_STATE broadcast in
            # response; missing callback means it never arrives. Warn
            # loudly so operators catch the unwired daemon.
            logger.warning(
                "QUIET_MODE_TOGGLE received but no callback wired (handler_not_registered)",
            )
            return
        payload = msg.payload or {}
        kind = payload.get("kind")
        if kind not in ("snooze_15", "quiet_session", "pause", "off"):
            logger.warning(
                "QUIET_MODE_TOGGLE from %s: invalid kind=%r",
                client.client_id, kind,
            )
            return
        raw_duration = payload.get("duration_minutes")
        duration_minutes: int | None
        if raw_duration is None:
            duration_minutes = None
        else:
            try:
                duration_int = int(raw_duration)
            except (TypeError, ValueError):
                logger.warning(
                    "QUIET_MODE_TOGGLE from %s: invalid duration_minutes=%r",
                    client.client_id, raw_duration,
                )
                return
            # Phase-3 P1-1 / Audit-1.5 P1-1: the dashboard contract is
            # ``0 == use daemon default``. Don't coerce 0 → 1 minute or
            # the user's "Quiet for session" pick collapses to a 60 s
            # window when sent over the wire.
            if duration_int <= 0:
                duration_minutes = None
            else:
                duration_minutes = max(1, min(240, duration_int))
        source = payload.get("source")
        # Phase-3 P1-2 / Audit-1.5 P1-2: validate against the documented
        # enum. Unknown sources fall back to the client's identified
        # type so analytics never see attacker-controlled junk strings
        # (the wire still accepts them gracefully through ``extra="ignore"``
        # at the envelope layer).
        _ALLOWED_SOURCES = frozenset({
            "dashboard", "overlay", "tray", "shortcut", "popup",
            "vscode", "os_notification", "settings_sync", "daemon",
            "daemon_decay",
        })
        if not isinstance(source, str) or source not in _ALLOWED_SOURCES:
            source = client.client_type or "unknown"
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(kind, duration_minutes, source)
            else:
                callback(kind, duration_minutes, source)
        except Exception as exc:
            logger.error(
                "quiet_mode_toggle callback error from %s: %s",
                client.client_id, exc,
            )

    async def _handle_snooze_request(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.11: shorthand for a "Snooze 15" overlay click.

        Effectively delegates to :meth:`_handle_quiet_mode_toggle` with
        ``kind="snooze_15"``; carried as a separate message type so
        downstream analytics can attribute the source. The duration
        defaults to 15 minutes when the client omits it.
        """
        callback = self._quiet_mode_toggle_callback
        if callback is None:
            # Wave-2 P1: see _handle_quiet_mode_toggle.
            logger.warning(
                "SNOOZE_REQUEST received but no callback wired (handler_not_registered)",
            )
            return
        payload = msg.payload or {}
        raw_duration = payload.get("duration_minutes")
        duration_minutes: int = 15
        if raw_duration is not None:
            try:
                duration_int = int(raw_duration)
            except (TypeError, ValueError):
                logger.warning(
                    "SNOOZE_REQUEST from %s: invalid duration_minutes=%r",
                    client.client_id, raw_duration,
                )
                return
            # 0 / negative collapse to the default 15-minute snooze.
            duration_minutes = (
                max(1, min(240, duration_int)) if duration_int > 0 else 15
            )
        source = payload.get("source")
        _ALLOWED_SOURCES = frozenset({
            "dashboard", "overlay", "tray", "shortcut", "popup",
            "vscode", "os_notification", "settings_sync", "daemon",
            "daemon_decay",
        })
        if not isinstance(source, str) or source not in _ALLOWED_SOURCES:
            source = client.client_type or "overlay"
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback("snooze_15", duration_minutes, source)
            else:
                callback("snooze_15", duration_minutes, source)
        except Exception as exc:
            logger.error(
                "snooze_request callback error from %s: %s",
                client.client_id, exc,
            )

    async def _handle_why_detail_request(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.9: resolve a WHY_DETAIL_REQUEST into a WHY_DETAIL reply.

        The daemon callback returns a list of ``CausalSignal.model_dump``
        dicts (or None if no signals are available for the
        intervention id). The reply is routed back to the requesting
        client only — broadcasting would leak per-client UI state.
        """
        callback = self._why_detail_callback
        payload = msg.payload or {}
        intervention_id = payload.get("intervention_id")
        if not isinstance(intervention_id, str) or not intervention_id:
            logger.warning(
                "WHY_DETAIL_REQUEST from %s: missing/invalid intervention_id=%r",
                client.client_id,
                intervention_id,
            )
            return
        signals: list[dict[str, Any]] = []
        # Wave-2 P1 (audit-cross-pipeline): when no callback is wired
        # surface ``error="handler_not_registered"`` alongside the empty
        # signals list so the requesting surface (popup, VS Code panel)
        # can distinguish "no causal data" from "daemon never wired the
        # handler" — empty list alone is ambiguous.
        reply_error: str | None = None
        if callback is None:
            logger.warning(
                "WHY_DETAIL_REQUEST received but no callback wired; "
                "replying with handler_not_registered",
            )
            reply_error = "handler_not_registered"
        else:
            try:
                if asyncio.iscoroutinefunction(callback):
                    result = await callback(intervention_id)
                else:
                    result = callback(intervention_id)
                if result:
                    signals = [s for s in result if isinstance(s, dict)]
            except Exception as exc:
                logger.error(
                    "why_detail callback error from %s: %s",
                    client.client_id,
                    exc,
                )
        reply_payload: dict[str, Any] = {
            "intervention_id": intervention_id,
            "causal_signals": signals,
        }
        if reply_error is not None:
            reply_payload["error"] = reply_error
        await self.send_message(
            MessageType.WHY_DETAIL.value,
            reply_payload,
            target_client_types=(
                [client.client_type]
                if client.client_type and client.client_type != "unknown"
                else None
            ),
            correlation_id=msg.correlation_id,
        )

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
            # SECURITY: the InterventionApplied schema explicitly notes
            # ``source_client_type`` is "set by the server-side dispatcher
            # (never trusted from the wire)" (intervention.py:692-699).
            # ``setdefault`` allowed a hostile client to spoof the field
            # by stuffing a value into ``payload.source_client_type``;
            # unconditional overwrite mirrors the correct pattern in
            # ``_handle_user_action`` below.
            payload["source_client_type"] = client.client_type
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

    async def _handle_intervention_authorize(
        self,
        client: WebSocketClient,
        msg: WSMessage,
    ) -> None:
        """Validate a user gesture and dispatch only the consumed command."""

        surface = {
            "chrome": "browser",
            "edge": "browser",
            "vscode": "vscode",
            "desktop": "desktop",
        }.get(client.client_type)
        raw = dict(msg.payload or {})
        if surface is None:
            logger.warning(
                "INTERVENTION_AUTHORIZE rejected from unknown client type %s",
                client.client_type,
            )
            return
        # Source identity is transport-owned; never trust the wire value.
        raw["source_surface"] = surface
        try:
            request = InterventionAuthorizationRequest.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "Invalid INTERVENTION_AUTHORIZE from %s: %s",
                client.client_id,
                exc,
            )
            request_id = str(raw.get("authorization_request_id") or "invalid")
            intervention_id = str(raw.get("intervention_id") or "invalid")
            denial = AuthorizationDenied(
                authorization_request_id=request_id[:128] or "invalid",
                intervention_id=intervention_id[:128] or "invalid",
                reason_code="invalid_request",
                detail="authorization request failed schema validation",
            )
            await self._send_to(
                client,
                MessageType.INTERVENTION_AUTHORIZATION_DENIED.value,
                denial.model_dump(mode="json"),
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )
            return
        callback = self._intervention_authorize_callback
        if callback is None:
            denial = AuthorizationDenied(
                authorization_request_id=request.authorization_request_id,
                intervention_id=request.intervention_id,
                manifest_sha256=request.manifest_sha256,
                reason_code="execution_mode_denied",
                detail="transaction coordinator is not available",
            )
            await self._send_to(
                client,
                MessageType.INTERVENTION_AUTHORIZATION_DENIED.value,
                denial.model_dump(mode="json"),
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )
            return
        try:
            result = callback(
                request,
                client.client_instance_id or client.client_id,
                client.client_type,
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.exception(
                "INTERVENTION_AUTHORIZE callback failed for %s",
                request.intervention_id,
            )
            result = AuthorizationDenied(
                authorization_request_id=request.authorization_request_id,
                intervention_id=request.intervention_id,
                manifest_sha256=request.manifest_sha256,
                reason_code="invalid_request",
                detail="authorization service failed closed",
            )

        if isinstance(result, AuthorizationDenied):
            await self._send_to(
                client,
                MessageType.INTERVENTION_AUTHORIZATION_DENIED.value,
                result.model_dump(mode="json"),
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )
            return
        if not isinstance(result, InterventionApplyCommand):
            logger.error("authorization callback returned unsupported result")
            return

        dispatch = await self.dispatch_apply_command(
            result,
            requesting_client=client,
            correlation_id=msg.correlation_id,
            causation_id=str(msg.event_id),
        )
        sent = dispatch.delivered_targets
        expected_targets = dispatch.expected_targets
        dispatch_complete = sent == expected_targets
        compensation: InterventionRestoreCommand | None = None
        if (
            dispatch.attempted_targets == 0
            and self._intervention_dispatch_failure_callback is not None
        ):
            try:
                dispatch_failed = self._intervention_dispatch_failure_callback(
                    result.authorization.authorization_id,
                    reason="no_complete_executor_route",
                )
                if inspect.isawaitable(dispatch_failed):
                    await dispatch_failed
            except Exception:
                logger.exception(
                    "Failed to persist dispatch failure for %s",
                    result.authorization.authorization_id,
                )
        elif not dispatch_complete:
            callback = self._intervention_partial_dispatch_callback
            if callback is None:
                logger.critical(
                    "Partially dispatched %s without compensation callback",
                    result.authorization.authorization_id,
                )
            else:
                try:
                    compensation_result = callback(
                        result.authorization.authorization_id,
                        reason="partial_executor_dispatch",
                    )
                    if inspect.isawaitable(compensation_result):
                        compensation_result = await compensation_result
                    if isinstance(
                        compensation_result,
                        InterventionRestoreCommand,
                    ):
                        compensation = compensation_result
                        await self.send_restore_command(
                            compensation,
                            correlation_id=msg.correlation_id,
                            causation_id=str(msg.event_id),
                        )
                except Exception:
                    logger.exception(
                        "Failed to compensate partial dispatch for %s",
                        result.authorization.authorization_id,
                    )
        else:
            self._schedule_intervention_receipt_watchdog(result)
        await self._send_to(
            client,
            MessageType.INTERVENTION_TRANSACTION_STATE.value,
            {
                "intervention_id": result.authorization.intervention_id,
                "authorization_request_id": (
                    result.authorization.authorization_request_id
                ),
                "authorization_id": result.authorization.authorization_id,
                "manifest_sha256": result.authorization.manifest_sha256,
                "state": (
                    "applying"
                    if dispatch_complete
                    else "restoring"
                    if compensation is not None
                    else "failed"
                ),
                "target_count": sent,
                "expected_target_count": expected_targets,
                "compensation_restore_id": (
                    compensation.restore_id if compensation is not None else None
                ),
            },
            correlation_id=msg.correlation_id,
            causation_id=str(msg.event_id),
        )
        if not dispatch_complete:
            denial = AuthorizationDenied(
                authorization_request_id=request.authorization_request_id,
                intervention_id=request.intervention_id,
                manifest_sha256=request.manifest_sha256,
                reason_code="no_executor",
                detail=(
                    "no connected client owns the authorized capability"
                    if dispatch.attempted_targets == 0
                    else "delivery reached only part of the executor set; "
                    "the outcome is ambiguous and compensation was started"
                ),
            )
            await self._send_to(
                client,
                MessageType.INTERVENTION_AUTHORIZATION_DENIED.value,
                denial.model_dump(mode="json"),
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )

    def _schedule_intervention_receipt_watchdog(
        self,
        command: InterventionApplyCommand,
    ) -> None:
        authorization_id = command.authorization.authorization_id
        prior = self._intervention_receipt_watchdogs.pop(
            authorization_id,
            None,
        )
        if prior is not None:
            prior.cancel()
        task = asyncio.create_task(
            self._watch_intervention_receipt_deadline(command),
            name=f"intervention-receipt-{authorization_id}",
        )
        self._intervention_receipt_watchdogs[authorization_id] = task

    async def _watch_intervention_receipt_deadline(
        self,
        command: InterventionApplyCommand,
    ) -> None:
        authorization_id = command.authorization.authorization_id
        try:
            wall_remaining_ms = max(
                0,
                command.authorization.expires_at_unix_ms
                - self._clock.unix_ms(),
            )
            remaining_ms = min(
                command.authorization.ttl_ms,
                wall_remaining_ms,
            )
            if command.authorization.boot_id == self._clock.boot_id:
                elapsed_ms = max(
                    0,
                    self._clock.monotonic_ns()
                    - command.authorization.issued_at_mono_ns,
                ) // 1_000_000
                remaining_ms = min(
                    remaining_ms,
                    max(0, command.authorization.ttl_ms - elapsed_ms),
                )
            await asyncio.sleep(remaining_ms / 1_000)
            callback = self._intervention_partial_dispatch_callback
            if callback is None:
                logger.critical(
                    "Authorization %s reached its receipt deadline without "
                    "a compensation callback",
                    authorization_id,
                )
                return
            compensation = callback(
                authorization_id,
                reason="adapter_receipt_deadline_elapsed",
            )
            if inspect.isawaitable(compensation):
                compensation = await compensation
            if isinstance(compensation, InterventionRestoreCommand):
                await self.send_restore_command(compensation)
                await self.send_message(
                    MessageType.INTERVENTION_TRANSACTION_STATE.value,
                    {
                        "intervention_id": (
                            command.authorization.intervention_id
                        ),
                        "authorization_id": authorization_id,
                        "manifest_sha256": (
                            command.authorization.manifest_sha256
                        ),
                        "state": "restoring",
                        "reason": "adapter_receipt_deadline_elapsed",
                        "compensation_restore_id": compensation.restore_id,
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Receipt-deadline compensation failed for %s",
                authorization_id,
            )
        finally:
            current = self._intervention_receipt_watchdogs.get(
                authorization_id
            )
            if current is asyncio.current_task():
                self._intervention_receipt_watchdogs.pop(
                    authorization_id,
                    None,
                )

    async def _handle_intervention_receipt(
        self,
        client: WebSocketClient,
        msg: WSMessage,
    ) -> None:
        """Validate typed per-action receipts and advance the transaction."""

        try:
            batch = InterventionReceiptBatch.model_validate(msg.payload)
        except ValidationError as exc:
            logger.warning(
                "Invalid INTERVENTION_RECEIPT from %s: %s",
                client.client_id,
                exc,
            )
            return
        callback = self._intervention_receipt_callback
        if callback is None:
            logger.warning("Dropping receipt: transaction callback not configured")
            return
        if client.client_instance_id is None:
            logger.warning(
                "Dropping exact receipt from %s without durable client identity",
                client.client_id,
            )
            return
        try:
            result = callback(
                batch,
                client.client_instance_id,
                client.client_type,
            )
            if inspect.isawaitable(result):
                result = await result
            state, compensation = result
        except Exception:
            logger.exception(
                "INTERVENTION_RECEIPT callback failed for %s",
                batch.intervention_id,
            )
            return
        state_value = getattr(state, "value", state)
        if (
            batch.receipts[0].phase == ReceiptPhase.APPLY.value
            and str(state_value) != InterventionLifecycleState.APPLYING.value
        ):
            watchdog = self._intervention_receipt_watchdogs.pop(
                batch.authorization_id,
                None,
            )
            if watchdog is not None and watchdog is not asyncio.current_task():
                watchdog.cancel()
        await self.send_message(
            MessageType.INTERVENTION_TRANSACTION_STATE.value,
            {
                "intervention_id": batch.intervention_id,
                "authorization_id": batch.authorization_id,
                "manifest_sha256": batch.manifest_sha256,
                "state": str(state_value),
                # A requesting surface may authorize a capability owned by a
                # different adapter. Broadcast the typed, privacy-minimal
                # receipt projection so its pending user gesture resolves to
                # the real per-action outcome rather than an optimistic ACK.
                "receipt_results": [
                    {
                        "action_id": receipt.action_id,
                        "status": getattr(receipt.status, "value", receipt.status),
                        "detail": (
                            receipt.verification_detail
                            or receipt.error_message
                            or receipt.error_code
                            or ""
                        ),
                        "reversible": _receipt_has_restorable_effect(receipt),
                    }
                    for receipt in batch.receipts
                ],
            },
            correlation_id=msg.correlation_id,
        )
        if isinstance(compensation, InterventionRestoreCommand):
            await self.send_restore_command(
                compensation,
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )

    # ── P0 §3.1 / §3.2 / §3.3: history / trends / recap dispatch ─────

    async def _handle_request_session_list(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.1: serve a ``REQUEST_SESSION_LIST`` request.

        Payload shape::

            { "since": float | None, "limit": int }

        The daemon's ``list_sessions`` returns a Pydantic
        ``SessionListResponse`` we then serialise back to the requesting
        client. ``correlation_id`` is echoed so the client can match the
        reply to its in-flight request.
        """
        cb = self._session_list_callback
        if cb is None:
            # Wave-2 P1 (audit-cross-pipeline): clients (popup, history
            # tab) wait synchronously on this reply; silently dropping
            # leaves their loading spinners pinned forever. Echo back a
            # well-formed SESSION_LIST envelope with empty items + an
            # ``error`` field naming the failure mode so the client can
            # surface "history unavailable" instead of hanging.
            logger.warning(
                "REQUEST_SESSION_LIST received but no callback wired; "
                "replying with handler_not_registered",
            )
            await self.send_message(
                MessageType.SESSION_LIST.value,
                {
                    "items": [],
                    "next_cursor": None,
                    "cursor_session_id": None,
                    "total_known": 0,
                    "error": "handler_not_registered",
                },
                target_client_types=(
                    [client.client_type] if client.client_type and client.client_type != "unknown" else None
                ),
                correlation_id=msg.correlation_id,
            )
            return
        payload = msg.payload or {}
        since_raw = payload.get("since")
        try:
            since = float(since_raw) if since_raw is not None else None
        except (TypeError, ValueError):
            since = None
        try:
            limit = int(payload.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        try:
            # The callback is declared ``Callable[..., Awaitable[...]]``;
            # a defensively-registered sync callable is also tolerated by
            # awaiting only when the returned value is awaitable.
            result = cb(since, limit)
            resp = await result if inspect.isawaitable(result) else result
        except Exception:
            logger.exception(
                "session-list callback raised for client %s", client.client_id,
            )
            return
        if resp is None:
            return
        body: dict[str, Any] = (
            resp.model_dump(mode="json")
            if hasattr(resp, "model_dump")
            else dict(resp)
        )
        await self.send_message(
            MessageType.SESSION_LIST.value,
            body,
            target_client_types=(
                [client.client_type] if client.client_type and client.client_type != "unknown" else None
            ),
            correlation_id=msg.correlation_id,
        )

    async def _handle_request_session_detail(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.1: serve a ``REQUEST_SESSION_DETAIL`` request.

        Payload: ``{"session_id": str}``. The daemon's ``get_session``
        validates the id (defense against path traversal) and returns a
        ``SessionDetailResponse`` with either the report or an error
        code (``"not_found"`` / ``"unreadable"``).
        """
        cb = self._session_detail_callback
        if cb is None:
            # Wave-2 P1 (audit-cross-pipeline): mirror the existing
            # ``{report: None, error: ...}`` envelope (used for bad
            # input) so the client always gets a typed reply.
            logger.warning(
                "REQUEST_SESSION_DETAIL received but no callback wired; "
                "replying with handler_not_registered",
            )
            await self.send_message(
                MessageType.SESSION_DETAIL.value,
                {"report": None, "error": "handler_not_registered"},
                target_client_types=(
                    [client.client_type] if client.client_type and client.client_type != "unknown" else None
                ),
                correlation_id=msg.correlation_id,
            )
            return
        payload = msg.payload or {}
        session_id = payload.get("session_id")
        if not isinstance(session_id, str):
            # Reply with an error envelope so the client doesn't hang.
            await self.send_message(
                MessageType.SESSION_DETAIL.value,
                {"report": None, "error": "not_found"},
                target_client_types=(
                    [client.client_type] if client.client_type and client.client_type != "unknown" else None
                ),
                correlation_id=msg.correlation_id,
            )
            return
        try:
            result = cb(session_id)
            resp = await result if inspect.isawaitable(result) else result
        except Exception:
            logger.exception(
                "session-detail callback raised for client %s", client.client_id,
            )
            return
        if resp is None:
            return
        body = (
            resp.model_dump(mode="json")
            if hasattr(resp, "model_dump")
            else dict(resp)
        )
        await self.send_message(
            MessageType.SESSION_DETAIL.value,
            body,
            target_client_types=(
                [client.client_type] if client.client_type and client.client_type != "unknown" else None
            ),
            correlation_id=msg.correlation_id,
        )

    async def _handle_request_trends(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.2: serve a ``REQUEST_TRENDS`` request.

        Payload: ``{"window": "week"|"month"|"quarter", "refresh": bool}``.
        ``window`` defaults to ``"week"`` when missing or invalid; the
        daemon's ``get_trends`` clamps internally too as defense-in-depth.
        """
        cb = self._trends_callback
        if cb is None:
            # Wave-2 P1: the dashboard trends pane waits on the reply.
            # Send a minimal envelope with the error field set so the
            # client can show "trends unavailable" instead of spinning.
            logger.warning(
                "REQUEST_TRENDS received but no callback wired; "
                "replying with handler_not_registered",
            )
            await self.send_message(
                MessageType.TRENDS_PAYLOAD.value,
                {
                    "window": "week",
                    "daily": [],
                    # P2-CONTRACT-3: TrendsResponse.chronotype is non-null
                    # (default_factory=ChronotypeModel). A null here makes a
                    # client that revalidates the frame via
                    # TrendsResponse.model_validate fail; an empty object
                    # round-trips to a default ChronotypeModel.
                    "chronotype": {},
                    "error": "handler_not_registered",
                },
                target_client_types=(
                    [client.client_type] if client.client_type and client.client_type != "unknown" else None
                ),
                correlation_id=msg.correlation_id,
            )
            return
        payload = msg.payload or {}
        window = payload.get("window") if isinstance(payload.get("window"), str) else "week"
        refresh = bool(payload.get("refresh") or False)
        try:
            result = cb(window, refresh=refresh)
            resp = await result if inspect.isawaitable(result) else result
        except Exception:
            logger.exception(
                "trends callback raised for client %s", client.client_id,
            )
            return
        if resp is None:
            return
        body = (
            resp.model_dump(mode="json")
            if hasattr(resp, "model_dump")
            else dict(resp)
        )
        await self.send_message(
            MessageType.TRENDS_PAYLOAD.value,
            body,
            target_client_types=(
                [client.client_type] if client.client_type and client.client_type != "unknown" else None
            ),
            correlation_id=msg.correlation_id,
        )

    async def _handle_request_session_recap(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.3: serve a ``REQUEST_SESSION_RECAP`` request.

        Returns the cached most-recent recap payload (or an empty
        payload if no session has finished this process lifetime).
        """
        cb = self._session_recap_cache_callback
        recap: dict[str, Any] | None = None
        if cb is not None:
            try:
                if asyncio.iscoroutinefunction(cb):
                    recap = await cb()
                else:
                    recap = cb()
            except Exception:
                logger.exception(
                    "session-recap-cache callback raised for client %s",
                    client.client_id,
                )
                recap = None
        await self.send_message(
            MessageType.SESSION_RECAP.value,
            recap or {},
            target_client_types=(
                [client.client_type] if client.client_type and client.client_type != "unknown" else None
            ),
            correlation_id=msg.correlation_id,
        )

    async def _handle_session_recap_acknowledged(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.3 (Wave-2 P1): forward a ``SESSION_RECAP_ACKNOWLEDGED``
        frame to the daemon so its ``stop()`` can stop waiting.

        Payload is informational (``{session_id: str | None}``); the
        daemon flips an :class:`asyncio.Event` unconditionally so a
        slightly mismatched id (the daemon already moved on to a new
        session) still releases the wait.
        """
        callback = self._session_recap_acknowledged_callback
        if callback is None:
            logger.debug(
                "SESSION_RECAP_ACKNOWLEDGED received but no callback wired; dropping",
            )
            return
        payload = msg.payload or {}
        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            session_id = None
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(session_id)
            else:
                callback(session_id)
        except Exception as exc:
            logger.error(
                "session_recap_acknowledged callback error from %s: %s",
                client.client_id,
                exc,
            )

    def _resolve_daemon(self) -> Any:
        """Resolve the daemon through the typed runtime-status port.

        Returns ``None`` when the daemon hasn't registered itself yet
        (e.g. early in startup or in unit tests that construct the WS
        server in isolation). Handlers MUST tolerate this so a missing
        daemon never closes the socket on the peer.
        """
        try:
            if self._runtime_status is not None:
                return self._runtime_status.snapshot().daemon
            # One-release compatibility for isolated fixtures that still
            # compose only a ServiceProvider.
            return self._service_provider().get("daemon")
        except Exception:
            return None

    def _runtime_health_snapshot(self) -> RuntimeStatusSnapshot:
        """Read transport health without string-key discovery in production."""

        if self._runtime_status is not None:
            return self._runtime_status.snapshot()
        services = self._service_provider()
        raw_backend = services.get("store_backend")
        raw_healthy = services.get("store_healthy")
        return RuntimeStatusSnapshot(
            daemon=services.get("daemon"),
            latest_frame_meta=services.get("latest_frame_meta"),
            capture_stale=bool(services.get("capture_stale") or False),
            store_degraded=bool(services.get("store_degraded") or False),
            store_backend=(str(raw_backend) if raw_backend is not None else None),
            store_healthy=(
                bool(raw_healthy) if raw_healthy is not None else None
            ),
        )

    async def _send_daemon_not_ready(
        self,
        client: WebSocketClient,
        msg: WSMessage,
    ) -> None:
        """P2-22: unicast an ``ERROR`` frame to ``client`` indicating the daemon
        is not ready.

        Preserves the ``correlation_id`` from the triggering message so
        the client can match the error back to its pending request.
        Called from every ``_handle_*`` that performs an early return
        when ``_resolve_daemon()`` returns ``None`` (EXCEPT
        ``_handle_cost_request`` which is owned by Agent C).
        """
        await self._send_to(
            client,
            MessageType.ERROR.value,
            {
                "code": "daemon_not_ready",
                "correlation_id": getattr(msg, "correlation_id", None),
            },
            correlation_id=msg.correlation_id,
            causation_id=str(msg.event_id),
        )

    async def _handle_cost_request(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.15: send the current LLM cost snapshot to ``client`` only.

        Broadcasting would leak per-client UI state. A missing daemon
        (early startup) is logged at debug level and silently dropped.
        """
        daemon = self._resolve_daemon()
        if daemon is None or not hasattr(daemon, "get_cost_response"):
            logger.debug("COST_REQUEST received but no daemon; dropping")
            return
        try:
            payload = await daemon.get_cost_response()
            await self._send_to(
                client,
                MessageType.COST_RESPONSE.value,
                payload.model_dump(mode="json"),
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )
        except Exception:
            logger.exception("COST_REQUEST handling failed")

    async def _handle_test_provider(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.19: forward to ``daemon.test_provider`` and reply on the
        same client connection.
        """
        daemon = self._resolve_daemon()
        if daemon is None or not hasattr(daemon, "test_provider"):
            logger.debug("TEST_PROVIDER received but no daemon; sending daemon_not_ready")
            await self._send_daemon_not_ready(client, msg)
            return
        provider = str((msg.payload or {}).get("provider") or "").strip()
        try:
            result = await daemon.test_provider(provider)
            await self._send_to(
                client,
                MessageType.TEST_PROVIDER_RESULT.value,
                result.model_dump(mode="json"),
                correlation_id=msg.correlation_id,
                causation_id=str(msg.event_id),
            )
        except Exception:
            logger.exception("TEST_PROVIDER handling failed")

    async def _handle_goal_set(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.13: forward the user-supplied session goal to the daemon."""
        daemon = self._resolve_daemon()
        if daemon is None or not hasattr(daemon, "set_active_goal"):
            logger.debug("GOAL_SET received but no daemon; sending daemon_not_ready")
            await self._send_daemon_not_ready(client, msg)
            return
        title = str((msg.payload or {}).get("title") or "").strip()
        try:
            await daemon.set_active_goal(title)
        except Exception:
            logger.exception("GOAL_SET handling failed")

    async def _handle_force_recap(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.21: invoke ``daemon.force_recap``."""
        daemon = self._resolve_daemon()
        if daemon is None or not hasattr(daemon, "force_recap"):
            logger.debug("FORCE_RECAP received but no daemon; sending daemon_not_ready")
            await self._send_daemon_not_ready(client, msg)
            return
        try:
            await daemon.force_recap()
        except Exception:
            logger.exception("FORCE_RECAP handling failed")

    async def _handle_dismiss_overlay(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """P0 §3.21: invoke ``daemon.dismiss_active_overlay``."""
        daemon = self._resolve_daemon()
        if daemon is None or not hasattr(daemon, "dismiss_active_overlay"):
            logger.debug("DISMISS_OVERLAY received but no daemon; sending daemon_not_ready")
            await self._send_daemon_not_ready(client, msg)
            return
        try:
            await daemon.dismiss_active_overlay()
        except Exception:
            logger.exception("DISMISS_OVERLAY handling failed")

    async def _send_to(
        self,
        client: WebSocketClient,
        message_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> bool:
        """Send a single typed frame to a single client.

        Used by ``COST_REQUEST`` / ``TEST_PROVIDER`` handlers because the
        reply must be unicast (broadcast would leak per-client UI state).
        """
        self._sequence += 1
        message = WSMessage.from_clock(
            clock=self._clock,
            type=message_type,
            payload=payload,
            sequence=self._sequence,
            correlation_id=correlation_id,
            causation_id=causation_id,
            source_client_type="daemon",
        )
        try:
            await client.websocket.send(message.to_json())
            return True
        except Exception:
            logger.debug(
                "unicast send %s → %s failed",
                message_type,
                client.client_id,
                exc_info=True,
            )
            return False

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

    async def send_intervention(
        self,
        plan: InterventionPlan,
        *,
        action_manifest: ActionManifest | None = None,
        desktop_focused: bool | None = None,
        execution_mode: Literal[
            "suggest_only", "authorized", "research_autonomous"
        ] = "suggest_only",
    ) -> int:
        """
        Send INTERVENTION_TRIGGER to all connected clients.

        Args:
            plan: Intervention plan from LLM.
            desktop_focused: P0 §3.12 — when ``False``, every receiver
                stamps the payload with ``desktop_not_focused: True``.
                Browser extension fires ``chrome.notifications.create``
                and bumps the action badge; VS Code pulses its status
                bar item; the macOS dispatcher fires a
                UNUserNotification. ``None`` means "unknown" — the flag
                is not stamped at all (forward-compatible silent default).

        Returns:
            Number of clients successfully sent to.
        """
        msg = self._make_intervention_trigger(
            plan,
            action_manifest=action_manifest,
            desktop_focused=desktop_focused,
            execution_mode=execution_mode,
        )
        return await self._broadcast(msg)

    def _first_authenticated_client(
        self,
        client_types: set[str],
        *,
        require_instance_id: bool = False,
    ) -> WebSocketClient | None:
        return next(
            (
                client
                for client in self._clients.values()
                if client.authenticated
                and client.client_type in client_types
                and (
                    not require_instance_id
                    or client.client_instance_id is not None
                )
            ),
            None,
        )

    async def _bind_dispatch_targets(
        self,
        command_id: str,
        targets_by_action: dict[str, WebSocketClient],
    ) -> bool:
        callback = self._intervention_dispatch_binding_callback
        if callback is None:
            logger.error(
                "Exact command %s has no dispatch-binding callback; refusing send",
                command_id,
            )
            return False
        bindings = {
            action_id: client.client_instance_id
            for action_id, client in targets_by_action.items()
            if client.client_instance_id is not None
        }
        if len(bindings) != len(targets_by_action):
            logger.error(
                "Exact command %s selected a client without durable identity",
                command_id,
            )
            return False
        try:
            result = callback(command_id, bindings)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception:
            logger.exception(
                "Could not durably bind exact command %s before dispatch",
                command_id,
            )
            return False

    async def dispatch_apply_command(
        self,
        command: InterventionApplyCommand,
        *,
        requesting_client: WebSocketClient | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> ExactDispatchReport:
        """Serialize one exact forward dispatch against inverse dispatches."""

        async with self._intervention_wire_dispatch_lock:
            return await self._dispatch_apply_command_unlocked(
                command,
                requesting_client=requesting_client,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

    async def _dispatch_apply_command_unlocked(
        self,
        command: InterventionApplyCommand,
        *,
        requesting_client: WebSocketClient | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> ExactDispatchReport:
        """Route one consumed command to exactly one client per executor.

        The full command is sent to each selected executor; each client must
        execute only actions whose ``executor`` it owns. Selecting one client
        per executor prevents two Chrome windows from applying the same tab
        operation concurrently.
        """

        needed = {action.executor for action in command.actions}
        expected_targets = len(needed)
        now_unix_ms = self._clock.unix_ms()
        authorization_expired = (
            now_unix_ms >= command.authorization.expires_at_unix_ms
        )
        if command.authorization.boot_id == self._clock.boot_id:
            authorization_expired = authorization_expired or (
                max(
                    0,
                    self._clock.monotonic_ns()
                    - command.authorization.issued_at_mono_ns,
                )
                // 1_000_000
                >= command.authorization.ttl_ms
            )
        manifest_expired = now_unix_ms >= command.manifest.expires_at_unix_ms
        if command.manifest.boot_id == self._clock.boot_id:
            manifest_expired = manifest_expired or (
                max(
                    0,
                    self._clock.monotonic_ns()
                    - command.manifest.created_at_mono_ns,
                )
                // 1_000_000
                >= command.manifest.ttl_ms
            )
        if authorization_expired or manifest_expired:
            return ExactDispatchReport(expected_targets, 0, 0)
        targets_by_executor: dict[str, WebSocketClient] = {}
        if "browser" in needed:
            browser = (
                requesting_client
                if requesting_client is not None
                and requesting_client.authenticated
                and requesting_client.client_type in {"chrome", "edge"}
                and requesting_client.client_instance_id is not None
                else self._first_authenticated_client(
                    {"chrome", "edge"},
                    require_instance_id=True,
                )
            )
            if browser is not None:
                targets_by_executor["browser"] = browser
        if "editor" in needed:
            editor = (
                requesting_client
                if requesting_client is not None
                and requesting_client.authenticated
                and requesting_client.client_type == "vscode"
                and requesting_client.client_instance_id is not None
                else self._first_authenticated_client(
                    {"vscode"},
                    require_instance_id=True,
                )
            )
            if editor is not None:
                targets_by_executor["editor"] = editor
        if "desktop" in needed:
            desktop = (
                requesting_client
                if requesting_client is not None
                and requesting_client.authenticated
                and requesting_client.client_type == "desktop"
                and requesting_client.client_instance_id is not None
                else self._first_authenticated_client(
                    {"desktop"},
                    require_instance_id=True,
                )
            )
            if desktop is not None:
                targets_by_executor["desktop"] = desktop
        # No command is partially routed merely because one required surface
        # is disconnected. A later authorization can be requested after the
        # missing capability reconnects.
        if set(targets_by_executor) != needed:
            return ExactDispatchReport(expected_targets, 0, 0)
        targets_by_action = {
            action.action_id: targets_by_executor[action.executor]
            for action in command.actions
        }
        if not await self._bind_dispatch_targets(
            command.authorization.authorization_id,
            targets_by_action,
        ):
            return ExactDispatchReport(expected_targets, 0, 0)
        targets: list[WebSocketClient] = []
        seen_client_ids: set[str] = set()
        for target in targets_by_action.values():
            if target.client_id not in seen_client_ids:
                seen_client_ids.add(target.client_id)
                targets.append(target)
        sent = 0
        payload = command.model_dump(mode="json")
        for target in targets:
            if await self._send_to(
                target,
                MessageType.INTERVENTION_APPLY.value,
                payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
            ):
                sent += 1
        return ExactDispatchReport(expected_targets, len(targets), sent)

    async def send_apply_command(
        self,
        command: InterventionApplyCommand,
        *,
        requesting_client: WebSocketClient | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """Compatibility projection returning verified send completions."""

        report = await self.dispatch_apply_command(
            command,
            requesting_client=requesting_client,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return report.delivered_targets

    async def send_restore_command(
        self,
        command: InterventionRestoreCommand,
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """Serialize an exact inverse behind any in-flight forward write."""

        async with self._intervention_wire_dispatch_lock:
            return await self._send_restore_command_unlocked(
                command,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

    async def _send_restore_command_unlocked(
        self,
        command: InterventionRestoreCommand,
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """Route exact inverse actions to one client per owning executor."""

        client_types_by_executor = {
            "browser": {"chrome", "edge"},
            "editor": {"vscode"},
            "desktop": {"desktop"},
        }
        targets_by_action: dict[str, WebSocketClient] = {}
        for action in command.actions:
            client_types = client_types_by_executor.get(action.executor, set())
            target = next(
                (
                    client
                    for client in self._clients.values()
                    if client.authenticated
                    and client.client_type in client_types
                    and client.client_instance_id
                    == action.owner_client_instance_id
                ),
                None,
            )
            if target is not None:
                targets_by_action[action.action_id] = target
        # Restoration is idempotent and ownership-scoped. Route to every
        # reachable owner now; missing owners remain pending and receive the
        # same restore_id when they reconnect. Requiring all owners at once
        # would strand an already-mutated online surface behind an offline
        # peer.
        if not targets_by_action:
            return 0
        if not await self._bind_dispatch_targets(
            command.restore_id,
            targets_by_action,
        ):
            return 0
        targets: list[WebSocketClient] = []
        seen_client_ids: set[str] = set()
        for target in targets_by_action.values():
            if target.client_id not in seen_client_ids:
                seen_client_ids.add(target.client_id)
                targets.append(target)
        sent = 0
        payload = command.model_dump(mode="json")
        for target in targets:
            if await self._send_to(
                target,
                MessageType.INTERVENTION_RESTORE.value,
                payload,
                correlation_id=correlation_id,
                causation_id=causation_id,
            ):
                sent += 1
        return sent

    async def send_restore(
        self,
        intervention_id: str,
        *,
        user_action: str,
        command: InterventionRestoreCommand | None = None,
    ) -> int:
        """Send an exact restore, or a presentation-only legacy close cue."""

        if command is not None:
            return await self.send_restore_command(command)
        self._sequence += 1
        return await self._broadcast(
            WSMessage.from_clock(
                clock=self._clock,
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
            WSMessage.from_clock(
                clock=self._clock,
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
            WSMessage.from_clock(
                clock=self._clock,
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
        message = WSMessage.from_clock(
            clock=self._clock,
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

    # Phase-4b TASK I: WS broadcast types eligible for per-client
    # newest-wins coalescing. Adding a type here means a slow client
    # cannot accumulate a backlog of these messages — the queue depth
    # is 1, and a fresh frame evicts any pending older frame. Outbound
    # request-response and INTERVENTION_* paths bypass this queue and
    # go through the direct-send path because their semantics require
    # in-order delivery of every frame.
    _COALESCE_ELIGIBLE: frozenset[str] = frozenset({
        MessageType.STATE_UPDATE.value,
        MessageType.AMBIENT_STATE_UPDATE.value,
    })

    async def _ensure_coalesce_loop(self, client: WebSocketClient) -> None:
        """Phase-4b TASK I: lazily create the per-client coalesce queue +
        consumer task. Idempotent — repeated calls are no-ops once the
        task is running.
        """
        if client.coalesce_queue is not None and client.coalesce_task is not None:
            return
        client.coalesce_queue = asyncio.Queue(maxsize=1)
        client.coalesce_task = asyncio.create_task(
            self._drain_coalesce_queue(client),
            name=f"ws-coalesce-{client.client_id}",
        )

    async def _drain_coalesce_queue(self, client: WebSocketClient) -> None:
        """Phase-4b TASK I: per-client send loop. Awaits the next
        coalesced frame and writes it. A send error / timeout closes
        the socket with the F22 ``slow consumer`` reason and removes
        the client from the registry so the disconnect contract is
        preserved across coalesce-eligible broadcasts.
        """
        queue = client.coalesce_queue
        if queue is None:
            return
        try:
            while True:
                frame = await queue.get()
                reason: str | None = None
                try:
                    await asyncio.wait_for(
                        client.websocket.send(frame),
                        timeout=self._BROADCAST_PER_CLIENT_TIMEOUT_S,
                    )
                except TimeoutError:
                    reason = "slow consumer"
                except Exception:
                    reason = "send error"
                if reason is not None:
                    logger.debug(
                        "coalesce send failed for %s: %s",
                        client.client_id, reason,
                    )
                    # Mirror the F22 contract on the direct-send path:
                    # explicit close + remove + structured disconnect.
                    self._clients.pop(client.client_id, None)
                    await self._close_slow_consumer(client, reason)
                    return
        except asyncio.CancelledError:
            return

    def _coalesce_put_nowait(
        self, client: WebSocketClient, frame: str,
    ) -> bool:
        """Phase-4b TASK I: newest-wins put on the per-client queue.

        Returns True if the frame was queued (with or without evicting
        an older frame), False if no queue exists for the client.

        P1-8: on a confirmed drop (producer raced consumer and lost the
        second put_nowait too), emit a WARNING log and increment the
        ``cortex_ws_coalesce_drops_total`` Prometheus counter so the
        silent-drop rate is observable in /metrics.
        """
        queue = client.coalesce_queue
        if queue is None:
            return False
        try:
            queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(frame)
                return True
            except asyncio.QueueFull:
                # Producer raced with consumer; treat as dropped.
                qs = queue.qsize()
                logger.warning(
                    "WS_COALESCE_DROP client=%s queue_size=%s",
                    client.client_id,
                    qs,
                )
                from cortex.libs.observability.metrics import WS_COALESCE_DROPS_TOTAL
                WS_COALESCE_DROPS_TOTAL.inc()
                return False

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
        # F19: stamp the outgoing message with the caller's correlation id
        # so receivers can echo it back on USER_ACTION / INTERVENTION_APPLIED
        # replies and the full intent-to-effect chain stays traceable.
        if msg.correlation_id is None:
            msg.correlation_id = get_correlation_id()

        self._events.outbound_transport.publish(
            OutboundTransportEvent(
                message_type=str(msg.type),
                payload=dict(msg.payload or {}),
                correlation_id=msg.correlation_id,
                target_client_types=tuple(msg.target_client_types or ()),
            )
        )

        if not self._clients:
            return 0

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

        # Phase-4b TASK I: route coalesce-eligible high-frequency
        # broadcasts (STATE_UPDATE, AMBIENT_STATE_UPDATE) through the
        # per-client newest-wins queue. Slow clients only ever see the
        # latest frame — a backlog cannot build up. INTERVENTION_*,
        # response frames, and any request-response messages bypass
        # this path because they require in-order delivery.
        if msg.type in self._COALESCE_ELIGIBLE:
            queued = 0
            for _cid, client in targets:
                await self._ensure_coalesce_loop(client)
                if self._coalesce_put_nowait(client, payload):
                    queued += 1
            return queued

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
        broadcast_start = monotonic_seconds(self._clock)
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

        elapsed_s = monotonic_seconds(self._clock) - broadcast_start
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
            dead = self._clients.get(client_id)
            if dead is not None:
                del self._clients[client_id]
                await self._close_slow_consumer(dead, reason)
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

        Surfaces the canonical support state, evidence status/coverage,
        deterministic scores, exclusions, and model identity. Probability
        fields remain absent unless a future registered calibrated model runs.
        """
        self._sequence += 1
        # Mirror availability onto the WS envelope. ``rules`` means the
        # registered deterministic engine ran; ``fallback`` is fail-closed.
        # A rules frame can still be degraded while evidence warms or is
        # insufficient, and clients render that status explicitly.
        no_classifier = estimate.classifier_source is None
        evidence_unavailable = estimate.status != "estimated"
        degraded = no_classifier or evidence_unavailable
        envelope_source: Literal["rules", "classifier", "fallback"] = (
            "fallback" if no_classifier else "rules"
        )

        # ── Capture status sub-payload ─────────────────────────────────
        # Surface capture status so the consumer dashboard can render
        # "Camera offline" vs "Looking for your face" vs "Reading your
        # pulse" instead of a bare ``--`` while the rPPG window fills.
        # ``latest_frame_meta`` is stamped on every capture tick by
        # ``runtime_daemon._process_capture_output``; absence here means
        # the capture loop hasn't produced a frame yet (camera not open,
        # permission denied, or daemon mid-startup).
        frames_flowing = False
        face_detected = False
        stale = False
        capture_sequence: int | None = None
        store_degraded = False
        store_backend: str | None = None
        store_healthy: bool | None = None
        try:
            runtime_status = self._runtime_health_snapshot()
            frame_meta = runtime_status.latest_frame_meta
            if frame_meta is not None:
                fm_mono_ns = getattr(frame_meta, "observed_at_mono_ns", None)
                fm_boot_id = getattr(frame_meta, "boot_id", None)
                if isinstance(fm_mono_ns, int) and fm_boot_id == self._clock.boot_id:
                    age_seconds = max(
                        0.0,
                        (self._clock.monotonic_ns() - fm_mono_ns) / 1e9,
                    )
                else:
                    fm_unix_ms = getattr(frame_meta, "observed_at_unix_ms", None)
                    if isinstance(fm_unix_ms, int):
                        age_seconds = max(
                            0.0,
                            (self._clock.unix_ms() - fm_unix_ms) / 1000.0,
                        )
                    else:
                        # One-release v1 compatibility: FrameMeta.timestamp is
                        # documented UTC Unix seconds. Never reinterpret it as
                        # monotonic time.
                        fm_ts = float(getattr(frame_meta, "timestamp", 0.0))
                        age_seconds = max(
                            0.0,
                            self._clock.unix_ms() / 1000.0 - fm_ts,
                        )
                frames_flowing = age_seconds < 2.0
                face_detected = bool(
                    getattr(frame_meta, "face_detected", False)
                )
                seq = getattr(frame_meta, "sequence", None)
                if seq is not None:
                    try:
                        capture_sequence = int(seq)
                    except (TypeError, ValueError):
                        capture_sequence = None
            # Daemon plants this when the capture pipeline fails to
            # start so the very first broadcast carries the offline
            # marker.
            if runtime_status.capture_stale:
                stale = True
            # If there ARE recent frames, capture is healthy regardless of
            # what the stale flag says — clear it so a transient init
            # failure followed by a successful resume doesn't leave the
            # UI stuck in "offline".
            if frames_flowing:
                stale = False
            store_degraded = runtime_status.store_degraded
            store_backend = runtime_status.store_backend
            store_healthy = runtime_status.store_healthy
        except Exception:
            # Health projection is best-effort; never block a broadcast.
            logger.debug("runtime status lookup failed", exc_info=True)

        capture_status = CaptureStatus(
            frames_flowing=frames_flowing,
            face_detected=face_detected,
            stale=stale,
            sequence=capture_sequence,
        )
        # B4 (Phase 4.1): expose the store degradation indicator on
        # every broadcast so a late-joining client still learns the
        # daemon is running on an in-memory store.
        store_health = StoreHealth(
            degraded=store_degraded,
            backend=store_backend,
            healthy=store_healthy,
        )

        # ── Biometrics sub-payload (optional) ──────────────────────────
        # The producer omits the ``biometrics`` key entirely when the
        # incoming dict is empty / None, so a late-joining client
        # doesn't see a misleading all-null bundle on the first frame.
        biometrics_model: BiometricsSummary | None
        if biometrics:
            biometrics_model = BiometricsSummary.model_validate(biometrics)
        else:
            biometrics_model = None

        # ── Build typed payload model ───────────────────────────────────
        payload_model = StateUpdatePayload(
            state=estimate.state,
            support_state=estimate.support_state,
            status=estimate.status,
            confidence=estimate.confidence,
            scores=estimate.scores,
            support_scores=estimate.support_scores,
            evidence_coverage=estimate.evidence_coverage,
            contributing_features=estimate.contributing_features,
            exclusions=estimate.exclusions,
            model=estimate.model,
            probabilities=estimate.probabilities,
            signal_quality=estimate.signal_quality,
            dwell_seconds=estimate.dwell_seconds,
            reasons=list(estimate.reasons),
            stress_integral=estimate.stress_integral,
            calibrated_probabilities=estimate.__dict__.get(
                "calibrated_probabilities"
            ),
            classifier_source=estimate.classifier_source,
            classifier_alpha=estimate.classifier_alpha,
            source=envelope_source,
            degraded=degraded,
            timestamp=_serialize_timestamp(
                estimate.__dict__.get("timestamp")
            ),
            observed_at_unix_ms=estimate.observed_at_unix_ms,
            observed_at_mono_ns=estimate.observed_at_mono_ns,
            boot_id=estimate.boot_id,
            # G1 (audit-prod): stamp the deduped list of
            # currently-IDENTIFY-ed client types so consumers (desktop
            # dashboard) can light up the Chrome / Edge / Editor
            # connection dots without subscribing to a separate event
            # stream.
            connected_clients=self.connected_client_types(),
            capture=capture_status,
            store=store_health,
            biometrics=biometrics_model,
            sequence=self._sequence,
        )

        return WSMessage.from_clock(
            clock=self._clock,
            type=MessageType.STATE_UPDATE,
            payload=payload_model.model_dump(mode="json"),
            sequence=self._sequence,
            source_client_type="daemon",
        )

    def _make_intervention_trigger(
        self,
        plan: InterventionPlan,
        *,
        action_manifest: ActionManifest | None = None,
        desktop_focused: bool | None = None,
        execution_mode: Literal[
            "suggest_only", "authorized", "research_autonomous"
        ] = "suggest_only",
    ) -> WSMessage:
        """Create an INTERVENTION_TRIGGER message.

        Surfaces ``causal_explanation`` (so the VS Code "Why this?" panel
        and the popup transparency section can render the grounded
        rationale), ``consent_level`` (the consent gate that produced this
        plan), and ``plan_warnings`` (degradations the planner applied).

        P0 §3.12: when ``desktop_focused`` is False, stamps
        ``desktop_not_focused: True`` so receivers know to surface the
        cue via OS-level channels (chrome.notifications, VS Code status
        bar pulse) instead of relying on the dashboard's overlay.
        """
        self._sequence += 1

        # Audit-prod fix (G4 P0): mirror the dashboard's connected-clients
        # snapshot onto the intervention trigger so the WS-mode overlay's
        # action buttons gate on the same authoritative list the
        # STATE_UPDATE flow uses. Without this the WS-mode overlay always
        # renders browser-bound actions disabled.
        connected = self.connected_client_types()

        # P0 §3.12: stamp the focus state when known so receivers can
        # surface OS-level notification cues for users on another Space
        # or in fullscreen. ``None`` means "unknown"; only stamp the
        # flag when we explicitly observed unfocused.
        desktop_not_focused: bool | None
        if desktop_focused is False:
            desktop_not_focused = True
        else:
            desktop_not_focused = None

        # Phase-4 Debt-1 closure: construct the typed
        # ``InterventionTriggerPayload`` (an ``InterventionPlan`` subclass
        # with two optional envelope-level fields appended) by re-dumping
        # the plan and re-validating. The wire shape stays flat —
        # ``payload.intervention_id`` resolves directly, no nested
        # ``payload.plan.intervention_id`` indirection — so the existing
        # browser-extension consumers don't need changes.
        plan_dict = plan.model_dump(mode="json")
        plan_dict["action_manifest"] = (
            action_manifest.model_dump(mode="json")
            if action_manifest is not None
            else None
        )
        plan_dict["desktop_not_focused"] = desktop_not_focused
        plan_dict["connected_clients"] = connected
        plan_dict["execution_mode"] = execution_mode
        payload_model = InterventionTriggerPayload.model_validate(plan_dict)

        # F16-srv: stamp a deterministic cid per intervention emission so a
        # later USER_ACTION can be matched against the active emission.
        cid = f"iv_{plan.intervention_id}_{self._sequence}"
        if plan.intervention_id:
            self._active_intervention_cid[plan.intervention_id] = cid

        # Drop ``desktop_not_focused`` when None so the wire shape
        # matches the legacy "only present when explicitly set"
        # contract — receivers branch on key-existence rather than
        # value-truthiness (see test_os_notification_routing). The
        # ``connected_clients`` field is always present per the G4
        # closure since the producer always knows its current list,
        # but we still defensively drop it if the model dumped None.
        payload_dict = payload_model.model_dump(mode="json")
        if payload_dict.get("desktop_not_focused") is None:
            payload_dict.pop("desktop_not_focused", None)
        if payload_dict.get("connected_clients") is None:
            payload_dict.pop("connected_clients", None)

        return WSMessage.from_clock(
            clock=self._clock,
            type=MessageType.INTERVENTION_TRIGGER,
            payload=payload_dict,
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
