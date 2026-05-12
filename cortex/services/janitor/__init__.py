"""Janitor — periodic housekeeping tasks (G.2 retention sweep)."""

from cortex.services.janitor.retention import sweep_once

__all__ = ["sweep_once"]
