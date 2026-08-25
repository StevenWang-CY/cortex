"""Select one complete, exclusive Apple notarization authentication mode.

The release workflow supports either an App Store Connect API key or an
Apple ID plus app-specific password.  This module validates only variable
presence and never reads, logs, transforms, or persists credential values.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Literal

NotaryAuthMode = Literal["api-key", "apple-id"]

API_KEY_VARIABLES: tuple[str, ...] = (
    "APPLE_NOTARY_KEY_P8_BASE64",
    "APPLE_NOTARY_KEY_ID",
    "APPLE_NOTARY_ISSUER_ID",
)
APPLE_ID_VARIABLES: tuple[str, ...] = (
    "APPLE_ID_USERNAME",
    "APPLE_ID_APP_PASSWORD",
    "APPLE_TEAM_ID",
)


class NotaryAuthConfigurationError(ValueError):
    """Raised when notarization credentials are absent, partial, or mixed."""


def _missing(environ: Mapping[str, str], names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in names if not environ.get(name, "").strip())


def select_notary_auth_mode(environ: Mapping[str, str]) -> NotaryAuthMode:
    """Return the sole complete credential mode, failing closed otherwise."""

    api_missing = _missing(environ, API_KEY_VARIABLES)
    apple_id_missing = _missing(environ, APPLE_ID_VARIABLES)
    api_started = len(api_missing) != len(API_KEY_VARIABLES)
    apple_id_started = len(apple_id_missing) != len(APPLE_ID_VARIABLES)

    if api_started and api_missing:
        raise NotaryAuthConfigurationError(
            "incomplete App Store Connect API-key credentials; missing "
            + ", ".join(api_missing)
        )
    if apple_id_started and apple_id_missing:
        raise NotaryAuthConfigurationError(
            "incomplete Apple-ID notarization credentials; missing "
            + ", ".join(apple_id_missing)
        )

    api_complete = not api_missing
    apple_id_complete = not apple_id_missing
    if api_complete and apple_id_complete:
        raise NotaryAuthConfigurationError(
            "configure exactly one notarization credential mode, not both"
        )
    if not api_complete and not apple_id_complete:
        raise NotaryAuthConfigurationError(
            "missing notarization credentials; configure one complete credential mode"
        )
    return "api-key" if api_complete else "apple-id"


def main() -> int:
    try:
        mode = select_notary_auth_mode(os.environ)
    except NotaryAuthConfigurationError as error:
        print(f"notarization credential configuration invalid: {error}", file=sys.stderr)
        return 1
    print(mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
