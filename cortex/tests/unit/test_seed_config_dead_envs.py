"""Audit F38 + F39 — regression guard for dead LLM provider config.

Cortex moved off Azure OpenAI / self-hosted Qwen / local Ollama when it
migrated to the Anthropic SDK in v0.2.0. The env vars that backed those
providers (``CORTEX_LLM__MODE``, ``CORTEX_LLM__AZURE__*``,
``CORTEX_LLM__REMOTE__*``, ``CORTEX_LLM__LOCAL__*``,
``CORTEX_LLM__MODEL_NAME``, the ``qwen3-8b`` / ``llama3.1`` model
hints) are no longer read by the code, but they kept resurfacing in
shipped templates and user-facing docs — silently misleading users to
fill in credentials that go nowhere.

This test fails if any of the dead names reappears in:

* ``cortex/scripts/seed_config.py`` — what the seeder emits.
* ``cortex/.env.example`` — what users copy into ``.env``.

User-facing docs (``README.md``, ``Setup.md``, ``Architecture.md``) are
exercised in a softer assertion: the dead names may still appear inside
a deprecation footnote, but the live provider table must include the
three real provider names.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


# Names that v0.2.0 deletion stranded. Any reappearance in a shipped
# template is a regression — the validator silently maps stale values
# to the rule-based fallback, which is worse than failing loudly
# because the user is left thinking they configured an LLM.
#
# Each entry is matched as a *whole token* against the cleaned source
# (see :func:`_dead_name_appears`) so e.g. ``CORTEX_LLM__MODE`` does NOT
# match the live ``CORTEX_LLM__MODEL_DEFAULT`` env var.
DEAD_ENV_NAMES = (
    "CORTEX_LLM__MODE",
    "CORTEX_LLM__AZURE",  # any AZURE__* subkey
    "CORTEX_LLM__REMOTE",
    "CORTEX_LLM__LOCAL",
    "CORTEX_LLM__MODEL_NAME",
)


# Dead values (model IDs, deployment names) that pin the wrong provider
# semantics even if the env var name was renamed. Listed separately so
# the failure message tells the reader *what* dead identifier they
# accidentally reintroduced.
DEAD_VALUES = (
    "qwen3-8b",
    "llama3.1:8b",
    "gpt-4o-mini",
    "gpt-5-mini",
)


def _dead_name_appears(name: str, scanned: str) -> bool:
    """True iff ``name`` appears as a complete env-var token in ``scanned``.

    We anchor each match with ``(?![A-Z0-9_])`` so ``CORTEX_LLM__MODE``
    is found, ``CORTEX_LLM__MODEL_DEFAULT`` is not.
    """
    return re.search(re.escape(name) + r"(?![A-Z0-9_])", scanned) is not None


def _strip_comments(text: str) -> str:
    """Drop ``#`` line comments so a deprecation note doesn't trigger a hit.

    A line whose first non-whitespace character is ``#`` is dropped
    entirely. Inline trailing comments (``KEY=value  # note``) are also
    truncated. Block strings inside Python source are preserved — we
    only drop ``#`` shell-comments, which is the format used in
    ``.env`` templates and in the heredoc emitted by ``seed_config.py``.
    """
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # Trailing comment after the value.
        if "#" in line:
            line = line.split("#", 1)[0]
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def test_seed_config_has_no_dead_env_names() -> None:
    """seed_config.py must not emit any legacy provider env var."""
    source = (REPO_ROOT / "cortex" / "scripts" / "seed_config.py").read_text()
    scanned = _strip_comments(source)
    for dead in DEAD_ENV_NAMES:
        assert not _dead_name_appears(dead, scanned), (
            f"seed_config.py reintroduced dead env var {dead!r}; "
            f"validator silently maps it to the rule-based fallback so "
            f"users think they have an LLM when they don't."
        )


def test_seed_config_has_no_dead_values() -> None:
    source = (REPO_ROOT / "cortex" / "scripts" / "seed_config.py").read_text()
    scanned = _strip_comments(source)
    for value in DEAD_VALUES:
        assert value not in scanned, (
            f"seed_config.py reintroduced dead provider value {value!r}; "
            f"the corresponding provider was removed in v0.2.0."
        )


def test_env_example_has_no_dead_env_names() -> None:
    """The shipped ``cortex/.env.example`` must not list dead env vars.

    Deprecation footnotes / comments are allowed — they help users on
    0.1.x ``.env`` files realise why their old setting is being
    ignored — so the check skips ``#`` comment lines via
    :func:`_strip_comments`.
    """
    env_example = REPO_ROOT / "cortex" / ".env.example"
    text = env_example.read_text()
    scanned = _strip_comments(text)
    for dead in DEAD_ENV_NAMES:
        assert not _dead_name_appears(dead, scanned), (
            f".env.example reintroduced dead env var {dead!r} outside a "
            f"comment block; users copying this template will fill in "
            f"credentials that go nowhere."
        )


def test_env_example_has_no_dead_values() -> None:
    text = (REPO_ROOT / "cortex" / ".env.example").read_text()
    scanned = _strip_comments(text)
    for value in DEAD_VALUES:
        assert value not in scanned, (
            f".env.example reintroduced dead provider value {value!r}."
        )


def test_env_example_lists_real_providers() -> None:
    """Positive check: the file names a real ``CORTEX_LLM__PROVIDER`` value."""
    text = (REPO_ROOT / "cortex" / ".env.example").read_text()
    assert "CORTEX_LLM__PROVIDER" in text, (
        ".env.example must document the canonical CORTEX_LLM__PROVIDER selector."
    )
    # The three currently-supported transports.
    provider_match = re.search(
        r"CORTEX_LLM__PROVIDER\s*=\s*(bedrock|vertex|direct)", text
    )
    assert provider_match is not None, (
        "CORTEX_LLM__PROVIDER must be set to bedrock, vertex, or direct."
    )


def test_docs_name_the_three_real_providers() -> None:
    """README, Setup, and Architecture each mention the real provider names.

    This is the F39 doc-drift guard: the LLM-provider section must
    name ``bedrock``, ``vertex``, and ``direct`` (or their
    user-friendly forms ``AWS Bedrock``, ``GCP Vertex``,
    ``Anthropic API``) so a reader is never sent looking for a
    provider Cortex no longer supports.
    """
    expected_markers = ("Bedrock", "Vertex", "Anthropic")
    for doc_name in ("README.md", "Setup.md", "Architecture.md"):
        doc = (REPO_ROOT / doc_name).read_text()
        missing = [m for m in expected_markers if m not in doc]
        assert not missing, (
            f"{doc_name} is missing live-provider markers {missing}; "
            f"users will be sent looking for providers that don't exist."
        )


def test_docs_mention_the_provider_selector_env_var() -> None:
    """At least one doc names ``ANTHROPIC_PROVIDER`` / ``CORTEX_LLM__PROVIDER``.

    The audit prompt requires that the rewritten LLM-provider section
    names the env var that selects the transport.
    """
    found = False
    for doc_name in ("README.md", "Setup.md"):
        doc = (REPO_ROOT / doc_name).read_text()
        if "CORTEX_LLM__PROVIDER" in doc or "ANTHROPIC_PROVIDER" in doc:
            found = True
            break
    assert found, (
        "Neither README.md nor Setup.md documents the provider-selector "
        "env var; users won't know how to switch transports."
    )
