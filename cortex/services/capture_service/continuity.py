"""Face-loss continuity policy for camera-derived temporal state.

A brief tracker dropout (a blink of the detector, one dark frame, a frame
skipped under load) is not a change of subject.  Every camera-derived
consumer already tolerates bounded gaps in physical time:

* the RGB window interpolates across gaps up to
  ``RPPGSignalConfig.max_interpolation_gap_ms`` (default 250 ms);
* the beat ledger is time-indexed and simply yields no beats inside a gap;
* blink exposure and head-pose freeze detection are gap-aware.

Discarding all of that state on the tracker's frame-count hysteresis
(``face_lost_tolerance_frames / fps`` = 167 ms at 30 fps) threw away up to a
46 s RGB history and a 600 s beat ledger for a loss shorter than the
interpolation gap.  :class:`FaceLossTracker` decides *once per loss* when the
loss has become long enough that bridging is impossible, and only then asks
the owner to reset camera-dependent state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FaceTransition = Literal["lost", "reacquired"]


def should_reset_camera_state(
    loss_duration_ms: float,
    *,
    reset_after_ms: float,
) -> bool:
    """Return ``True`` when a face loss can no longer be bridged.

    ``reset_after_ms`` should be the physio interpolation gap
    (``max_interpolation_gap_ms``): shorter losses are interpolated by the
    window preparation and leave every time-indexed consumer consistent.
    """

    if reset_after_ms < 0:
        raise ValueError("reset_after_ms must be non-negative")
    return float(loss_duration_ms) > float(reset_after_ms)


@dataclass(frozen=True)
class FaceLossDecision:
    """Outcome of one face presence observation."""

    face_present: bool
    transition: FaceTransition | None
    loss_duration_ms: float
    reset_after_ms: float
    should_reset: bool


class FaceLossTracker:
    """Track face presence in monotonic time and issue at most one reset per loss.

    ``observe`` must be called for every scheduled camera observation (present
    or not).  ``should_reset`` becomes ``True`` on the first observation at
    which the current loss exceeds ``reset_after_ms`` — whether the face is
    still absent or has just been reacquired — and never again for that loss.

    A loss is measured from the last observation that carried a face (the
    same endpoint-to-endpoint span the interpolation gap uses), or from the
    first faceless observation when no face has been seen yet.
    """

    def __init__(self, *, reset_after_ms: float, initially_present: bool = True) -> None:
        if reset_after_ms < 0:
            raise ValueError("reset_after_ms must be non-negative")
        self._reset_after_ms = float(reset_after_ms)
        self._initially_present = bool(initially_present)
        self._face_present = self._initially_present
        self._last_present_mono_ns: int | None = None
        self._lost_since_mono_ns: int | None = None
        self._reset_issued = False

    @property
    def reset_after_ms(self) -> float:
        return self._reset_after_ms

    @property
    def face_present(self) -> bool:
        return self._face_present

    def loss_duration_ms(self, now_mono_ns: int) -> float:
        """Duration of the loss in progress at ``now_mono_ns`` (0 when present)."""

        if self._lost_since_mono_ns is None:
            return 0.0
        reference = (
            self._last_present_mono_ns
            if self._last_present_mono_ns is not None
            else self._lost_since_mono_ns
        )
        return max(0.0, (int(now_mono_ns) - reference) / 1_000_000.0)

    def observe(self, *, face_present: bool, observed_at_mono_ns: int) -> FaceLossDecision:
        now = int(observed_at_mono_ns)
        if now < 0:
            raise ValueError("observed_at_mono_ns must be non-negative")
        previous = self._face_present
        transition: FaceTransition | None = None
        should_reset = False

        if face_present:
            loss_ms = self.loss_duration_ms(now)
            if not previous:
                transition = "reacquired"
                should_reset = not self._reset_issued and should_reset_camera_state(
                    loss_ms, reset_after_ms=self._reset_after_ms
                )
            self._face_present = True
            self._last_present_mono_ns = now
            self._lost_since_mono_ns = None
            self._reset_issued = False
            return FaceLossDecision(
                face_present=True,
                transition=transition,
                loss_duration_ms=loss_ms,
                reset_after_ms=self._reset_after_ms,
                should_reset=should_reset,
            )

        if previous or self._lost_since_mono_ns is None:
            transition = "lost" if previous else None
            self._lost_since_mono_ns = now
            self._reset_issued = False
        self._face_present = False
        loss_ms = self.loss_duration_ms(now)
        if not self._reset_issued and should_reset_camera_state(
            loss_ms, reset_after_ms=self._reset_after_ms
        ):
            should_reset = True
            self._reset_issued = True
        return FaceLossDecision(
            face_present=False,
            transition=transition,
            loss_duration_ms=loss_ms,
            reset_after_ms=self._reset_after_ms,
            should_reset=should_reset,
        )

    def reset(self) -> None:
        """Forget the current loss (for example after a camera change)."""

        self._face_present = self._initially_present
        self._last_present_mono_ns = None
        self._lost_since_mono_ns = None
        self._reset_issued = False
