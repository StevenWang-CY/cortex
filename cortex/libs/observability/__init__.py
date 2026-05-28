"""Cortex observability helpers.

This package centralises the Prometheus metric singletons used by the
daemon. Importing the package is sufficient to register the metrics on
the default ``prometheus_client`` registry — the ``metrics`` submodule
is eagerly re-exported so callers can write::

    from cortex.libs.observability import (
        WS_COALESCE_DROPS_TOTAL,
        STATE_TRANSITIONS_TOTAL,
    )

rather than::

    from cortex.libs.observability.metrics import (
        WS_COALESCE_DROPS_TOTAL,
        STATE_TRANSITIONS_TOTAL,
    )

The two forms are equivalent; the shorter form is preferred for new
callers because it reads as "ask the observability package for this
counter" rather than coupling consumers to the internal module
layout. The submodule re-export also guarantees the registry-side-effect
import has executed by the time the counter handle is in scope.
"""

from cortex.libs.observability.metrics import (
    DAEMON_UPTIME_SECONDS,
    INTERVENTIONS_APPLIED_TOTAL,
    KEYRING_TIMEOUTS_TOTAL,
    REGISTRY,
    STATE_TRANSITIONS_TOTAL,
    WS_COALESCE_DROPS_TOTAL,
)

__all__ = [
    "DAEMON_UPTIME_SECONDS",
    "INTERVENTIONS_APPLIED_TOTAL",
    "KEYRING_TIMEOUTS_TOTAL",
    "REGISTRY",
    "STATE_TRANSITIONS_TOTAL",
    "WS_COALESCE_DROPS_TOTAL",
]
