"""
Cortex Prometheus metrics (P1-19).

All counters / gauges are module-level singletons.  Import this module
anywhere you need to increment a metric; the prometheus_client registry
accumulates the values and ``/metrics`` serves them via
``prometheus_client.make_asgi_app()``.

Available metrics
-----------------
WS_COALESCE_DROPS_TOTAL
    Incremented each time a WebSocket frame is silently dropped because
    the per-client coalesce queue raced the consumer (P1-8).

KEYRING_TIMEOUTS_TOTAL
    Incremented each time a macOS Keychain call exceeds the configured
    timeout (pre-existing metric, exposed here so it appears in /metrics).

STATE_TRANSITIONS_TOTAL
    Labelled ``{from_state, to_state}``.  Incremented by the state engine
    on every recognised transition.

INTERVENTIONS_APPLIED_TOTAL
    Labelled ``{action_type, consent_level}``.  Incremented by the
    intervention executor each time it successfully applies an action.

DAEMON_UPTIME_SECONDS
    Gauge set to ``time.monotonic() - _start_time`` on each scrape.
    Updated lazily via a ``before_collect`` callback so scrapes are cheap.
"""

from __future__ import annotations

import time

import prometheus_client as _prom
from prometheus_client import CollectorRegistry, Counter, Gauge

# ---------------------------------------------------------------------------
# Module-level registry (shared with the default registry so both the ASGI
# app and direct increments see the same data).
# ---------------------------------------------------------------------------

REGISTRY: CollectorRegistry = _prom.REGISTRY

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

WS_COALESCE_DROPS_TOTAL: Counter = Counter(
    "cortex_ws_coalesce_drops_total",
    "Number of WebSocket frames dropped by the newest-wins coalesce producer",
    registry=REGISTRY,
)

KEYRING_TIMEOUTS_TOTAL: Counter = Counter(
    "cortex_keyring_timeouts_total",
    "Number of macOS Keychain calls that exceeded the configured timeout",
    registry=REGISTRY,
)

STATE_TRANSITIONS_TOTAL: Counter = Counter(
    "cortex_state_transitions_total",
    "Number of recognised state-engine transitions",
    ["from_state", "to_state"],
    registry=REGISTRY,
)

INTERVENTIONS_APPLIED_TOTAL: Counter = Counter(
    "cortex_interventions_applied_total",
    "Number of intervention actions successfully applied",
    ["action_type", "consent_level"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

DAEMON_UPTIME_SECONDS: Gauge = Gauge(
    "cortex_daemon_uptime_seconds",
    "Wall-clock seconds since the metrics module was first imported",
    registry=REGISTRY,
)

_MODULE_IMPORT_TIME: float = time.monotonic()


class _UptimeCallback:
    """Updates DAEMON_UPTIME_SECONDS before each Prometheus scrape."""

    def describe(self):  # type: ignore[override]
        return []

    def collect(self):  # type: ignore[override]
        DAEMON_UPTIME_SECONDS.set(time.monotonic() - _MODULE_IMPORT_TIME)
        return []


REGISTRY.register(_UptimeCallback())
