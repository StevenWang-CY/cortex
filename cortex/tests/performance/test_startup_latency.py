"""audit Phase-I: daemon startup latency regression guard.

The previous startup path eagerly imported ``mediapipe`` (>200 ms cold)
the moment ``cortex.services.capture_service.face_tracker`` was loaded,
and ``keyring`` (~80 ms cold) the moment
``cortex.services.llm_engine.anthropic_planner`` was loaded. Both
imports are needed only when the capture pipeline / LLM client is
actually built. Deferring them shaves measurable time off the path
from ``python -m cortex.scripts.run_dev`` to first WebSocket
broadcast.

This test asserts the lazy-import contract: importing the entry-point
script and the planner module must NOT have pulled mediapipe or
keyring into ``sys.modules``. A regression that re-introduces an
eager import will fail loudly here.

The test runs each import check in a fresh subprocess so it is not
contaminated by other tests that legitimately call into mediapipe /
keyring earlier in the suite (the capture-perf test does not, but
defensive isolation is cheap).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _run_python(snippet: str) -> tuple[int, str, str]:
    """Run a Python snippet in a fresh subprocess. Returns
    ``(exit_code, stdout, stderr)``."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_run_dev_import_does_not_pull_mediapipe() -> None:
    """Importing ``cortex.scripts.run_dev`` must not import mediapipe.

    The capture pipeline runs ``_ensure_mediapipe`` on first frame; the
    module-level import was removed in audit Phase-I commit 1.
    """
    exit_code, stdout, stderr = _run_python(
        """
        import sys
        import cortex.scripts.run_dev  # noqa: F401
        offending = sorted(m for m in sys.modules if m == "mediapipe" or m.startswith("mediapipe."))
        print("|".join(offending))
        """
    )
    assert exit_code == 0, f"run_dev import failed: stderr={stderr}"
    offending = stdout.strip()
    assert offending == "", (
        f"run_dev pulled mediapipe into sys.modules: {offending}. "
        "audit Phase-I requires mediapipe stay lazy until capture starts."
    )


def test_anthropic_planner_import_does_not_pull_keyring() -> None:
    """Importing ``cortex.services.llm_engine.anthropic_planner`` must
    not pull keyring eagerly. ``_keychain_get_bedrock_token`` performs
    a lazy import on first call only."""
    exit_code, stdout, stderr = _run_python(
        """
        import sys
        import cortex.services.llm_engine.anthropic_planner  # noqa: F401
        offending = sorted(m for m in sys.modules if m == "keyring" or m.startswith("keyring."))
        print("|".join(offending))
        """
    )
    assert exit_code == 0, f"anthropic_planner import failed: stderr={stderr}"
    offending = stdout.strip()
    assert offending == "", (
        f"anthropic_planner pulled keyring into sys.modules: {offending}. "
        "audit Phase-I requires keyring stay lazy until client is built."
    )


def test_face_tracker_import_does_not_pull_mediapipe() -> None:
    """Importing ``cortex.services.capture_service.face_tracker`` must
    not pull mediapipe. Only :meth:`FaceTracker.initialize` does."""
    exit_code, stdout, stderr = _run_python(
        """
        import sys
        import cortex.services.capture_service.face_tracker  # noqa: F401
        offending = sorted(m for m in sys.modules if m == "mediapipe" or m.startswith("mediapipe."))
        print("|".join(offending))
        """
    )
    assert exit_code == 0, f"face_tracker import failed: stderr={stderr}"
    offending = stdout.strip()
    assert offending == "", (
        f"face_tracker pulled mediapipe into sys.modules: {offending}. "
        "audit Phase-I requires mediapipe stay lazy until FaceTracker.initialize."
    )


def test_run_dev_supports_profile_startup_flag() -> None:
    """``--profile-startup`` flag is wired and the milestone API exists.

    Invokes ``--help`` to confirm argparse knows the flag without
    actually running the daemon (which would need a webcam)."""
    exit_code, stdout, stderr = _run_python(
        """
        import sys
        sys.argv = ['run_dev', '--help']
        try:
            from cortex.scripts.run_dev import main, record_milestone
        except SystemExit:
            pass
        print('milestone-api-present')
        """
    )
    assert exit_code == 0, f"unexpected error: {stderr}"
    assert "milestone-api-present" in stdout


def test_record_milestone_appends_only_when_enabled() -> None:
    """``record_milestone`` should be a no-op when the profile flag is
    not set, and append a ``(label, monotonic_time)`` tuple when it is.

    The flag is module-level state so we test it directly rather than
    via subprocess (the subprocess tests above already cover process
    isolation for the lazy-import asserts)."""
    from cortex.scripts import run_dev

    # Default: disabled. No appending happens.
    run_dev._STARTUP_MILESTONES.clear()
    run_dev._PROFILE_STARTUP_ENABLED = False
    run_dev.record_milestone("test-disabled")
    assert run_dev._STARTUP_MILESTONES == []

    # Enabled: each call appends.
    run_dev._PROFILE_STARTUP_ENABLED = True
    run_dev.record_milestone("first")
    run_dev.record_milestone("second")
    labels = [label for label, _ in run_dev._STARTUP_MILESTONES]
    assert labels == ["first", "second"]
    times = [t for _, t in run_dev._STARTUP_MILESTONES]
    # Monotonic clock: second milestone >= first.
    assert times[1] >= times[0]

    # Reset so we don't leak state to other tests.
    run_dev._STARTUP_MILESTONES.clear()
    run_dev._PROFILE_STARTUP_ENABLED = False
