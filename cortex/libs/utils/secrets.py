"""
Secret helpers for packaged/local Cortex deployments.

Development can use environment variables and dotenv. Packaged macOS builds can
prefer the system Keychain for API credentials.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


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
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.debug("Keychain lookup failed for %s/%s: %s", service, account, exc)
        return None

    secret = result.stdout.strip()
    return secret or None

