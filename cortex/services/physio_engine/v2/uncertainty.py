"""Honest labelling for heuristic (non-statistical) estimate bounds.

Several v2 estimates attach an interval that is a *deterministic heuristic
bound* (spectral resolution, window quality, channel disagreement) rather
than a statistical interval with a coverage probability. The wire schema
(:class:`cortex.libs.schemas.physiology.EstimateUncertainty`) expresses the
difference explicitly: heuristic bounds carry ``interval_kind="heuristic"``
and no ``confidence_level``; statistical intervals (for example the
deterministic moving-block bootstrap in
:mod:`cortex.services.physio_engine.v2.hrv`) carry their real level.

``method`` is additionally prefixed with :data:`HEURISTIC_METHOD_PREFIX` so
that a human reading a payload sees the kind without consulting the schema.
"""

from __future__ import annotations

from typing import Literal

from cortex.libs.schemas.physiology import EstimateUncertainty

IntervalKind = Literal["heuristic", "statistical"]

HEURISTIC_METHOD_PREFIX = "heuristic:"
"""``method`` prefix that marks a bound as non-statistical."""


def heuristic_interval(
    *,
    lower: float,
    upper: float,
    method: str,
) -> EstimateUncertainty:
    """Build a bound that is explicitly labelled as heuristic."""

    if not method:
        raise ValueError("heuristic interval method must be non-empty")
    if method.startswith(HEURISTIC_METHOD_PREFIX):
        labelled = method
    else:
        labelled = f"{HEURISTIC_METHOD_PREFIX}{method}"
    return EstimateUncertainty(
        lower=float(lower),
        upper=float(upper),
        confidence_level=None,
        interval_kind="heuristic",
        method=labelled,
    )


def interval_kind(uncertainty: EstimateUncertainty | None) -> IntervalKind | None:
    """Return the declared kind of an interval, or ``None`` when absent."""

    if uncertainty is None:
        return None
    return uncertainty.interval_kind
