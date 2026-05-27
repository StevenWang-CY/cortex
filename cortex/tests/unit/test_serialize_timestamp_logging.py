"""P2-2: _serialize_timestamp logs a DEBUG message when isoformat() raises.

Before the fix the ``except Exception: pass`` silently swallowed
isoformat errors. After the fix a ``logger.debug`` with ``exc_info=True``
is emitted so the root cause is visible in debug logging, while the
fall-through behaviour (return the raw value) is preserved.

Asserts:
* An object whose ``isoformat()`` raises still returns the raw object.
* A DEBUG log record containing "timestamp ISO serialize failed" is emitted.
* A datetime-like object that returns normally is not logged.
* None input returns None.
* A plain float passes through unchanged.
"""

from __future__ import annotations

import logging

import pytest

from cortex.services.api_gateway.websocket_server import _serialize_timestamp


class _BrokenIso:
    """Pretends to have an isoformat() but always raises."""

    def isoformat(self):
        raise RuntimeError("isoformat intentionally broken")


class _GoodIso:
    """Returns a valid ISO string."""

    def isoformat(self) -> str:
        return "2026-05-27T00:00:00+00:00"


def test_broken_isoformat_returns_raw_object(caplog) -> None:
    """Fall-through: the raw object is returned even when isoformat() raises."""
    obj = _BrokenIso()
    with caplog.at_level(logging.DEBUG, logger="cortex.services.api_gateway.websocket_server"):
        result = _serialize_timestamp(obj)
    assert result is obj, "Must return the raw object on isoformat failure"


def test_broken_isoformat_emits_debug_log(caplog) -> None:
    """A DEBUG record mentioning 'timestamp ISO serialize failed' is emitted."""
    obj = _BrokenIso()
    with caplog.at_level(logging.DEBUG, logger="cortex.services.api_gateway.websocket_server"):
        _serialize_timestamp(obj)

    matching = [
        r for r in caplog.records
        if "timestamp ISO serialize failed" in r.message
    ]
    assert matching, (
        f"Expected 'timestamp ISO serialize failed' in DEBUG log; "
        f"got records: {[r.message for r in caplog.records]}"
    )
    assert matching[0].levelno == logging.DEBUG


def test_broken_isoformat_log_has_exc_info(caplog) -> None:
    """The debug log must capture exc_info so the stack trace is visible."""
    obj = _BrokenIso()
    with caplog.at_level(logging.DEBUG, logger="cortex.services.api_gateway.websocket_server"):
        _serialize_timestamp(obj)

    matching = [r for r in caplog.records if "timestamp ISO serialize failed" in r.message]
    assert matching
    # When exc_info=True is passed, the LogRecord has a non-None exc_info tuple.
    assert matching[0].exc_info is not None, (
        "exc_info=True must be set on the debug log record"
    )


def test_good_isoformat_no_log(caplog) -> None:
    """A working isoformat() call is not logged."""
    obj = _GoodIso()
    with caplog.at_level(logging.DEBUG, logger="cortex.services.api_gateway.websocket_server"):
        result = _serialize_timestamp(obj)
    assert result == "2026-05-27T00:00:00+00:00"
    debug_records = [r for r in caplog.records if "timestamp ISO" in r.message]
    assert debug_records == []


def test_none_returns_none() -> None:
    assert _serialize_timestamp(None) is None


def test_float_passes_through() -> None:
    val = 1_716_000_000.5
    assert _serialize_timestamp(val) == val


def test_plain_string_passes_through() -> None:
    val = "not-a-datetime"
    assert _serialize_timestamp(val) == val
