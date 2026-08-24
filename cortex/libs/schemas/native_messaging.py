"""Native-messaging request and response schemas.

The Chrome native-host channel (:mod:`cortex.scripts.native_host`) used
to validate inbound messages only by length cap (8 MB). Everything past
that was passed straight to the dispatcher. ``launch_daemon`` in
particular reads ``project_root`` from the message and uses it as the
working directory for the daemon subprocess; a malformed or hostile
extension could therefore steer the daemon's CWD or crash the host
with an 8 MB blob of garbage.

This module pins the contract with a Pydantic discriminated-union
schema:

* ``launch``  — optional ``project_root`` constrained to an allowlist
  of canonical directories (``~/Desktop``, ``~/Documents``,
  ``~/Projects``, ``/Applications/Cortex.app``, plus any directory
  named in ``$CORTEX_NATIVE_HOST_PROJECT_ROOTS`` — a comma-separated
  list to support bespoke developer setups; comma matches the
  documented example in ``.env.example`` and avoids ambiguity with the
  ``:`` that Unix uses as the PATH separator).
* ``stop``    — no extra fields.
* ``status``  — no extra fields.
* ``get_auth_token`` — no extra fields. The native host returns the
  capability token via :mod:`cortex.libs.auth.local_token` (F07b).

The dispatching helper :func:`parse_native_message` returns the parsed
model on success or a structured error envelope on failure. The native
host's ``main()`` loop forwards the envelope back over native-messaging
stdout so the extension can surface a meaningful error rather than
hanging on a silent reject.

Responses use the same generated contract. Every response carries the
request command as its discriminator; parse/dispatch failures use the
``"error"`` discriminator and may carry the rejected command name as
``request_command``. This prevents the Python host and TypeScript
client from independently inventing response field names (the auth
response historically used ``token`` in Python and ``auth_token`` in
TypeScript, so both isolated test suites passed while the real channel
failed).

Tighter size cap (64 KB) lives next to the schemas so the guardrails
ship together; every legitimate native-host message is well under 1 KB.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

# Maximum byte length of a native-messaging payload. Tightened from the
# legacy 8 MB ceiling — every legitimate request is < 1 KB; the cap is
# generous to allow future growth without inviting OOM amplification.
MAX_MESSAGE_BYTES: int = 64 * 1024


def _expand(path: str) -> Path:
    """Resolve ``path`` with ``~`` expansion. Does *not* require it to exist."""
    return Path(os.path.expanduser(path)).resolve()


def _default_project_root_allowlist() -> tuple[Path, ...]:
    """Canonical install / project locations the daemon may launch from.

    ``$CORTEX_NATIVE_HOST_PROJECT_ROOTS`` is a comma-separated env var
    that power users can set to extend the list (e.g. for a custom
    Code workspace tree under ``~/work/`` that lives outside the four
    default roots). Empty entries and unresolvable paths are dropped.
    Comma matches the documented example in ``.env.example`` and avoids
    colliding with the ``:`` Unix uses as its PATH separator (which a
    real absolute path never contains).
    """
    home = Path.home()
    roots: list[Path] = [
        home / "Desktop",
        home / "Documents",
        home / "Projects",
        Path("/Applications/Cortex.app"),
    ]
    extra = os.environ.get("CORTEX_NATIVE_HOST_PROJECT_ROOTS", "")
    for entry in extra.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            roots.append(_expand(entry))
        except (OSError, RuntimeError):
            # Unresolvable extra entries are ignored, not fatal — the
            # default allowlist still applies.
            continue
    return tuple(roots)


def _is_under_allowlist(candidate: Path, allowlist: tuple[Path, ...]) -> bool:
    """True iff ``candidate`` is the same as, or nested under, an allowlist root."""
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return False
    for root in allowlist:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Command schemas
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    """Shared config for native-messaging models.

    ``extra="forbid"`` catches unexpected fields so a tampered extension
    cannot smuggle in attributes the daemon would later trust.
    """

    model_config = ConfigDict(extra="forbid")


class LaunchMessage(_Base):
    """``{"command":"launch", "project_root": "/Users/.../Project X"}``.

    ``project_root`` is optional. When present the value must be an
    existing directory that lives under the project-root allowlist; the
    validator runs at parse time so an invalid path is reported before
    dispatch.
    """

    command: Literal["launch"]
    project_root: str | None = Field(default=None, max_length=4096)

    @field_validator("project_root")
    @classmethod
    def _validate_project_root(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        try:
            candidate = _expand(value)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"unresolvable_path: {exc}") from exc
        if not candidate.is_dir():
            raise ValueError("project_root_not_a_directory")
        if not _is_under_allowlist(candidate, _default_project_root_allowlist()):
            raise ValueError("project_root_outside_allowlist")
        return str(candidate)


class StopMessage(_Base):
    command: Literal["stop"]


class StatusMessage(_Base):
    command: Literal["status"]


class GetAuthTokenMessage(_Base):
    command: Literal["get_auth_token"]


class RaiseDashboardMessage(_Base):
    """``{"command":"raise_dashboard", "target":"desktop"}``.

    Audit-prod Phase-4 closure: native_host.py was peeking the raw JSON
    for this command and dispatching outside the Pydantic-validated
    union. Promoting the command to a typed message gives the same
    validation guarantees the rest of the native-host vocabulary has,
    and lets the codegen pipeline emit a generated TypeScript type for
    the extension's side of the channel.

    ``target`` is a short string identifying the surface to raise (the
    desktop shell window, an editor host, etc.). The 64-char cap is
    generous for short identifiers and prevents a hostile / malformed
    extension from blowing up the host with a megabyte target.
    """

    command: Literal["raise_dashboard"]
    target: str = Field(..., max_length=64)


NativeMessage = Annotated[
    LaunchMessage | StopMessage | StatusMessage | GetAuthTokenMessage | RaiseDashboardMessage,
    Field(discriminator="command"),
]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class LaunchResponse(_Base):
    command: Literal["launch"]
    status: Literal["launched", "already_running", "timeout", "error"]
    error: str | None = Field(default=None, max_length=4096)


class StopResponse(_Base):
    command: Literal["stop"]
    status: Literal["stopped", "error"]
    error: str | None = Field(default=None, max_length=4096)


class DaemonStatusResponse(_Base):
    command: Literal["status"]
    status: Literal["running", "stopped"]


class GetAuthTokenResponse(_Base):
    """Successful capability-token response.

    Errors use :class:`NativeErrorResponse`, keeping this success shape
    strict enough that a client cannot accidentally accept a legacy
    ``token`` key or an empty/non-hex secret.
    """

    command: Literal["get_auth_token"]
    status: Literal["ok"]
    auth_token: str = Field(min_length=32, max_length=1024, pattern=r"^[0-9a-f]+$")


class RaiseDashboardResponse(_Base):
    command: Literal["raise_dashboard"]
    status: Literal["ok"]
    http_status: int = Field(ge=100, le=599)


class NativeErrorResponse(_Base):
    command: Literal["error"]
    status: Literal["error"] = "error"
    request_command: str | None = Field(default=None, max_length=64)
    error: str = Field(min_length=1, max_length=4096)
    detail: str | None = Field(default=None, max_length=8192)


NativeHostResponse = Annotated[
    LaunchResponse
    | StopResponse
    | DaemonStatusResponse
    | GetAuthTokenResponse
    | RaiseDashboardResponse
    | NativeErrorResponse,
    Field(discriminator="command"),
]

_NATIVE_MESSAGE_ADAPTER: TypeAdapter[NativeMessage] = TypeAdapter(NativeMessage)
_NATIVE_RESPONSE_ADAPTER: TypeAdapter[NativeHostResponse] = TypeAdapter(NativeHostResponse)


def validate_native_response(data: Any) -> NativeHostResponse:
    """Validate a native-host response against the canonical union.

    The host uses this immediately before framing a response. Tests and
    non-browser clients may also use it as the runtime counterpart to
    the generated TypeScript decoder.
    """

    return _NATIVE_RESPONSE_ADAPTER.validate_python(data)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class ParseResult(BaseModel):
    """Outcome of :func:`parse_native_message`.

    Exactly one of ``message`` / ``error`` is populated. The native host
    inspects ``error`` to build the response envelope returned to the
    extension.
    """

    message: NativeMessage | None = None
    error: str | None = None
    detail: str | None = None


def parse_native_message(raw: bytes) -> ParseResult:
    """Parse a 4-byte-length-prefix-stripped native-messaging payload.

    Args:
        raw: The decoded payload bytes (length prefix already
            consumed by the caller).

    Returns:
        :class:`ParseResult` carrying either the validated message or
        a structured error. The caller never raises on bad input — the
        native host's ``main()`` always has a response to send.
    """
    if len(raw) > MAX_MESSAGE_BYTES:
        return ParseResult(
            error="message_too_large",
            detail=f"len={len(raw)} > max={MAX_MESSAGE_BYTES}",
        )

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ParseResult(error="invalid_encoding", detail=str(exc))

    try:
        data: Any = json.loads(decoded)
    except json.JSONDecodeError as exc:
        return ParseResult(error="malformed_json", detail=str(exc))

    if not isinstance(data, dict):
        return ParseResult(error="not_an_object")

    # Default to ``launch`` for legacy callers that omit ``command``.
    # All command names not in the union surface as ``unknown_command``
    # rather than crashing.
    if "command" not in data:
        data["command"] = "launch"

    try:
        # Pydantic v2 discriminated-union validation. Unknown command
        # names produce a ValidationError with ``discriminator`` in the
        # error path.
        parsed = _NATIVE_MESSAGE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        return ParseResult(error="invalid_message", detail=str(exc))

    return ParseResult(message=parsed)


__all__ = [
    "DaemonStatusResponse",
    "GetAuthTokenMessage",
    "GetAuthTokenResponse",
    "LaunchMessage",
    "LaunchResponse",
    "MAX_MESSAGE_BYTES",
    "NativeErrorResponse",
    "NativeHostResponse",
    "NativeMessage",
    "ParseResult",
    "RaiseDashboardMessage",
    "RaiseDashboardResponse",
    "StatusMessage",
    "StopResponse",
    "StopMessage",
    "parse_native_message",
    "validate_native_response",
]
