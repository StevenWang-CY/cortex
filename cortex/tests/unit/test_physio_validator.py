"""P1-5: PhysioFeatures cross-field validator tests.

When ``valid=False``, all data fields (pulse_bpm, HRV metrics,
respiration_rate_bpm, hr_delta_5s) must be None. Signal-quality fields
(pulse_quality, physio_sqi, physio_sqi_components) are exempt.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cortex.libs.schemas.features import PhysioFeatures


class TestPhysioFeaturesValidInvalidContract:
    """valid=True: data fields may be set freely."""

    def test_valid_true_with_all_data_fields(self) -> None:
        pf = PhysioFeatures(
            pulse_bpm=72.0,
            pulse_quality=0.8,
            pulse_variability_proxy=45.0,
            hrv_sdnn=50.0,
            hrv_pnn50=0.25,
            hrv_sd1=30.0,
            hrv_sd2=60.0,
            hrv_lf_hf_ratio=1.5,
            hrv_sample_entropy=1.2,
            hr_delta_5s=2.0,
            respiration_rate_bpm=15.0,
            valid=True,
        )
        assert pf.valid is True
        assert pf.pulse_bpm == 72.0

    def test_valid_true_minimal(self) -> None:
        pf = PhysioFeatures(pulse_quality=0.0, valid=True)
        assert pf.valid is True

    """valid=False: all data fields must be None."""

    def test_valid_false_all_none_ok(self) -> None:
        pf = PhysioFeatures(pulse_quality=0.0, valid=False)
        assert pf.valid is False
        assert pf.pulse_bpm is None

    def test_valid_false_pulse_bpm_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(
                pulse_quality=0.5,
                pulse_bpm=72.0,
                valid=False,
            )
        assert "pulse_bpm" in str(exc_info.value)

    def test_valid_false_hrv_sdnn_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(pulse_quality=0.0, hrv_sdnn=50.0, valid=False)
        assert "hrv_sdnn" in str(exc_info.value)

    def test_valid_false_hrv_pnn50_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(pulse_quality=0.0, hrv_pnn50=0.3, valid=False)
        assert "hrv_pnn50" in str(exc_info.value)

    def test_valid_false_respiration_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(pulse_quality=0.0, respiration_rate_bpm=15.0, valid=False)
        assert "respiration_rate_bpm" in str(exc_info.value)

    def test_valid_false_hr_delta_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(pulse_quality=0.0, hr_delta_5s=2.0, valid=False)
        assert "hr_delta_5s" in str(exc_info.value)

    def test_valid_false_hrv_sd1_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(pulse_quality=0.0, hrv_sd1=30.0, valid=False)
        assert "hrv_sd1" in str(exc_info.value)

    def test_valid_false_hrv_sd2_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(pulse_quality=0.0, hrv_sd2=60.0, valid=False)
        assert "hrv_sd2" in str(exc_info.value)

    def test_valid_false_lf_hf_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(pulse_quality=0.0, hrv_lf_hf_ratio=1.5, valid=False)
        assert "hrv_lf_hf_ratio" in str(exc_info.value)

    def test_valid_false_sample_entropy_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(
                pulse_quality=0.0, hrv_sample_entropy=1.2, valid=False
            )
        assert "hrv_sample_entropy" in str(exc_info.value)

    def test_valid_false_pulse_variability_proxy_set_raises(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(
                pulse_quality=0.0, pulse_variability_proxy=45.0, valid=False
            )
        assert "pulse_variability_proxy" in str(exc_info.value)

    def test_valid_false_signal_quality_fields_exempt(self) -> None:
        """pulse_quality, physio_sqi, physio_sqi_components are SQI fields —
        they must NOT be rejected even when valid=False."""
        pf = PhysioFeatures(
            pulse_quality=0.9,
            physio_sqi=0.7,
            physio_sqi_components={"nsqi": 0.8, "snr": 0.6},
            valid=False,
        )
        assert pf.pulse_quality == 0.9
        assert pf.physio_sqi == 0.7

    def test_error_message_lists_all_bad_fields(self) -> None:
        """ValidationError message enumerates every offending field."""
        with pytest.raises(ValidationError) as exc_info:
            PhysioFeatures(
                pulse_quality=0.0,
                pulse_bpm=72.0,
                hrv_sdnn=50.0,
                valid=False,
            )
        msg = str(exc_info.value)
        assert "pulse_bpm" in msg
        assert "hrv_sdnn" in msg
