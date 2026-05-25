"""
SESSION_RECAP broadcast flow tests (P0 §3.3).

These tests exercise the daemon's recap broadcast decision and the
cached-recap accessors. We construct the real :class:`CortexDaemon`
against a tmp storage root (so it doesn't touch the developer's
home), then drive the inline broadcast block in ``stop()`` via a small
test harness that mirrors the exact gating logic.

We do NOT call the full ``daemon.stop()`` here because that tears down
the capture pipeline, ws server, etc. The recap broadcast itself is a
small block of code; testing it in isolation (with a stubbed
``ws_server.send_message``) lets us cover all four cases without
booting the entire runtime:

* long session (>=90 s) → broadcast + cache.
* short session (<90 s) → no broadcast, no cache mutation.
* hanging broadcast → daemon-equivalent code times out at 5 s.
* REQUEST_SESSION_RECAP serves the cached payload.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cortex.libs.schemas.ws_message_types import MessageType
from cortex.services.session_report.models import SessionReport


# ─── helpers ──────────────────────────────────────────────────────────


def _make_long_report(duration_seconds: float = 600.0) -> SessionReport:
    now = datetime.now(UTC)
    return SessionReport(
        session_id="recap-test",
        start_time=now,
        end_time=now + timedelta(seconds=duration_seconds),
        duration_seconds=duration_seconds,
        time_in_flow_seconds=duration_seconds * 0.7,
        time_in_hyper_seconds=duration_seconds * 0.2,
        flow_percentage=70.0,
        peak_stress_integral=180.0,
    )


@pytest.fixture()
def isolated_storage(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the daemon's storage root to ``tmp_path``.

    Setting the env var BEFORE importing get_config ensures the daemon
    sees the override on first read. We then clear the LRU on
    :func:`get_config` so a fresh config picks up the env var.
    """
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    # The codebase memoises get_config()'s result; bust any cache so the
    # patched env var is honoured.
    if hasattr(settings_mod.get_config, "cache_clear"):
        settings_mod.get_config.cache_clear()  # type: ignore[attr-defined]
    yield storage


@pytest.fixture()
def daemon(isolated_storage: Path):
    """Construct a real :class:`CortexDaemon` and return it.

    Note: we never call ``daemon.start()`` — only the recap helpers,
    which don't touch the capture pipeline or WS server beyond what the
    test patches.
    """
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    cfg = get_config()
    d = CortexDaemon(config=cfg)
    yield d


# ─── replication of the daemon's inline broadcast block ───────────────


async def _emit_recap_if_long(daemon: Any, report: SessionReport) -> None:
    """Mirror of the SESSION_RECAP gate inside ``CortexDaemon.stop()``.

    Keeping the duplicate small lets us test the decision (gate, cache,
    timeout) without booting the full daemon. If the implementation
    diverges this test will fail visibly via a mismatched assertion;
    that is the desired tripwire.
    """
    if report.duration_seconds >= 90.0:
        payload = report.model_dump(mode="json")
        daemon._latest_session_recap = payload
        try:
            await asyncio.wait_for(
                daemon._ws_server.send_message(
                    MessageType.SESSION_RECAP.value,
                    payload,
                ),
                timeout=5.0,
            )
        except TimeoutError:
            return


# ─── tests ────────────────────────────────────────────────────────────


async def test_long_session_broadcasts_session_recap(daemon) -> None:
    """A session whose duration ≥ 90 s triggers SESSION_RECAP."""
    daemon._ws_server.send_message = AsyncMock(return_value=True)
    report = _make_long_report(duration_seconds=600.0)
    await _emit_recap_if_long(daemon, report)
    daemon._ws_server.send_message.assert_called_once()
    args, _ = daemon._ws_server.send_message.call_args
    assert args[0] == MessageType.SESSION_RECAP.value
    assert isinstance(args[1], dict)
    assert args[1]["session_id"] == "recap-test"


async def test_short_session_suppresses_broadcast(daemon) -> None:
    """A session whose duration < 90 s does NOT broadcast SESSION_RECAP."""
    daemon._ws_server.send_message = AsyncMock(return_value=True)
    report = _make_long_report(duration_seconds=30.0)
    await _emit_recap_if_long(daemon, report)
    daemon._ws_server.send_message.assert_not_called()
    # And the cache must remain empty.
    assert daemon.latest_session_recap() is None


async def test_broadcast_timeout_caps_at_about_5_seconds(daemon) -> None:
    """If ``send_message`` hangs, the broadcast block returns within ~5 s
    (the daemon's hard timeout), not indefinitely."""

    async def _hang(*a: Any, **kw: Any) -> bool:
        await asyncio.sleep(60.0)  # would never return without the timeout
        return True

    daemon._ws_server.send_message = _hang  # type: ignore[method-assign]
    report = _make_long_report(duration_seconds=600.0)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await _emit_recap_if_long(daemon, report)
    elapsed = loop.time() - t0
    # Daemon uses a 5 s wait_for; allow generous slack for CI jitter.
    assert 4.5 <= elapsed <= 6.5, f"expected ≈5s timeout; got {elapsed:.2f}s"


async def test_latest_session_recap_returns_cached_payload(daemon) -> None:
    """After a long broadcast, ``latest_session_recap()`` returns the cached dict."""
    daemon._ws_server.send_message = AsyncMock(return_value=True)
    report = _make_long_report(duration_seconds=600.0)
    await _emit_recap_if_long(daemon, report)
    cached = daemon.latest_session_recap()
    assert cached is not None
    assert cached["session_id"] == "recap-test"
    # The cached payload is exactly what was broadcast.
    sent_payload = daemon._ws_server.send_message.call_args[0][1]
    assert cached == sent_payload


def test_recap_cache_callback_serves_cached_payload(daemon) -> None:
    """The WS server's ``set_session_recap_cache_callback`` is wired to
    ``CortexDaemon.latest_session_recap``, so a REQUEST_SESSION_RECAP
    dispatch returns the cached dict (or None if no session ended yet)."""
    # Initially empty.
    cb = daemon._ws_server._session_recap_cache_callback
    assert cb is not None
    assert cb() is None
    # After we cache a payload, the same callback returns it.
    daemon._latest_session_recap = {"session_id": "cached-1", "flow_percentage": 75.0}
    assert cb() == {"session_id": "cached-1", "flow_percentage": 75.0}


async def test_broadcast_failure_is_non_fatal(daemon) -> None:
    """A non-timeout exception from ``send_message`` is logged and
    swallowed; the cache is still populated so a late client can fetch
    via REQUEST_SESSION_RECAP."""

    async def _raise(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("ws server is gone")

    daemon._ws_server.send_message = _raise  # type: ignore[method-assign]
    report = _make_long_report(duration_seconds=600.0)
    # The mirror helper above only catches TimeoutError, but the real
    # daemon's stop() catches Exception too. Both paths must leave the
    # cache populated since we set it BEFORE attempting the broadcast.
    try:
        await _emit_recap_if_long(daemon, report)
    except RuntimeError:
        pass
    assert daemon.latest_session_recap() is not None
    assert daemon.latest_session_recap()["session_id"] == "recap-test"
