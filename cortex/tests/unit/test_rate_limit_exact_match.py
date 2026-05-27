"""P1-3: _normalise_route uses exact-match only.

Before the fix, ``_normalise_route`` used prefix matching with a
``path.startswith(route + "/")`` clause. This allowed crafted paths
like ``/shutdownX`` or ``/shutdown/extra`` to match the ``/shutdown``
bucket, creating a path-confusion bypass.

After the fix, only an exact-string match hits a bucket.

Asserts:
* ``/shutdownX`` returns ``None`` (no bucket matched).
* ``/shutdown/extra`` returns ``None``.
* ``/shutdown`` (exact) returns ``"shutdown"`` — still rate-limited.
* Other non-matching paths return ``None``.
"""

from __future__ import annotations

from cortex.services.api_gateway.middleware.rate_limit import (
    DEFAULT_LIMITS,
    _normalise_route,
)


def test_exact_shutdown_matches() -> None:
    assert _normalise_route("/shutdown", limits=DEFAULT_LIMITS) == "/shutdown"


def test_shutdown_with_suffix_no_match() -> None:
    """P1-3 regression: /shutdownX must NOT match the /shutdown bucket."""
    assert _normalise_route("/shutdownX", limits=DEFAULT_LIMITS) is None


def test_shutdown_with_path_suffix_no_match() -> None:
    """P1-3 regression: /shutdown/extra must NOT match the /shutdown bucket."""
    assert _normalise_route("/shutdown/extra", limits=DEFAULT_LIMITS) is None


def test_unrelated_path_no_match() -> None:
    assert _normalise_route("/health", limits=DEFAULT_LIMITS) is None


def test_llm_plan_exact_matches() -> None:
    assert _normalise_route("/llm/plan", limits=DEFAULT_LIMITS) == "/llm/plan"


def test_llm_plan_with_suffix_no_match() -> None:
    """Prefix clause removal is consistent across all bucket keys."""
    assert _normalise_route("/llm/plan/v2", limits=DEFAULT_LIMITS) is None


def test_empty_path_no_match() -> None:
    assert _normalise_route("", limits=DEFAULT_LIMITS) is None


def test_slash_only_no_match() -> None:
    assert _normalise_route("/", limits=DEFAULT_LIMITS) is None


def test_custom_limits_exact_match_only() -> None:
    """Works correctly with a caller-supplied limits dict too."""
    limits = {"/foo": 10, "/bar": 5}
    assert _normalise_route("/foo", limits=limits) == "/foo"
    assert _normalise_route("/foo/baz", limits=limits) is None
    assert _normalise_route("/foobar", limits=limits) is None
    assert _normalise_route("/bar", limits=limits) == "/bar"
