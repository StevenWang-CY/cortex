"""
Cortex Adapter Protocol

Defines the generic interface for workspace adapters (browser, editor,
terminal, Slack, Notion, etc.) so new adapters can be dropped in.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class AdapterResult(BaseModel):
    """Result of an adapter action execution."""
    success: bool = Field(..., description="Whether the action succeeded")
    data: dict[str, Any] = Field(default_factory=dict, description="Result data")
    reversible: bool = Field(True, description="Whether this action can be undone")
    reverse_action: str | None = Field(None, description="Action name to reverse this")
    error: str | None = Field(None, description="Error message if failed")


@runtime_checkable
class CortexAdapter(Protocol):
    """Protocol for pluggable workspace adapters."""

    @property
    def name(self) -> str:
        """Unique adapter name (e.g., 'chrome', 'vscode', 'slack')."""
        ...

    @property
    def capabilities(self) -> list[str]:
        """List of action types this adapter supports."""
        ...

    async def execute(self, action: str, params: dict[str, Any]) -> AdapterResult:
        """Execute an adapter action."""
        ...

    async def get_context(self) -> dict[str, Any]:
        """Get current context from this adapter."""
        ...

    async def health_check(self) -> bool:
        """Check if this adapter is healthy and connected."""
        ...
