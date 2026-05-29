"""Storage schema versioning + forward-migration scaffold.

Every JSON file written by Cortex that lives beyond a single session
(baselines, session reports, AMIP arm state) should be stamped with a
``_schema_version`` key so a future version of the daemon can detect
and migrate legacy files without crashing on missing fields.

Usage — writers::

    from cortex.libs.storage.schema_version import write_with_version

    write_with_version(path, {"key": "value"})

Usage — readers::

    from cortex.libs.storage.schema_version import read_with_version

    data = read_with_version(path)  # raises UnsupportedSchemaError if stale

Migration — add a migration callable to MIGRATIONS when the schema
bumps and increment SCHEMA_VERSION::

    MIGRATIONS[1] = lambda d: {**d, "new_field": "default"}
    SCHEMA_VERSION = 2

This is a scaffold; no existing writers are retro-fitted in this pass.
New writers should call ``write_with_version`` from the start.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cortex.libs.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

#: Increment when the on-disk schema gains or loses fields.
#: Writers stamp this value; readers refuse anything higher.
SCHEMA_VERSION: int = 1


class UnsupportedSchemaError(Exception):
    """Raised when a file's ``_schema_version`` is incompatible.

    Carries ``found`` (the version in the file) and ``supported``
    (SCHEMA_VERSION at read time) so callers can surface a human-readable
    message ("file written by a newer Cortex version; please update").
    """

    def __init__(self, path: Path, found: int | None, supported: int) -> None:
        self.path = path
        self.found = found
        self.supported = supported
        super().__init__(
            f"{path}: schema version {found!r} is not supported "
            f"(this build supports up to version {supported})"
        )


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

#: Maps ``from_version`` → callable that upgrades a dict from that version
#: to the next version. Chains are applied in order by :func:`migrate`.
#:
#: Example — if the schema bumps from v1 to v2::
#:
#:     MIGRATIONS[1] = lambda d: {**d, "new_field": "default"}
#:     SCHEMA_VERSION = 2
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def migrate(
    data: dict[str, Any],
    from_version: int,
    to_version: int = SCHEMA_VERSION,
) -> dict[str, Any]:
    """Chain MIGRATIONS from ``from_version`` up to ``to_version``.

    Args:
        data: The raw dict read from disk (must include ``_schema_version``
              or callers should set it before calling).
        from_version: The version the data is currently at.
        to_version: The target version (defaults to :data:`SCHEMA_VERSION`).

    Returns:
        The migrated dict with ``_schema_version`` updated to
        ``to_version``.

    Raises:
        KeyError: if a required migration step is absent from
                  :data:`MIGRATIONS`.
    """
    current = from_version
    result = dict(data)
    while current < to_version:
        step = MIGRATIONS[current]
        result = step(result)
        current += 1
    result["_schema_version"] = to_version
    return result


# ---------------------------------------------------------------------------
# Public I/O helpers
# ---------------------------------------------------------------------------

def write_with_version(path: Path, data: dict[str, Any]) -> None:
    """Stamp ``data`` with the current :data:`SCHEMA_VERSION` and write atomically.

    Uses :func:`cortex.libs.utils.atomic_write.atomic_write_json` so a
    SIGKILL mid-write does not corrupt the destination file.

    Args:
        path: Destination file path (parent dirs created automatically).
        data: Dict to serialise. A ``_schema_version`` key is added (or
              overwritten) with the current :data:`SCHEMA_VERSION`.

    Raises:
        OSError: on any I/O failure.
    """
    stamped = {**data, "_schema_version": SCHEMA_VERSION}
    atomic_write_json(path, stamped)
    logger.debug("write_with_version path=%s version=%d", path, SCHEMA_VERSION)


def read_with_version(path: Path) -> dict[str, Any]:
    """Read a versioned JSON file, raising if the version is unsupported.

    Args:
        path: File to read.

    Returns:
        The parsed dict (including the ``_schema_version`` key).

    Raises:
        UnsupportedSchemaError: if ``_schema_version`` is missing or
            greater than :data:`SCHEMA_VERSION`.
        FileNotFoundError: if the file does not exist.
        json.JSONDecodeError: if the file is not valid JSON.
    """
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise UnsupportedSchemaError(path, None, SCHEMA_VERSION)
    raw: dict[str, Any] = loaded
    found: int | None = raw.get("_schema_version")
    if found is None or found > SCHEMA_VERSION:
        raise UnsupportedSchemaError(path, found, SCHEMA_VERSION)
    logger.debug("read_with_version path=%s version=%d", path, found)
    return raw
