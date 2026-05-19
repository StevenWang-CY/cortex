"""Dashboard biometrics status banner — three-state UX.

When ``heart_rate`` is ``None`` post-first-STATE_UPDATE, the BPM/HRV/BLK
numerics row is swapped for a contextual status line that tells the
user whether the camera is offline, whether their face is in frame, or
whether the rPPG sliding window is still filling.

The driving signal is ``payload["capture"]`` which the daemon stamps in
``WebSocketServer._make_state_update`` from the latest ``FrameMeta``.
Run with:
    ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_dashboard_bio_status.py``
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

for _name in list(sys.modules):
    if _name == "PySide6" or _name.startswith("PySide6."):
        mod = sys.modules[_name]
        if not hasattr(mod, "__file__") or "site-packages" not in str(
            getattr(mod, "__file__", "") or ""
        ):
            del sys.modules[_name]

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 not available", allow_module_level=True)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def consumer(qapp, monkeypatch):
    from cortex.apps.desktop_shell import dashboard as dashboard_mod
    from cortex.apps.desktop_shell import mac_native

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )

    w = dashboard_mod.DashboardWindow()
    try:
        yield w._consumer  # type: ignore[attr-defined]
    finally:
        try:
            w.deleteLater()
        except RuntimeError:
            pass


def _payload(*, hr, frames_flowing, face_detected):
    bio: dict = {}
    if hr is not None:
        bio["heart_rate"] = hr
    return {
        "state": "FLOW",
        "biometrics": bio,
        "capture": {
            "frames_flowing": frames_flowing,
            "face_detected": face_detected,
        },
    }


def test_status_camera_offline_when_no_frames(consumer):
    consumer.update_state(
        _payload(hr=None, frames_flowing=False, face_detected=False)
    )
    assert consumer._bio_status_label.isVisibleTo(consumer)
    assert not consumer._bio_numerics.isVisibleTo(consumer)
    text = consumer._bio_status_label.text().lower()
    assert "camera" in text and ("offline" in text or "off" in text)


def test_status_no_face_when_frames_but_no_face(consumer):
    consumer.update_state(
        _payload(hr=None, frames_flowing=True, face_detected=False)
    )
    assert consumer._bio_status_label.isVisibleTo(consumer)
    assert not consumer._bio_numerics.isVisibleTo(consumer)
    text = consumer._bio_status_label.text().lower()
    assert "face" in text


def test_status_warmup_when_frames_face_no_hr(consumer):
    consumer.update_state(
        _payload(hr=None, frames_flowing=True, face_detected=True)
    )
    assert consumer._bio_status_label.isVisibleTo(consumer)
    assert not consumer._bio_numerics.isVisibleTo(consumer)
    text = consumer._bio_status_label.text().lower()
    assert "pulse" in text or "reading" in text


def test_status_hidden_once_hr_arrives(consumer):
    # Start in warmup.
    consumer.update_state(
        _payload(hr=None, frames_flowing=True, face_detected=True)
    )
    assert consumer._bio_status_label.isVisibleTo(consumer)
    # First HR lands.
    consumer.update_state(
        _payload(hr=72.0, frames_flowing=True, face_detected=True)
    )
    assert not consumer._bio_status_label.isVisibleTo(consumer)
    assert consumer._bio_numerics.isVisibleTo(consumer)
    assert consumer._bpm_label.text() == "72"


def test_status_returns_if_hr_drops_back_to_none(consumer):
    # HR present, then it drops out (face lost mid-session, e.g.).
    consumer.update_state(
        _payload(hr=72.0, frames_flowing=True, face_detected=True)
    )
    consumer.update_state(
        _payload(hr=None, frames_flowing=True, face_detected=False)
    )
    assert consumer._bio_status_label.isVisibleTo(consumer)
    assert not consumer._bio_numerics.isVisibleTo(consumer)
    assert "face" in consumer._bio_status_label.text().lower()


def test_status_defaults_to_warmup_when_capture_block_missing(consumer):
    # Older daemon that doesn't yet stamp ``capture`` falls through to
    # the most benign of the three states ("Reading your pulse…")
    # instead of accidentally showing "Camera offline".
    consumer.update_state({"state": "FLOW", "biometrics": {}})
    assert consumer._bio_status_label.isVisibleTo(consumer)
    text = consumer._bio_status_label.text().lower()
    assert "pulse" in text or "reading" in text
