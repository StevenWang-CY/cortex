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

import errno
import json
import logging
import os
import tempfile
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

    Uses a unique temp file per call (via :func:`tempfile.mkstemp`) so that
    concurrent writers to the same destination do not share a single temp fd
    and interleave bytes.  The temp file lives in the same directory as the
    destination so that ``os.replace`` is always an in-directory rename
    (guaranteed atomic on POSIX) rather than a cross-device copy.
    """
    if ensure_dir:
        path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp file in the same directory — no two concurrent calls share
    # an fd, eliminating byte interleaving and partial-overwrite races.
    fd, tmp_str = tempfile.mkstemp(
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
    )
    tmp = Path(tmp_str)
    try:
        # Write + fsync + rename. Using the fd returned by mkstemp keeps the
        # existing durability semantics; ``Path.write_text`` does not expose
        # the descriptor.
        with os.fdopen(fd, "w", encoding=encoding) as fp:
            fp.write(text)
            fp.flush()
            if fsync:
                try:
                    os.fsync(fp.fileno())
                except (AttributeError, OSError) as exc:
                    # Phase-4a fix: previously this swallowed every
                    # OSError, including ``ENOSPC`` (disk full). On an
                    # out-of-space write the file fd contains a partial
                    # / truncated buffer; promoting it via os.replace
                    # silently overwrites the prior good copy with
                    # garbage. Distinguish the two cases:
                    #   * AttributeError (Windows pre-3.3 / FUSE) or
                    #     non-ENOSPC OSError (e.g. EINVAL on /tmpfs):
                    #     durability is lost but the bytes are still
                    #     valid — proceed with the rename.
                    #   * ENOSPC: the bytes are NOT valid. Delete the
                    #     temp and re-raise so the caller sees the disk-
                    #     full condition instead of corrupting the
                    #     destination.
                    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        raise
                    # Otherwise tolerate the fsync failure; the rename
                    # is still atomic and we only lose durability.
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
