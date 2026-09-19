"""In the bundled .app, the user's own .env must win over shipped defaults.

``pydantic-settings`` reads ``env_file`` tuples in order and each file updates
the accumulated mapping, so the LAST entry has the highest precedence — which
is why the dev-mode branch ends with ``.env.local``. The bundled branch listed
the user's Application Support file FIRST while its comment claimed it was
"highest priority", so inside the .app the shipped defaults silently overrode
whatever the user had configured. That is the failure mode CLAUDE.md rule 21
exists for: environment config quietly beating everything else.
"""

from __future__ import annotations

import sys

import pytest

from cortex.libs.config.settings import _bundled_env_files


def test_dotenv_precedence_is_last_file_wins() -> None:
    """Pin the library behaviour the ordering depends on."""
    import tempfile
    from pathlib import Path

    from pydantic_settings import BaseSettings, SettingsConfigDict

    first = Path(tempfile.mkdtemp()) / "first.env"
    second = Path(tempfile.mkdtemp()) / "second.env"
    first.write_text("PROBE_VALUE=1111\n", encoding="utf-8")
    second.write_text("PROBE_VALUE=2222\n", encoding="utf-8")

    class _Probe(BaseSettings):
        model_config = SettingsConfigDict(
            env_file=(str(first), str(second)), extra="ignore",
        )
        probe_value: str = "unset"

    assert _Probe().probe_value == "2222", (
        "if this fails the ordering below must be inverted with it"
    )


def test_user_env_is_last_when_bundled(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    meipass = tmp_path / "meipass"
    home = tmp_path / "home"
    meipass.mkdir()
    (home / "Library" / "Application Support" / "Cortex").mkdir(parents=True)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr("cortex.libs.config.settings.Path.home", lambda: home)

    files = _bundled_env_files()

    assert len(files) == 2
    assert files[0].startswith(str(meipass)), "bundled defaults must come first"
    assert files[-1].endswith("Application Support/Cortex/.env"), (
        "the user's override must be last so it wins"
    )


def test_dev_mode_keeps_local_override_last() -> None:
    monkeypatch_free = _bundled_env_files()
    # Not frozen in the test process, so this is the dev branch.
    assert monkeypatch_free == (".env", ".env.local")
