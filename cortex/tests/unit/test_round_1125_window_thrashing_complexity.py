"""Window thrashing must reach the workspace-complexity score.

``compute_complexity_score`` averages seven factors; factor 4 is the
telemetry window-switch rate, fed through
``ContextAssembler.set_window_switch_rate``. The daemon builds the assembler
without a rate and nothing ever calls the setter, so the factor was a
constant zero: a user switching windows 30 times a minute produced the same
complexity (and the same ``complexity_threshold`` trigger gate) as one who
never left the editor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("CORTEX_STORAGE__PATH", str(storage))
    from cortex.libs.config import settings as settings_mod

    settings_mod.get_config.cache_clear()
    yield storage
    settings_mod.get_config.cache_clear()


def test_sampled_window_switch_rate_raises_context_complexity(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cortex.libs.config.settings import get_config
    from cortex.services.runtime_daemon import CortexDaemon

    daemon = CortexDaemon(config=get_config())
    baseline = daemon._aggregator.build_features()
    thrashing = baseline.model_copy(update={"window_switch_rate": 30.0})
    monkeypatch.setattr(daemon._aggregator, "build_features", lambda: thrashing)

    async def scenario() -> float:
        await daemon._sample_telemetry()
        await daemon._refresh_context()
        return float(daemon._latest_context.complexity_score)

    complexity = asyncio.run(scenario())
    # With no editor/browser/terminal context, the only non-zero factor is
    # window thrashing at its 30/min ceiling: 1/7 of the mean.
    assert complexity == pytest.approx(1.0 / 7.0)
