"""Capability-token rotation (audit Debt-2, Commit 5).

The Settings panel exposes "Rotate authentication token" so users on
a shared machine — or any user who suspects their token file may have
been read — can invalidate the current value. Rotation must:

1. Produce a fresh, distinct token (no string collision with the old).
2. Persist atomically to the same path ``auth_token_path()`` returns,
   with the file inheriting mode 0600 on POSIX.
3. Cause ``verify_token`` calls bearing the OLD token to start
   returning False on the very next call (no lingering cache).

The Settings panel emits ``auth_token_rotated(str)`` so the desktop
controller can refresh ``WebSocketBridge._auth_token`` and surface a
confirmation toast; that wiring is exercised separately in the
desktop controller tests.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from cortex.libs.auth.local_token import (
    load_or_create_token,
    rotate_token,
    verify_token,
)


def test_rotate_token_produces_a_new_value(tmp_path: Path) -> None:
    """Case 1: rotation returns a token distinct from the prior one."""
    token_file = tmp_path / "auth.token"
    original = load_or_create_token(token_file)
    rotated = rotate_token(token_file)
    assert rotated != original
    assert len(rotated) >= 32  # token_hex(32) → 64 hex chars


def test_rotate_token_writes_file_atomically_mode_0600(tmp_path: Path) -> None:
    """Case 2: the rotated file is owned + readable only by the user.

    The implementation writes a ``.tmp`` sibling, chmods to 0600, then
    ``os.replace``s the target. After rotation we observe (a) the
    target exists with the rotated token, (b) the .tmp sibling no
    longer exists, (c) on POSIX the mode is 0o600.
    """
    token_file = tmp_path / "auth.token"
    load_or_create_token(token_file)
    rotated = rotate_token(token_file)
    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8").strip() == rotated
    # .tmp sibling must have been moved (os.replace), not lingering.
    assert not token_file.with_suffix(token_file.suffix + ".tmp").exists()
    if not sys.platform.startswith("win"):
        mode = stat.S_IMODE(os.stat(token_file).st_mode)
        assert mode == 0o600, f"expected 0o600 got 0o{mode:o}"


def test_old_token_rejected_after_rotation(tmp_path: Path) -> None:
    """Case 3: ``verify_token`` rejects the old value the moment the
    rotation returns. This is the threat-model contract — a leaked
    token loses its power immediately, not after a TTL.
    """
    token_file = tmp_path / "auth.token"
    old = load_or_create_token(token_file)
    new = rotate_token(token_file)
    assert verify_token(old, path=token_file) is False
    assert verify_token(new, path=token_file) is True
