"""Release-time guards that have to hold outside a release.

* ``build-release:308`` — the icon pipeline's "do NOT silently produce a
  brand-less build" FATAL guard was unreachable in exactly the case it was
  written for. ``PRIMARY_FAILED`` and ``FALLBACK_FAILED`` are set in the two
  mutually exclusive branches of ``if command -v rsvg-convert``, so requiring
  both was unsatisfiable; and when rendering failed, ``TEMP_PNG`` was cleared,
  which skips the only block that sets ``ICON_BUILD_FAILED=1``. Both FATAL
  branches were dead and the script exited 0 with no ``.icns``.

* ``build-release:372`` — the published release body told downloaders to run
  a checksum command that exits non-zero on a correct download.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_BUILD_SCRIPT = _ROOT / "cortex" / "scripts" / "build_macos_app.sh"

_GUARD_BEGIN = "# cortex:icon-guard:begin"
_GUARD_END = "# cortex:icon-guard:end"


def _icon_guard_source() -> str:
    """The guard as it is written in the build script, not a copy of it."""
    text = _BUILD_SCRIPT.read_text()
    assert _GUARD_BEGIN in text and _GUARD_END in text, (
        "the icon guard markers are gone from build_macos_app.sh; this test "
        "executes the real guard text and cannot find it"
    )
    start = text.index(_GUARD_BEGIN) + len(_GUARD_BEGIN)
    return text[start : text.index(_GUARD_END)]


def _run_guard(*, icns: Path, primary: str, fallback: str, packaging: str) -> int:
    script = (
        "set -u\n"
        f'ICON_ICNS="{icns}"\n'
        f'ICON_SVG="/nonexistent/logo.svg"\n'
        f'PRIMARY_FAILED="{primary}"\n'
        f'FALLBACK_FAILED="{fallback}"\n'
        f'ICON_BUILD_FAILED="{packaging}"\n'
        + _icon_guard_source()
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False,
    ).returncode


def test_the_build_script_exists() -> None:
    assert _BUILD_SCRIPT.is_file()


def test_bash_still_parses_the_build_script() -> None:
    completed = subprocess.run(
        ["bash", "-n", str(_BUILD_SCRIPT)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "primary,fallback,packaging,label",
    [
        ("1", "0", "0", "rsvg-convert present and failed"),
        ("0", "1", "0", "rsvg-convert absent, qlmanage failed"),
        ("0", "0", "1", "sips or iconutil failed"),
        ("0", "0", "0", "nothing reported a failure, yet no icon exists"),
    ],
)
def test_a_missing_icon_fails_the_build(
    tmp_path: Path, primary: str, fallback: str, packaging: str, label: str,
) -> None:
    """Every route to a missing .icns must stop the build.

    The first case is the one that shipped: rsvg-convert present and failing
    set ``PRIMARY_FAILED=1`` and cleared ``TEMP_PNG``, which left
    ``FALLBACK_FAILED`` and ``ICON_BUILD_FAILED`` both 0 — so neither guard
    fired and PyInstaller ran on with no icon at all.
    """
    missing = tmp_path / "AppIcon.icns"
    assert _run_guard(
        icns=missing, primary=primary, fallback=fallback, packaging=packaging,
    ) == 1, f"{label}: a missing icon must fail the build"


def test_an_empty_icon_fails_the_build(tmp_path: Path) -> None:
    """A zero-byte .icns is a failed pipeline wearing the right filename."""
    empty = tmp_path / "AppIcon.icns"
    empty.write_bytes(b"")
    assert _run_guard(icns=empty, primary="0", fallback="0", packaging="0") == 1


def test_a_real_icon_passes(tmp_path: Path) -> None:
    """The guard tests the artefact, so a produced icon passes regardless of
    which renderer reported trouble along the way."""
    icns = tmp_path / "AppIcon.icns"
    icns.write_bytes(b"icns\x00\x00\x01\x00")
    assert _run_guard(icns=icns, primary="1", fallback="0", packaging="1") == 0


# ── build-release:372 ───────────────────────────────────────────────────


def _published_checksum_command(manifest_name: str) -> str:
    """The checksum command exactly as the public release body states it."""
    from cortex.scripts.validate_release_records import render_assurance_notes

    notes = render_assurance_notes({
        "assurance_tier": "self-attested",
        "hardware_verified_architectures": [],
        "ci_only_architectures": [],
        "hosts": {},
        "cases_not_run": {},
    })
    assert "shasum -a 256" in notes
    # Pulled out of the rendered text rather than restated, so the test
    # cannot drift from what downloaders are actually told to run.
    command = next(
        segment
        for segment in notes.replace("`", "\n").splitlines()
        if segment.startswith("shasum -a 256")
    )
    return command.replace("SHA256SUMS-<arch>", manifest_name)


def test_the_published_checksum_command_succeeds_on_a_dmg_only_download(
    tmp_path: Path,
) -> None:
    """Run the exact command from the release body against a real manifest.

    ``generate_release_evidence`` writes a line for every file it discovered
    in the evidence directory, and those files are published only inside the
    separate evidence ZIP. The release body used to say
    ``shasum -a 256 -c SHA256SUMS-<arch>`` with no qualification, so a reader
    who downloaded the DMG and the checksum file — the natural reading — got
    a "No such file or directory" per evidence file and a non-zero exit on a
    perfectly good download.
    """
    dmg = tmp_path / "Cortex-0.5.0-macos-arm64.dmg"
    dmg.write_bytes(b"not really a disk image")
    digest = subprocess.run(
        ["shasum", "-a", "256", dmg.name],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    manifest = tmp_path / "SHA256SUMS-arm64"
    manifest.write_text(
        digest
        # The rest of the evidence bundle, which ships inside the ZIP.
        + "0" * 64 + "  cortex-app.spdx.json\n"
        + "0" * 64 + "  python-lock.cdx.json\n"
        + "0" * 64 + "  release-verification.json\n"
        + "0" * 64 + "  release-metadata.json\n"
    )

    command = _published_checksum_command(manifest.name)

    completed = subprocess.run(
        ["bash", "-c", command], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, (
        "the command published to downloaders fails on a correct DMG-only "
        f"download:\n{completed.stdout}{completed.stderr}"
    )
    assert dmg.name in completed.stdout


def test_the_published_checksum_command_still_fails_on_a_bad_download(
    tmp_path: Path,
) -> None:
    """Tolerating absent evidence files must not tolerate a corrupt DMG."""
    dmg = tmp_path / "Cortex-0.5.0-macos-arm64.dmg"
    dmg.write_bytes(b"tampered")
    manifest = tmp_path / "SHA256SUMS-arm64"
    manifest.write_text(
        "0" * 64 + f"  {dmg.name}\n" + "0" * 64 + "  cortex-app.spdx.json\n"
    )

    command = _published_checksum_command(manifest.name)

    assert subprocess.run(
        ["bash", "-c", command], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).returncode != 0


def test_the_published_checksum_command_fails_when_the_dmg_is_absent(
    tmp_path: Path,
) -> None:
    """Nothing to check is a failure, not a pass.

    This is what makes tolerating missing files safe: a mistyped or
    unmoved DMG cannot be mistaken for a verified one.
    """
    manifest = tmp_path / "SHA256SUMS-arm64"
    manifest.write_text("0" * 64 + "  cortex-app.spdx.json\n")

    command = _published_checksum_command(manifest.name)

    assert subprocess.run(
        ["bash", "-c", command], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).returncode != 0
