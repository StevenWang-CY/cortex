"""Correlation IDs (audit F19).

A single request can fan out across the HTTP gateway, the WebSocket server,
the LLM planner, the state engine, and back to the UI. Without a shared
identifier on every log line, support tickets become "grep four log streams
and time-align by eyeball." This module gives every entry point a way to
mint or accept a correlation id and bind it to:

1. A ``ContextVar`` so any sync/async code path can read it.
2. ``structlog``'s contextvars so every structlog-emitted record carries it
   without explicit threading.
3. The stdlib logging ``LogRecord`` via a ``Filter`` so legacy
   ``logging.getLogger(__name__).info(...)`` callers also acquire it.

The id is short (12 hex chars prefixed with ``cid_``) to keep log lines
readable and ``X-Cortex-Request-ID`` headers compact.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from contextvars import ContextVar
from typing import Iterator

import structlog

_CID_VAR: ContextVar[str | None] = ContextVar("cortex_correlation_id", default=None)


def new_correlation_id() -> str:
    """Mint a fresh correlation id (e.g. ``cid_3f9a1b2c8d0e``)."""
    return "cid_" + secrets.token_hex(6)


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current task/scope, if any."""
    return _CID_VAR.get()


def bind_correlation_id(value: str) -> None:
    """Bind ``value`` as the active correlation id for the current scope.

    Sets both the local ``ContextVar`` and ``structlog``'s contextvars so
    structured log records pick it up automatically.
    """
    _CID_VAR.set(value)
    structlog.contextvars.bind_contextvars(correlation_id=value)


def clear_correlation_id() -> None:
    """Clear the correlation id from the current scope."""
    _CID_VAR.set(None)
    structlog.contextvars.unbind_contextvars("correlation_id")


@contextlib.contextmanager
def correlation_scope(value: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of the ``with`` block.

    If ``value`` is falsy a fresh id is minted. The previous binding (if
    any) is restored on exit so nested scopes do not corrupt parent state.
    """
    previous = _CID_VAR.get()
    cid = value or new_correlation_id()
    bind_correlation_id(cid)
    try:
        yield cid
    finally:
        if previous is None:
            clear_correlation_id()
        else:
            bind_correlation_id(previous)


class CorrelationIdFilter(logging.Filter):
    """Stdlib logging filter that injects the active correlation id.

    Attaches the current id as ``record.correlation_id`` so format strings
    like ``"%(correlation_id)s %(message)s"`` (or JSON formatters that
    serialise extra fields) can surface it. Records emitted outside any
    correlation scope get an empty string so the formatter never raises a
    ``KeyError``.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        record.correlation_id = _CID_VAR.get() or ""
        return True


def install_stdlib_filter(root: logging.Logger | None = None) -> CorrelationIdFilter:
    """Attach :class:`CorrelationIdFilter` to the root logger.

    Idempotent: re-installation does not duplicate filters.
    """
    target = root or logging.getLogger()
    for existing in target.filters:
        if isinstance(existing, CorrelationIdFilter):
            return existing
    f = CorrelationIdFilter()
    target.addFilter(f)
    return f
