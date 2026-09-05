"""D10 / D9 — capability-token file: never world-readable, cheaply verified.

D10: ``load_or_create_token``/``rotate_token`` wrote the temp file with
``write_text`` (mode ``0o666 & ~umask``) and only then ``chmod``-ed it to
0600 — a window in which any local user could read the secret. The file
is now created with ``os.open(..., O_CREAT | O_EXCL, 0o600)`` so it never
exists with a wider mode.

D9: ``verify_token`` ran on the event loop for every request and AUTH
frame and re-read the file each time. The reader now caches on the file's
(inode, mtime, size) signature and re-reads only when the file changed —
rotation is still picked up on the very next call.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from cortex.libs.auth import local_token
from cortex.libs.auth.local_token import (
    load_or_create_token,
    load_token_or_none,
    rotate_token,
    verify_token,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX permission semantics do not apply"
)


@pytest.fixture()
def permissive_umask():
    """Make the old ``write_text``-then-``chmod`` sequence observable: with
    umask 0 a plain create would briefly be 0666."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def _spy_os_open(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[tuple[str, int, int, int]]:
    observed: list[tuple[str, int, int, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):  # noqa: ANN001
        fd = real_open(path, flags, mode, *args, **kwargs)
        if str(path).startswith(str(root)):
            observed.append((str(path), flags, mode, stat.S_IMODE(os.fstat(fd).st_mode)))
        return fd

    monkeypatch.setattr(os, "open", spy)
    return observed


@pytest.mark.usefixtures("permissive_umask")
def test_token_file_is_created_0600_exclusive_and_never_wider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = _spy_os_open(monkeypatch, tmp_path)
    target = tmp_path / "auth.token"

    token = load_or_create_token(target)
    rotated = rotate_token(target)
    assert rotated != token

    assert len(observed) == 2, observed
    for path, flags, mode, actual_mode in observed:
        assert path.startswith(str(tmp_path / ".auth.token."))
        assert flags & os.O_CREAT
        assert flags & os.O_EXCL, "temp file must be created exclusively"
        assert mode == 0o600, "creation mode must already be owner-only"
        assert actual_mode == 0o600, "the file must never exist with a wider mode"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8").strip() == rotated
    assert not list(tmp_path.glob(".auth.token.*")), "no temp files may linger"


def test_stale_temp_file_cannot_block_rotation(tmp_path: Path) -> None:
    target = tmp_path / "auth.token"
    load_or_create_token(target)
    # A crashed earlier writer left junk behind under the legacy name.
    (tmp_path / "auth.token.tmp").write_text("junk\n", encoding="utf-8")
    rotated = rotate_token(target)
    assert verify_token(rotated, path=target) is True


def test_verify_token_caches_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "auth.token"
    token = load_or_create_token(target)

    reads = 0
    real_read_text = Path.read_text

    def counting_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
        nonlocal reads
        if self == target:
            reads += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    for _ in range(200):
        assert verify_token(token, path=target) is True
    assert reads == 1, "steady-state verification must not re-read the file"

    rotated = rotate_token(target)
    assert verify_token(token, path=target) is False, "old token rejected immediately"
    assert verify_token(rotated, path=target) is True
    assert reads == 2

    # An out-of-process rewrite (different inode/mtime) is also picked up.
    external = "e" * 64
    tmp = tmp_path / "external.tmp"
    tmp.write_text(external + "\n", encoding="utf-8")
    os.replace(tmp, target)
    assert verify_token(rotated, path=target) is False
    assert verify_token(external, path=target) is True
    assert reads == 3


def test_missing_file_is_not_cached_as_present(tmp_path: Path) -> None:
    target = tmp_path / "auth.token"
    token = load_or_create_token(target)
    assert load_token_or_none(target) == token
    target.unlink()
    assert load_token_or_none(target) is None
    assert verify_token(token, path=target) is False
    # A fresh token written afterwards is visible again.
    fresh = load_or_create_token(target)
    assert verify_token(fresh, path=target) is True


def test_cache_is_keyed_per_path(tmp_path: Path) -> None:
    first = tmp_path / "one.token"
    second = tmp_path / "two.token"
    token_one = load_or_create_token(first)
    token_two = load_or_create_token(second)
    assert token_one != token_two
    assert verify_token(token_one, path=first) is True
    assert verify_token(token_one, path=second) is False
    assert verify_token(token_two, path=second) is True
    local_token._invalidate_token_cache(first)
    assert verify_token(token_one, path=first) is True
