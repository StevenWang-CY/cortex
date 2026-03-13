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
import time
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_intervention")

# --- Mock Intervention Payload ---

def make_mock_intervention() -> dict:
    """Build a realistic intervention payload with all new features."""
    return {
        "type": "INTERVENTION_TRIGGER",
        "payload": {
            "intervention_id": f"test-{uuid.uuid4().hex[:8]}",
            "level": "simplified_workspace",
            "headline": "Too many tabs — let's focus",
            "situation_summary": (
                "You have 12 tabs open across multiple topics. "
                "Several are unrelated to your current task (PyTorch CUDA debugging). "
                "Let's organize them so you can focus."
            ),
            "primary_focus": "PyTorch CUDA error in train.py",
            "micro_steps": [
                "Fix the CUDA device mismatch in train.py line 42",
                "Check the PyTorch docs tab for .to(device) usage",
                "Run the training script again with CUDA_LAUNCH_BLOCKING=1",
            ],
            "hide_targets": [
                "browser_tabs_except_active",
                "terminal_lines_before_last_error_block",
            ],
            "ui_plan": {
                "dim_background": True,
                "show_overlay": True,
                "fold_unrelated_code": True,
                "intervention_type": "simplified_workspace",
            },
            "tone": "direct",
            "suggested_actions": [
                {
                    "action_id": f"act-{uuid.uuid4().hex[:8]}",
                    "action_type": "close_tab",
                    "tab_index": 3,
                    "target": None,
                    "label": "Close Reddit tab",
                    "reason": "Not related to your current task",
                    "category": "recommended",
                    "reversible": True,
                    "group_id": None,
                    "metadata": {"tab_title": "Reddit - r/funny"},
                },
                {
                    "action_id": f"act-{uuid.uuid4().hex[:8]}",
                    "action_type": "close_tab",
                    "tab_index": 5,
                    "target": None,
                    "label": "Close YouTube tab",
                    "reason": "Media distraction",
                    "category": "recommended",
                    "reversible": True,
                    "group_id": None,
                    "metadata": {"tab_title": "YouTube - Lo-fi beats"},
                },
                {
                    "action_id": f"act-{uuid.uuid4().hex[:8]}",
                    "action_type": "close_tab",
                    "tab_index": 8,
                    "target": None,
                    "label": "Close Twitter tab",
                    "reason": "Social media not relevant",
                    "category": "recommended",
                    "reversible": True,
                    "group_id": None,
                    "metadata": {"tab_title": "Twitter / X"},
                },
                {
                    "action_id": f"act-{uuid.uuid4().hex[:8]}",
                    "action_type": "close_tab",
                    "tab_index": 9,
                    "target": None,
                    "label": "Close Gmail tab",
                    "reason": "Email can wait",
                    "category": "recommended",
                    "reversible": True,
                    "group_id": None,
                    "metadata": {"tab_title": "Gmail"},
                },
                {
                    "action_id": f"act-{uuid.uuid4().hex[:8]}",
                    "action_type": "close_tab",
                    "tab_index": 11,
                    "target": None,
                    "label": "Close arXiv paper tab",
                    "reason": "Paper reading can wait",
                    "category": "recommended",
                    "reversible": True,
                    "group_id": None,
                    "metadata": {"tab_title": "arXiv — Attention Is All You Need"},
                },
            ],
            "error_analysis": {
                "error_type": "runtime",
                "root_cause": (
                    "Tensor device mismatch: model is on cuda:0 but input tensor "
                    "is on cpu. The data loader is not moving batches to GPU before "
                    "forward pass."
                ),
                "suggested_fix": (
                    "Add `inputs = inputs.to(device)` and `labels = labels.to(device)` "
                    "after unpacking the batch in the training loop (train.py:42)."
                ),
                "search_query": "RuntimeError expected all tensors to be on the same device pytorch",
                "relevant_doc_url": "https://pytorch.org/docs/stable/tensor_attributes.html#torch.device",
            },
            "tab_recommendations": {
                "tabs": [
                    {
                        "tab_index": 0,
                        "tab_title": "VS Code — train.py",
                        "action": "keep",
                        "reason": "Active file with the error",
                        "relevance_score": 1.0,
                    },
                    {
                        "tab_index": 1,
                        "tab_title": "PyTorch Docs — CUDA Semantics",
                        "action": "keep",
                        "reason": "Directly relevant to debugging CUDA device issues",
                        "relevance_score": 0.95,
                    },
                    {
                        "tab_index": 2,
                        "tab_title": "GitHub — your-repo/ml-project",
                        "action": "keep",
                        "reason": "Your project repository",
                        "relevance_score": 0.85,
                    },
                    {
                        "tab_index": 3,
                        "tab_title": "Reddit — r/funny",
                        "action": "close",
                        "reason": "Not relevant to debugging",
                        "relevance_score": 0.05,
                    },
                    {
                        "tab_index": 4,
                        "tab_title": "Stack Overflow — CUDA out of memory",
                        "action": "keep",
                        "reason": "Relevant CUDA troubleshooting",
                        "relevance_score": 0.8,
                    },
                    {
                        "tab_index": 5,
                        "tab_title": "YouTube — Lo-fi beats to study to",
                        "action": "close",
                        "reason": "Media distraction during debugging",
                        "relevance_score": 0.1,
                    },
                    {
                        "tab_index": 6,
                        "tab_title": "Stack Overflow — tensor device mismatch",
                        "action": "group",
                        "reason": "Related to your current error",
                        "relevance_score": 0.75,
                        "group_name": "CUDA Error References",
                    },
                    {
                        "tab_index": 7,
                        "tab_title": "PyTorch Forums — DataLoader GPU",
                        "action": "group",
                        "reason": "Related to your current error",
                        "relevance_score": 0.7,
                        "group_name": "CUDA Error References",
                    },
                    {
                        "tab_index": 8,
                        "tab_title": "Twitter / X",
                        "action": "close",
                        "reason": "Social media not relevant",
                        "relevance_score": 0.02,
                    },
                    {
                        "tab_index": 9,
                        "tab_title": "Gmail",
                        "action": "close",
                        "reason": "Email can wait while debugging",
                        "relevance_score": 0.1,
                    },
                    {
                        "tab_index": 10,
                        "tab_title": "Google — pytorch cuda error",
                        "action": "keep",
                        "reason": "Active search for your error",
                        "relevance_score": 0.6,
                    },
                    {
                        "tab_index": 11,
                        "tab_title": "arXiv — Attention Is All You Need",
                        "action": "close",
                        "reason": "Paper reading can wait",
                        "relevance_score": 0.15,
                    },
                ],
                "summary": (
                    "Keeping 5 tabs directly related to your CUDA debugging task. "
                    "Hiding 5 distraction/low-relevance tabs. "
                    "Grouping 2 StackOverflow tabs about the same CUDA error."
                ),
            },
        },
        "timestamp": time.time(),
        "sequence": 1,
        "source_client_type": "daemon",
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
        nonlocal intervention_sent
        while True:
            if clients and not intervention_sent:
                await asyncio.sleep(2)  # Give the extension time to settle
                if clients:
                    intervention = make_mock_intervention()
                    msg_json = json.dumps(intervention)
                    for ws in clients.copy():
                        try:
                            await ws.send(msg_json)
                            logger.info(
                                "Sent INTERVENTION_TRIGGER to client (id=%s)",
                                intervention["payload"]["intervention_id"],
                            )
                        except Exception:
                            pass
                    intervention_sent = True
                    logger.info(
                        "\n"
                        "=" * 50 + "\n"
                        "  INTERVENTION SENT!\n"
                        "  Switch to Chrome to see the overlay.\n"
                        "=" * 50
                    )
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
