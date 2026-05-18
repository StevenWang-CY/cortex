"""Audit F49 — onboarding completion marker survives back-and-forward.

Pre-fix, the only signal that onboarding had completed was the side
effect of clicking the final Get Started button (which dropped a
``.onboarding_complete`` sentinel in the storage path). Step-level
state was not persisted, so a user who re-opened the wizard to fix one
permission and clicked Get Started again would have no record of which
specific steps they actually finished, and a crash mid-wizard lost all
progress.

F49 introduces :class:`OnboardingState` — a per-step completion record
persisted to ``<config_dir>/onboarding_state.json`` via
``atomic_write_json``. Four cases below:

1. Mark every step complete → ``is_complete`` true, file present.
2. Mark all complete, then mark one back to incomplete, then mark it
   forward again → ``is_complete`` still true (the other three steps
   were preserved across the back navigation).
3. Mark only two steps → ``is_complete`` false; file present but
   partial.
4. Simulated crash mid-write (atomic_write_json's tmp file is
   intentionally orphaned) → next load returns a fresh state, the
   previous on-disk file (if any) survives.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cortex.apps.desktop_shell.onboarding import (
    ONBOARDING_STEPS,
    OnboardingState,
)


def test_marker_present_when_all_steps_completed(tmp_path: Path) -> None:
    target = tmp_path / "onboarding_state.json"
    state = OnboardingState()
    for step in ONBOARDING_STEPS:
        state.mark_complete(step, path=target)
    assert state.is_complete
    assert target.exists()
    # File round-trips through load() to the same set.
    reloaded = OnboardingState.load(path=target)
    assert reloaded.completed_steps == set(ONBOARDING_STEPS)
    assert reloaded.is_complete


def test_back_then_forward_preserves_marker(tmp_path: Path) -> None:
    """The user goes back to re-grant a permission and then forward
    again. The other three steps must remain complete; the marker is
    re-present after the forward step."""
    target = tmp_path / "onboarding_state.json"
    state = OnboardingState()
    for step in ONBOARDING_STEPS:
        state.mark_complete(step, path=target)
    assert state.is_complete

    # User navigates BACK on the camera step.
    state.mark_incomplete("camera", path=target)
    assert not state.is_complete
    reloaded = OnboardingState.load(path=target)
    # The other three steps survive the back navigation.
    assert reloaded.completed_steps == {
        s for s in ONBOARDING_STEPS if s != "camera"
    }

    # User navigates FORWARD again on the camera step.
    state.mark_complete("camera", path=target)
    assert state.is_complete
    reloaded = OnboardingState.load(path=target)
    assert reloaded.is_complete


def test_partial_completion_marker_absent(tmp_path: Path) -> None:
    """Only two of four steps complete → ``is_complete`` false."""
    target = tmp_path / "onboarding_state.json"
    state = OnboardingState()
    state.mark_complete("camera", path=target)
    state.mark_complete("accessibility", path=target)
    assert not state.is_complete
    reloaded = OnboardingState.load(path=target)
    assert reloaded.completed_steps == {"camera", "accessibility"}
    assert not reloaded.is_complete


def test_atomic_write_under_simulated_crash(tmp_path: Path) -> None:
    """When the os.replace step of atomic_write_json fails, the prior
    on-disk file must survive intact. Simulated via a patched
    ``os.replace`` that raises OSError on the second write."""
    target = tmp_path / "onboarding_state.json"

    # First write succeeds — sets a known-good baseline.
    state = OnboardingState()
    state.mark_complete("camera", path=target)
    state.mark_complete("accessibility", path=target)
    assert target.exists()
    baseline_text = target.read_text()

    # Second write — patched os.replace raises mid-flight, simulating a
    # power loss between the tmp-file write and the atomic rename.
    real_replace = __import__("os").replace
    state.mark_complete("llm_backend")  # mutate in-memory only

    def _failing_replace(src, dst):
        raise OSError(28, "simulated disk full")

    with patch("os.replace", _failing_replace):
        with pytest.raises(OSError):
            state.save(path=target)

    # Prior on-disk file is unchanged (atomic_write_json never crossed
    # the rename point).
    assert target.read_text() == baseline_text
    # Sanity: re-read and confirm the prior state survived.
    reloaded = OnboardingState.load(path=target)
    assert reloaded.completed_steps == {"camera", "accessibility"}

    # Real write works again now that we're no longer patching.
    _ = real_replace  # silence unused warning
    state.save(path=target)
    reloaded = OnboardingState.load(path=target)
    assert "llm_backend" in reloaded.completed_steps


def test_load_returns_fresh_state_when_file_missing(tmp_path: Path) -> None:
    target = tmp_path / "does_not_exist.json"
    state = OnboardingState.load(path=target)
    assert state.completed_steps == set()
    assert not state.is_complete


def test_unknown_step_id_raises(tmp_path: Path) -> None:
    state = OnboardingState()
    with pytest.raises(ValueError):
        state.mark_complete("nonexistent", path=tmp_path / "x.json")
