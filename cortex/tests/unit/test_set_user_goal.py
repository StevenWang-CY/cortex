"""Audit-2 — daemon.set_user_goal end-to-end.

Verifies the goal-override path the desktop dashboard relies on:

1. ``set_user_goal("text")`` stores the override on the daemon.
2. The next ``_context_loop`` build (or any cached context) carries
   ``context.current_goal_hint == "text"``.
3. ``set_user_goal("")`` clears the override.
4. ``_handle_user_action({"action": "set_goal:foo"})`` routes correctly
   when intervention_id is empty (the WS-mode dashboard path).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cortex.services.runtime_daemon import CortexDaemon


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> CortexDaemon:
    # Skip the heavy __init__ (camera, pipelines) by creating a bare
    # object and setting just the fields ``set_user_goal`` needs.
    d = CortexDaemon.__new__(CortexDaemon)
    d._user_goal_override = None
    d._latest_context = None
    return d


def test_set_user_goal_stores_override(daemon: CortexDaemon) -> None:
    asyncio.run(daemon.set_user_goal("ship the milestone"))
    assert daemon._user_goal_override == "ship the milestone"


def test_set_user_goal_strips_whitespace(daemon: CortexDaemon) -> None:
    asyncio.run(daemon.set_user_goal("   debug the build   "))
    assert daemon._user_goal_override == "debug the build"


def test_set_user_goal_empty_clears(daemon: CortexDaemon) -> None:
    asyncio.run(daemon.set_user_goal("first"))
    asyncio.run(daemon.set_user_goal(""))
    assert daemon._user_goal_override is None


def test_set_user_goal_mutates_cached_context(daemon: CortexDaemon) -> None:
    """If a context is already cached, the override is applied
    immediately so the very next intervention picks it up without
    waiting for the 5 s _context_loop tick."""
    cached = SimpleNamespace(current_goal_hint=None)
    daemon._latest_context = cached
    asyncio.run(daemon.set_user_goal("explore tests"))
    assert cached.current_goal_hint == "explore tests"


def test_set_goal_action_routes_through_handle_user_action(
    daemon: CortexDaemon,
) -> None:
    """The WS-mode dashboard sends ``USER_ACTION{"action":"set_goal:foo"}``
    with an empty intervention_id; the daemon must route to set_user_goal
    before the intervention_id guard rejects the message."""
    asyncio.run(daemon._handle_user_action({
        "action": "set_goal:write docs",
        "intervention_id": "",
    }))
    assert daemon._user_goal_override == "write docs"
