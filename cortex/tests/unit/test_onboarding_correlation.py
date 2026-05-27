"""P1-20 — Onboarding correlation id.

Each wizard session mints a single cid on __init__; every USER_ACTION
emitted during that session carries the same cid.

Run with: ``QT_QPA_PLATFORM=offscreen pytest cortex/tests/unit/test_onboarding_correlation.py``
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

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
def wizard(qapp, monkeypatch, tmp_path):
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import onboarding as onb_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )
    # Redirect QSettings + onboarding state to tmp_path so we don't
    # pollute the real user's config dir.
    monkeypatch.setattr(
        onb_mod,
        "onboarding_state_path",
        lambda: tmp_path / "onboarding_state.json",
    )
    # Suppress permission polling in tests.
    monkeypatch.setattr(
        onb_mod,
        "check_camera_permission",
        lambda: False,
    )
    monkeypatch.setattr(
        onb_mod,
        "check_accessibility_permission",
        lambda: False,
    )

    w = onb_mod.OnboardingWindow()
    yield w
    try:
        w.deleteLater()
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Test 1: cid is minted at wizard construction
# ---------------------------------------------------------------------------

def test_onboarding_cid_minted_at_init(wizard):
    cid = wizard._onboarding_cid
    assert cid, "cid must not be empty"
    assert cid.startswith("cid_"), f"Expected cid_ prefix, got {cid!r}"


# ---------------------------------------------------------------------------
# Test 2: two wizard instances get different cids
# ---------------------------------------------------------------------------

def test_each_wizard_entry_gets_distinct_cid(qapp, monkeypatch, tmp_path):
    from cortex.apps.desktop_shell import mac_native
    from cortex.apps.desktop_shell import onboarding as onb_mod

    monkeypatch.setattr(mac_native, "apply_vibrancy", lambda *a, **kw: False)
    monkeypatch.setattr(
        mac_native, "apply_unified_titlebar", lambda *a, **kw: False
    )
    monkeypatch.setattr(
        onb_mod, "onboarding_state_path",
        lambda: tmp_path / "onboarding_state.json",
    )
    monkeypatch.setattr(onb_mod, "check_camera_permission", lambda: False)
    monkeypatch.setattr(onb_mod, "check_accessibility_permission", lambda: False)

    w1 = onb_mod.OnboardingWindow()
    w2 = onb_mod.OnboardingWindow()
    try:
        assert w1._onboarding_cid != w2._onboarding_cid, (
            "Each wizard entry must get a fresh cid"
        )
    finally:
        w1.deleteLater()
        w2.deleteLater()


# ---------------------------------------------------------------------------
# Test 3: USER_ACTION log lines carry the wizard cid
# ---------------------------------------------------------------------------

class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_user_action_log_lines_share_cid(wizard, tmp_path):
    """mark_step_complete for each step emits a USER_ACTION log record
    that contains the wizard's cid."""
    from cortex.apps.desktop_shell import onboarding as onb_mod

    handler = _CapturingHandler()
    log = logging.getLogger("cortex.apps.desktop_shell.onboarding")
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)

    expected_cid = wizard._onboarding_cid

    # Simulate the user completing each key onboarding step.
    for step in ("camera", "accessibility", "llm_backend", "extensions"):
        wizard.mark_step_complete(step)

    log.removeHandler(handler)

    # Filter to USER_ACTION records.
    user_action_records = [
        r for r in handler.records
        if "USER_ACTION" in r.getMessage()
    ]

    assert len(user_action_records) >= 4, (
        f"Expected ≥4 USER_ACTION records, got {len(user_action_records)}: "
        f"{[r.getMessage() for r in user_action_records]}"
    )

    # Every record must carry the same cid.
    for rec in user_action_records:
        msg = rec.getMessage()
        assert expected_cid in msg, (
            f"USER_ACTION record missing cid {expected_cid!r}: {msg!r}"
        )
