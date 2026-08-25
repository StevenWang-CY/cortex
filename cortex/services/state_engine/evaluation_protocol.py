"""Leakage-resistant evaluation scaffolding for future support models.

This module does not train or validate a model. It encodes the split and label
invariants that a preregistered study must satisfy before any learned output is
eligible for the production registry.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256


class SupportOutcome(StrEnum):
    """Decision target, intentionally distinct from a cognitive-state label."""

    SUPPORT_HELPFUL = "support_helpful"
    SUPPORT_NOT_HELPFUL = "support_not_helpful"
    UNCERTAIN = "uncertain"


class StudyExclusion(StrEnum):
    WITHDRAWN_CONSENT = "withdrawn_consent"
    CORRUPT_TELEMETRY = "corrupt_telemetry"
    PROTOCOL_DEVIATION = "protocol_deviation"
    DUPLICATE_EPISODE = "duplicate_episode"


@dataclass(frozen=True, slots=True)
class LabeledEpisode:
    participant_id: str
    episode_id: str
    outcome: SupportOutcome
    label_source: str
    exclusion: StudyExclusion | None = None

    def __post_init__(self) -> None:
        if not self.participant_id.strip() or not self.episode_id.strip():
            raise ValueError("participant_id and episode_id must be non-empty")
        if not self.label_source.strip():
            raise ValueError("label_source must be non-empty")

    @property
    def evaluation_eligible(self) -> bool:
        return self.exclusion is None and self.outcome != SupportOutcome.UNCERTAIN


@dataclass(frozen=True, slots=True)
class ParticipantFold:
    index: int
    train_participant_ids: tuple[str, ...]
    test_participant_ids: tuple[str, ...]
    train_episode_ids: tuple[str, ...]
    test_episode_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if set(self.train_participant_ids) & set(self.test_participant_ids):
            raise ValueError("participant leakage between train and test")
        if set(self.train_episode_ids) & set(self.test_episode_ids):
            raise ValueError("episode leakage between train and test")


@dataclass(frozen=True, slots=True)
class CalibrationSplit:
    development_participant_ids: tuple[str, ...]
    calibration_participant_ids: tuple[str, ...]
    development_episode_ids: tuple[str, ...]
    calibration_episode_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if set(self.development_participant_ids) & set(
            self.calibration_participant_ids
        ):
            raise ValueError("participant leakage into probability calibration")


def _eligible_unique(episodes: Iterable[LabeledEpisode]) -> list[LabeledEpisode]:
    result: list[LabeledEpisode] = []
    seen_episode_ids: set[str] = set()
    for episode in episodes:
        if not episode.evaluation_eligible:
            continue
        if episode.episode_id in seen_episode_ids:
            raise ValueError(f"duplicate eligible episode_id: {episode.episode_id}")
        seen_episode_ids.add(episode.episode_id)
        result.append(episode)
    if not result:
        raise ValueError("no evaluation-eligible episodes")
    return result


def build_participant_held_out_folds(
    episodes: Iterable[LabeledEpisode],
    *,
    n_splits: int,
) -> tuple[ParticipantFold, ...]:
    """Create deterministic folds balanced by participant episode count.

    Every eligible participant appears in exactly one test fold. All episodes
    for that participant follow them, preventing within-person leakage.
    """

    eligible = _eligible_unique(episodes)
    by_participant: dict[str, list[LabeledEpisode]] = defaultdict(list)
    for episode in eligible:
        by_participant[episode.participant_id].append(episode)
    if n_splits < 2 or n_splits > len(by_participant):
        raise ValueError("n_splits must be between 2 and the participant count")

    buckets: list[list[str]] = [[] for _ in range(n_splits)]
    bucket_sizes = [0] * n_splits
    ordered_groups = sorted(
        by_participant,
        key=lambda participant: (-len(by_participant[participant]), participant),
    )
    for participant_id in ordered_groups:
        target = min(range(n_splits), key=lambda index: (bucket_sizes[index], index))
        buckets[target].append(participant_id)
        bucket_sizes[target] += len(by_participant[participant_id])

    all_participants = set(by_participant)
    folds: list[ParticipantFold] = []
    for index, bucket in enumerate(buckets):
        test_participants = set(bucket)
        train_participants = all_participants - test_participants
        folds.append(ParticipantFold(
            index=index,
            train_participant_ids=tuple(sorted(train_participants)),
            test_participant_ids=tuple(sorted(test_participants)),
            train_episode_ids=tuple(sorted(
                episode.episode_id
                for participant in train_participants
                for episode in by_participant[participant]
            )),
            test_episode_ids=tuple(sorted(
                episode.episode_id
                for participant in test_participants
                for episode in by_participant[participant]
            )),
        ))

    observed_test_participants = [
        participant
        for fold in folds
        for participant in fold.test_participant_ids
    ]
    if len(observed_test_participants) != len(set(observed_test_participants)):
        raise RuntimeError("a participant appeared in more than one test fold")
    if set(observed_test_participants) != all_participants:
        raise RuntimeError("not every participant received a held-out fold")
    return tuple(folds)


def split_development_and_calibration(
    episodes: Iterable[LabeledEpisode],
    *,
    calibration_fraction: float = 0.2,
    seed: str = "cortex-calibration-v1",
) -> CalibrationSplit:
    """Reserve whole participants for post-fit probability calibration."""

    if not 0.0 < calibration_fraction < 1.0:
        raise ValueError("calibration_fraction must be between zero and one")
    eligible = _eligible_unique(episodes)
    by_participant: dict[str, list[LabeledEpisode]] = defaultdict(list)
    for episode in eligible:
        by_participant[episode.participant_id].append(episode)
    if len(by_participant) < 2:
        raise ValueError("calibration splitting requires at least two participants")

    ordered = sorted(
        by_participant,
        key=lambda participant: sha256(
            f"{seed}:{participant}".encode()
        ).hexdigest(),
    )
    calibration_count = min(
        len(ordered) - 1,
        max(1, round(len(ordered) * calibration_fraction)),
    )
    calibration_participants = set(ordered[:calibration_count])
    development_participants = set(ordered[calibration_count:])
    return CalibrationSplit(
        development_participant_ids=tuple(sorted(development_participants)),
        calibration_participant_ids=tuple(sorted(calibration_participants)),
        development_episode_ids=tuple(sorted(
            episode.episode_id
            for participant in development_participants
            for episode in by_participant[participant]
        )),
        calibration_episode_ids=tuple(sorted(
            episode.episode_id
            for participant in calibration_participants
            for episode in by_participant[participant]
        )),
    )

