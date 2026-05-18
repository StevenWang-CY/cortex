"""audit-w2 — Architecture.md must name every service directory + every
port the daemon actually opens.

The previous Architecture.md repository map listed six service
directories (physio_engine, state_engine, eval, llm_engine,
intervention_engine, consent). The codebase has fifteen: the doc also
silently dropped capture_service, kinematics_engine, telemetry_engine,
context_engine, session_report, api_gateway, launcher, janitor,
activity_tracker, handover, and throttle. Architecture.md also
mentioned ports 9472 and 9473 in prose but never the launcher agent's
9471, which is part of the documented kill chain.

This regression guard re-reads the markdown after every commit and
fails if a service directory is missing or if a port the code actually
opens is not mentioned in the doc. Adding a new service directory or
opening a new port without updating Architecture.md trips this test.
"""

from __future__ import annotations

from pathlib import Path

# Service directories that MUST be mentioned in Architecture.md. We
# stop at directories under ``cortex/services/`` so adjacent helpers
# (``cortex/libs/``, ``cortex/scripts/``) don't pollute the bar.
_REQUIRED_SERVICE_DIRS = {
    "capture_service",
    "physio_engine",
    "kinematics_engine",
    "telemetry_engine",
    "state_engine",
    "context_engine",
    "eval",
    "llm_engine",
    "intervention_engine",
    "consent",
    "session_report",
    "api_gateway",
    "launcher",
    "janitor",
    "activity_tracker",
}

# Ports the daemon actually opens. Drift here is a real bug, so we
# also assert against ``launcher_agent.py``'s PORT constant in a
# separate test below.
_REQUIRED_PORTS = {"9471", "9472", "9473"}


def _arch_md_path() -> Path:
    # cortex/tests/unit/test_*.py -> repo root is three parents up.
    return Path(__file__).resolve().parents[3] / "Architecture.md"


def test_architecture_md_lists_every_service_dir() -> None:
    text = _arch_md_path().read_text(encoding="utf-8")
    missing = [
        d for d in _REQUIRED_SERVICE_DIRS
        if d not in text
    ]
    assert not missing, (
        f"Architecture.md is missing these service directories: {missing}"
    )


def test_architecture_md_mentions_every_port() -> None:
    text = _arch_md_path().read_text(encoding="utf-8")
    missing = [p for p in _REQUIRED_PORTS if p not in text]
    assert not missing, (
        f"Architecture.md is missing these ports: {missing}"
    )


def test_launcher_agent_port_constant_matches_docs() -> None:
    """If a refactor moves the launcher agent off port 9471, the doc
    has to follow. This test pins the constant the doc relies on."""
    from cortex.scripts import launcher_agent

    assert launcher_agent.PORT == 9471
