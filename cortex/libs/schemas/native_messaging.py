"""Native-messaging request schemas (audit F14 + F37).

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
  named in ``$CORTEX_NATIVE_HOST_PROJECT_ROOTS`` — a colon-separated
  list to support bespoke developer setups).
* ``stop``    — no extra fields.
* ``status``  — no extra fields.
* ``get_auth_token`` — no extra fields. The native host returns the
  capability token via :mod:`cortex.libs.auth.local_token` (F07b).

The dispatching helper :func:`parse_native_message` returns the parsed
model on success or a structured error envelope on failure. The native
host's ``main()`` loop forwards the envelope back over native-messaging
stdout so the extension can surface a meaningful error rather than
hanging on a silent reject.

Tighter size cap (64 KB) lives next to the schema so the two
guardrails ship together; every legitimate native-host request is
well under 1 KB.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Maximum byte length of a native-messaging payload. Tightened from the
# legacy 8 MB ceiling — every legitimate request is < 1 KB; the cap is
# generous to allow future growth without inviting OOM amplification.
MAX_MESSAGE_BYTES: int = 64 * 1024


def _expand(path: str) -> Path:
    """Resolve ``path`` with ``~`` expansion. Does *not* require it to exist."""
    return Path(os.path.expanduser(path)).resolve()


def _default_project_root_allowlist() -> tuple[Path, ...]:
    """Canonical install / project locations the daemon may launch from.

    ``$CORTEX_NATIVE_HOST_PROJECT_ROOTS`` is a colon-separated env var
    that power users can set to extend the list (e.g. for a custom
    Code workspace tree under ``~/work/`` that lives outside the four
    default roots). Empty entries and unresolvable paths are dropped.
    """
    home = Path.home()
    roots: list[Path] = [
        home / "Desktop",
        home / "Documents",
        home / "Projects",
        Path("/Applications/Cortex.app"),
    ]
    extra = os.environ.get("CORTEX_NATIVE_HOST_PROJECT_ROOTS", "")
    for entry in extra.split(":"):
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


NativeMessage = Annotated[
    LaunchMessage | StopMessage | StatusMessage | GetAuthTokenMessage,
    Field(discriminator="command"),
]


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
        from pydantic import TypeAdapter

        adapter: TypeAdapter[NativeMessage] = TypeAdapter(NativeMessage)
        parsed = adapter.validate_python(data)
    except ValidationError as exc:
        return ParseResult(error="invalid_message", detail=str(exc))

    return ParseResult(message=parsed)


__all__ = [
    "GetAuthTokenMessage",
    "LaunchMessage",
    "MAX_MESSAGE_BYTES",
    "NativeMessage",
    "ParseResult",
    "StatusMessage",
    "StopMessage",
    "parse_native_message",
]
