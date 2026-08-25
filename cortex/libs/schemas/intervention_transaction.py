"""Transactional intervention wire and persistence contracts.

An :class:`InterventionPlan` is presentation content.  It is never authority
to mutate a workspace.  Workspace authority is represented by the separate,
immutable chain defined here::

    ActionManifest -> ActionAuthorization -> ActionReceipt

The manifest is encoded once as canonical JSON and addressed by SHA-256.  An
authorization names that exact digest and an exact, sorted subset of action
IDs.  Receipts then prove what an adapter attempted, how long it took, and
whether the post-condition was verified.  Keeping these as strict Pydantic
models makes Python the single source of truth for browser and VS Code schema
code generation.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _canonical_json(value: object) -> str:
    """Return the one byte-stable JSON representation used for hashing."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class InterventionLifecycleState(str, Enum):  # noqa: UP042 - TS codegen
    """Persisted state of one intervention transaction."""

    PROPOSED = "proposed"
    DELIVERED = "delivered"
    AUTHORIZED = "authorized"
    APPLYING = "applying"
    APPLIED = "applied"
    PARTIAL = "partial"
    FAILED = "failed"
    RESTORING = "restoring"
    RESTORED = "restored"
    RESTORE_FAILED = "restore_failed"
    ABANDONED = "abandoned"


INTERVENTION_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    InterventionLifecycleState.PROPOSED.value: frozenset(
        {
            InterventionLifecycleState.DELIVERED.value,
            InterventionLifecycleState.ABANDONED.value,
        }
    ),
    InterventionLifecycleState.DELIVERED.value: frozenset(
        {
            InterventionLifecycleState.AUTHORIZED.value,
            InterventionLifecycleState.ABANDONED.value,
        }
    ),
    InterventionLifecycleState.AUTHORIZED.value: frozenset(
        {
            InterventionLifecycleState.APPLYING.value,
            InterventionLifecycleState.ABANDONED.value,
        }
    ),
    InterventionLifecycleState.APPLYING.value: frozenset(
        {
            InterventionLifecycleState.APPLIED.value,
            InterventionLifecycleState.PARTIAL.value,
            InterventionLifecycleState.FAILED.value,
            InterventionLifecycleState.RESTORING.value,
        }
    ),
    InterventionLifecycleState.APPLIED.value: frozenset(
        {
            InterventionLifecycleState.AUTHORIZED.value,
            InterventionLifecycleState.RESTORING.value,
        }
    ),
    InterventionLifecycleState.PARTIAL.value: frozenset(
        {
            InterventionLifecycleState.AUTHORIZED.value,
            InterventionLifecycleState.RESTORING.value,
        }
    ),
    InterventionLifecycleState.FAILED.value: frozenset(
        {
            InterventionLifecycleState.AUTHORIZED.value,
            InterventionLifecycleState.RESTORING.value,
            InterventionLifecycleState.ABANDONED.value,
        }
    ),
    InterventionLifecycleState.RESTORING.value: frozenset(
        {
            InterventionLifecycleState.APPLIED.value,
            InterventionLifecycleState.RESTORED.value,
            InterventionLifecycleState.RESTORE_FAILED.value,
        }
    ),
    InterventionLifecycleState.RESTORE_FAILED.value: frozenset(
        {
            InterventionLifecycleState.RESTORING.value,
            InterventionLifecycleState.ABANDONED.value,
        }
    ),
    InterventionLifecycleState.RESTORED.value: frozenset(),
    InterventionLifecycleState.ABANDONED.value: frozenset(),
}


class AuthorizationState(str, Enum):  # noqa: UP042 - TS codegen
    """One-time authorization ledger state."""

    ISSUED = "issued"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ReceiptPhase(str, Enum):  # noqa: UP042 - TS codegen
    APPLY = "apply"
    COMPENSATE = "compensate"
    RESTORE = "restore"


class ReceiptStatus(str, Enum):  # noqa: UP042 - TS codegen
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ALREADY_COMPLETE = "already_complete"


class VerificationStatus(str, Enum):  # noqa: UP042 - TS codegen
    VERIFIED = "verified"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class ManifestAction(BaseModel):
    """One exact, adapter-routable effect in a canonical manifest.

    Parameters and inverse data are JSON *strings*, not mutable dictionaries.
    This prevents a caller from mutating a nested object after the manifest
    digest was checked.  Validators require the strings themselves to already
    be canonical.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    action_id: str = Field(..., min_length=1, max_length=128)
    ordinal: int = Field(..., ge=0, le=31)
    executor: Literal["browser", "editor", "terminal", "desktop", "daemon"]
    capability: str = Field(..., min_length=1, max_length=96)
    parameters_json: str = Field("{}", max_length=32_768)
    reverse_capability: str | None = Field(None, max_length=96)
    workspace_mutation: bool = True
    required_consent_level: int = Field(..., ge=0, le=4)
    source: Literal["planner_command", "suggested_action", "system"]

    @field_validator("parameters_json")
    @classmethod
    def _parameters_are_canonical_object(cls, value: str) -> str:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("parameters_json must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("parameters_json must encode an object")
        try:
            canonical = _canonical_json(decoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("parameters_json contains a non-JSON value") from exc
        if value != canonical:
            raise ValueError("parameters_json must use canonical JSON encoding")
        return value

    @property
    def parameters(self) -> dict[str, Any]:
        """Decode a fresh parameter object; callers cannot mutate the model."""

        decoded = json.loads(self.parameters_json)
        if not isinstance(decoded, dict):  # validator makes this unreachable
            raise ValueError("manifest parameters are not an object")
        return {str(key): value for key, value in decoded.items()}

    @classmethod
    def from_parameters(
        cls,
        *,
        action_id: str,
        ordinal: int,
        executor: Literal["browser", "editor", "terminal", "desktop", "daemon"],
        capability: str,
        parameters: dict[str, Any] | None,
        reverse_capability: str | None,
        workspace_mutation: bool,
        required_consent_level: int,
        source: Literal["planner_command", "suggested_action", "system"],
    ) -> ManifestAction:
        return cls(
            action_id=action_id,
            ordinal=ordinal,
            executor=executor,
            capability=capability,
            parameters_json=_canonical_json(parameters or {}),
            reverse_capability=reverse_capability,
            workspace_mutation=workspace_mutation,
            required_consent_level=required_consent_level,
            source=source,
        )


class ActionManifestBody(BaseModel):
    """The exact object serialized into :attr:`ActionManifest.canonical_json`."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    schema_version: Literal["1"] = "1"
    intervention_id: str = Field(..., min_length=1, max_length=128)
    actions: tuple[ManifestAction, ...] = Field(default_factory=tuple, max_length=32)

    @model_validator(mode="after")
    def _unique_action_ids(self) -> ActionManifestBody:
        ids = [action.action_id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest action_id values must be unique")
        ordinals = [action.ordinal for action in self.actions]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("manifest action ordinals must be unique")
        if ordinals != sorted(ordinals):
            raise ValueError("manifest actions must be ordered by ordinal")
        if ordinals != list(range(len(ordinals))):
            raise ValueError("manifest action ordinals must be contiguous from zero")
        return self

    def to_canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


class ActionManifest(BaseModel):
    """Immutable, content-addressed action manifest delivered with a proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    schema_version: Literal["1"] = "1"
    intervention_id: str = Field(..., min_length=1, max_length=128)
    canonical_json: str = Field(..., min_length=2, max_length=131_072)
    manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    action_count: int = Field(..., ge=0, le=32)
    created_at_unix_ms: int = Field(..., ge=0)
    created_at_mono_ns: int = Field(..., ge=0)
    expires_at_unix_ms: int = Field(..., ge=0)
    ttl_ms: int = Field(..., ge=1, le=3_600_000)
    boot_id: UUID

    @model_validator(mode="after")
    def _validate_digest_and_body(self) -> ActionManifest:
        if _sha256_text(self.canonical_json) != self.manifest_sha256:
            raise ValueError("manifest_sha256 does not match canonical_json")
        try:
            raw = json.loads(self.canonical_json)
            body = ActionManifestBody.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical_json is not a valid action manifest") from exc
        if body.to_canonical_json() != self.canonical_json:
            raise ValueError("canonical_json is not canonical")
        if body.intervention_id != self.intervention_id:
            raise ValueError("manifest intervention_id mismatch")
        if len(body.actions) != self.action_count:
            raise ValueError("manifest action_count mismatch")
        if self.expires_at_unix_ms != self.created_at_unix_ms + self.ttl_ms:
            raise ValueError("manifest expiry must equal creation time plus ttl")
        return self

    @property
    def body(self) -> ActionManifestBody:
        return ActionManifestBody.model_validate_json(self.canonical_json)

    @classmethod
    def create(
        cls,
        *,
        intervention_id: str,
        actions: tuple[ManifestAction, ...] | list[ManifestAction],
        created_at_unix_ms: int,
        created_at_mono_ns: int,
        boot_id: UUID,
        ttl_ms: int = 300_000,
    ) -> ActionManifest:
        body = ActionManifestBody(
            intervention_id=intervention_id,
            actions=tuple(actions),
        )
        canonical = body.to_canonical_json()
        return cls(
            intervention_id=intervention_id,
            canonical_json=canonical,
            manifest_sha256=_sha256_text(canonical),
            action_count=len(body.actions),
            created_at_unix_ms=created_at_unix_ms,
            created_at_mono_ns=created_at_mono_ns,
            expires_at_unix_ms=created_at_unix_ms + ttl_ms,
            ttl_ms=ttl_ms,
            boot_id=boot_id,
        )


class InterventionAuthorizationRequest(BaseModel):
    """Client request produced by one explicit, user-visible gesture."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    authorization_request_id: str = Field(..., min_length=1, max_length=128)
    intervention_id: str = Field(..., min_length=1, max_length=128)
    manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    approved_action_ids: tuple[str, ...] = Field(..., min_length=1, max_length=32)
    source_surface: Literal["browser", "vscode", "desktop", "http"]
    requested_at_unix_ms: int = Field(..., ge=0)
    requested_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID

    @field_validator("approved_action_ids")
    @classmethod
    def _canonical_action_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not action_id or len(action_id) > 128 for action_id in value):
            raise ValueError("approved_action_ids contains an invalid id")
        canonical = tuple(sorted(set(value)))
        if value != canonical:
            raise ValueError("approved_action_ids must be sorted and unique")
        return value


class ActionAuthorization(BaseModel):
    """One-time authority for an exact action subset and consent revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    authorization_id: str = Field(
        default_factory=lambda: f"authz_{uuid4().hex}",
        min_length=1,
        max_length=128,
    )
    authorization_request_id: str = Field(..., min_length=1, max_length=128)
    intervention_id: str = Field(..., min_length=1, max_length=128)
    manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    authorized_action_ids: tuple[str, ...] = Field(..., min_length=1, max_length=32)
    consent_revision: int = Field(..., ge=0)
    authorization_kind: Literal["user_confirmed", "research_autonomous"]
    source_surface: Literal["browser", "vscode", "desktop", "http", "daemon"]
    source_client_id: str | None = Field(None, max_length=128)
    requester_boot_id: UUID
    issued_at_unix_ms: int = Field(..., ge=0)
    issued_at_mono_ns: int = Field(..., ge=0)
    expires_at_unix_ms: int = Field(..., ge=0)
    ttl_ms: int = Field(..., ge=1, le=300_000)
    boot_id: UUID
    nonce: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=32,
        max_length=64,
    )

    @model_validator(mode="after")
    def _validate_authorization(self) -> ActionAuthorization:
        canonical = tuple(sorted(set(self.authorized_action_ids)))
        if self.authorized_action_ids != canonical:
            raise ValueError("authorized_action_ids must be sorted and unique")
        if self.expires_at_unix_ms != self.issued_at_unix_ms + self.ttl_ms:
            raise ValueError("authorization expiry must equal issue time plus ttl")
        if (
            self.authorization_kind == "research_autonomous"
            and self.source_surface != "daemon"
        ) or (
            self.authorization_kind == "user_confirmed"
            and self.source_surface == "daemon"
        ):
            raise ValueError("authorization kind and source surface disagree")
        return self


class AuthorizationDenied(BaseModel):
    """Typed, non-authoritative response to a rejected authorization request."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    authorization_request_id: str
    intervention_id: str
    manifest_sha256: str | None = None
    reason_code: Literal[
        "unknown_intervention",
        "manifest_mismatch",
        "action_mismatch",
        "consent_denied",
        "consent_changed",
        "execution_mode_denied",
        "transaction_closed",
        "invalid_request",
        "no_executor",
    ]
    detail: str = Field("", max_length=500)


class InterventionApplyCommand(BaseModel):
    """Daemon-to-adapter command emitted only after atomic auth consumption."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    manifest: ActionManifest
    authorization: ActionAuthorization
    actions: tuple[ManifestAction, ...] = Field(..., min_length=1, max_length=32)

    @model_validator(mode="after")
    def _exact_subset(self) -> InterventionApplyCommand:
        if self.manifest.intervention_id != self.authorization.intervention_id:
            raise ValueError("authorization intervention mismatch")
        if self.manifest.manifest_sha256 != self.authorization.manifest_sha256:
            raise ValueError("authorization manifest mismatch")
        ids = tuple(sorted(action.action_id for action in self.actions))
        if ids != self.authorization.authorized_action_ids:
            raise ValueError("apply actions do not equal authorized action ids")
        manifest_actions = {action.action_id: action for action in self.manifest.body.actions}
        for action in self.actions:
            if manifest_actions.get(action.action_id) != action:
                raise ValueError("apply action differs from manifest")
        return self


class ActionReceipt(BaseModel):
    """Typed result for one apply, compensation, or restore attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    receipt_id: str = Field(
        default_factory=lambda: f"rcpt_{uuid4().hex}",
        min_length=1,
        max_length=128,
    )
    intervention_id: str = Field(..., min_length=1, max_length=128)
    authorization_id: str = Field(..., min_length=1, max_length=128)
    manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    action_id: str = Field(..., min_length=1, max_length=128)
    phase: ReceiptPhase
    attempt: int = Field(1, ge=1, le=100)
    idempotency_key: str = Field(..., min_length=1, max_length=512)
    status: ReceiptStatus
    started_at_unix_ms: int = Field(..., ge=0)
    ended_at_unix_ms: int = Field(..., ge=0)
    started_at_mono_ns: int = Field(..., ge=0)
    ended_at_mono_ns: int = Field(..., ge=0)
    duration_ms: int = Field(..., ge=0)
    boot_id: UUID
    inverse_payload_json: str | None = Field(None, max_length=32_768)
    verification: VerificationStatus
    verification_detail: str = Field("", max_length=500)
    after_fingerprint: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(None, max_length=96)
    error_message: str | None = Field(None, max_length=500)
    retryable: bool = False
    source_client_type: str | None = Field(None, max_length=64)
    source_client_id: str | None = Field(None, max_length=128)

    @field_validator("inverse_payload_json")
    @classmethod
    def _inverse_is_canonical_object(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("inverse_payload_json must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("inverse_payload_json must encode an object")
        if _canonical_json(decoded) != value:
            raise ValueError("inverse_payload_json must be canonical")
        return value

    @model_validator(mode="after")
    def _validate_times_and_outcome(self) -> ActionReceipt:
        if self.ended_at_unix_ms < self.started_at_unix_ms:
            raise ValueError("receipt wall end precedes start")
        if self.ended_at_mono_ns < self.started_at_mono_ns:
            raise ValueError("receipt monotonic end precedes start")
        measured = (self.ended_at_mono_ns - self.started_at_mono_ns) // 1_000_000
        if abs(measured - self.duration_ms) > 1:
            raise ValueError("receipt duration does not match monotonic endpoints")
        if (
            self.status
            in {ReceiptStatus.SUCCEEDED, ReceiptStatus.ALREADY_COMPLETE}
            and self.error_code is not None
        ):
            raise ValueError("successful receipt cannot carry error_code")
        if self.status == ReceiptStatus.FAILED and not self.error_code:
            raise ValueError("failed receipt requires error_code")
        if (
            self.status == ReceiptStatus.FAILED
            and self.verification != VerificationStatus.FAILED
        ):
            raise ValueError("failed receipt must report failed verification")
        if (
            self.status
            in {ReceiptStatus.SUCCEEDED, ReceiptStatus.ALREADY_COMPLETE}
            and self.verification == VerificationStatus.FAILED
        ):
            raise ValueError("successful receipt cannot report failed verification")
        if self.verification == VerificationStatus.FAILED and not self.error_code:
            raise ValueError("failed verification requires error_code")
        expected_idempotency_key = (
            f"{self.authorization_id}:{self.action_id}:"
            f"{self.phase}:{self.attempt}"
        )
        if self.idempotency_key != expected_idempotency_key:
            raise ValueError(
                "idempotency_key must bind authorization, action, phase, and attempt"
            )
        return self


class InterventionReceiptBatch(BaseModel):
    """Client-to-daemon receipt envelope for one authorization or restore."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    intervention_id: str
    manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    authorization_id: str
    receipts: tuple[ActionReceipt, ...] = Field(..., min_length=1, max_length=96)

    @model_validator(mode="after")
    def _receipts_match_envelope(self) -> InterventionReceiptBatch:
        ids = [receipt.receipt_id for receipt in self.receipts]
        if len(ids) != len(set(ids)):
            raise ValueError("receipt_id values must be unique within a batch")
        idempotency_keys = [receipt.idempotency_key for receipt in self.receipts]
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ValueError(
                "idempotency_key values must be unique within a batch"
            )
        if len({receipt.phase for receipt in self.receipts}) != 1:
            raise ValueError("a receipt batch must contain exactly one phase")
        for receipt in self.receipts:
            if receipt.intervention_id != self.intervention_id:
                raise ValueError("receipt intervention_id mismatch")
            if receipt.manifest_sha256 != self.manifest_sha256:
                raise ValueError("receipt manifest mismatch")
            if receipt.authorization_id != self.authorization_id:
                raise ValueError("receipt authorization mismatch")
        return self


class RestoreAction(BaseModel):
    """Exact inverse operation derived from a successful apply receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    action_id: str
    executor: Literal["browser", "editor", "terminal", "desktop", "daemon"]
    reverse_capability: str
    inverse_payload_json: str = Field(..., max_length=32_768)
    original_authorization_id: str
    inverse_receipt_id: str | None = Field(None, min_length=1, max_length=128)
    owner_client_instance_id: str = Field(..., min_length=1, max_length=128)

    @field_validator("inverse_payload_json")
    @classmethod
    def _canonical_inverse(cls, value: str) -> str:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or _canonical_json(decoded) != value:
            raise ValueError("restore inverse payload must be a canonical JSON object")
        return value


class InterventionRestoreCommand(BaseModel):
    """Local deterministic restore command; never grants a forward effect."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    restore_id: str = Field(
        default_factory=lambda: f"restore_{uuid4().hex}",
        min_length=1,
        max_length=128,
    )
    intervention_id: str
    manifest_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    reason: Literal[
        "user_undo",
        "dismissed",
        "snoozed",
        "timed_out",
        "natural_recovery",
        "system_cancelled",
        "partial_compensation",
        "startup_recovery",
        "emergency_restore",
    ]
    requested_at_unix_ms: int = Field(..., ge=0)
    requested_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID
    actions: tuple[RestoreAction, ...] = Field(default_factory=tuple, max_length=32)

    @model_validator(mode="after")
    def _unique_restore_actions(self) -> InterventionRestoreCommand:
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("restore action_id values must be unique")
        return self


class AuthorizationLedgerEntry(BaseModel):
    """Persisted mutable wrapper around a frozen authorization."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    authorization: ActionAuthorization
    state: AuthorizationState = AuthorizationState.ISSUED
    consumed_at_unix_ms: int | None = Field(None, ge=0)
    consumed_at_mono_ns: int | None = Field(None, ge=0)
    state_reason: str | None = Field(None, max_length=500)


class LifecycleTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    from_state: InterventionLifecycleState | None
    to_state: InterventionLifecycleState
    reason: str = Field(..., min_length=1, max_length=200)
    at_unix_ms: int = Field(..., ge=0)
    at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID


class ExecutorDispatchBinding(BaseModel):
    """Durable record of the exact client selected for each executor.

    WebSocket transaction-state broadcasts intentionally expose transaction
    identifiers to every first-party surface.  A receipt therefore cannot be
    authenticated by an authorization id alone: it must also come from the
    precise client id selected before dispatch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)

    command_id: str = Field(..., min_length=1, max_length=128)
    action_client_instance_ids: dict[str, str] = Field(
        ...,
        min_length=1,
        max_length=32,
    )
    bound_at_unix_ms: int = Field(..., ge=0)
    bound_at_mono_ns: int = Field(..., ge=0)
    boot_id: UUID

    @field_validator("action_client_instance_ids")
    @classmethod
    def _nonempty_client_instance_ids(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        if any(
            not action_id
            or len(action_id) > 128
            or not client_id
            or len(client_id) > 128
            for action_id, client_id in value.items()
        ):
            raise ValueError(
                "dispatch action/client identities must be non-empty and bounded"
            )
        return value


class InterventionTransaction(BaseModel):
    """Durable aggregate for one intervention and all adapter attempts."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    intervention_id: str
    manifest: ActionManifest
    state: InterventionLifecycleState = InterventionLifecycleState.PROPOSED
    revision: int = Field(0, ge=0)
    authorizations: list[AuthorizationLedgerEntry] = Field(default_factory=list)
    receipts: list[ActionReceipt] = Field(default_factory=list)
    consent_evidence_receipt_ids: list[str] = Field(default_factory=list)
    transitions: list[LifecycleTransition] = Field(default_factory=list)
    dispatch_history: list[ExecutorDispatchBinding] = Field(default_factory=list)
    active_restore: InterventionRestoreCommand | None = None
    restore_history: list[InterventionRestoreCommand] = Field(default_factory=list)
    created_at_unix_ms: int = Field(..., ge=0)
    updated_at_unix_ms: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _validate_aggregate(self) -> InterventionTransaction:
        """Reject journals whose cross-record provenance is inconsistent."""

        def value(item: Enum | str | None) -> str | None:
            return item.value if isinstance(item, Enum) else item

        if self.manifest.intervention_id != self.intervention_id:
            raise ValueError("transaction manifest identity mismatch")
        if self.updated_at_unix_ms < self.created_at_unix_ms:
            raise ValueError("transaction update precedes creation")
        if not self.transitions:
            raise ValueError("transaction lifecycle history is empty")
        prior_state: str | None = None
        prior_wall = -1
        for transition in self.transitions:
            if value(transition.from_state) != prior_state:
                raise ValueError("transaction lifecycle chain is discontinuous")
            target_state = value(transition.to_state)
            if prior_state is None:
                if target_state != InterventionLifecycleState.PROPOSED.value:
                    raise ValueError("transaction lifecycle must start proposed")
            elif target_state not in INTERVENTION_ALLOWED_TRANSITIONS.get(
                prior_state, frozenset()
            ):
                raise ValueError("transaction lifecycle transition is invalid")
            if transition.at_unix_ms < prior_wall:
                raise ValueError("transaction lifecycle wall time regressed")
            prior_state = target_state
            prior_wall = transition.at_unix_ms
        if prior_state != value(self.state):
            raise ValueError("transaction state differs from lifecycle history")
        if self.updated_at_unix_ms < prior_wall:
            raise ValueError("transaction update precedes lifecycle history")

        manifest_actions = {
            action.action_id: action for action in self.manifest.body.actions
        }
        authorization_ids: set[str] = set()
        authorization_request_ids: set[str] = set()
        authorization_nonces: set[str] = set()
        authorization_actions: dict[str, set[str]] = {}
        authorized_action_ids: set[str] = set()
        for entry in self.authorizations:
            authorization = entry.authorization
            if authorization.authorization_id in authorization_ids:
                raise ValueError("duplicate transaction authorization id")
            if (
                authorization.authorization_request_id
                in authorization_request_ids
            ):
                raise ValueError("duplicate transaction authorization request id")
            if authorization.nonce in authorization_nonces:
                raise ValueError("duplicate transaction authorization nonce")
            authorization_ids.add(authorization.authorization_id)
            authorization_request_ids.add(
                authorization.authorization_request_id
            )
            authorization_nonces.add(authorization.nonce)
            action_ids = set(authorization.authorized_action_ids)
            if (
                authorization.intervention_id != self.intervention_id
                or authorization.manifest_sha256
                != self.manifest.manifest_sha256
                or authorization.boot_id != self.manifest.boot_id
                or authorization.issued_at_mono_ns
                < self.manifest.created_at_mono_ns
                or authorization.expires_at_unix_ms
                > self.manifest.expires_at_unix_ms
                or any(
                    action_id not in manifest_actions
                    for action_id in action_ids
                )
            ):
                raise ValueError("authorization provenance mismatch")
            if authorized_action_ids & action_ids:
                raise ValueError("manifest action was authorized more than once")
            authorized_action_ids.update(action_ids)
            authorization_actions[authorization.authorization_id] = action_ids
            if value(entry.state) == AuthorizationState.CONSUMED.value and (
                entry.consumed_at_unix_ms is None
                or entry.consumed_at_mono_ns is None
            ):
                raise ValueError("consumed authorization lacks consumption time")
            if value(entry.state) != AuthorizationState.CONSUMED.value and (
                entry.consumed_at_unix_ms is not None
                or entry.consumed_at_mono_ns is not None
            ):
                raise ValueError("unconsumed authorization has consumption time")
            if (
                entry.consumed_at_unix_ms is not None
                and entry.consumed_at_unix_ms
                < authorization.issued_at_unix_ms
            ):
                raise ValueError("authorization consumed before it was issued")
            if (
                entry.consumed_at_mono_ns is not None
                and entry.consumed_at_mono_ns
                < authorization.issued_at_mono_ns
            ):
                raise ValueError(
                    "authorization monotonic consumption precedes issuance"
                )

        restore_ids: set[str] = set()
        restore_action_ids: dict[str, set[str]] = {}
        restore_action_owners: dict[str, dict[str, str]] = {}
        restores_by_id: dict[str, InterventionRestoreCommand] = {}
        for restore in self.restore_history:
            if restore.restore_id in restore_ids:
                raise ValueError("duplicate transaction restore id")
            restore_ids.add(restore.restore_id)
            restores_by_id[restore.restore_id] = restore
            if (
                restore.intervention_id != self.intervention_id
                or restore.manifest_sha256 != self.manifest.manifest_sha256
            ):
                raise ValueError("restore provenance mismatch")
            for inverse in restore.actions:
                action = manifest_actions.get(inverse.action_id)
                forward_binding = next(
                    (
                        item
                        for item in self.dispatch_history
                        if item.command_id
                        == inverse.original_authorization_id
                    ),
                    None,
                )
                if (
                    action is None
                    or inverse.executor != action.executor
                    or inverse.reverse_capability != action.reverse_capability
                    or inverse.original_authorization_id not in authorization_ids
                    or inverse.action_id
                    not in authorization_actions.get(
                        inverse.original_authorization_id,
                        set(),
                    )
                    or not inverse.owner_client_instance_id
                    or forward_binding is None
                    or forward_binding.action_client_instance_ids.get(
                        inverse.action_id
                    )
                    != inverse.owner_client_instance_id
                ):
                    raise ValueError("restore action provenance mismatch")
                if inverse.inverse_receipt_id is None:
                    if inverse.inverse_payload_json != "{}":
                        raise ValueError(
                            "journal-fallback restore must carry an empty inverse"
                        )
                else:
                    inverse_receipt = next(
                        (
                            receipt
                            for receipt in self.receipts
                            if receipt.receipt_id == inverse.inverse_receipt_id
                        ),
                        None,
                    )
                    if (
                        inverse_receipt is None
                        or value(inverse_receipt.phase)
                        != ReceiptPhase.APPLY.value
                        or inverse_receipt.authorization_id
                        != inverse.original_authorization_id
                        or inverse_receipt.action_id != inverse.action_id
                        or inverse_receipt.inverse_payload_json
                        != inverse.inverse_payload_json
                    ):
                        raise ValueError("restore inverse provenance mismatch")
            expected_order = sorted(
                (action.action_id for action in restore.actions),
                key=lambda action_id: manifest_actions[action_id].ordinal,
                reverse=True,
            )
            if [action.action_id for action in restore.actions] != expected_order:
                raise ValueError("restore actions are not in reverse manifest order")
            restore_action_ids[restore.restore_id] = {
                action.action_id for action in restore.actions
            }
            restore_action_owners[restore.restore_id] = {
                action.action_id: action.owner_client_instance_id
                for action in restore.actions
            }
        if self.active_restore is not None and not any(
            restore == self.active_restore for restore in self.restore_history
        ):
            raise ValueError("active restore is absent from restore history")
        if value(self.state) == InterventionLifecycleState.RESTORING.value and (
            self.active_restore is None
        ):
            raise ValueError("restoring transaction lacks active restore")
        if value(self.state) in {
            InterventionLifecycleState.RESTORED.value,
            InterventionLifecycleState.APPLIED.value,
            InterventionLifecycleState.ABANDONED.value,
            InterventionLifecycleState.PROPOSED.value,
            InterventionLifecycleState.DELIVERED.value,
            InterventionLifecycleState.AUTHORIZED.value,
            InterventionLifecycleState.APPLYING.value,
            InterventionLifecycleState.PARTIAL.value,
            InterventionLifecycleState.FAILED.value,
        } and self.active_restore is not None:
            raise ValueError("settled transaction retains an active restore")
        if value(self.state) == InterventionLifecycleState.RESTORE_FAILED.value and (
            self.active_restore is None
        ):
            raise ValueError("restore_failed transaction lacks failed restore")

        command_ids = authorization_ids | restore_ids
        dispatch_command_ids: set[str] = set()
        for binding in self.dispatch_history:
            if binding.command_id not in command_ids:
                raise ValueError("dispatch binding references unknown command")
            if binding.command_id in dispatch_command_ids:
                raise ValueError("duplicate current dispatch binding")
            dispatch_command_ids.add(binding.command_id)
            expected_action_ids = (
                authorization_actions[binding.command_id]
                if binding.command_id in authorization_actions
                else restore_action_ids[binding.command_id]
            )
            bound_action_ids = set(binding.action_client_instance_ids)
            if (
                not bound_action_ids.issubset(expected_action_ids)
                or (
                    binding.command_id in authorization_actions
                    and bound_action_ids != expected_action_ids
                )
                or (
                    binding.command_id in restore_action_owners
                    and any(
                        restore_action_owners[binding.command_id].get(action_id)
                        != client_id
                        for action_id, client_id in (
                            binding.action_client_instance_ids.items()
                        )
                    )
                )
            ):
                raise ValueError("dispatch binding action provenance mismatch")
        receipt_ids: set[str] = set()
        idempotency_keys: set[str] = set()
        for receipt in self.receipts:
            if (
                receipt.receipt_id in receipt_ids
                or receipt.idempotency_key in idempotency_keys
            ):
                raise ValueError("duplicate persisted receipt identity")
            receipt_ids.add(receipt.receipt_id)
            idempotency_keys.add(receipt.idempotency_key)
            apply_receipt = value(receipt.phase) == ReceiptPhase.APPLY.value
            expected_commands = (
                authorization_ids
                if apply_receipt
                else restore_ids
            )
            allowed_action_ids = (
                authorization_actions.get(receipt.authorization_id, set())
                if apply_receipt
                else restore_action_ids.get(receipt.authorization_id, set())
            )
            if (
                receipt.intervention_id != self.intervention_id
                or receipt.manifest_sha256 != self.manifest.manifest_sha256
                or receipt.action_id not in manifest_actions
                or receipt.authorization_id not in expected_commands
                or receipt.action_id not in allowed_action_ids
            ):
                raise ValueError("receipt provenance mismatch")
            if not apply_receipt:
                restore = restores_by_id[receipt.authorization_id]
                expected_phase = (
                    ReceiptPhase.COMPENSATE.value
                    if restore.reason == "partial_compensation"
                    else ReceiptPhase.RESTORE.value
                )
                if value(receipt.phase) != expected_phase:
                    raise ValueError("inverse receipt phase provenance mismatch")
            receipt_binding = next(
                (
                    item
                    for item in self.dispatch_history
                    if item.command_id == receipt.authorization_id
                ),
                None,
            )
            expected_executor = manifest_actions[receipt.action_id].executor
            manifest_action = manifest_actions[receipt.action_id]
            receipt_succeeded = value(receipt.status) in {
                ReceiptStatus.SUCCEEDED.value,
                ReceiptStatus.ALREADY_COMPLETE.value,
            }
            if manifest_action.workspace_mutation and (
                value(receipt.verification)
                == VerificationStatus.NOT_APPLICABLE.value
                or (
                    receipt_succeeded
                    and (
                        value(receipt.verification)
                        != VerificationStatus.VERIFIED.value
                        or receipt.inverse_payload_json is None
                        or receipt.after_fingerprint is None
                    )
                )
            ):
                raise ValueError("workspace receipt lacks verified effect evidence")
            source_executor = {
                "chrome": "browser",
                "edge": "browser",
                "browser": "browser",
                "vscode": "editor",
                "editor": "editor",
                "desktop": "desktop",
                "terminal": "terminal",
                "daemon": "daemon",
            }.get(receipt.source_client_type or "")
            if (
                receipt_binding is None
                or source_executor != expected_executor
                or receipt.source_client_id is None
                or receipt_binding.action_client_instance_ids.get(
                    receipt.action_id
                )
                != receipt.source_client_id
            ):
                raise ValueError("receipt source provenance mismatch")
        if len(self.consent_evidence_receipt_ids) != len(
            set(self.consent_evidence_receipt_ids)
        ):
            raise ValueError("duplicate claimed consent evidence receipt")
        receipts_by_id = {
            receipt.receipt_id: receipt for receipt in self.receipts
        }
        for receipt_id in self.consent_evidence_receipt_ids:
            evidence = receipts_by_id.get(receipt_id)
            decoded_inverse = (
                json.loads(evidence.inverse_payload_json)
                if evidence is not None
                and evidence.inverse_payload_json is not None
                else {}
            )
            if (
                evidence is None
                or value(evidence.phase) != ReceiptPhase.APPLY.value
                or value(evidence.status)
                not in {
                    ReceiptStatus.SUCCEEDED.value,
                    ReceiptStatus.ALREADY_COMPLETE.value,
                }
                or value(evidence.verification)
                != VerificationStatus.VERIFIED.value
                or decoded_inverse.get("noEffect") is True
            ):
                raise ValueError("claimed consent evidence is not a verified apply")
        return self


class InterventionTransactionJournal(BaseModel):
    """Versioned root written atomically by the WP6 JSON store."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: Literal["1"] = "1"
    transactions: dict[str, InterventionTransaction] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _transaction_keys_match(self) -> InterventionTransactionJournal:
        if any(
            key != transaction.intervention_id
            for key, transaction in self.transactions.items()
        ):
            raise ValueError("transaction journal key mismatch")
        return self


def manifest_suggestion_matches(
    manifest: ActionManifest,
    candidate: object,
) -> bool:
    """Return whether displayed suggestion copy is digest-bound authority.

    A user gesture is meaningful only if the label/target/reason they saw is
    the same validated ``SuggestedAction`` embedded in the immutable manifest.
    Comparing only ``action_id`` would permit a stale or tampered presentation
    to authorize a different effect that happened to reuse that identifier.
    """

    from cortex.libs.schemas.intervention import SuggestedAction

    try:
        displayed = SuggestedAction.model_validate(candidate)
        action = next(
            item
            for item in manifest.body.actions
            if item.action_id == displayed.action_id
            and item.source == "suggested_action"
        )
        embedded = SuggestedAction.model_validate(
            action.parameters.get("suggested_action")
        )
    except (StopIteration, TypeError, ValueError):
        return False
    return embedded == displayed


__all__ = [
    "ActionAuthorization",
    "ActionManifest",
    "ActionManifestBody",
    "ActionReceipt",
    "AuthorizationDenied",
    "AuthorizationLedgerEntry",
    "AuthorizationState",
    "ExecutorDispatchBinding",
    "INTERVENTION_ALLOWED_TRANSITIONS",
    "InterventionApplyCommand",
    "InterventionAuthorizationRequest",
    "InterventionLifecycleState",
    "InterventionReceiptBatch",
    "InterventionRestoreCommand",
    "InterventionTransaction",
    "InterventionTransactionJournal",
    "LifecycleTransition",
    "ManifestAction",
    "manifest_suggestion_matches",
    "ReceiptPhase",
    "ReceiptStatus",
    "RestoreAction",
    "VerificationStatus",
]
