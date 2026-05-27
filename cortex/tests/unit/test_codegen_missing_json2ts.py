"""P2-15: generate_ts_schemas exits non-zero when json2ts is unavailable."""

from __future__ import annotations

import pytest


def test_resolve_json2ts_exits_nonzero_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When shutil.which('json2ts') returns None AND no local node_modules bin
    exists, _resolve_json2ts_command must raise SystemExit with a non-zero code.
    """
    import shutil
    from pathlib import Path

    # Ensure no env override interferes.
    monkeypatch.delenv("CORTEX_JSON2TS_CMD", raising=False)

    # Make shutil.which always return None.
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    # Make the local node_modules path appear absent.
    monkeypatch.setattr(Path, "exists", lambda _self: False)

    from cortex.scripts import generate_ts_schemas

    with pytest.raises(SystemExit) as exc_info:
        generate_ts_schemas._resolve_json2ts_command()

    assert exc_info.value.code != 0, (
        "SystemExit code must be non-zero when json2ts is absent"
    )


def test_resolve_json2ts_exits_with_code_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Specifically verify exit code is 2 (convention: dependency not found)."""
    import shutil
    from pathlib import Path

    monkeypatch.delenv("CORTEX_JSON2TS_CMD", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    monkeypatch.setattr(Path, "exists", lambda _self: False)

    from cortex.scripts import generate_ts_schemas

    with pytest.raises(SystemExit) as exc_info:
        generate_ts_schemas._resolve_json2ts_command()

    assert exc_info.value.code == 2
