"""Nothing is published about a pulse that is no longer there.

Found in review: with the pulse removed from a replay while the face stayed
in view, heart rate stopped at once but SDNN went on being published for
150 s. The trailing window was anchored at the ledger's last beat instead of
at the present, a still-published RMSSD marked the channel live, and the
value's quality never aged.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from cortex.libs.schemas.physiology import PhysiologyMetric
from cortex.services.eval.physio_qualification import replay_trace
from cortex.services.eval.physio_synthetic import SyntheticSubject, synthesize
from cortex.services.physio_engine.camera_physiology import PhysiologyUpdate
from cortex.services.physio_engine.v2.hrv import MAX_INTERVAL_STALENESS_SECONDS

PULSE_ENDS_AT_S = 200.0


@pytest.fixture(scope="module")
def rows() -> list[tuple[float, PhysiologyUpdate]]:
    subject = SyntheticSubject(duration_seconds=300.0, seed=3, rsa_amplitude_ms=12.0)
    with_pulse = synthesize(subject)
    without_pulse = synthesize(dataclasses.replace(subject, modulation_depth=0.0))
    # Same face, same light, same head movement: only the pulse disappears.
    rgb = np.where(
        (with_pulse.sample_times_s < PULSE_ENDS_AT_S)[:, None],
        with_pulse.rgb,
        without_pulse.rgb,
    )
    collected: list[tuple[float, PhysiologyUpdate]] = []
    replay_trace(
        with_pulse.sample_times_s,
        rgb,
        with_pulse.head_vertical_face_units,
        on_update=lambda at, update: collected.append((at, update)),
    )
    return collected


def test_variability_was_being_published_before_the_pulse_ended(
    rows: list[tuple[float, PhysiologyUpdate]],
) -> None:
    before = [u for at, u in rows if 150.0 < at < PULSE_ENDS_AT_S]
    assert before
    published = [u for u in before if u.features.hrv_sdnn is not None]
    assert len(published) > 0.8 * len(before), "fixture must publish SDNN while the pulse is there"


def test_no_variability_is_published_once_the_beats_are_stale(
    rows: list[tuple[float, PhysiologyUpdate]],
) -> None:
    # The pulse window still holds real beats for up to 20 s after the cut;
    # allow that and the staleness bound, then nothing may remain.
    settle = PULSE_ENDS_AT_S + 20.0 + MAX_INTERVAL_STALENESS_SECONDS + 5.0
    late = [u for at, u in rows if at > settle]
    assert len(late) > 30
    for update in late:
        assert update.features.pulse_bpm is None
        assert update.features.hrv_sdnn is None
        assert update.features.pulse_variability_proxy is None
        assert update.features.hrv_sd2 is None
        assert update.pulse is not None
        for metric in (PhysiologyMetric.SDNN, PhysiologyMetric.RMSSD):
            estimate = update.pulse.hrv_estimates[metric]
            assert estimate.value is None
        # With nothing the rules may read, the channel is not live.
        assert update.features.valid is False or update.features.respiration_rate_bpm is not None


def test_a_published_variability_window_ends_near_the_present(
    rows: list[tuple[float, PhysiologyUpdate]],
) -> None:
    """While it is published, SDNN describes a window that trails now."""

    checked = 0
    for _at, update in rows:
        if update.pulse is None:
            continue
        estimate = update.pulse.hrv_estimates[PhysiologyMetric.SDNN]
        if estimate.value is None:
            continue
        lag_s = (update.pulse.summary.window_end_mono_ns - estimate.window_end_mono_ns) / 1e9
        assert -0.001 <= lag_s <= MAX_INTERVAL_STALENESS_SECONDS + 0.001, lag_s
        checked += 1
    assert checked > 50


def test_an_rmssd_alone_does_not_mark_the_channel_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RMSSD did not qualify; a stride on which it is the only published
    measurement is not a live channel, and no variability field is filled."""

    from cortex.services.physio_engine import camera_physiology

    def _only_rmssd(estimate: object) -> float | None:
        metric = getattr(estimate, "metric", None)
        return 42.0 if metric == PhysiologyMetric.RMSSD.value else None

    monkeypatch.setattr(camera_physiology, "_published", _only_rmssd)

    recording = synthesize(SyntheticSubject(duration_seconds=40.0, seed=5))
    collected: list[PhysiologyUpdate] = []
    replay_trace(
        recording.sample_times_s,
        recording.rgb,
        recording.head_vertical_face_units,
        on_update=lambda _at, update: collected.append(update),
    )
    with_pulse_window = [u for u in collected if u.pulse is not None]
    assert with_pulse_window, "the processor never assembled a pulse window"
    for update in with_pulse_window:
        assert update.features.valid is False
        assert update.features.hrv_sdnn is None
        assert update.features.hrv_sd2 is None
        assert update.features.hrv_lf_hf_ratio is None
