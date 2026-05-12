"""
Test Intervention — Send a mock intervention to the Chrome extension.

Starts a minimal WebSocket server on port 9473 and sends a realistic
INTERVENTION_TRIGGER with tab_recommendations, error_analysis, and
suggested_actions to test the preview → confirm → execute flow.

Usage:
    python -m cortex.scripts.test_intervention
"""

from __future__ import annotations

import asyncio
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_intervention")

# --- Mock Intervention Payload ---

def make_mock_intervention() -> dict:
    return {
        "type": "MORNING_BRIEFING",
        "payload": {
            "summary": "You spent the last hour working on the Cortex Ethereal UI Overhaul, specifically dialing in the SVG geometric curvature of the central logo inside newtab.tsx. You were experimenting with the arc sweeps and scaling transformations to get the perfect elegant 'C' wrap. Looks like the geometric math checked out perfectly.",
            "action_items": [
                "Verify the extreme 250-degree C curvature in the popup UI",
                "Reload the extension in Chrome to pick up the new icon cache",
                "Push the completely rebuilt visual codebase to your version control",
                "Start exploring the Activity Context grouping features next"
            ],
            "left_off_at": "Refining the UI logomark"
        }
    }


# --- WebSocket Server ---

async def run_test_server() -> None:
    """Start a WebSocket server that sends a mock intervention on connect."""
    try:
        import websockets
    except ImportError:
        logger.error(
            "websockets package not installed. Run: pip install websockets"
        )
        return

    clients: set = set()
    intervention_sent = False

    async def handler(websocket):
        nonlocal intervention_sent
        client_addr = websocket.remote_address
        clients.add(websocket)
        logger.info("Client connected from %s (%d total)", client_addr, len(clients))

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")
                    logger.info("Received: %s", msg_type)

                    if msg_type == "IDENTIFY":
                        logger.info(
                            "Client identified as: %s",
                            msg.get("payload", {}).get("client_type", "unknown"),
                        )

                    elif msg_type == "EXECUTE_ALL_RECOMMENDED":
                        logger.info(
                            "User confirmed! Executing %d actions",
                            len(msg.get("actions", [])),
                        )
                        # Respond with success for each action
                        results = [
                            {"success": True, "action_type": a.get("action_type")}
                            for a in msg.get("actions", [])
                        ]
                        await websocket.send(json.dumps({
                            "type": "ACTION_RESULTS",
                            "payload": {"results": results},
                        }))

                    elif msg_type == "USER_ACTION":
                        action = msg.get("payload", {}).get("action", "")
                        logger.info("User action: %s", action)

                    elif msg_type == "ACTION_EXECUTE":
                        logger.info(
                            "Action executed: %s",
                            msg.get("payload", {}).get("action_type", "?"),
                        )

                except json.JSONDecodeError:
                    logger.warning("Bad JSON from client")
        except Exception as e:
            logger.info("Client disconnected: %s", e)
        finally:
            clients.discard(websocket)
            logger.info("Client removed (%d remaining)", len(clients))

    async def send_intervention_after_delay():
        """Wait for a client, then send the intervention after a short delay."""
        while True:
            if clients:
                await asyncio.sleep(2)  # Broadcast every 2 seconds constantly!
                if clients:
                    intervention = make_mock_intervention()
                    msg = json.dumps(intervention)
                    for ws in clients.copy():
                        try:
                            await ws.send(msg)
                            logger.info(f"Sent Mock Briefing to {ws.remote_address}")
                        except Exception as e:
                            logger.error(f"Failed to send: {e}")
            else:
                await asyncio.sleep(1)

    logger.info("Starting WebSocket server on ws://127.0.0.1:9473")
    logger.info("Waiting for Chrome extension to connect...")
    logger.info("Steps:")
    logger.info("  1. Open chrome://extensions")
    logger.info("  2. Enable Developer mode (top right toggle)")
    logger.info("  3. Click 'Load unpacked'")
    logger.info("  4. Select: cortex/apps/browser_extension/build/chrome-mv3-prod")
    logger.info("  5. Open any webpage — the intervention will appear in ~2s")
    logger.info("")
    logger.info("Press Ctrl+C to stop.")

    async with websockets.serve(handler, "127.0.0.1", 9473):
        await send_intervention_after_delay()
        # Keep running
        await asyncio.Future()


def main() -> None:
    try:
        asyncio.run(run_test_server())
    except KeyboardInterrupt:
        logger.info("Server stopped.")


if __name__ == "__main__":
    main()
