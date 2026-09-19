"""The switches Settings offers must actually do something.

Two roadmap items from ``IMPLEMENTATION.md`` §30.9, and one defect found
while implementing them.

* **Session recorder stream.** The recorder writes one ``state_estimate``
  per change of ``(state, status)``, which is what History needs. The
  offline replay harness needs every tick, so a rerun sees the input
  sequence the live run saw. The switch existed as a class attribute a
  developer had to edit in source; it is now
  ``CORTEX_DEBUG__RECORD_FULL_STATE_STREAM`` and a live settings key.

* **Debug logging checkboxes.** Settings renders four of them under
  "Verbose logging for support. Leave these off unless asked". They were
  persisted to QSettings and sent to the daemon, and ``apply_settings`` had
  no branch for any of them — so a support engineer could ask a user to tick
  one and receive exactly the same log as before.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

# Deliberately NOT imported at module scope: these symbols are new, and a
# module-level import makes this whole file uncollectable against the
# previous tree, which turns a demonstrated defect into an import error.
# The behavioural tests below name the logger namespace directly so they
# fail with a real assertion there instead.
_SUBSYSTEM_NAMESPACES = {
    "capture": "cortex.services.capture_service",
    "rppg": "cortex.services.physio_engine",
    "state": "cortex.services.state_engine",
    "llm": "cortex.services.llm_engine",
}


@pytest.fixture()
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    if hasattr(settings_mod.get_config, "cache_clear"):
        settings_mod.get_config.cache_clear()  # type: ignore[attr-defined]
    return storage


@pytest.fixture()
def daemon(isolated_storage: Path):  # type: ignore[no-untyped-def]
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    return CortexDaemon(config=get_config())


@pytest.fixture(autouse=True)
def _restore_subsystem_levels():
    """Log levels are process-global; put them back."""
    before = {
        namespace: logging.getLogger(namespace).level
        for namespace in _SUBSYSTEM_NAMESPACES.values()
    }
    yield
    for namespace, level in before.items():
        logging.getLogger(namespace).setLevel(level)


# ---------------------------------------------------------------------------
# Support logging switches
# ---------------------------------------------------------------------------


def test_every_settings_checkbox_maps_to_a_real_logger() -> None:
    """The dialog's four boxes and the config's four flags agree."""
    from cortex.libs.config.settings import DebugConfig
    from cortex.libs.logging.structured import DEBUG_SUBSYSTEM_LOGGERS

    assert DEBUG_SUBSYSTEM_LOGGERS == _SUBSYSTEM_NAMESPACES
    for subsystem, namespace in DEBUG_SUBSYSTEM_LOGGERS.items():
        assert subsystem in DebugConfig.model_fields, (
            f"{subsystem!r} has no config field to persist into"
        )
        # The namespace must be a package that exists, not a guess.
        assert namespace.startswith("cortex.services."), namespace


@pytest.mark.parametrize("subsystem", sorted(_SUBSYSTEM_NAMESPACES))
def test_turning_a_switch_on_raises_that_subsystem_to_debug(
    subsystem: str,
) -> None:
    from cortex.libs.logging.structured import apply_debug_subsystems

    namespace = _SUBSYSTEM_NAMESPACES[subsystem]
    logging.getLogger(namespace).setLevel(logging.NOTSET)

    apply_debug_subsystems({subsystem: True})

    assert logging.getLogger(namespace).level == logging.DEBUG
    # Only that one.
    for other, other_namespace in _SUBSYSTEM_NAMESPACES.items():
        if other != subsystem:
            assert logging.getLogger(other_namespace).level == logging.NOTSET


def test_turning_a_switch_off_restores_inheritance_not_info() -> None:
    """Off must mean "like everything else", not "quieter than everything else".

    Pinning the namespace to INFO would make a switched-off subsystem silent
    on a daemon whose root level is DEBUG — the opposite of what the control
    says it does.
    """
    from cortex.libs.logging.structured import apply_debug_subsystems

    namespace = _SUBSYSTEM_NAMESPACES["capture"]
    apply_debug_subsystems({"capture": True})
    assert logging.getLogger(namespace).level == logging.DEBUG

    apply_debug_subsystems({"capture": False})

    assert logging.getLogger(namespace).level == logging.NOTSET


def test_unknown_keys_are_ignored() -> None:
    """The flags arrive inside a settings payload full of other keys."""
    from cortex.libs.logging.structured import apply_debug_subsystems

    applied = apply_debug_subsystems({"capture": True, "webcam_enabled": True})
    assert applied == {"capture": "DEBUG"}


@pytest.mark.asyncio
async def test_apply_settings_wires_the_checkbox_through(
    daemon,  # type: ignore[no-untyped-def]
) -> None:
    """The exact key the desktop dialog sends must reach the logger.

    ``SettingsDialog.get_settings`` emits ``debug_capture``, ``debug_rppg``,
    ``debug_state`` and ``debug_llm``. Before this fix ``apply_settings``
    matched none of them.
    """
    namespace = _SUBSYSTEM_NAMESPACES["rppg"]
    logging.getLogger(namespace).setLevel(logging.NOTSET)

    await daemon.apply_settings({"debug_rppg": True})

    assert logging.getLogger(namespace).level == logging.DEBUG
    assert daemon.config.debug.rppg is True

    await daemon.apply_settings({"debug_rppg": False})

    assert logging.getLogger(namespace).level == logging.NOTSET
    assert daemon.config.debug.rppg is False


# ---------------------------------------------------------------------------
# Full state stream
# ---------------------------------------------------------------------------


def test_the_state_stream_flag_comes_from_configuration(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It was a class attribute requiring a source edit to capture a replay."""
    from cortex.libs.config import settings as settings_mod
    from cortex.services.runtime_daemon import CortexDaemon

    monkeypatch.setenv("CORTEX_DEBUG__RECORD_FULL_STATE_STREAM", "true")
    if hasattr(settings_mod.get_config, "cache_clear"):
        settings_mod.get_config.cache_clear()  # type: ignore[attr-defined]

    daemon = CortexDaemon(config=settings_mod.get_config())

    assert daemon.config.debug.record_full_state_stream is True
    assert daemon._record_full_state_stream is True


def test_the_state_stream_records_transitions_only_by_default(
    daemon,  # type: ignore[no-untyped-def]
) -> None:
    from types import SimpleNamespace

    recorded: list[str] = []
    daemon._recorder = SimpleNamespace(
        append=lambda event_type, payload: recorded.append(event_type)
    )
    assert daemon._record_full_state_stream is False

    for _ in range(5):
        daemon._record_state_estimate(
            SimpleNamespace(
                state="FLOW",
                status="estimated",
                model_dump=lambda mode="json": {},
            )
        )

    assert recorded == ["state_estimate"], (
        "unchanged ticks must not be recorded by default"
    )


@pytest.mark.asyncio
async def test_the_state_stream_can_be_armed_live(
    daemon,  # type: ignore[no-untyped-def]
) -> None:
    """A replay capture can be armed mid-session without a restart."""
    from types import SimpleNamespace

    recorded: list[str] = []
    daemon._recorder = SimpleNamespace(
        append=lambda event_type, payload: recorded.append(event_type)
    )

    await daemon.apply_settings({"record_full_state_stream": True})
    assert daemon._record_full_state_stream is True
    assert daemon.config.debug.record_full_state_stream is True

    for _ in range(5):
        daemon._record_state_estimate(
            SimpleNamespace(
                state="FLOW",
                status="estimated",
                model_dump=lambda mode="json": {},
            )
        )

    assert recorded == ["state_estimate"] * 5, (
        "every tick must be recorded once the stream is armed"
    )
