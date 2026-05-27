"""P2-18: cortex/.env.example must cover all P0 §3.7 biology-break keys."""

from __future__ import annotations

from pathlib import Path

_ENV_EXAMPLE = Path(__file__).resolve().parent.parent.parent.parent / "cortex" / ".env.example"

# All §3.7 biology-break keys that must appear in .env.example.
# CORTEX_INTERVENTION__BIOLOGY_BREAK_AUDIO_MUTE_AFTER_MIC_SECONDS covers the
# mic-active audio-mute spec line (§3.7 risk mitigation, spec line 643).
_REQUIRED_KEYS = [
    "CORTEX_INTERVENTION__ENABLE_BIOLOGY_BREAK",
    "CORTEX_INTERVENTION__BIOLOGY_BREAK_AUDIO_MUTE_AFTER_MIC_SECONDS",
]


def _contents() -> str:
    return _ENV_EXAMPLE.read_text(encoding="utf-8")


def test_env_example_exists() -> None:
    assert _ENV_EXAMPLE.exists(), f".env.example not found at {_ENV_EXAMPLE}"


def test_biology_break_enable_key_present() -> None:
    assert "CORTEX_INTERVENTION__ENABLE_BIOLOGY_BREAK" in _contents(), (
        "cortex/.env.example must contain CORTEX_INTERVENTION__ENABLE_BIOLOGY_BREAK"
    )


def test_biology_break_audio_mute_key_present() -> None:
    assert "CORTEX_INTERVENTION__BIOLOGY_BREAK_AUDIO_MUTE_AFTER_MIC_SECONDS" in _contents(), (
        "cortex/.env.example must contain "
        "CORTEX_INTERVENTION__BIOLOGY_BREAK_AUDIO_MUTE_AFTER_MIC_SECONDS"
    )


def test_all_required_p0_keys_present() -> None:
    contents = _contents()
    missing = [k for k in _REQUIRED_KEYS if k not in contents]
    assert missing == [], (
        f"cortex/.env.example is missing §3.7 keys: {missing}"
    )
