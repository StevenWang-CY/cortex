"""Pydantic schemas for Cortex.

WIRE_CONTRACTS
==============

Timestamp unit
--------------
All ``float`` timestamps in this package are **UNIX epoch seconds
(wall-clock UTC)**.  Use ``time.time()`` on the producer side; compare to
``Date.now() / 1000`` on the TypeScript consumer side.  ``datetime``
fields serialize to ISO-8601 strings via Pydantic's JSON encoder.

Enum policy
-----------
Pydantic **enums** are configured with ``use_enum_values=True`` on every
model that carries an enum field, so the **wire JSON contains the plain
string value** (e.g. ``"FLOW"``, not ``"UserState.FLOW"``).  The
TypeScript codegen pipeline emits matching **literal string unions**
(e.g. ``"FLOW" | "HYPO" | "HYPER" | "RECOVERY"``) rather than TS enums,
keeping the contract simple for JavaScript consumers.

Null policy
-----------
**Optional fields** (``field: T | None``) may be serialized as JSON
``null`` *or* omitted entirely (when ``exclude_none=True`` is passed to
``model_dump``).  Consumers **must treat absent keys and ``null`` values
as equivalent** — never assume an absent key means ``False`` or ``0``.
"""

# Pydantic schemas for Cortex

from cortex.libs.schemas.consent import (
    ActionConsentState,
    ConsentDecision,
    ConsentLadderState,
    ConsentLevel,
    ConsentRecord,
)
from cortex.libs.schemas.context import (
    BrowserContext,
    Diagnostic,
    EditorContext,
    TabInfo,
    TaskContext,
    TerminalContext,
)
from cortex.libs.schemas.eval import (
    BanditWeights,
    HelpfulnessRecord,
    InterventionSnapshot,
)
from cortex.libs.schemas.features import (
    FeatureVector,
    FrameMeta,
    KinematicFeatures,
    PhysioFeatures,
    TelemetryFeatures,
)
from cortex.libs.schemas.intervention import (
    DismissalRecord,
    FoldState,
    InterventionOutcome,
    InterventionPlan,
    SimplificationConstraints,
    TabVisibility,
    UIPlan,
    WorkspaceSnapshot,
)
from cortex.libs.schemas.longitudinal import (
    ChronotypeModel,
    DailyBaseline,
    HourlyOverloadRate,
    TaskOverloadPattern,
)
from cortex.libs.schemas.session_history import (
    SESSION_ID_PATTERN,
    SessionDetailResponse,
    SessionIdStr,
    SessionListResponse,
    SessionSummary,
    TrendsRequest,
    TrendsResponse,
)
from cortex.libs.schemas.session_report import (
    ActivitySummary,
    ComparisonStats,
    SessionReport,
)
from cortex.libs.schemas.state import (
    SignalQuality,
    StateEstimate,
    StateScores,
    StateTransition,
    UserBaselines,
    UserState,
)
from cortex.libs.schemas.transition_graph import (
    FocusEdge,
    FocusNode,
    FocusTransitionGraph,
)
from cortex.libs.schemas.ws_message import WSMessage
from cortex.libs.schemas.ws_message_types import MessageType

__all__ = [
    # Features
    "FrameMeta",
    "PhysioFeatures",
    "KinematicFeatures",
    "TelemetryFeatures",
    "FeatureVector",
    # State
    "UserState",
    "SignalQuality",
    "StateScores",
    "StateEstimate",
    "UserBaselines",
    "StateTransition",
    # Context
    "Diagnostic",
    "EditorContext",
    "TerminalContext",
    "TabInfo",
    "BrowserContext",
    "TaskContext",
    # Intervention
    "UIPlan",
    "SimplificationConstraints",
    "InterventionPlan",
    "FoldState",
    "TabVisibility",
    "WorkspaceSnapshot",
    "InterventionOutcome",
    "DismissalRecord",
    # Transition Graph
    "FocusNode",
    "FocusEdge",
    "FocusTransitionGraph",
    # Longitudinal
    "DailyBaseline",
    "HourlyOverloadRate",
    "TaskOverloadPattern",
    "ChronotypeModel",
    # Session History (P0 §3.1 / §3.2 / §3.3 wire envelopes)
    "SessionSummary",
    "SessionListResponse",
    "SessionDetailResponse",
    "TrendsResponse",
    "TrendsRequest",
    "SessionIdStr",
    "SESSION_ID_PATTERN",
    # Session Report (canonical schema; legacy import path lives at
    # cortex.services.session_report.models)
    "SessionReport",
    "ActivitySummary",
    "ComparisonStats",
    # Consent
    "ConsentLevel",
    "ConsentRecord",
    "ActionConsentState",
    "ConsentLadderState",
    "ConsentDecision",
    # Eval
    "InterventionSnapshot",
    "HelpfulnessRecord",
    "BanditWeights",
    # WS Envelope (Debt-1 codegen source of truth)
    "WSMessage",
    "MessageType",
]
