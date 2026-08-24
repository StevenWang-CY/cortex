"""Deterministic identities for calibration-producing algorithms."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import CodeType

from cortex.libs.schemas.physiology import SignalAlgorithmIdentity


def _normalized_constant(value: object) -> object:
    if isinstance(value, CodeType):
        return {"type": "code", "value": _normalized_code(value)}
    if value is None:
        return {"type": "none"}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, tuple):
        return {
            "type": "tuple",
            "value": [_normalized_constant(item) for item in value],
        }
    if isinstance(value, frozenset):
        normalized = [_normalized_constant(item) for item in value]
        return {
            "type": "frozenset",
            "value": sorted(
                normalized,
                key=lambda item: json.dumps(item, sort_keys=True),
            ),
        }
    if isinstance(value, (str, int, float, bool, complex)):
        return {"type": type(value).__qualname__, "value": repr(value)}
    return {"type": type(value).__qualname__}


def _normalized_code(code: CodeType) -> dict[str, object]:
    """Serialize executable structure without paths, addresses, or line numbers."""

    return {
        "bytecode": code.co_code.hex(),
        "constants": [_normalized_constant(item) for item in code.co_consts],
        "names": code.co_names,
        "varnames": code.co_varnames,
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
    }


def _executable_fallback_bytes(target: type[object]) -> bytes:
    members: dict[str, object] = {}
    for name, member in sorted(vars(target).items()):
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        code = getattr(member, "__code__", None)
        if isinstance(code, CodeType):
            members[name] = _normalized_code(code)
    payload = {
        "module": target.__module__,
        "qualname": target.__qualname__,
        "members": members,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def source_digest(value: object) -> str:
    """Hash the source module containing an instance or class implementation."""

    target = value if isinstance(value, type) else type(value)
    source = inspect.getsourcefile(target)
    if source is None:
        return hashlib.sha256(_executable_fallback_bytes(target)).hexdigest()
    try:
        return hashlib.sha256(Path(source).read_bytes()).hexdigest()
    except OSError:
        return hashlib.sha256(_executable_fallback_bytes(target)).hexdigest()


def configuration_digest(
    parameters: dict[str, str | int | float | bool],
) -> str:
    payload = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def calibration_algorithm_identity(
    value: object,
    *,
    name: str,
    version: str,
    parameters: dict[str, str | int | float | bool],
) -> SignalAlgorithmIdentity:
    """Create the exact identity persisted in a measured profile."""

    return SignalAlgorithmIdentity(
        name=name,
        version=version,
        implementation_sha256=source_digest(value),
        configuration_sha256=configuration_digest(parameters),
        parameters=parameters,
        selection_mode="fixed",
    )
