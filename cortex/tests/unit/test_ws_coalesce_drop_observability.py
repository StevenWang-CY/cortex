"""P1-8: WS coalesce drop emits a WARNING log and increments the
cortex_ws_coalesce_drops_total Prometheus counter.

The drop path is the innermost ``except asyncio.QueueFull`` branch in
``_coalesce_put_nowait`` — the producer raced the consumer and lost the
second ``put_nowait`` attempt.

Test strategy
-------------
1. Create a ``WebSocketClient`` with ``coalesce_queue = asyncio.Queue(maxsize=1)``.
2. Pre-fill the queue with a sentinel frame so it is full.
3. Patch the ``get_nowait()`` call to raise ``asyncio.QueueEmpty`` (simulating
   the consumer raced us) so the evict-then-put path also ends up full.
4. Call ``_coalesce_put_nowait`` and assert:
   a. Return value is ``False``.
   b. A WARNING log was emitted containing ``WS_COALESCE_DROP``.
   c. The ``WS_COALESCE_DROPS_TOTAL`` counter incremented.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from cortex.libs.observability.metrics import WS_COALESCE_DROPS_TOTAL
from cortex.services.api_gateway.websocket_server import WebSocketClient, WebSocketServer


def _make_client(client_id: str = "test-c1") -> WebSocketClient:
    class _FakeSock:
        async def send(self, _raw: str) -> None:
            return

    client = WebSocketClient(client_id=client_id, websocket=_FakeSock(), client_type="chrome")
    client.coalesce_queue = asyncio.Queue(maxsize=1)
    return client


@pytest.mark.asyncio
async def test_coalesce_drop_logs_warning(caplog) -> None:
    """A confirmed drop emits a WARNING with WS_COALESCE_DROP in the message."""
    server = WebSocketServer()
    client = _make_client()

    # Fill the queue so the first put_nowait in _coalesce_put_nowait succeeds
    # normally, but we want to hit the inner QueueFull branch.
    # Strategy: pre-fill the queue AND make get_nowait raise QueueEmpty so the
    # evict step does nothing, then the second put_nowait fails with QueueFull.
    queue = client.coalesce_queue

    # Pre-fill the queue with a frame.
    queue.put_nowait("__existing_frame__")
    assert queue.full()

    # Patch queue.get_nowait to raise QueueEmpty (simulate consumer raced us).
    original_get_nowait = queue.get_nowait  # noqa: F841 — kept for symmetry with restore pattern used elsewhere in this suite

    def _raise_empty():
        raise asyncio.QueueEmpty

    queue.get_nowait = _raise_empty

    with caplog.at_level(logging.WARNING, logger="cortex.services.api_gateway.websocket_server"):
        result = server._coalesce_put_nowait(client, "__new_frame__")

    assert result is False, "Drop path must return False"
    assert any("WS_COALESCE_DROP" in r.message for r in caplog.records), (
        f"Expected WS_COALESCE_DROP in log records; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_coalesce_drop_increments_counter() -> None:
    """A confirmed drop increments WS_COALESCE_DROPS_TOTAL."""
    server = WebSocketServer()
    client = _make_client("counter-client")

    queue = client.coalesce_queue
    queue.put_nowait("__existing__")

    def _raise_empty():
        raise asyncio.QueueEmpty

    queue.get_nowait = _raise_empty

    before = _get_counter_value(WS_COALESCE_DROPS_TOTAL)
    server._coalesce_put_nowait(client, "__dropped__")
    after = _get_counter_value(WS_COALESCE_DROPS_TOTAL)

    assert after == before + 1, (
        f"Counter must increment by 1 on drop; was {before}, now {after}"
    )


def _get_counter_value(counter) -> float:
    """Read the current value from a prometheus_client Counter."""
    # prometheus_client Counter exposes ._value.get() on the internal
    # _ValueClass. For the default (non-multiprocess) registry this is
    # the direct float value.
    try:
        return counter._value.get()
    except AttributeError:
        # Fallback for older prometheus_client versions.
        return float(counter._metrics.get((), counter)._value.get() if hasattr(counter, "_metrics") else 0)


@pytest.mark.asyncio
async def test_normal_put_no_warning(caplog) -> None:
    """A put into an empty queue succeeds silently — no WARNING emitted."""
    server = WebSocketServer()
    client = _make_client("normal-c")

    with caplog.at_level(logging.WARNING, logger="cortex.services.api_gateway.websocket_server"):
        result = server._coalesce_put_nowait(client, "__frame__")

    assert result is True
    drop_records = [r for r in caplog.records if "WS_COALESCE_DROP" in r.message]
    assert drop_records == [], f"No drop warning expected; got {drop_records}"


@pytest.mark.asyncio
async def test_no_queue_returns_false(caplog) -> None:
    """When coalesce_queue is None the function returns False without logging."""
    server = WebSocketServer()
    client = _make_client("no-q")
    client.coalesce_queue = None

    with caplog.at_level(logging.WARNING, logger="cortex.services.api_gateway.websocket_server"):
        result = server._coalesce_put_nowait(client, "__frame__")

    assert result is False
    drop_records = [r for r in caplog.records if "WS_COALESCE_DROP" in r.message]
    assert drop_records == []
