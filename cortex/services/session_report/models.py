"""Session Report — data models (legacy import shim).

The canonical schema definitions live in :mod:`cortex.libs.schemas.session_report`
so they participate in the Debt-1 TypeScript codegen pipeline. This
module re-exports them under the legacy path so existing imports
(``from cortex.services.session_report.models import SessionReport``)
keep working.

Do NOT add new schemas here — define them in
:mod:`cortex.libs.schemas.session_report` and re-export below.
"""

from __future__ import annotations

from cortex.libs.schemas.session_report import (
    ActivitySummary,
    BreakRecord,
    ComparisonStats,
    SessionReport,
    StateTransition,
)

__all__ = [
    "ActivitySummary",
    "BreakRecord",
    "ComparisonStats",
    "SessionReport",
    "StateTransition",
]
