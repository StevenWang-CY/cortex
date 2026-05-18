"""Atomic file writes (audit F02).

A naive ``path.write_text(...)`` is not crash-safe: a SIGKILL or
disk-full midway through the write leaves the file truncated or empty.
For values the daemon writes once at shutdown (session reports,
baselines) this is the difference between "lose the report" and "keep
the prior known-good".

``atomic_write_text`` / ``atomic_write_json`` write to ``<path>.tmp``
in the same directory, ``fsync`` the temp file, then ``os.replace``
atomically swap it over the destination. ``os.replace`` is atomic on
POSIX and on NTFS Win32 (per Python docs).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    ensure_dir: bool = True,
    fsync: bool = True,
) -> None:
    """Atomically replace ``path`` with ``text``.

    Raises ``OSError`` on any failure; the destination file is unchanged
    if the temp write or rename fails. Callers that want to swallow the
    error must do so explicitly so the failure is logged at a meaningful
    layer (not buried at the bottom of a try/except chain).
    """
    if ensure_dir:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Write + fsync + rename. Open with low-level os APIs so fsync sees a
    # real fd; ``Path.write_text`` does not expose the descriptor.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fp:
            fp.write(text)
            fp.flush()
            if fsync:
                try:
                    os.fsync(fp.fileno())
                except OSError:
                    # Some FUSE filesystems reject fsync. The rename is
                    # still atomic; we lose only the durability guarantee.
                    pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
    ensure_dir: bool = True,
) -> None:
    """Atomically write JSON to ``path``. See :func:`atomic_write_text`."""
    atomic_write_text(
        path,
        json.dumps(data, indent=indent),
        ensure_dir=ensure_dir,
    )
