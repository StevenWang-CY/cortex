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

from cortex.libs.config.settings import APIConfig
from cortex.libs.schemas.intervention import InterventionPlan
from cortex.libs.schemas.state import StateEstimate

logger = logging.getLogger(__name__)


@dataclass
class WebSocketClient:
    """Represents a connected WebSocket client."""

    client_id: str
    websocket: Any  # websockets.WebSocketServerProtocol
    connected_at: float = field(default_factory=time.monotonic)
    client_type: str = "unknown"  # "vscode", "chrome", "desktop", "unknown"
    last_message_at: float = 0.0


@dataclass
class WSMessage:
    """WebSocket message envelope."""

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
    def from_json(cls, data: str) -> WSMessage:
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
        self._activity_sync_callback: Any = None

        # Latest state for new connections
        self._latest_state: StateEstimate | None = None
        self._pending_context_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def connected_clients(self) -> list[str]:
        return list(self._clients.keys())

    def set_user_action_callback(self, callback: Any) -> None:
        """Set callback for USER_ACTION messages from extensions."""
        self._user_action_callback = callback

    def set_settings_callback(self, callback: Any) -> None:
        """Set callback for SETTINGS_SYNC messages from clients."""
        self._settings_callback = callback

    def set_activity_sync_callback(self, callback: Any) -> None:
        """Set callback for ACTIVITY_SYNC messages from browser extension."""
        self._activity_sync_callback = callback

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
        """Handle a new WebSocket client connection."""
        client_id = f"client_{id(websocket)}"
        client = WebSocketClient(
            client_id=client_id,
            websocket=websocket,
        )
        self._clients[client_id] = client
        logger.info(f"Client connected: {client_id}")

        # Send current state to new client if available
        if self._latest_state is not None:
            try:
                msg = self._make_state_update(self._latest_state)
                await websocket.send(msg.to_json())
            except Exception:
                pass

        try:
            async for raw_message in websocket:
                await self._process_message(client, raw_message)
        except Exception as e:
            logger.debug(f"Client {client_id} disconnected: {e}")
        finally:
            self._clients.pop(client_id, None)
            logger.info(f"Client disconnected: {client_id}")

    async def _process_message(
        self, client: WebSocketClient, raw: str,
    ) -> None:
        """Process an incoming message from a client."""
        try:
            msg = WSMessage.from_json(raw)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Invalid message from {client.client_id}: {e}")
            return

        client.last_message_at = time.monotonic()

        if msg.type == "USER_ACTION":
            await self._handle_user_action(client, msg)
        elif msg.type == "ACTION_EXECUTE":
            await self._handle_user_action(client, msg)
        elif msg.type == "IDENTIFY":
            # Client identifying its type
            client.client_type = msg.payload.get("client_type", "unknown")
            logger.info(
                f"Client {client.client_id} identified as {client.client_type}"
            )
        elif msg.type == "CONTEXT_RESPONSE":
            self._handle_context_response(msg)
        elif msg.type == "SETTINGS_SYNC":
            await self._handle_settings_sync(client, msg)
        elif msg.type == "ACTIVITY_SYNC":
            await self._handle_activity_sync(client, msg)
        else:
            logger.debug(f"Unknown message type from {client.client_id}: {msg.type}")

    async def _handle_user_action(
        self, client: WebSocketClient, msg: WSMessage,
    ) -> None:
        """Handle USER_ACTION message from extension."""
        action = msg.payload.get("action")
        intervention_id = msg.payload.get("intervention_id")

        logger.info(
            f"User action from {client.client_id}: {action} "
            f"(intervention: {intervention_id})"
        )

        if self._user_action_callback is not None:
            try:
                if asyncio.iscoroutinefunction(self._user_action_callback):
                    await self._user_action_callback(msg.payload)
                else:
                    self._user_action_callback(msg.payload)
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

    async def _handle_settings_sync(self, client: WebSocketClient, msg: WSMessage) -> None:
        """Forward settings updates to the daemon."""
        if self._settings_callback is None:
            return
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
                type="INTERVENTION_RESTORE",
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
                type="SETTINGS_SYNC",
                payload=settings,
                sequence=self._sequence,
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
        message = WSMessage(
            type="CONTEXT_REQUEST",
            payload={},
            sequence=self._sequence,
            correlation_id=correlation_id,
            target_client_types=[client_type],
            source_client_type="daemon",
        )
        try:
            await target.websocket.send(message.to_json())
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_context_requests.pop(correlation_id, None)
            logger.debug("Context request to %s timed out", client_type)
            return {}
        except Exception:
            self._pending_context_requests.pop(correlation_id, None)
            logger.exception("Context request to %s failed", client_type)
            return {}

    async def _broadcast(self, msg: WSMessage) -> int:
        """Broadcast a message to all connected clients."""
        if not self._clients:
            return 0

        sent = 0
        dead_clients: list[str] = []

        target_types = set(msg.target_client_types or [])
        for client_id, client in self._clients.items():
            if target_types and client.client_type not in target_types:
                continue
            try:
                await asyncio.wait_for(client.websocket.send(msg.to_json()), timeout=1.0)
                sent += 1
            except Exception:
                dead_clients.append(client_id)

        # Clean up dead connections
        for client_id in dead_clients:
            self._clients.pop(client_id, None)
            logger.debug(f"Removed dead client: {client_id}")

        return sent

    def _make_state_update(
        self,
        estimate: StateEstimate,
        biometrics: dict[str, float | None] | None = None,
    ) -> WSMessage:
        """Create a STATE_UPDATE message."""
        self._sequence += 1
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
        }
        if biometrics:
            payload["biometrics"] = biometrics
        return WSMessage(
            type="STATE_UPDATE",
            payload=payload,
            sequence=self._sequence,
            source_client_type="daemon",
        )

    def _make_intervention_trigger(
        self, plan: InterventionPlan,
    ) -> WSMessage:
        """Create an INTERVENTION_TRIGGER message."""
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
        }
        if plan.error_analysis is not None:
            payload["error_analysis"] = plan.error_analysis.model_dump()
        if plan.tab_recommendations is not None:
            payload["tab_recommendations"] = plan.tab_recommendations.model_dump()
        return WSMessage(
            type="INTERVENTION_TRIGGER",
            payload=payload,
            sequence=self._sequence,
            source_client_type="daemon",
        )

    def reset(self) -> None:
        """Reset server state (does not stop the server)."""
        self._sequence = 0
        self._latest_state = None
        self._pending_context_requests.clear()
