"""Pydantic schemas for Cortex.

WIRE_CONTRACTS
==============

Timestamp unit
--------------
Version-2 contracts use explicit ``*_unix_ms`` / ``*_mono_ns`` names plus a
``boot_id``. Legacy ``float timestamp`` fields remain epoch seconds for one
compatibility interval and must not be introduced into new schemas.

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
from cortex.libs.schemas.native_messaging import (
    DaemonStatusResponse,
    GetAuthTokenMessage,
    GetAuthTokenResponse,
    LaunchMessage,
    LaunchResponse,
    NativeErrorResponse,
    NativeHostResponse,
    NativeMessage,
    RaiseDashboardMessage,
    RaiseDashboardResponse,
    StatusMessage,
    StopMessage,
    StopResponse,
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
from cortex.libs.schemas.temporal import EventTime
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
    "EventTime",
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
    # Native messaging (generated request/response contracts)
    "NativeMessage",
    "LaunchMessage",
    "StopMessage",
    "StatusMessage",
    "GetAuthTokenMessage",
    "RaiseDashboardMessage",
    "NativeHostResponse",
    "LaunchResponse",
    "StopResponse",
    "DaemonStatusResponse",
    "GetAuthTokenResponse",
    "RaiseDashboardResponse",
    "NativeErrorResponse",
    # WS Envelope (Debt-1 codegen source of truth)
    "WSMessage",
    "MessageType",
]
