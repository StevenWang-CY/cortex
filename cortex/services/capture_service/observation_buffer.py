"""Bounded, time-indexed storage and readiness gates for observations."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar
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
    """One scalar/vector sample plus its observation integrity metadata.

    ``motion_face_widths_per_second`` is the nose-tip translation speed of
    this observation normalised by face width (resolution and frame-rate
    independent; computed by :class:`FaceTracker` and exported through the
    capture quality scorer).  It is the motion evidence consumed by the pulse
    pipeline.  ``head_jitter_deg`` is the deprecated legacy motion proxy
    (pixel displacement scaled by ``45 / frame_width``); it is retained only
    so callers that have not migrated keep constructing observations and is
    not used by any gate once ``motion_face_widths_per_second`` is present.
    """

    observed_at_unix_ms: int
    observed_at_mono_ns: int
    boot_id: UUID
    sequence: int
    value: NDArray[np.float64] | None
    validity: str
    missing_reason: MissingReason | str | None
    quality: float
    head_jitter_deg: float = 0.0
    head_vertical_face_units: float | None = None
    motion_face_widths_per_second: float | None = None

    @property
    def is_valid(self) -> bool:
        return (
            self.validity == ObservationValidity.VALID.value
            and self.value is not None
            and bool(np.isfinite(self.value).all())
        )


# Closed catalog of structured readiness reason codes.  ``MissingReason``
# values (lower-cased) are also valid codes: they describe *why* scheduled
# observations were invalid, while the codes below describe which gate the
# window as a whole failed.
READINESS_CODE_NO_OBSERVATIONS = "no_observations"
READINESS_CODE_FILLING = "filling"
READINESS_CODE_VALID_FRACTION = "valid_fraction_below_gate"
READINESS_CODE_GAP_TOO_LONG = "gap_too_long"
READINESS_CODE_MOTION_FRACTION = "motion_fraction_above_cap"
READINESS_CODE_DUPLICATE_TIMESTAMPS = "duplicate_timestamps"
READINESS_CODE_TOO_FEW_VALID = "too_few_valid_samples"


@dataclass(frozen=True)
class ReadinessReason:
    """One structured, human-readable reason a window is not publishable."""

    code: str
    message: str
    observed: float | None = None
    required: float | None = None
    missing_reason: MissingReason | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "observed": self.observed,
            "required": self.required,
            "missing_reason": (
                self.missing_reason.value if self.missing_reason is not None else None
            ),
        }


@dataclass(frozen=True)
class WindowReadinessDiagnostics:
    """Structured readiness evidence for status payloads and UI copy.

    Everything here is derived from the scheduled observations inside the
    requested window; nothing is inferred from the nominal frame rate.
    """

    ready: bool
    window_seconds: float
    observed_span_seconds: float
    scheduled_count: int
    valid_count: int
    valid_fraction: float
    temporal_coverage: float
    max_interpolation_gap_ms: float
    reasons: tuple[ReadinessReason, ...]
    rejection_counts: dict[str, int]

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serialisable projection for transport/status use."""

        gap = self.max_interpolation_gap_ms
        return {
            "ready": self.ready,
            "window_seconds": self.window_seconds,
            "observed_span_seconds": self.observed_span_seconds,
            "scheduled_count": self.scheduled_count,
            "valid_count": self.valid_count,
            "valid_fraction": self.valid_fraction,
            "temporal_coverage": self.temporal_coverage,
            "max_interpolation_gap_ms": gap if np.isfinite(gap) else None,
            "reasons": [reason.to_payload() for reason in self.reasons],
            "rejection_counts": dict(self.rejection_counts),
        }


@dataclass(frozen=True)
class PreparedObservationWindow:
    """Result of validating and uniformly resampling a numeric window.

    ``valid_fraction``, ``artifact_fraction`` and ``quality`` are ratios over
    the observations actually *scheduled* inside the window
    (``scheduled_sample_count``), never over the nominal frame-rate product
    (``expected_sample_count``, retained for diagnostics only).  A camera that
    steadily delivers fewer frames than configured therefore reaches
    readiness as long as the time gates (``temporal_coverage`` and
    ``max_interpolation_gap_ms``) hold.
    """

    ready: bool
    values: NDArray[np.float64] | None
    sample_times_mono_ns: NDArray[np.int64] | None
    head_vertical_face_units: NDArray[np.float64] | None
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
    diagnostics: WindowReadinessDiagnostics
    scheduled_sample_count: int = 0
    mean_motion_face_widths_per_second: float | None = None

    def readiness_payload(self) -> dict[str, Any]:
        """Convenience projection of :attr:`diagnostics` for status payloads."""

        return self.diagnostics.to_payload()


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
    numeric values.  Coverage ratios are computed over the scheduled
    observations inside the window; the nominal frame rate is used only for
    the ``expected_sample_count`` diagnostic and as the resampling fallback
    when the measured cadence is implausible.
    """

    if window_seconds <= 0 or nominal_fps <= 0:
        raise ValueError("window_seconds and nominal_fps must be positive")
    if not 0.0 <= min_valid_fraction <= 1.0:
        raise ValueError("min_valid_fraction must be within [0, 1]")
    if not 0.0 <= max_motion_fraction <= 1.0:
        raise ValueError("max_motion_fraction must be within [0, 1]")

    expected_count = max(2, int(round(window_seconds * nominal_fps)))
    reasons: list[MissingReason] = []
    structured: list[ReadinessReason] = []

    if not observations:
        return _unavailable_window(
            expected_count=expected_count,
            window_seconds=window_seconds,
            reasons=(MissingReason.INSUFFICIENT_WINDOW,),
            structured=(
                ReadinessReason(
                    code=READINESS_CODE_NO_OBSERVATIONS,
                    message=f"no camera observations yet (window {window_seconds:.0f} s)",
                    observed=0.0,
                    required=window_seconds,
                    missing_reason=MissingReason.INSUFFICIENT_WINDOW,
                ),
            ),
        )

    ordered = list(observations)
    latest_ns = ordered[-1].observed_at_mono_ns
    window_ns = int(window_seconds * 1_000_000_000)
    start_ns = latest_ns - window_ns
    selected = [item for item in ordered if item.observed_at_mono_ns >= start_ns]
    if not selected:
        return _unavailable_window(
            expected_count=expected_count,
            window_seconds=window_seconds,
            reasons=(MissingReason.INSUFFICIENT_WINDOW,),
            structured=(
                ReadinessReason(
                    code=READINESS_CODE_NO_OBSERVATIONS,
                    message=f"no camera observations inside the last {window_seconds:.0f} s",
                    observed=0.0,
                    required=window_seconds,
                    missing_reason=MissingReason.INSUFFICIENT_WINDOW,
                ),
            ),
        )

    times = np.asarray([item.observed_at_mono_ns for item in selected], dtype=np.int64)
    diffs = np.diff(times)
    if bool((diffs <= 0).any()):
        reasons.append(MissingReason.ARTIFACT)
        structured.append(
            ReadinessReason(
                code=READINESS_CODE_DUPLICATE_TIMESTAMPS,
                message="repeated capture timestamps inside the window",
                observed=float(int(np.sum(diffs <= 0))),
                required=0.0,
                missing_reason=MissingReason.ARTIFACT,
            )
        )

    span_ns = max(0, int(times[-1]) - int(times[0]))
    span_seconds = span_ns / 1_000_000_000.0
    temporal_coverage = min(1.0, span_ns / window_ns) if window_ns else 0.0

    scheduled_count = len(selected)
    valid_items = [item for item in selected if item.is_valid]
    valid_count = len(valid_items)
    invalid_items = [item for item in selected if not item.is_valid]
    valid_fraction = valid_count / scheduled_count
    artifact_count = len(invalid_items)
    artifact_fraction = artifact_count / scheduled_count
    motion_count = sum(
        item.missing_reason == MissingReason.MOTION for item in invalid_items
    )
    motion_fraction = motion_count / scheduled_count
    quality_mass = sum(item.quality for item in valid_items) / scheduled_count
    quality = float(np.clip(quality_mass * temporal_coverage, 0.0, 1.0))
    rejection_counts = _rejection_counts(invalid_items)

    if valid_count == 0:
        reasons.extend(
            _missing_reasons(selected) or [MissingReason.INSUFFICIENT_WINDOW]
        )
    if temporal_coverage < min_valid_fraction:
        reasons.append(MissingReason.INSUFFICIENT_WINDOW)
        structured.append(
            ReadinessReason(
                code=READINESS_CODE_FILLING,
                message=f"filling {span_seconds:.1f}/{window_seconds:.0f} s",
                observed=span_seconds,
                required=window_seconds * min_valid_fraction,
                missing_reason=MissingReason.INSUFFICIENT_WINDOW,
            )
        )
    if valid_fraction < min_valid_fraction:
        reasons.append(MissingReason.INSUFFICIENT_WINDOW)
        structured.append(
            ReadinessReason(
                code=READINESS_CODE_VALID_FRACTION,
                message=(
                    f"{valid_count}/{scheduled_count} scheduled frames usable "
                    f"({valid_fraction:.0%}); need {min_valid_fraction:.0%}"
                ),
                observed=valid_fraction,
                required=min_valid_fraction,
                missing_reason=MissingReason.INSUFFICIENT_WINDOW,
            )
        )
    if motion_fraction > max_motion_fraction:
        reasons.append(MissingReason.MOTION)
        structured.append(
            ReadinessReason(
                code=READINESS_CODE_MOTION_FRACTION,
                message=(
                    f"{motion_fraction:.0%} of scheduled frames rejected for motion; "
                    f"cap {max_motion_fraction:.0%}"
                ),
                observed=motion_fraction,
                required=max_motion_fraction,
                missing_reason=MissingReason.MOTION,
            )
        )

    valid_times = np.asarray(
        [item.observed_at_mono_ns for item in valid_items], dtype=np.int64
    )
    max_gap_ms = float("inf")
    if len(valid_times) >= 2:
        max_gap_ms = float(np.max(np.diff(valid_times))) / 1_000_000.0
    if max_gap_ms > max_interpolation_gap_ms:
        reasons.append(MissingReason.INSUFFICIENT_WINDOW)
        if len(valid_times) >= 2:
            structured.append(
                ReadinessReason(
                    code=READINESS_CODE_GAP_TOO_LONG,
                    message=(
                        f"longest gap between usable frames {max_gap_ms:.0f} ms "
                        f"exceeds {max_interpolation_gap_ms:.0f} ms"
                    ),
                    observed=max_gap_ms,
                    required=max_interpolation_gap_ms,
                    missing_reason=MissingReason.INSUFFICIENT_WINDOW,
                )
            )

    # Explain *why* scheduled frames were unusable, most frequent first, so a
    # status surface can say "face lost" or "low light" rather than "not ready".
    structured.extend(
        _rejection_reasons(rejection_counts, scheduled_count=scheduled_count)
    )

    if reasons or len(valid_items) < 2:
        if len(valid_items) < 2:
            reasons.append(MissingReason.INSUFFICIENT_WINDOW)
            structured.append(
                ReadinessReason(
                    code=READINESS_CODE_TOO_FEW_VALID,
                    message=f"{valid_count} usable frame(s); need at least 2",
                    observed=float(valid_count),
                    required=2.0,
                    missing_reason=MissingReason.INSUFFICIENT_WINDOW,
                )
            )
        diagnostics = WindowReadinessDiagnostics(
            ready=False,
            window_seconds=window_seconds,
            observed_span_seconds=span_seconds,
            scheduled_count=scheduled_count,
            valid_count=valid_count,
            valid_fraction=valid_fraction,
            temporal_coverage=temporal_coverage,
            max_interpolation_gap_ms=max_gap_ms,
            reasons=_unique_structured(structured),
            rejection_counts=rejection_counts,
        )
        return PreparedObservationWindow(
            ready=False,
            values=None,
            sample_times_mono_ns=None,
            head_vertical_face_units=None,
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
            diagnostics=diagnostics,
            scheduled_sample_count=scheduled_count,
            mean_motion_face_widths_per_second=_mean_motion(valid_items),
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
    # Interpolate in a window-relative clock to preserve nanosecond deltas.
    # Casting an absolute, long-running monotonic timestamp directly to
    # float64 progressively loses low bits.
    origin_ns = int(valid_times[0])
    valid_relative_ns = (valid_times - origin_ns).astype(np.float64)
    grid_relative_ns = np.linspace(
        0.0,
        float(int(valid_times[-1]) - origin_ns),
        grid_count,
        dtype=np.float64,
    )
    grid_mono_ns = origin_ns + np.rint(grid_relative_ns).astype(np.int64)
    matrix = np.stack([np.asarray(item.value, dtype=np.float64) for item in valid_items])
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    resampled = np.empty((grid_count, matrix.shape[1]), dtype=np.float64)
    for channel in range(matrix.shape[1]):
        resampled[:, channel] = np.interp(
            grid_relative_ns, valid_relative_ns, matrix[:, channel]
        )

    head_values: NDArray[np.float64] | None = None
    head_items = [
        item
        for item in valid_items
        if item.head_vertical_face_units is not None
        and np.isfinite(item.head_vertical_face_units)
    ]
    if len(head_items) >= 2:
        head_times = np.asarray(
            [item.observed_at_mono_ns for item in head_items], dtype=np.int64
        )
        head_gaps_ms = float(np.max(np.diff(head_times))) / 1_000_000.0
        endpoint_gap_ms = max(
            (int(head_times[0]) - int(valid_times[0])) / 1_000_000.0,
            (int(valid_times[-1]) - int(head_times[-1])) / 1_000_000.0,
        )
        if max(head_gaps_ms, endpoint_gap_ms) <= max_interpolation_gap_ms:
            raw_head_values: list[float] = []
            for item in head_items:
                value = item.head_vertical_face_units
                if value is not None:
                    raw_head_values.append(float(value))
            head_values_raw = np.asarray(raw_head_values, dtype=np.float64)
            head_values = np.interp(
                grid_relative_ns,
                (head_times - origin_ns).astype(np.float64),
                head_values_raw,
            )

    diagnostics = WindowReadinessDiagnostics(
        ready=True,
        window_seconds=window_seconds,
        observed_span_seconds=span_seconds,
        scheduled_count=scheduled_count,
        valid_count=valid_count,
        valid_fraction=valid_fraction,
        temporal_coverage=temporal_coverage,
        max_interpolation_gap_ms=max_gap_ms,
        reasons=(),
        rejection_counts=rejection_counts,
    )
    return PreparedObservationWindow(
        ready=True,
        values=resampled,
        sample_times_mono_ns=grid_mono_ns,
        head_vertical_face_units=head_values,
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
        diagnostics=diagnostics,
        scheduled_sample_count=scheduled_count,
        mean_motion_face_widths_per_second=_mean_motion(valid_items),
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


def _rejection_counts(invalid_items: list[NumericObservation]) -> dict[str, int]:
    """Count unusable scheduled observations by closed-catalog reason."""

    counter: Counter[str] = Counter()
    for item in invalid_items:
        reason = item.missing_reason
        if reason is None:
            counter[MissingReason.UNKNOWN.value] += 1
            continue
        try:
            counter[MissingReason(reason).value] += 1
        except ValueError:
            counter[MissingReason.UNKNOWN.value] += 1
    return dict(sorted(counter.items(), key=lambda pair: (-pair[1], pair[0])))


def _rejection_reasons(
    rejection_counts: dict[str, int],
    *,
    scheduled_count: int,
) -> list[ReadinessReason]:
    result: list[ReadinessReason] = []
    for reason_value, count in rejection_counts.items():
        if count <= 0 or scheduled_count <= 0:
            continue
        fraction = count / scheduled_count
        try:
            reason = MissingReason(reason_value)
        except ValueError:
            reason = MissingReason.UNKNOWN
        result.append(
            ReadinessReason(
                code=reason.value.lower(),
                message=(
                    f"{count}/{scheduled_count} scheduled frames unusable "
                    f"({fraction:.0%}): {_describe_missing_reason(reason)}"
                ),
                observed=fraction,
                required=None,
                missing_reason=reason,
            )
        )
    return result


def _describe_missing_reason(reason: MissingReason) -> str:
    descriptions = {
        MissingReason.NO_FACE: "face not detected",
        MissingReason.LOW_LIGHT: "too dark",
        MissingReason.SATURATED: "too bright",
        MissingReason.MOTION: "head motion",
        MissingReason.OCCLUDED: "face region occluded",
        MissingReason.CAMERA_WARMUP: "camera warming up",
        MissingReason.FRAME_DROPPED: "frame dropped or skipped",
        MissingReason.PERMISSION: "camera permission missing",
        MissingReason.SOURCE_DISCONNECTED: "camera disconnected",
        MissingReason.INSUFFICIENT_WINDOW: "insufficient window",
        MissingReason.ARTIFACT: "timing or detector artifact",
        MissingReason.UNKNOWN: "unknown",
    }
    return descriptions.get(reason, reason.value.lower())


def _unique_reasons(reasons: list[MissingReason]) -> tuple[MissingReason, ...]:
    return tuple(dict.fromkeys(reasons))


def _unique_structured(reasons: list[ReadinessReason]) -> tuple[ReadinessReason, ...]:
    seen: set[str] = set()
    unique: list[ReadinessReason] = []
    for reason in reasons:
        if reason.code in seen:
            continue
        seen.add(reason.code)
        unique.append(reason)
    return tuple(unique)


def _mean_head_jitter(observations: list[NumericObservation]) -> float:
    if not observations:
        return 0.0
    return float(np.mean([item.head_jitter_deg for item in observations]))


def _mean_motion(observations: list[NumericObservation]) -> float | None:
    """Mean nose-tip translation (face widths/second) over valid samples."""

    values = [
        float(item.motion_face_widths_per_second)
        for item in observations
        if item.motion_face_widths_per_second is not None
        and np.isfinite(item.motion_face_widths_per_second)
    ]
    if not values:
        return None
    return float(np.mean(values))


def _unavailable_window(
    *,
    expected_count: int,
    window_seconds: float,
    reasons: tuple[MissingReason, ...],
    structured: tuple[ReadinessReason, ...],
) -> PreparedObservationWindow:
    return PreparedObservationWindow(
        ready=False,
        values=None,
        sample_times_mono_ns=None,
        head_vertical_face_units=None,
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
        diagnostics=WindowReadinessDiagnostics(
            ready=False,
            window_seconds=window_seconds,
            observed_span_seconds=0.0,
            scheduled_count=0,
            valid_count=0,
            valid_fraction=0.0,
            temporal_coverage=0.0,
            max_interpolation_gap_ms=float("inf"),
            reasons=structured,
            rejection_counts={},
        ),
        scheduled_sample_count=0,
        mean_motion_face_widths_per_second=None,
    )
