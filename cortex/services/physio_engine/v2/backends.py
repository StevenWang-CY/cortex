"""Validated registry for fixed rPPG extraction backends."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from cortex.libs.schemas.physiology import SignalAlgorithmIdentity
from cortex.services.physio_engine.rppg import (
    RPPGAlgorithm,
    _pos_single_window,
    extract_bvp_chrom,
    extract_bvp_green,
    extract_bvp_pos,
)
from cortex.services.physio_engine.v2.provenance import code_sha256

Extractor = Callable[[NDArray[np.float64], float], NDArray[np.float64]]


class BackendValidationError(RuntimeError):
    """The requested backend cannot prove the identity it was configured with."""


@dataclass(frozen=True)
class BackendDefinition:
    """Executable backend plus the assets required to identify it."""

    algorithm: RPPGAlgorithm
    version: str
    extractor: Extractor
    implementation_components: tuple[Callable[..., object], ...] = ()
    asset_path: Path | None = None
    asset_sha256: str | None = None


@dataclass(frozen=True)
class ResolvedBackend:
    """A validated backend safe to execute for the lifetime of a run."""

    definition: BackendDefinition
    identity: SignalAlgorithmIdentity

    def extract(
        self,
        rgb_window: NDArray[np.float64],
        *,
        fs: float,
    ) -> NDArray[np.float64]:
        if rgb_window.ndim != 2 or rgb_window.shape[1] != 3:
            raise ValueError("rPPG input must have shape [samples, 3]")
        if rgb_window.shape[0] < 2:
            raise ValueError("rPPG input requires at least two samples")
        if fs <= 0 or not np.isfinite(fs):
            raise ValueError("rPPG sample rate must be finite and positive")
        if not bool(np.isfinite(rgb_window).all()):
            raise ValueError("rPPG input must be finite")
        output = np.asarray(
            self.definition.extractor(rgb_window, float(fs)), dtype=np.float64
        )
        if output.shape != (rgb_window.shape[0],):
            raise BackendValidationError(
                f"backend {self.identity.name} returned {output.shape}, expected "
                f"({rgb_window.shape[0]},)"
            )
        if not bool(np.isfinite(output).all()):
            raise BackendValidationError(
                f"backend {self.identity.name} returned non-finite samples"
            )
        return output


def _implementation_sha256(definition: BackendDefinition) -> str:
    """Hash executable code, dependencies declared by the backend, and defaults."""

    functions = (definition.extractor, *definition.implementation_components)
    return code_sha256(functions)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RPPGBackendRegistry:
    """Resolve rPPG implementations without silent substitution.

    POS, CHROM and GREEN are code-native and therefore identify their exact
    executable implementation. Future learned backends must register an
    asset path and expected checksum; a missing or corrupt asset fails before
    processing starts.
    """

    def __init__(self) -> None:
        self._definitions: dict[RPPGAlgorithm, BackendDefinition] = {}

    @classmethod
    def with_packaged_backends(cls) -> RPPGBackendRegistry:
        registry = cls()
        registry.register(
            BackendDefinition(
                RPPGAlgorithm.POS,
                "pos/2.0.0",
                extract_bvp_pos,
                implementation_components=(_pos_single_window,),
            )
        )
        registry.register(
            BackendDefinition(RPPGAlgorithm.CHROM, "chrom/2.0.0", extract_bvp_chrom)
        )
        registry.register(
            BackendDefinition(RPPGAlgorithm.GREEN, "green/2.0.0", extract_bvp_green)
        )
        return registry

    def register(self, definition: BackendDefinition) -> None:
        if definition.algorithm in self._definitions:
            raise BackendValidationError(
                f"duplicate rPPG backend: {definition.algorithm.value}"
            )
        if (definition.asset_path is None) != (definition.asset_sha256 is None):
            raise BackendValidationError(
                "asset_path and asset_sha256 must be provided together"
            )
        self._definitions[definition.algorithm] = definition

    def resolve(
        self,
        algorithm: RPPGAlgorithm | str,
        *,
        expected_implementation_sha256: str | None = None,
    ) -> ResolvedBackend:
        try:
            key = algorithm if isinstance(algorithm, RPPGAlgorithm) else RPPGAlgorithm(algorithm)
        except ValueError as exc:
            raise BackendValidationError(
                f"unregistered rPPG backend {algorithm!r}; no fallback was applied"
            ) from exc
        definition = self._definitions.get(key)
        if definition is None:
            raise BackendValidationError(
                f"rPPG backend {key.value!r} is not registered; no fallback was applied"
            )

        implementation_sha256 = _implementation_sha256(definition)
        if (
            expected_implementation_sha256 is not None
            and implementation_sha256 != expected_implementation_sha256
        ):
            raise BackendValidationError(
                f"rPPG backend {key.value!r} checksum mismatch: expected "
                f"{expected_implementation_sha256}, got {implementation_sha256}"
            )

        asset_sha256: str | None = None
        if definition.asset_path is not None:
            if not definition.asset_path.is_file():
                raise BackendValidationError(
                    f"rPPG backend {key.value!r} asset is missing: "
                    f"{definition.asset_path}"
                )
            asset_sha256 = _file_sha256(definition.asset_path)
            if asset_sha256 != definition.asset_sha256:
                raise BackendValidationError(
                    f"rPPG backend {key.value!r} asset checksum mismatch"
                )

        return ResolvedBackend(
            definition=definition,
            identity=SignalAlgorithmIdentity(
                name=key.value,
                version=definition.version,
                implementation_sha256=implementation_sha256,
                asset_sha256=asset_sha256,
                selection_mode="fixed",
            ),
        )

    def validate_all(self) -> tuple[SignalAlgorithmIdentity, ...]:
        return tuple(
            self.resolve(algorithm).identity
            for algorithm in sorted(self._definitions, key=lambda item: item.value)
        )
