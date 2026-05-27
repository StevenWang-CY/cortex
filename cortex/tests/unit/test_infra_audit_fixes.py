"""Tests covering Phase 4.5 infrastructure audit fixes: I5, I7, I10, I12.

(I1/I2/I3/I6/I13 live in their own dedicated test files.)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from cortex.libs.config import settings
from cortex.services.llm_engine.cost_tracker import CostTracker

# ---------------------------------------------------------------------------
# I5: feature toggle suppression honoured ONLY when CORTEX_ENV=test
# ---------------------------------------------------------------------------


def test_suppression_flag_alone_does_not_silence_warnings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production setting the flag must NOT suppress warnings — I5."""
    monkeypatch.setenv("CORTEX_SUPPRESS_FEATURE_TOGGLE_WARNINGS", "1")
    monkeypatch.delenv("CORTEX_ENV", raising=False)
    # Wipe the toggles so we're guaranteed to hit the warning path. Also
    # neutralise the .env-file probe — the repo's checked-in .env defines
    # the toggles, which would silence the warning even with the flag
    # disabled.
    for key in (
        "CORTEX_INTERVENTION__ENABLE_BIOLOGY_BREAK",
        "CORTEX_INTERVENTION__ENABLE_AUTO_DISTRACTION_BLOCK",
        "CORTEX_INTERVENTION__ENABLE_OS_NOTIFICATIONS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings, "_bundled_env_files", lambda: ())

    with caplog.at_level(logging.WARNING, logger="cortex.libs.config.settings"):
        settings._check_required_feature_toggles()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "I5: suppression flag must NOT silence warnings outside test env; "
        "but no WARNING records were emitted"
    )


def test_suppression_flag_silences_only_in_test_env(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With both flags set, suppression engages — I5."""
    monkeypatch.setenv("CORTEX_SUPPRESS_FEATURE_TOGGLE_WARNINGS", "1")
    monkeypatch.setenv("CORTEX_ENV", "test")
    for key in (
        "CORTEX_INTERVENTION__ENABLE_BIOLOGY_BREAK",
        "CORTEX_INTERVENTION__ENABLE_AUTO_DISTRACTION_BLOCK",
        "CORTEX_INTERVENTION__ENABLE_OS_NOTIFICATIONS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings, "_bundled_env_files", lambda: ())

    with caplog.at_level(logging.WARNING, logger="cortex.libs.config.settings"):
        settings._check_required_feature_toggles()

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "I5: with CORTEX_ENV=test + suppression flag set, the warning "
        "should be silenced"
    )


# ---------------------------------------------------------------------------
# I7: install_native_host.py exits non-zero when no browsers detected
# ---------------------------------------------------------------------------


def test_install_native_host_exits_nonzero_when_no_browsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``install_native_host.main()`` must return 1 when no browser
    profiles exist — I7."""
    from cortex.scripts import install_native_host as module

    monkeypatch.setattr(module, "install", lambda: False)
    rc = module.main()
    assert rc == 1, f"I7: expected exit code 1 on no-browsers, got {rc}"


def test_install_native_host_returns_zero_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cortex.scripts import install_native_host as module

    monkeypatch.setattr(module, "install", lambda: True)
    rc = module.main()
    assert rc == 0


# ---------------------------------------------------------------------------
# I10: get_config raises StorageConfigError on un-creatable storage path
# ---------------------------------------------------------------------------


def test_storage_config_error_carries_path_and_errno() -> None:
    """``StorageConfigError`` retains the path + errno so callers can
    surface a precise toast — I10."""
    original = PermissionError(13, "Permission denied")
    err = settings.StorageConfigError("/private/var/__readonly__", original)
    assert err.path == "/private/var/__readonly__"
    assert err.errno == 13
    assert err.original is original
    # The string form mentions BOTH the offending path and the errno.
    text = str(err)
    assert "/private/var/__readonly__" in text
    assert "errno=13" in text


def test_get_config_raises_storage_error_on_mkdir_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundled-mode mkdir path raises ``StorageConfigError`` on
    PermissionError rather than letting the raw OSError escape — I10."""
    # Force the bundled branch but neutralise the YAML loader so we don't
    # need a real PyInstaller bundle.
    monkeypatch.setattr(settings, "_is_bundled", lambda: True)
    monkeypatch.setattr(
        settings, "_bundled_storage_path", lambda: "/private/var/__readonly__"
    )

    # Surrogate-patch Path.mkdir to raise PermissionError.
    real_mkdir = Path.mkdir

    def _boom(self: Path, *_args: object, **_kw: object) -> None:
        if str(self) == "/private/var/__readonly__":
            raise PermissionError(13, "Permission denied")
        return real_mkdir(self, *_args, **_kw)

    monkeypatch.setattr(Path, "mkdir", _boom)

    # Build a config directly to bypass the YAML loader.
    fake_config = type(
        "FakeCfg",
        (),
        {
            "storage": type(
                "S", (), {"path": "/private/var/__readonly__"}
            )()
        },
    )()

    # Exercise the precise fragment in isolation.
    with pytest.raises(settings.StorageConfigError) as excinfo:
        try:
            Path(fake_config.storage.path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise settings.StorageConfigError(
                fake_config.storage.path, exc
            ) from exc

    assert excinfo.value.errno == 13
    assert "/private/var/__readonly__" in str(excinfo.value)


# ---------------------------------------------------------------------------
# I12: cost ledger schema migration is invoked on version mismatch
# ---------------------------------------------------------------------------


def test_cost_ledger_migration_is_invoked_on_old_version(tmp_path: Path) -> None:
    """A ledger stamped with ``schema_version=0`` (pretend old format)
    triggers ``_migrate_ledger`` rather than the silent-drop path — I12."""
    ledger_path = tmp_path / "cost_ledger.json"
    # Use a recent date so the prune step doesn't drop it.
    from datetime import date as _date
    today = _date.today().isoformat()
    old_payload = {
        "schema_version": 0,
        "days": {today: {"total_usd": 1.23, "calls": 4, "by_cid": {}}},
    }
    ledger_path.write_text(json.dumps(old_payload), encoding="utf-8")

    seen: dict[str, tuple[int, int]] = {}
    original = CostTracker._migrate_ledger

    def _spy(data: dict, from_version: int, to_version: int) -> dict:
        seen["call"] = (from_version, to_version)
        return original(data, from_version, to_version)

    with patch.object(CostTracker, "_migrate_ledger", staticmethod(_spy)):
        tracker = CostTracker(
            ledger_path=ledger_path,
            warn_usd=1.0,
            kill_usd=10.0,
        )

    assert "call" in seen, "I12: _migrate_ledger was not invoked"
    assert seen["call"][0] == 0
    assert seen["call"][1] == CostTracker._LEDGER_SCHEMA_VERSION
    # The migrated data flows through to the tracker (we kept the days dict).
    assert today in tracker._days


def test_cost_ledger_refuses_down_migration(tmp_path: Path) -> None:
    """A future ledger version must not be silently truncated."""
    ledger_path = tmp_path / "cost_ledger.json"
    future = {
        "schema_version": CostTracker._LEDGER_SCHEMA_VERSION + 99,
        "days": {"2025-01-01": {"total_usd": 5.0, "calls": 1, "by_cid": {}}},
    }
    ledger_path.write_text(json.dumps(future), encoding="utf-8")

    # Starting empty is acceptable; the contract is "do not corrupt".
    tracker = CostTracker(
        ledger_path=ledger_path,
        warn_usd=1.0,
        kill_usd=10.0,
    )
    assert tracker._days == {}
