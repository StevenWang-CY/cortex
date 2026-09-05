"""Daemon pidfile handshake shared by the daemon and its launchers (D1).

The launchers (``cortex/scripts/launcher_agent.py`` and the native
messaging host) stop the daemon by PID. Port scans and ``pgrep`` patterns
only identify it heuristically; the pidfile written here is the
authoritative identification — it survives App Translocation (where the
executable path no longer starts with ``/Applications/Cortex.app``), a
renamed dev checkout, and the in-process desktop shell whose argv is
indistinguishable from the WS-mode shell that must never be killed.

Writers
    ``cortex.scripts.run_dev`` (dev mode) and — patch note for the runtime
    owner — ``CortexDaemon.start``/``stop``.
Readers
    ``cortex.scripts.launcher_agent.find_daemon_pids`` (which validates the
    PID against the live command line before trusting it, so a stale file
    left behind by a crash can never target a recycled PID).

The file lives at ``<config_dir>/daemon.pid`` next to ``auth.token`` and
contains the decimal PID followed by a newline. It is written atomically
(temp file + ``os.replace``) with mode 0600.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DAEMON_PIDFILE_NAME = "daemon.pid"


def daemon_pidfile_path() -> Path:
    """Return ``<config_dir>/daemon.pid``.

    Imports ``get_config_dir`` lazily so this module stays importable in
    minimal contexts (and keeps the launcher-side resolver in
    ``launcher_agent._daemon_pid_path`` the only other copy of the rule).
    """
    from cortex.libs.utils.platform import get_config_dir

    return get_config_dir() / DAEMON_PIDFILE_NAME


def read_daemon_pidfile(path: Path | None = None) -> int | None:
    """Return the PID recorded in the pidfile, or ``None`` if absent/invalid."""
    target = path or daemon_pidfile_path()
    try:
        text = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    pid = int(text)
    return pid if pid > 1 else None


def write_daemon_pidfile(path: Path | None = None, *, pid: int | None = None) -> Path:
    """Atomically record ``pid`` (default: this process) in the pidfile."""
    target = path or daemon_pidfile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    value = os.getpid() if pid is None else int(pid)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{value}\n")
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.debug("daemon pidfile written: %s (pid=%d)", target, value)
    return target


def remove_daemon_pidfile(path: Path | None = None, *, pid: int | None = None) -> bool:
    """Remove the pidfile only if it still names ``pid`` (default: this process).

    A process exiting late must never delete the pidfile a newer daemon
    has already written for itself. Returns True iff a file was removed.
    """
    target = path or daemon_pidfile_path()
    expected = os.getpid() if pid is None else int(pid)
    if read_daemon_pidfile(target) != expected:
        return False
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("could not remove daemon pidfile %s", target, exc_info=True)
        return False
    return True


__all__ = [
    "DAEMON_PIDFILE_NAME",
    "daemon_pidfile_path",
    "read_daemon_pidfile",
    "remove_daemon_pidfile",
    "write_daemon_pidfile",
]
