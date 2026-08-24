"""Stable algorithm and configuration fingerprints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from types import CodeType

ConfigurationScalar = str | int | float | bool


def _stable_code_bytes(code: CodeType) -> bytes:
    parts = [
        code.co_code,
        repr(code.co_names).encode("utf-8"),
        repr(code.co_varnames).encode("utf-8"),
    ]
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            parts.append(_stable_code_bytes(constant))
        else:
            parts.append(repr(constant).encode("utf-8"))
    return b"\0".join(parts)


def code_sha256(
    functions: Sequence[Callable[..., object]],
    *,
    dependency_sha256: Sequence[str] = (),
) -> str:
    """Hash stable executable properties and declared implementation inputs."""

    digest = hashlib.sha256()
    for function in functions:
        digest.update(_stable_code_bytes(function.__code__))
        digest.update(repr(function.__defaults__).encode("utf-8"))
        digest.update(repr(function.__kwdefaults__).encode("utf-8"))
    for dependency in dependency_sha256:
        digest.update(dependency.encode("ascii"))
    return digest.hexdigest()


def configuration_sha256(parameters: Mapping[str, ConfigurationScalar]) -> str:
    """Hash a canonical, reviewable algorithm parameter mapping."""

    payload = json.dumps(
        dict(parameters), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

