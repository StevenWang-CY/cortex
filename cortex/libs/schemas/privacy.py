"""Wire contracts for explicit workspace-context disclosure.

These models are intentionally transport-neutral.  The context broker is the
only component that may turn a :class:`TaskContext` into an external-model
request; clients first receive an exact, short-lived preview and then confirm
that preview once.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

from cortex.libs.schemas.context import TaskContext
from cortex.libs.schemas.intervention import InterventionPlan, SimplificationConstraints
from cortex.libs.schemas.state import StateEstimate
from cortex.libs.schemas.temporal import DualClockModel

CONTEXT_DISCLOSURE_REVISION = "context-disclosure-v1"
CONTEXT_SEND_CONFIRMATION = "SEND PREVIEWED CONTEXT ONCE"

ContextClassification = Literal[
    "operational_aggregate",
    "support_estimate",
    "user_goal",
    "workspace_metadata",
    "workspace_content",
    "behavioral_preference",
]
ContextOrigin = Literal[
    "daemon",
    "editor",
    "terminal",
    "browser",
    "user",
]
DisclosureDisposition = Literal["included", "omitted", "redacted"]


class ContextSourceSelection(BaseModel):
    """Per-request opt-ins; every potentially identifying source is off."""

    workspace_aggregates: bool = False
    support_estimate: bool = False
    user_goal: bool = False
    editor_metadata: bool = False
    editor_content: bool = False
    terminal_content: bool = False
    browser_metadata: bool = False
    browser_content: bool = False
    learned_preferences: bool = False
    extra_context: bool = False


class ContextFieldDisclosure(BaseModel):
    """How one catalogued field is handled in the outbound request."""

    field_path: str = Field(..., min_length=1, max_length=160)
    classification: ContextClassification
    origin: ContextOrigin
    disposition: DisclosureDisposition
    redaction_count: int = Field(default=0, ge=0)
    value_preview: str = Field(default="", max_length=512)


class ProviderRetentionDisclosure(BaseModel):
    """Provider-specific retention caveat shown before every external send."""

    provider: Literal["bedrock", "vertex", "direct"]
    destination: str = Field(..., min_length=1, max_length=120)
    configured_mode: Literal[
        "unverified",
        "provider_default",
        "zero_data_retention_contract",
    ]
    summary: str = Field(..., min_length=1, max_length=1200)
    documentation_url: HttpUrl
    verified_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    account_contract_must_be_verified: bool = True
    zero_retention_asserted_by_cortex: bool = False


class ContextPreviewRequest(BaseModel):
    """Local request to prepare, but not send, an external-model payload."""

    state_estimate: StateEstimate
    task_context: TaskContext
    selection: ContextSourceSelection = Field(default_factory=ContextSourceSelection)
    constraints: SimplificationConstraints | None = None
    template_name: str | None = Field(default=None, max_length=80)
    extra_context: str = Field(default="", max_length=20_000)


class CurrentContextPreviewRequest(BaseModel):
    """Prepare a preview from the daemon's current in-memory snapshot.

    Unlike :class:`ContextPreviewRequest`, this UI-oriented contract never
    asks a desktop client to copy raw workspace content across a second local
    boundary.  Only source choices and an optional user-authored note cross
    the authenticated loopback API.
    """

    selection: ContextSourceSelection = Field(default_factory=ContextSourceSelection)
    constraints: SimplificationConstraints | None = None
    template_name: str | None = Field(default=None, max_length=80)
    extra_context: str = Field(default="", max_length=2_000)


class ContextPreviewResponse(DualClockModel):
    """Exact redacted context plus a one-time confirmation handle."""

    disclosure_revision: Literal["context-disclosure-v1"] = "context-disclosure-v1"
    preview_id: str = Field(..., min_length=20, max_length=160)
    request_digest: str = Field(..., min_length=64, max_length=64)
    expires_at_unix_ms: int = Field(..., ge=0)
    confirmation_phrase: Literal["SEND PREVIEWED CONTEXT ONCE"] = "SEND PREVIEWED CONTEXT ONCE"
    provider: Literal["bedrock", "vertex", "direct"]
    model: str = Field(..., min_length=1, max_length=200)
    template_name: str = Field(..., min_length=1, max_length=80)
    retention: ProviderRetentionDisclosure
    selection: ContextSourceSelection
    outbound_context: TaskContext
    outbound_user_prompt: str = Field(..., max_length=24_000)
    field_disclosures: list[ContextFieldDisclosure] = Field(max_length=128)
    redaction_count: int = Field(default=0, ge=0)
    omitted_field_count: int = Field(default=0, ge=0)
    outbound_utf8_bytes: int = Field(..., ge=0, le=96_000)
    raw_context_retained: bool = False
    prepared_payload_retained_in_memory: bool = True
    prepared_payload_cancellable: bool = True
    authority_granted: bool = False
    note: str = Field(
        default=(
            "Previewing does not send data or authorize workspace actions. "
            "Confirmation authorizes this exact redacted payload once."
        ),
        max_length=400,
    )


class ContextPrivacyStatusResponse(DualClockModel):
    """Current planner privacy posture for local settings surfaces."""

    planner_mode: Literal["no_llm", "no_content", "external_redacted"]
    network_allowed_by_configuration: bool = False
    disclosure_revision: Literal["context-disclosure-v1"] = "context-disclosure-v1"
    pending_previews: int = Field(default=0, ge=0, le=64)
    provider: Literal["bedrock", "vertex", "direct"]
    retention: ProviderRetentionDisclosure


class ContextPreviewConfirmationRequest(BaseModel):
    """Consume one prepared preview; the handle is burned on every attempt."""

    preview_id: str = Field(..., min_length=20, max_length=160)
    confirmation_phrase: Literal["SEND PREVIEWED CONTEXT ONCE"]


class ContextPreviewCancellationResponse(DualClockModel):
    """Result of explicitly burning a prepared preview without sending it."""

    preview_id: str = Field(..., min_length=20, max_length=160)
    cancelled: bool
    sent: bool = False
    authority_granted: bool = False
    note: str = Field(
        default=(
            "The handle can no longer send a provider request. Nothing was sent "
            "and no workspace action was authorized."
        ),
        max_length=300,
    )


class ContextPreviewConfirmationResponse(DualClockModel):
    """Validated planner response from one explicitly confirmed payload."""

    preview_id: str = Field(..., min_length=20, max_length=160)
    sent: bool = True
    plan: InterventionPlan
    fallback_used: bool = False
    authority_granted: bool = False
    note: str = Field(
        default=(
            "The response was schema-validated locally. It is a proposal only; "
            "workspace actions still require a separate exact authorization."
        ),
        max_length=400,
    )
