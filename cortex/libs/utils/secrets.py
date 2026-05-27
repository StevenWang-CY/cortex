"""
Secret helpers for packaged/local Cortex deployments.

Development can use environment variables and dotenv. Packaged macOS builds can
prefer the system Keychain for API credentials.

Phase-4a hardening (Debt-1)
---------------------------

Two timeout-related defects motivated this rewrite:

* ``security find-generic-password`` is a subprocess; a wedged Keychain
  prompt or a hung TCC daemon could pin the call indefinitely. Bound
  the call with ``timeout=5`` and a ``TimeoutExpired`` catch.
* ``keyring.get_password`` itself can block on user-presence sheets on
  macOS or on a dbus round trip on Linux. Callers in long-lived event
  loops (``cortex.libs.config.settings`` at module import,
  ``cortex.services.llm_engine.anthropic_planner`` at planner
  construction, ``cortex.apps.desktop_shell.onboarding`` during the
  BYOK step) get bitten if the backend stalls. The new
  ``get_password_safe`` runs the call in a thread pool with a hard
  ``future.result(timeout=...)`` so a misbehaving backend at most
  delays startup by ``timeout`` seconds before degrading.
"""

from __future__ import annotations

import concurrent.futures
import logging
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Default hard-timeout for ``security find-generic-password``. macOS
# normally returns synchronously in <50 ms; anything slower is a sign
# the user is being prompted for a TCC grant or the Keychain is wedged.
_SECURITY_SUBPROCESS_TIMEOUT_S: float = 5.0

# Default hard-timeout for ``keyring.get_password``. Same rationale as
# above — the call is normally instant; anything slower means a
# backend stall and we'd rather degrade than hang.
_KEYRING_DEFAULT_TIMEOUT_S: float = 5.0


def get_keychain_password(service: str, account: str) -> str | None:
    """
    Read a generic password from the macOS Keychain.

    Returns None when the secret is missing or Keychain access is unavailable.
    """
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-w",
                "-s",
                service,
                "-a",
                account,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_SECURITY_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Phase-4a: a wedged TCC prompt / Keychain unlock sheet should
        # degrade to ``None`` (caller falls back to env var) instead of
        # pinning the calling event loop for tens of seconds.
        logger.warning(
            "Keychain lookup for %s/%s timed out after %.1fs; "
            "returning None (caller falls back to env var)",
            service,
            account,
            _SECURITY_SUBPROCESS_TIMEOUT_S,
        )
        return None
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.debug("Keychain lookup failed for %s/%s: %s", service, account, exc)
        return None

    secret = result.stdout.strip()
    return secret or None


def get_password_safe(
    service: str,
    username: str,
    timeout: float = _KEYRING_DEFAULT_TIMEOUT_S,
) -> str | None:
    """Read a generic password via ``keyring`` with a hard wall-clock timeout.

    Wraps ``keyring.get_password`` in a single-worker ``ThreadPoolExecutor``
    so that a stalled backend (macOS unlock sheet, dbus hang, GNOME
    keyring unavailable, …) cannot block the caller for longer than
    ``timeout`` seconds. On timeout we log a warning and return ``None``
    — the caller falls back to env vars / .env values as if the secret
    was simply absent.

    Note: this DOES leak a thread if the backend never returns. We
    accept that trade for liveness — at worst one daemon-lifetime
    thread per stalled backend. The alternative (signal-based timeout)
    is not safe inside an asyncio loop.

    Args:
        service: Keychain service identifier (e.g. ``"cortex.bedrock"``).
        username: Account within that service (e.g. ``"bearer_token"``).
        timeout: Wall-clock seconds before we give up and return ``None``.

    Returns:
        The stored password, or ``None`` if missing / timed out / backend
        unavailable.
    """
    try:
        import keyring  # local import — keyring is an optional dep at runtime
    except ImportError:
        logger.debug("keyring library not installed; treating %s/%s as absent", service, username)
        return None

    def _read() -> str | None:
        try:
            return keyring.get_password(service, username)
        except Exception:
            # ``keyring`` raises both its own ``KeyringError`` family and
            # bare ``RuntimeError`` from broken backends. We treat every
            # exception as "absent" so the caller falls back gracefully.
            logger.debug(
                "keyring.get_password raised for %s/%s",
                service,
                username,
                exc_info=True,
            )
            return None

    # NOTE: do not use ``with ThreadPoolExecutor(...)`` here — its
    # ``__exit__`` calls ``shutdown(wait=True)`` which would re-block
    # the caller for the full backend stall, defeating the timeout
    # entirely. We use ``shutdown(wait=False)`` so the helper returns
    # as soon as ``future.result(timeout=...)`` resolves; the worker
    # thread is allowed to drain in the background. The cost is at
    # most one daemon-lifetime thread per stalled backend.
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="cortex-keyring",
    )
    try:
        future = executor.submit(_read)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "keyring.get_password for %s/%s timed out after %.1fs; "
                "returning None (caller falls back to env var)",
                service,
                username,
                timeout,
            )
            # Best-effort cancel; the thread may still be blocked in the
            # backend, but it will not deliver its eventual result.
            future.cancel()
            return None
    finally:
        # wait=False: don't block on the still-running worker thread.
        executor.shutdown(wait=False)


__all__ = [
    "get_keychain_password",
    "get_password_safe",
]
