import asyncio
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("test_briefing")

def make_mock_briefing() -> dict:
    return {
        "type": "MORNING_BRIEFING",
        "payload": {
            "summary": "You spent the last hour working on the Cortex Ethereal UI Overhaul, specifically dialing in the SVG geometric curvature of the central logo inside newtab.tsx. You were experimenting with the arc sweeps and scaling transformations to get the perfect elegant 'C' wrap.",
            "action_items": [
                "Verify the extreme 250-degree C curvature in the popup UI",
                "Reload the extension in Chrome to pick up the new icon cache",
                "Push the new codebase to version control"
            ],
            "left_off_at": "Refining the UI logomark"
        }
    }

async def run_test_server() -> None:
    try:
        import websockets
    except ImportError:
        logger.error("pip install websockets")
        return

    clients = set()
    briefing_sent = False

    async def handler(websocket):
        nonlocal briefing_sent
        clients.add(websocket)
        logger.info("Client connected!")
        try:
            async for _ in websocket:
                pass
        except Exception:
            pass
        finally:
            clients.discard(websocket)

    async def send_after_delay():
        nonlocal briefing_sent
        while True:
            if clients and not briefing_sent:
                await asyncio.sleep(1)
                if clients:
                    for ws in clients.copy():
                        try:
                            await ws.send(json.dumps(make_mock_briefing()))
                        except Exception:
                            pass
                    briefing_sent = True
                    logger.info("\n=== MORNING BRIEFING SENT! ===\nOpen your Cortex Chrome Popup to see 'Where you left off'.")
            await asyncio.sleep(1)

    logger.info("WebSocket listening on ws://127.0.0.1:9473...")
    async with websockets.serve(handler, "127.0.0.1", 9473):
        await send_after_delay()
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(run_test_server())
