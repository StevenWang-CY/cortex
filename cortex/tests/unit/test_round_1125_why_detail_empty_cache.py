"""A "Why?" for an intervention with no strong signal must not be rewritten later.

When a plan is built the daemon attaches the top causal signals and caches
them per intervention id. If nothing exceeded the attribution floor at
trigger time the cached list is empty, which ``attribute_top_signals``
documents as the honest answer ("no strong observed contributor").

The popup sends ``WHY_DETAIL_REQUEST`` exactly when the plan it holds has no
causal signals. ``CortexDaemon.get_causal_signals`` tested the cache with
``if cached:``, so the empty list counted as a miss and the daemon answered
with a live attribution of the *current* feature vector -- signals from the
moment the user clicked, presented as the reason for an earlier
intervention.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from cortex.services.runtime_daemon import CortexDaemon


class _LiveAttributor:
    def __init__(self) -> None:
        self.calls = 0

    def attribute_top_signals(self, features: Any, baselines: Any) -> list[Any]:
        self.calls += 1
        return [SimpleNamespace(model_dump=lambda mode="json": {"name": "Tab switches"})]


def test_an_empty_trigger_time_rationale_is_returned_as_empty() -> None:
    attributor = _LiveAttributor()
    daemon = SimpleNamespace(
        _causal_signals_by_intervention={"iv-quiet": []},
        _current_feature_vector=lambda: object(),
        _causal_attributor=attributor,
        _scorer=SimpleNamespace(baselines=None),
    )

    signals = asyncio.run(CortexDaemon.get_causal_signals(daemon, "iv-quiet"))  # type: ignore[arg-type]

    assert signals == []
    assert attributor.calls == 0


def test_an_unknown_intervention_still_falls_back_to_live_attribution() -> None:
    attributor = _LiveAttributor()
    daemon = SimpleNamespace(
        _causal_signals_by_intervention={},
        _current_feature_vector=lambda: object(),
        _causal_attributor=attributor,
        _scorer=SimpleNamespace(baselines=None),
    )

    signals = asyncio.run(CortexDaemon.get_causal_signals(daemon, "iv-evicted"))  # type: ignore[arg-type]

    assert signals == [{"name": "Tab switches"}]
