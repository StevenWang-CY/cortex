"""Canonical-version drift and runtime projection tests."""

from __future__ import annotations

from cortex import __version__
from cortex.scripts.sync_versions import canonical_version, check
from cortex.services.api_gateway.app import create_app


def test_all_generated_version_surfaces_are_current() -> None:
    version = canonical_version()
    assert check(version) == []
    assert __version__ == version


def test_fastapi_reports_canonical_version() -> None:
    assert create_app().version == canonical_version()
