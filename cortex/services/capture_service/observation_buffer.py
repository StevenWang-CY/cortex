"""Bounded, time-indexed storage and readiness gates for observations."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar
from uuid import UUID

import numpy as np
from numpy.typing import NDArray

from cortex.libs.schemas.observations import MissingReason, ObservationValidity


class TimedObservation(Protocol):
    """Minimum metadata required by :class:`ObservationBuffer`."""

    observed_at_mono_ns: int
    boot_id: UUID
    sequence: int


ObservationT = TypeVar("ObservationT", bound=TimedObservation)


class ObservationBuffer(Generic[ObservationT]):
    """A bounded acquisition-order buffer with one monotonic clock domain.

    Equal timestamps are retained so the window gate can reject ambiguous
    samples explicitly.  Backwards timestamps are a producer contract error
    and are rejected rather than silently reordered.
    """

    def __init__(self, *, max_age_seconds: float, max_items: int) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        self._max_age_ns = int(max_age_seconds * 1_000_000_000)
        self._items: deque[ObservationT] = deque(maxlen=max_items)
        self._boot_id: UUID | None = None

    def __len__(self) -> int:
        return len(self._items)

    @property
    def boot_id(self) -> UUID | None:
        return self._boot_id

    def clear(self) -> None:
        self._items.clear()
        self._boot_id = None

    def append(self, observation: ObservationT) -> None:
        if observation.observed_at_mono_ns < 0:
            raise ValueError("observation monotonic time must be non-negative")
        if self._boot_id is not None and observation.boot_id != self._boot_id:
            # Monotonic values from different boots are incomparable.
            self.clear()
        if (
            self._items
            and observation.observed_at_mono_ns
            < self._items[-1].observed_at_mono_ns
        ):
            raise ValueError("observation timestamps must not move backwards")
        self._boot_id = observation.boot_id
        self._items.append(observation)
        cutoff = observation.observed_at_mono_ns - self._max_age_ns
        while self._items and self._items[0].observed_at_mono_ns < cutoff:
            self._items.popleft()

    def snapshot(self, *, since_mono_ns: int | None = None) -> tuple[ObservationT, ...]:
        if since_mono_ns is None:
            return tuple(self._items)
        return tuple(
            item for item in self._items
            if item.observed_at_mono_ns >= since_mono_ns
        )


@dataclass(frozen=True)
class NumericObservation(TimedObservation):
    """One scalar/vector sample plus its observation integrity metadata."""

    observed_at_unix_ms: int
    observed_at_mono_ns: int
    boot_id: UUID
    sequence: int
    value: NDArray[np.float64] | None
    validity: str
    missing_reason: MissingReason | str | None
    quality: float
    head_jitter_deg: float = 0.0

    @property
    def is_valid(self) -> bool:
        return (
            self.validity == ObservationValidity.VALID.value
            and self.value is not None
            and bool(np.isfinite(self.value).all())
        )


@dataclass(frozen=True)
class PreparedObservationWindow:
    """Result of validating and uniformly resampling a numeric window."""

    ready: bool
    values: NDArray[np.float64] | None
    sample_rate_hz: float
    valid_fraction: float
    temporal_coverage: float
    artifact_fraction: float
    quality: float
    valid_duration_ms: int
    sample_count: int
    expected_sample_count: int
    max_interpolation_gap_ms: float
    unavailable_reasons: tuple[MissingReason, ...]
    mean_head_jitter_deg: float


def prepare_observation_window(
    observations: tuple[NumericObservation, ...] | list[NumericObservation],
    *,
    window_seconds: float,
    nominal_fps: float,
    min_valid_fraction: float = 0.80,
    max_interpolation_gap_ms: float = 250.0,
    max_motion_fraction: float = 0.10,
    fps_clamp_min: float = 10.0,
    fps_clamp_max: float = 60.0,
) -> PreparedObservationWindow:
    """Gate and resample one observation window without fabricating data.

    Interpolation is allowed only between valid endpoints and only when the
    longest endpoint-to-endpoint gap is bounded.  All-missing, short,
    duplicate-time and low-coverage windows return ``ready=False`` with no
    numeric values.
    """

    if window_seconds <= 0 or nominal_fps <= 0:
        raise ValueError("window_seconds and nominal_fps must be positive")
    if not 0.0 <= min_valid_fraction <= 1.0:
        raise ValueError("min_valid_fraction must be within [0, 1]")
    if not 0.0 <= max_motion_fraction <= 1.0:
        raise ValueError("max_motion_fraction must be within [0, 1]")

    expected_count = max(2, int(round(window_seconds * nominal_fps)))
    reasons: list[MissingReason] = []

    if not observations:
        return _unavailable_window(
            expected_count=expected_count,
            reasons=(MissingReason.INSUFFICIENT_WINDOW,),
        )

    ordered = list(observations)
    latest_ns = ordered[-1].observed_at_mono_ns
    window_ns = int(window_seconds * 1_000_000_000)
    start_ns = latest_ns - window_ns
    selected = [item for item in ordered if item.observed_at_mono_ns >= start_ns]
    if not selected:
        return _unavailable_window(
            expected_count=expected_count,
            reasons=(MissingReason.INSUFFICIENT_WINDOW,),
        )

    times = np.asarray([item.observed_at_mono_ns for item in selected], dtype=np.int64)
    diffs = np.diff(times)
    if bool((diffs <= 0).any()):
        reasons.append(MissingReason.ARTIFACT)

    span_ns = max(0, int(times[-1]) - int(times[0]))
    temporal_coverage = min(1.0, span_ns / window_ns) if window_ns else 0.0

    valid_items = [item for item in selected if item.is_valid]
    valid_count = len(valid_items)
    valid_fraction = min(1.0, valid_count / expected_count)
    artifact_count = sum(not item.is_valid for item in selected)
    artifact_fraction = min(1.0, artifact_count / expected_count)
    motion_count = sum(
        not item.is_valid and item.missing_reason == MissingReason.MOTION
        for item in selected
    )
    motion_fraction = min(1.0, motion_count / expected_count)
    quality_mass = sum(item.quality for item in valid_items) / expected_count
    quality = float(np.clip(quality_mass * temporal_coverage, 0.0, 1.0))

    if valid_count == 0:
        reasons.extend(
            _missing_reasons(selected) or [MissingReason.INSUFFICIENT_WINDOW]
        )
    if valid_fraction < min_valid_fraction or temporal_coverage < min_valid_fraction:
        reasons.append(MissingReason.INSUFFICIENT_WINDOW)
    if motion_fraction > max_motion_fraction:
        reasons.append(MissingReason.MOTION)

    valid_times = np.asarray(
        [item.observed_at_mono_ns for item in valid_items], dtype=np.int64
    )
    max_gap_ms = float("inf")
    if len(valid_times) >= 2:
        max_gap_ms = float(np.max(np.diff(valid_times))) / 1_000_000.0
    if max_gap_ms > max_interpolation_gap_ms:
        reasons.append(MissingReason.INSUFFICIENT_WINDOW)

    if reasons or len(valid_items) < 2:
        if len(valid_items) < 2:
            reasons.append(MissingReason.INSUFFICIENT_WINDOW)
        return PreparedObservationWindow(
            ready=False,
            values=None,
            sample_rate_hz=0.0,
            valid_fraction=valid_fraction,
            temporal_coverage=temporal_coverage,
            artifact_fraction=artifact_fraction,
            quality=quality,
            valid_duration_ms=(
                int((valid_times[-1] - valid_times[0]) // 1_000_000)
                if len(valid_times) >= 2 else 0
            ),
            sample_count=valid_count,
            expected_sample_count=expected_count,
            max_interpolation_gap_ms=max_gap_ms,
            unavailable_reasons=_unique_reasons(reasons),
            mean_head_jitter_deg=_mean_head_jitter(valid_items),
        )

    positive_diffs_s = np.diff(times).astype(np.float64) / 1_000_000_000.0
    measured_fps = 1.0 / float(np.median(positive_diffs_s))
    sample_rate_hz = (
        measured_fps
        if fps_clamp_min <= measured_fps <= fps_clamp_max
        else nominal_fps
    )
    grid_count = max(
        2,
        int(round(
            (int(valid_times[-1]) - int(valid_times[0]))
            / 1_000_000_000.0
            * sample_rate_hz
        )) + 1,
    )
    grid = np.linspace(
        float(valid_times[0]), float(valid_times[-1]), grid_count, dtype=np.float64
    )
    matrix = np.stack([np.asarray(item.value, dtype=np.float64) for item in valid_items])
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    resampled = np.empty((grid_count, matrix.shape[1]), dtype=np.float64)
    valid_times_f = valid_times.astype(np.float64)
    for channel in range(matrix.shape[1]):
        resampled[:, channel] = np.interp(
            grid, valid_times_f, matrix[:, channel]
        )

    return PreparedObservationWindow(
        ready=True,
        values=resampled,
        sample_rate_hz=float(sample_rate_hz),
        valid_fraction=valid_fraction,
        temporal_coverage=temporal_coverage,
        artifact_fraction=artifact_fraction,
        quality=quality,
        valid_duration_ms=int(
            (int(valid_times[-1]) - int(valid_times[0])) // 1_000_000
        ),
        sample_count=valid_count,
        expected_sample_count=expected_count,
        max_interpolation_gap_ms=max_gap_ms,
        unavailable_reasons=(),
        mean_head_jitter_deg=_mean_head_jitter(valid_items),
    )


def _missing_reasons(observations: list[NumericObservation]) -> list[MissingReason]:
    reasons: list[MissingReason] = []
    for item in observations:
        if item.missing_reason is None:
            continue
        try:
            reasons.append(MissingReason(item.missing_reason))
        except ValueError:
            reasons.append(MissingReason.UNKNOWN)
    return list(_unique_reasons(reasons))


def _unique_reasons(reasons: list[MissingReason]) -> tuple[MissingReason, ...]:
    return tuple(dict.fromkeys(reasons))


def _mean_head_jitter(observations: list[NumericObservation]) -> float:
    if not observations:
        return 0.0
    return float(np.mean([item.head_jitter_deg for item in observations]))


def _unavailable_window(
    *,
    expected_count: int,
    reasons: tuple[MissingReason, ...],
) -> PreparedObservationWindow:
    return PreparedObservationWindow(
        ready=False,
        values=None,
        sample_rate_hz=0.0,
        valid_fraction=0.0,
        temporal_coverage=0.0,
        artifact_fraction=1.0,
        quality=0.0,
        valid_duration_ms=0,
        sample_count=0,
        expected_sample_count=expected_count,
        max_interpolation_gap_ms=float("inf"),
        unavailable_reasons=reasons,
        mean_head_jitter_deg=0.0,
    )
