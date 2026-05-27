"""I2: ``get_config()`` must not leave the Bedrock bearer token in
``os.environ``.

Previously ``get_config()`` read the keychain token at module-import
time and wrote it to ``os.environ["AWS_BEARER_TOKEN_BEDROCK"]`` so the
Anthropic SDK could pick it up at construction time. That env mutation
leaked into every subprocess the daemon spawned (capture workers,
native messaging host re-launches, project launcher terminals); a
debugger attached to any descendant could read the long-lived bearer.

The new path:

* ``get_config()`` performs no token I/O.
* ``get_bedrock_token()`` consults a process-cached value or the
  Keychain on demand.
* ``bedrock_token_env_scope()`` is a context manager that scopes the
  env mutation to the SDK constructor call only.

This test asserts the env is clean after ``get_config()`` returns AND
that the context manager properly restores the prior env state.
"""

from __future__ import annotations

import os

import pytest

from cortex.libs.config import settings


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    """Force fresh state for each test — clear the cache, the env, and
    the module-level token cache."""
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    settings.get_config.cache_clear()
    settings._clear_bedrock_token_cache()
    yield
    settings.get_config.cache_clear()
    settings._clear_bedrock_token_cache()


def test_get_config_does_not_set_bedrock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of I2: after ``get_config()`` returns the env is
    clean. Even when the keychain *would* return a token, the bearer
    is not written into ``os.environ``."""
    # Stub the keychain helper so we never actually consult macOS.
    monkeypatch.setattr(
        "cortex.libs.utils.secrets.get_password_safe",
        lambda _service, _account: "fake-bedrock-token-do-not-leak",
    )
    settings.get_config()
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ


def test_bedrock_token_env_scope_restores_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bedrock_token_env_scope`` exposes the token to the SDK during
    the ``with`` block and removes it on exit. Critically, a prior
    user-supplied env value is restored, not clobbered."""
    monkeypatch.setattr(
        "cortex.libs.utils.secrets.get_password_safe",
        lambda _service, _account: "kc-token",
    )
    # Force provider=bedrock so the token cache populates.
    config = settings.get_config()
    if config.llm.provider != "bedrock":
        pytest.skip("default config does not use bedrock; skip")

    # Case 1: env was empty before scope; must be empty after.
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    with settings.bedrock_token_env_scope(config) as token:
        assert token == "kc-token"
        assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "kc-token"
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ


def test_bedrock_token_env_scope_preserves_prior_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a user already exported AWS_BEARER_TOKEN_BEDROCK in their
    shell, the context manager must not silently overwrite it on exit."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "shell-supplied-token")
    config = settings.get_config()
    if config.llm.provider != "bedrock":
        pytest.skip("default config does not use bedrock; skip")

    with settings.bedrock_token_env_scope(config) as token:
        # When env already carries a token, get_bedrock_token returns
        # the env value (user override beats keychain).
        assert token == "shell-supplied-token"
        assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "shell-supplied-token"
    # On exit the user-supplied value survives.
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "shell-supplied-token"
