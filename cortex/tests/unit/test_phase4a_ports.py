"""Phase-4a Debt-1: ports registry + InterventionPort protocol smoke tests.

These are introduced as foundations a later agent will build on, so the
tests here only assert the *contract* (constants are integers, the
Protocol type-checks against the concrete engine) rather than wiring.
"""

from __future__ import annotations

from cortex.libs.config import ports
from cortex.libs.ports import InterventionPort


def test_ports_module_exposes_three_constants() -> None:
    assert isinstance(ports.HTTP_API_PORT, int)
    assert isinstance(ports.WEBSOCKET_PORT, int)
    assert isinstance(ports.LAUNCHER_AGENT_PORT, int)


def test_ports_match_legacy_daemon_defaults() -> None:
    """Hard-coded numbers — bump in sync with CLAUDE.md `Ports` table."""
    assert ports.HTTP_API_PORT == 9472
    assert ports.WEBSOCKET_PORT == 9473
    assert ports.LAUNCHER_AGENT_PORT == 9471


def test_api_config_defaults_read_from_ports_module() -> None:
    """If a future port migration only edits ``ports.py`` it must
    propagate to ``APIConfig`` automatically."""
    from cortex.libs.config.settings import APIConfig

    cfg = APIConfig()
    assert cfg.port == ports.HTTP_API_PORT
    assert cfg.ws_port == ports.WEBSOCKET_PORT


def test_intervention_port_is_runtime_checkable() -> None:
    """The Protocol must be ``@runtime_checkable`` so dependency-
    injected stubs can be ``isinstance``-checked at app construction
    time without each implementation explicitly subclassing the Protocol.
    """

    class _Stub:
        def capture_snapshot(self, context=None, intervention_id=None, *, timestamp=None):
            raise NotImplementedError

        def prepare_plan(self, plan, *, tab_count=None):
            raise NotImplementedError

    assert isinstance(_Stub(), InterventionPort)


def test_intervention_port_rejects_partial_implementation() -> None:
    """A class missing one method must NOT pass the isinstance check."""

    class _Partial:
        def capture_snapshot(self, context=None, intervention_id=None, *, timestamp=None):
            raise NotImplementedError

    assert not isinstance(_Partial(), InterventionPort)


# P2-14: Port uniqueness + valid range
def test_ports_unique() -> None:
    """All three ports must be distinct integers in the unprivileged range."""
    assert len({ports.HTTP_API_PORT, ports.WEBSOCKET_PORT, ports.LAUNCHER_AGENT_PORT}) == 3, (
        "HTTP_API_PORT, WEBSOCKET_PORT, and LAUNCHER_AGENT_PORT must all be distinct"
    )
    for p in (ports.HTTP_API_PORT, ports.WEBSOCKET_PORT, ports.LAUNCHER_AGENT_PORT):
        assert 1024 < p < 65535, (
            f"Port {p} is outside the valid unprivileged range (1024, 65535)"
        )
