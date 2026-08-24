"""Absolute-time beat reconciliation and IBI derivation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from cortex.libs.schemas.physiology import (
    BeatCandidate,
    BeatEvent,
    BeatRejectionReason,
    BeatStatus,
    InterBeatInterval,
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


@dataclass
class _BeatCluster:
    beat_id: str
    candidates: dict[str, BeatCandidate] = field(default_factory=dict)
    status: str = BeatStatus.PROVISIONAL.value
    rejection_reason: str | None = None

    @property
    def canonical(self) -> BeatCandidate:
        # Deterministic preference: quality, prominence, then earliest time/id.
        return max(
            self.candidates.values(),
            key=lambda item: (
                item.quality,
                item.prominence,
                -item.absolute_mono_ns,
                item.candidate_id,
            ),
        )

    @property
    def source_window_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.source_window_id for item in self.candidates.values()}))

    def refresh_status(self) -> None:
        if self.status == BeatStatus.REJECTED.value:
            return
        confirmed = any(not item.near_window_boundary for item in self.candidates.values())
        if confirmed:
            self.status = BeatStatus.ACCEPTED.value
            self.rejection_reason = None


class BeatLedger:
    """Reconcile peaks from overlapping windows into one causal beat stream.

    The ledger is idempotent by candidate identity, keeps boundary peaks
    provisional until a later overlap confirms them, resolves refractory
    conflicts by deterministic evidence rank, and derives intervals only
    between named accepted beats.
    """

    def __init__(
        self,
        *,
        min_hr_bpm: float = 30.0,
        max_hr_bpm: float = 210.0,
        min_candidate_quality: float = 0.20,
        duplicate_tolerance_ms: float = 120.0,
        history_seconds: float = 600.0,
    ) -> None:
        if not 0 < min_hr_bpm < max_hr_bpm:
            raise ValueError("beat HR bounds must be positive and ordered")
        self._min_hr_bpm = float(min_hr_bpm)
        self._max_hr_bpm = float(max_hr_bpm)
        self._min_candidate_quality = float(min_candidate_quality)
        self._duplicate_tolerance_ns = int(duplicate_tolerance_ms * 1_000_000)
        self._refractory_ns = int(60_000_000_000 / max_hr_bpm)
        self._history_ns = int(history_seconds * 1_000_000_000)
        self._clusters: list[_BeatCluster] = []
        self._standalone_rejections: dict[str, BeatEvent] = {}
        self._seen_candidates: dict[str, int] = {}

    def reset(self) -> None:
        self._clusters.clear()
        self._standalone_rejections.clear()
        self._seen_candidates.clear()

    def ingest(
        self,
        candidates: tuple[BeatCandidate, ...] | list[BeatCandidate],
        *,
        window_id: str,
        window_start_mono_ns: int,
        window_end_mono_ns: int,
        boundary_margin_ns: int,
    ) -> tuple[tuple[BeatEvent, ...], tuple[InterBeatInterval, ...]]:
        if window_end_mono_ns <= window_start_mono_ns:
            raise ValueError("beat window must be strictly ordered")
        if boundary_margin_ns < 0:
            raise ValueError("boundary margin must be non-negative")

        touched: set[str] = set()
        for candidate in sorted(
            candidates, key=lambda item: (item.absolute_mono_ns, item.candidate_id)
        ):
            if candidate.source_window_id != window_id:
                raise ValueError("candidate source_window_id does not match ingest window")
            if candidate.candidate_id in self._seen_candidates:
                continue
            self._seen_candidates[candidate.candidate_id] = candidate.absolute_mono_ns
            if not (
                window_start_mono_ns
                <= candidate.absolute_mono_ns
                <= window_end_mono_ns
            ):
                raise ValueError("candidate falls outside its source window")
            if candidate.quality < self._min_candidate_quality:
                self._reject_candidate(candidate, BeatRejectionReason.LOW_QUALITY)
                continue

            duplicate = self._nearest_cluster(
                candidate.absolute_mono_ns,
                tolerance_ns=self._duplicate_tolerance_ns,
            )
            if duplicate is not None:
                duplicate.candidates[candidate.candidate_id] = candidate
                duplicate.refresh_status()
                touched.add(duplicate.beat_id)
                continue

            conflict = self._nearest_cluster(
                candidate.absolute_mono_ns,
                tolerance_ns=self._refractory_ns,
            )
            if conflict is not None:
                if self._rank(candidate) > self._rank(conflict.canonical):
                    conflict.status = BeatStatus.REJECTED.value
                    conflict.rejection_reason = BeatRejectionReason.REFRACTORY_CONFLICT.value
                else:
                    self._reject_candidate(
                        candidate, BeatRejectionReason.REFRACTORY_CONFLICT
                    )
                    continue

            cluster = _BeatCluster(
                beat_id=_stable_id(
                    "beat", candidate.absolute_mono_ns, candidate.candidate_id
                ),
                candidates={candidate.candidate_id: candidate},
            )
            cluster.refresh_status()
            self._clusters.append(cluster)
            touched.add(cluster.beat_id)

        # A provisional boundary peak is rejected once a later window puts
        # that time safely in its interior but does not reproduce the peak.
        interior_start = window_start_mono_ns + boundary_margin_ns
        interior_end = window_end_mono_ns - boundary_margin_ns
        for cluster in self._clusters:
            if cluster.status != BeatStatus.PROVISIONAL.value:
                continue
            beat_ns = cluster.canonical.absolute_mono_ns
            if cluster.beat_id in touched:
                continue
            source_seen = window_id in cluster.source_window_ids
            safely_observed_without_peak = (
                interior_start <= beat_ns <= interior_end and not source_seen
            )
            no_future_overlap_possible = window_start_mono_ns > beat_ns + boundary_margin_ns
            if safely_observed_without_peak or no_future_overlap_possible:
                cluster.status = BeatStatus.REJECTED.value
                cluster.rejection_reason = BeatRejectionReason.WINDOW_BOUNDARY.value

        self._prune(window_end_mono_ns)
        events = self.events()
        return events, self._derive_intervals(events)

    def events(self) -> tuple[BeatEvent, ...]:
        cluster_events = [self._event_from_cluster(cluster) for cluster in self._clusters]
        events = cluster_events + list(self._standalone_rejections.values())
        return tuple(sorted(events, key=lambda item: (item.absolute_mono_ns, item.beat_id)))

    def intervals(self) -> tuple[InterBeatInterval, ...]:
        return self._derive_intervals(self.events())

    @staticmethod
    def _rank(candidate: BeatCandidate) -> tuple[float, float, int, str]:
        return (
            candidate.quality,
            candidate.prominence,
            -candidate.absolute_mono_ns,
            candidate.candidate_id,
        )

    def _nearest_cluster(
        self,
        absolute_mono_ns: int,
        *,
        tolerance_ns: int,
    ) -> _BeatCluster | None:
        eligible = [
            cluster
            for cluster in self._clusters
            if cluster.status != BeatStatus.REJECTED.value
            and abs(cluster.canonical.absolute_mono_ns - absolute_mono_ns) <= tolerance_ns
        ]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda item: (
                abs(item.canonical.absolute_mono_ns - absolute_mono_ns),
                item.beat_id,
            ),
        )

    def _reject_candidate(
        self,
        candidate: BeatCandidate,
        reason: BeatRejectionReason,
    ) -> None:
        beat_id = _stable_id("beat_rejected", candidate.candidate_id)
        self._standalone_rejections[beat_id] = BeatEvent(
            beat_id=beat_id,
            absolute_mono_ns=candidate.absolute_mono_ns,
            status=BeatStatus.REJECTED,
            rejection_reason=reason,
            quality=candidate.quality,
            prominence=candidate.prominence,
            source_window_ids=(candidate.source_window_id,),
        )

    @staticmethod
    def _event_from_cluster(cluster: _BeatCluster) -> BeatEvent:
        candidate = cluster.canonical
        return BeatEvent(
            beat_id=cluster.beat_id,
            absolute_mono_ns=candidate.absolute_mono_ns,
            status=cluster.status,
            rejection_reason=cluster.rejection_reason,
            quality=candidate.quality,
            prominence=candidate.prominence,
            source_window_ids=cluster.source_window_ids,
        )

    def _derive_intervals(
        self, events: tuple[BeatEvent, ...]
    ) -> tuple[InterBeatInterval, ...]:
        accepted = [item for item in events if item.status == BeatStatus.ACCEPTED.value]
        accepted.sort(key=lambda item: (item.absolute_mono_ns, item.beat_id))
        intervals: list[InterBeatInterval] = []
        accepted_durations: list[float] = []
        min_ms = 60_000.0 / self._max_hr_bpm
        max_ms = 60_000.0 / self._min_hr_bpm
        for left, right in zip(accepted, accepted[1:], strict=False):
            duration_ms = (right.absolute_mono_ns - left.absolute_mono_ns) / 1_000_000.0
            reason: BeatRejectionReason | None = None
            if duration_ms < min_ms:
                reason = BeatRejectionReason.IBI_TOO_SHORT
            elif duration_ms > max_ms:
                reason = BeatRejectionReason.IBI_TOO_LONG
            elif len(accepted_durations) >= 5:
                local_median = float(np.median(accepted_durations[-5:]))
                tolerance = max(250.0, 0.30 * local_median)
                if abs(duration_ms - local_median) > tolerance:
                    reason = BeatRejectionReason.IBI_LOCAL_OUTLIER
            if reason is None:
                accepted_durations.append(duration_ms)
            intervals.append(
                InterBeatInterval(
                    ibi_id=_stable_id("ibi", left.beat_id, right.beat_id),
                    left_beat_id=left.beat_id,
                    right_beat_id=right.beat_id,
                    start_mono_ns=left.absolute_mono_ns,
                    end_mono_ns=right.absolute_mono_ns,
                    duration_ms=duration_ms,
                    status=(BeatStatus.ACCEPTED if reason is None else BeatStatus.REJECTED),
                    rejection_reason=reason,
                    quality=min(left.quality, right.quality),
                )
            )
        return tuple(intervals)

    def _prune(self, latest_mono_ns: int) -> None:
        cutoff = latest_mono_ns - self._history_ns
        self._clusters = [
            cluster
            for cluster in self._clusters
            if cluster.canonical.absolute_mono_ns >= cutoff
        ]
        stale_rejections = [
            beat_id
            for beat_id, event in self._standalone_rejections.items()
            if event.absolute_mono_ns < cutoff
        ]
        for beat_id in stale_rejections:
            self._standalone_rejections.pop(beat_id)
        self._seen_candidates = {
            candidate_id: mono_ns
            for candidate_id, mono_ns in self._seen_candidates.items()
            if mono_ns >= cutoff
        }
