"""Single source of truth for Cortex daemon ports.

The Cortex daemon binds three TCP ports: HTTP (FastAPI), WebSocket, and
an optional launcher agent. They are referenced from many places —
``settings.py``, ``run_dev.py``, ``native_host.py``,
``install_native_host.py``, the browser extension manifest patcher,
the desktop shell controller, even the C wrapper in ``.cortex_launcher.c``.
Pre-Phase-4a these were duplicated across modules and a port bump in
one place could silently break the others. Phase-4a Debt-1 centralises
the constants here so a future port migration only touches one file.

Importing this module never has side effects. Callers wishing to read
ports from their environment / settings should still do so through
``cortex.libs.config.settings.APIConfig`` — that class reads the
defaults below for free (its field defaults are bound to these
constants).

These integers are unconditional defaults. They are NOT runtime values:
if the user overrides ``CORTEX_API__PORT`` in their ``.env`` the
running daemon uses the env value, but the constants here keep the
build-time defaults aligned across modules that don't have access to a
loaded ``CortexConfig``.
"""

from __future__ import annotations

#: FastAPI HTTP API port (cortex/services/api_gateway/routes.py).
HTTP_API_PORT: int = 9472

#: WebSocket server port (cortex/services/api_gateway/websocket_server.py).
WEBSOCKET_PORT: int = 9473

#: Optional launcher-agent HTTP port (cortex/scripts/launcher_agent.py).
LAUNCHER_AGENT_PORT: int = 9471


__all__ = [
    "HTTP_API_PORT",
    "LAUNCHER_AGENT_PORT",
    "WEBSOCKET_PORT",
]
