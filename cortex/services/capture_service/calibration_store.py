"""Crash-safe persistence for immutable calibration profiles."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from uuid import UUID

from cortex.application.clock import SYSTEM_CLOCK, Clock
from cortex.libs.schemas.calibration import (
    ActiveCalibrationPointer,
    CalibrationProfile,
    CalibrationProvenance,
)
from cortex.libs.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)


def _canonical_profile_bytes(profile: CalibrationProfile) -> bytes:
    return json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def calibration_profile_sha256(profile: CalibrationProfile) -> str:
    return hashlib.sha256(_canonical_profile_bytes(profile)).hexdigest()


class CalibrationProfileStore:
    """Own immutable profile files and an atomic active-profile pointer."""

    def __init__(self, storage_path: str | Path, *, clock: Clock | None = None) -> None:
        self._storage_path = Path(storage_path).expanduser()
        self._clock = clock or SYSTEM_CLOCK
        self.root = self._storage_path / "calibration"
        self.profiles_dir = self.root / "profiles"
        self.demo_profiles_dir = self.root / "demo_profiles"
        self.active_pointer_path = self.root / "active.json"

    def profile_path(self, profile_id: object, *, demo: bool = False) -> Path:
        try:
            canonical_id = UUID(str(profile_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("calibration profile id must be a UUID") from exc
        directory = self.demo_profiles_dir if demo else self.profiles_dir
        return directory / f"{canonical_id}.json"

    def save_inactive(self, profile: CalibrationProfile) -> Path:
        """Persist once; an existing UUID may only contain identical bytes."""

        path = self.profile_path(profile.profile_id, demo=profile.is_demo)
        if path.exists():
            existing = CalibrationProfile.model_validate_json(path.read_text())
            if _canonical_profile_bytes(existing) != _canonical_profile_bytes(profile):
                raise ValueError(f"calibration profile {profile.profile_id} is immutable")
            return path
        atomic_write_json(path, profile.model_dump(mode="json"))
        return path

    def activate(
        self,
        profile: CalibrationProfile,
        *,
        pointer: ActiveCalibrationPointer | None = None,
    ) -> ActiveCalibrationPointer:
        """Commit a measured profile; the pointer is written last."""

        if profile.provenance != CalibrationProvenance.MEASURED.value:
            raise ValueError("demo or synthetic calibration profiles cannot become active")
        if not profile.is_approved:
            raise ValueError("an active calibration profile requires explicit approval")
        self.save_inactive(profile)

        expected_sha256 = calibration_profile_sha256(profile)
        if pointer is None:
            pointer = ActiveCalibrationPointer(
                profile_id=profile.profile_id,
                profile_sha256=expected_sha256,
                activated_at_unix_ms=self._clock.unix_ms(),
            )
        elif (
            pointer.profile_id != profile.profile_id
            or pointer.profile_sha256 != expected_sha256
        ):
            raise ValueError("active calibration pointer does not match profile")
        atomic_write_json(
            self.active_pointer_path,
            pointer.model_dump(mode="json"),
        )

        # The pointer above is the sole transaction authority.  The legacy
        # file is only a compatibility projection, so it is updated after the
        # commit and cannot turn a failed pointer write into a partial
        # activation.  Projection failure must likewise not roll back a
        # successfully committed profile or prevent the live graph swap.
        try:
            legacy = profile.to_user_baselines()
            atomic_write_json(
                self._storage_path / "baselines" / "default.json",
                legacy.model_dump(mode="json"),
            )
        except OSError:
            logger.warning(
                "Active calibration committed but legacy baseline projection failed",
                exc_info=True,
            )
        return pointer

    def save_demo(self, profile: CalibrationProfile) -> Path:
        if profile.provenance != CalibrationProvenance.DEMO.value:
            raise ValueError("save_demo accepts demo provenance only")
        return self.save_inactive(profile)

    def load_profile(self, profile_id: object, *, demo: bool = False) -> CalibrationProfile:
        path = self.profile_path(profile_id, demo=demo)
        return CalibrationProfile.model_validate_json(path.read_text())

    def load_active(self) -> CalibrationProfile | None:
        if not self.active_pointer_path.exists():
            return None
        pointer = ActiveCalibrationPointer.model_validate_json(
            self.active_pointer_path.read_text()
        )
        profile = self.load_profile(pointer.profile_id)
        if profile.provenance != CalibrationProvenance.MEASURED.value:
            raise ValueError("active calibration pointer references non-measured data")
        if calibration_profile_sha256(profile) != pointer.profile_sha256:
            raise ValueError("active calibration profile checksum mismatch")
        if not profile.is_approved:
            raise ValueError("active calibration profile is not approved")
        return profile
