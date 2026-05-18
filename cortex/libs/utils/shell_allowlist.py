"""Shell-command allowlist (audit F12).

``ProjectLauncher.terminal_commands`` is a list of strings loaded from
user-importable YAML. Passing those strings to
``asyncio.create_subprocess_shell`` lets a hostile project file run
arbitrary commands (``rm -rf ~``, exfil curl, ...). The fix is to never
spawn a shell at all: tokenise the command with :func:`shlex.split`,
look at the first token, and refuse anything that is not on a fixed
allowlist of editor / terminal launchers.

This module exposes a single helper, :func:`validate_command`, so the
allowlist lives in one place. The launcher consumes it; tests exercise
it directly. The default allowlist intentionally omits
shell-interpreter binaries (``bash``, ``sh``, ``zsh``, ``fish``) and
``osascript``/``open`` — anything that would itself accept arbitrary
follow-on arguments.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence

# Built-in trusted binary names. These are short, well-known launcher
# binaries that the user typically runs by hand to open a workspace.
# Each one is matched on the *basename* of the resolved command — a
# project YAML that says ``vscode .`` matches whether ``vscode`` lives in
# ``/usr/local/bin``, ``/opt/homebrew/bin``, or anywhere else on
# ``$PATH``.
DEFAULT_ALLOWLIST: tuple[str, ...] = (
    "vscode",
    "code",
    "cursor",
    "codium",
    "iterm",
    "terminal",
    "wezterm",
    "kitty",
)


def validate_command(
    command: str,
    *,
    allowlist: Sequence[str] | None = None,
) -> tuple[list[str], str | None]:
    """Tokenise ``command`` and check it against the allowlist.

    Args:
        command: A raw command string from a project YAML, e.g.
            ``"code --diff a b"``.
        allowlist: Optional override of the default allowlist. The
            user-configurable
            :attr:`cortex.libs.config.settings.LauncherConfig.user_command_allowlist`
            is appended to :data:`DEFAULT_ALLOWLIST` by the caller.

    Returns:
        ``(argv, None)`` when the command is safe to run via
        :func:`asyncio.create_subprocess_exec`. ``argv`` is the
        tokenised list with the resolved binary basename in slot 0.

        ``([], "<reason>")`` when the command is rejected. The reason
        string is suitable for surfacing in an error envelope; the
        launcher wraps it as ``{"error": "unsupported_command",
        "command": "<quoted>"}``.
    """
    if not command or not command.strip():
        return [], "empty_command"

    # ``shlex.split`` raises ValueError on unterminated quoting. Treat
    # that as a rejection rather than letting the launcher hit an
    # uncaught exception.
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return [], f"unparseable_command: {exc}"

    if not argv:
        return [], "empty_command"

    binary_basename = os.path.basename(argv[0]).lower()

    effective_allowlist = tuple(DEFAULT_ALLOWLIST)
    if allowlist:
        # Lowercase user additions so the comparison is case-insensitive
        # without leaking that detail back into the original strings.
        effective_allowlist = effective_allowlist + tuple(
            entry.lower() for entry in allowlist
        )

    if binary_basename not in effective_allowlist:
        return [], "binary_not_in_allowlist"

    return argv, None


__all__ = ["DEFAULT_ALLOWLIST", "validate_command"]
