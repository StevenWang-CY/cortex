"""Phase-4a Debt-1: ``atomic_write_text`` must not promote a partial
file when ``fsync`` fails with ``ENOSPC`` (disk full).

The pre-Phase-4a code swallowed every OSError from fsync silently and
proceeded to ``os.replace`` the (truncated) temp file over the
destination, replacing the prior known-good copy with a partially-
written one. The fix distinguishes ``ENOSPC`` (re-raise + unlink temp)
from benign FUSE-style ``fsync`` rejections (continue with rename).

These tests pin both branches.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cortex.libs.utils.atomic_write import atomic_write_json, atomic_write_text


def test_enospc_during_fsync_does_not_promote_partial_file(
    tmp_path: Path,
) -> None:
    """When ``fsync`` raises ENOSPC the helper must:

    * unlink the temp file (no zero-byte / truncated remnant),
    * NOT call ``os.replace`` (prior file is preserved),
    * re-raise so the caller sees the disk-full condition.
    """
    target = tmp_path / "session.json"
    target.write_text('{"prior": true}')

    def fail_fsync(_fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with patch("cortex.libs.utils.atomic_write.os.fsync", side_effect=fail_fsync):
        with pytest.raises(OSError) as excinfo:
            atomic_write_json(target, {"new": True})
        assert excinfo.value.errno == errno.ENOSPC

    # Prior contents survive.
    assert json.loads(target.read_text()) == {"prior": True}

    # No .tmp sidecar leaked into the directory.
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == ["session.json"], (
        f"expected only the prior session.json, got {siblings}"
    )


def test_benign_fsync_failure_still_renames(tmp_path: Path) -> None:
    """A non-ENOSPC OSError from fsync (FUSE filesystems reject fsync
    with EINVAL or similar) must not block the rename. We lose the
    durability guarantee but the bytes are still valid."""
    target = tmp_path / "session.json"

    def fail_fsync_benign(_fd: int) -> None:
        # EINVAL is the canonical "fsync not supported here" code.
        raise OSError(errno.EINVAL, "Invalid argument")

    with patch(
        "cortex.libs.utils.atomic_write.os.fsync",
        side_effect=fail_fsync_benign,
    ):
        atomic_write_json(target, {"new": True})

    # File got written and renamed despite the fsync failure.
    assert json.loads(target.read_text()) == {"new": True}


def test_atomic_write_text_enospc_path(tmp_path: Path) -> None:
    """Same invariant for the text variant, since both share fsync
    handling."""
    target = tmp_path / "out.txt"
    target.write_text("prior")

    def fail_fsync(_fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    with patch("cortex.libs.utils.atomic_write.os.fsync", side_effect=fail_fsync):
        with pytest.raises(OSError) as excinfo:
            atomic_write_text(target, "new content")
        assert excinfo.value.errno == errno.ENOSPC

    assert target.read_text() == "prior"
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == ["out.txt"]
