"""Audit F02 — atomic file writes.

Covers the helper used to persist session reports at shutdown.
Previously a naive ``path.write_text`` could leave a truncated file on
disk-full or SIGKILL mid-write, and the surrounding ``try/except``
silently swallowed it. ``atomic_write_json`` writes to a sibling
``.tmp`` and ``os.replace``s into place — failures keep the prior
on-disk file intact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from cortex.libs.utils.atomic_write import atomic_write_json, atomic_write_text


def test_atomic_write_text_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "out" / "result.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"
    # ensure_dir=True created the parent.
    assert target.parent.is_dir()


def test_atomic_write_json_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "session.json"
    payload = {"id": "abc", "events": [1, 2, 3]}
    atomic_write_json(target, payload)
    assert json.loads(target.read_text()) == payload


def test_atomic_write_does_not_leave_tmp_file_on_success(tmp_path: Path) -> None:
    target = tmp_path / "session.json"
    atomic_write_json(target, {"k": "v"})
    siblings = list(tmp_path.iterdir())
    # Only the final file should remain.
    assert siblings == [target]


def test_atomic_write_preserves_prior_file_on_replace_failure(
    tmp_path: Path,
) -> None:
    """If ``os.replace`` raises (e.g. permission denied), the destination
    must keep its prior contents. Simulated via a patched ``os.replace``.
    """
    target = tmp_path / "session.json"
    target.write_text('{"prior": true}')

    with patch(
        "cortex.libs.utils.atomic_write.os.replace",
        side_effect=PermissionError("simulated"),
    ):
        with pytest.raises(PermissionError):
            atomic_write_json(target, {"new": True})

    # Prior contents must survive.
    assert json.loads(target.read_text()) == {"prior": True}


def test_atomic_write_cleans_up_tmp_on_failed_write(
    tmp_path: Path,
) -> None:
    """If the temp write raises mid-stream, the .tmp file must not leak
    into the directory listing on the next call."""
    target = tmp_path / "session.json"

    # Force write() to raise after fdopen succeeds.
    original_open = os.fdopen

    def boom(fd, *args, **kwargs):
        wrapper = original_open(fd, *args, **kwargs)
        original_write = wrapper.write

        def _raise(_text):  # noqa: ARG001
            original_write("partial")
            raise OSError("disk full simulated")

        wrapper.write = _raise  # type: ignore[method-assign]
        return wrapper

    with patch("cortex.libs.utils.atomic_write.os.fdopen", side_effect=boom):
        with pytest.raises(OSError, match="disk full simulated"):
            atomic_write_json(target, {"new": True})

    # Destination never created; tmp cleaned up.
    assert not target.exists()
    siblings = list(tmp_path.iterdir())
    assert siblings == [], f"unexpected leftover files: {siblings}"
