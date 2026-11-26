"""``x.com`` must match X, not every domain that ends in "x.com".

``app_classifier.classify_tab_type`` matches social sites with the unanchored
pattern ``x\\.com``, so any host ending in ``…x.com`` -- dropbox.com,
nginx.com, fedex.com, box.com, vox.com, netflix.com -- is classified
``social``. The daemon runs this classifier over every ACTIVITY_SYNC entry
and records ``social`` hosts as distraction domains in the session report,
which the History tab lists as "top distractions" and the longitudinal
task-pattern rollup marks as triggers. (``tab_classifier.classify_tab``
already anchors the same pattern with ``(?<![a-z])``.)
"""

from __future__ import annotations

import pytest

from cortex.services.context_engine.app_classifier import classify_tab_type


@pytest.mark.parametrize(
    "url",
    [
        "https://www.dropbox.com/home",
        "https://nginx.com/resources/",
        "https://www.fedex.com/en-us/tracking.html",
        "https://app.box.com/folder/0",
    ],
)
def test_hosts_ending_in_x_dot_com_are_not_social(url: str) -> None:
    assert classify_tab_type(url) != "social"


@pytest.mark.parametrize("url", ["https://x.com/home", "https://twitter.com/home"])
def test_x_itself_is_still_social(url: str) -> None:
    assert classify_tab_type(url) == "social"
