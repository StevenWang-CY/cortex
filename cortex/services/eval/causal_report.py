"""Deprecated compatibility shim for the pre-WP8 report entry point.

The old function name is retained only so third-party imports fail safely into
a plainly labelled legacy diagnostic. New code must use
``generate_daily_policy_diagnostics`` or the separately prespecified MRT
pipeline.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from cortex.application.clock import SYSTEM_CLOCK
from cortex.services.eval.policy_diagnostics import generate_legacy_policy_diagnostics


def generate_daily_causal_report(storage_root: str, day: str | None = None) -> Path:
    warnings.warn(
        "generate_daily_causal_report is deprecated; output is legacy policy diagnostics, "
        "not a causal report",
        DeprecationWarning,
        stacklevel=2,
    )
    return generate_legacy_policy_diagnostics(
        storage_root,
        day=day or SYSTEM_CLOCK.today_utc().isoformat(),
    )


__all__ = ["generate_daily_causal_report"]
