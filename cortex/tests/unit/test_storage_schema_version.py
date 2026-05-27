"""P1-16: Storage schema versioning + migration scaffold tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_write_stamps_schema_version(tmp_path: Path) -> None:
    from cortex.libs.storage.schema_version import SCHEMA_VERSION, write_with_version

    target = tmp_path / "data.json"
    write_with_version(target, {"foo": "bar"})
    raw = json.loads(target.read_text())
    assert raw["_schema_version"] == SCHEMA_VERSION
    assert raw["foo"] == "bar"


def test_read_returns_data(tmp_path: Path) -> None:
    from cortex.libs.storage.schema_version import SCHEMA_VERSION, read_with_version, write_with_version

    target = tmp_path / "data.json"
    write_with_version(target, {"answer": 42})
    data = read_with_version(target)
    assert data["answer"] == 42
    assert data["_schema_version"] == SCHEMA_VERSION


def test_read_raises_on_missing_version(tmp_path: Path) -> None:
    from cortex.libs.storage.schema_version import UnsupportedSchemaError, read_with_version

    target = tmp_path / "legacy.json"
    target.write_text(json.dumps({"no_version": True}))
    with pytest.raises(UnsupportedSchemaError) as exc_info:
        read_with_version(target)
    assert exc_info.value.found is None


def test_read_raises_when_version_exceeds_schema_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a file written by a newer Cortex; current code should refuse it."""
    import cortex.libs.storage.schema_version as sv_mod

    # Write a file at version 1.
    target = tmp_path / "future.json"
    sv_mod.write_with_version(target, {"x": 1})

    # Pretend this binary only knows version 0.
    monkeypatch.setattr(sv_mod, "SCHEMA_VERSION", 0)

    with pytest.raises(sv_mod.UnsupportedSchemaError) as exc_info:
        sv_mod.read_with_version(target)
    assert exc_info.value.found == 1
    assert exc_info.value.supported == 0


def test_migration_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Register a v1→v2 migration and confirm migrate() applies it."""
    import cortex.libs.storage.schema_version as sv_mod

    # Write a v1 file.
    target = tmp_path / "v1.json"
    sv_mod.write_with_version(target, {"legacy_field": "hello"})

    # Register a migration that adds new_field.
    monkeypatch.setitem(
        sv_mod.MIGRATIONS,
        1,
        lambda d: {**d, "new_field": "migrated"},
    )

    raw = json.loads(target.read_text())
    result = sv_mod.migrate(raw, from_version=1, to_version=2)

    assert result["new_field"] == "migrated"
    assert result["legacy_field"] == "hello"
    assert result["_schema_version"] == 2
