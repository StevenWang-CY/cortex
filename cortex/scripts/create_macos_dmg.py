"""Create the canonical macOS DMG with bounded transient-error recovery."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

_TRANSIENT_CREATE_ERRORS = ("resource busy", "temporarily unavailable")
_HDIUTIL_ERRNO_RE = re.compile(
    r"(?:DIHLDiskImageCreate\(\) returned|hdiutil: create: returning)\s+(?P<errno>\d+)"
)
_TRANSIENT_ERRNOS = frozenset({16, 35})  # macOS EBUSY and EAGAIN


class DmgCreationError(RuntimeError):
    """Raised when the canonical ``hdiutil create`` operation cannot finish."""


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _remove_partial_output(output: Path) -> None:
    """Remove only the exact file/symlink owned by this DMG operation."""

    if not output.exists() and not output.is_symlink():
        return
    if not output.is_file() and not output.is_symlink():
        raise DmgCreationError(f"refusing to replace non-file DMG output: {output}")
    output.unlink()


def create_macos_dmg(
    *,
    volume_name: str,
    source_dir: Path,
    output: Path,
    evidence_dir: Path,
    max_attempts: int = 3,
    retry_delay_seconds: float = 5.0,
    runner: CommandRunner | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Create a compressed read-only DMG and return the successful attempt.

    Only known transient host errors are retried. Every attempt's merged output
    is retained in ``evidence_dir`` so release review can distinguish recovery
    from a clean first attempt. Non-transient failures remain fail-fast.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be non-negative")
    if not volume_name.strip():
        raise ValueError("volume_name must not be empty")
    if not source_dir.is_dir():
        raise DmgCreationError(f"DMG source directory does not exist: {source_dir}")
    if output.suffix.lower() != ".dmg":
        raise DmgCreationError(f"DMG output must end in .dmg: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    run_command = runner or _run
    command = [
        "hdiutil",
        "create",
        "-verbose",
        "-volname",
        volume_name,
        "-srcfolder",
        str(source_dir),
        "-ov",
        "-format",
        "UDZO",
        str(output),
    ]

    for attempt in range(1, max_attempts + 1):
        _remove_partial_output(output)

        result = run_command(command)
        transcript = result.stdout or ""
        evidence_path = evidence_dir / f"hdiutil-create-attempt-{attempt}.txt"
        evidence_path.write_text(transcript, encoding="utf-8")
        if transcript:
            print(transcript, end="" if transcript.endswith("\n") else "\n")

        if result.returncode == 0:
            if not output.is_file():
                raise DmgCreationError(
                    f"hdiutil reported success without producing the DMG: {output}"
                )
            return attempt

        _remove_partial_output(output)
        normalized = transcript.casefold()
        reported_errnos = {
            int(match.group("errno")) for match in _HDIUTIL_ERRNO_RE.finditer(transcript)
        }
        transient = any(marker in normalized for marker in _TRANSIENT_CREATE_ERRORS) or bool(
            reported_errnos & _TRANSIENT_ERRNOS
        )
        if not transient:
            raise DmgCreationError(
                f"hdiutil create failed with non-transient exit {result.returncode}; "
                f"see {evidence_path}"
            )
        if attempt == max_attempts:
            raise DmgCreationError(
                f"hdiutil create remained busy after {max_attempts} attempts; "
                f"see {evidence_path}"
            )

        delay = retry_delay_seconds * attempt
        print(
            f"[WARN] hdiutil create transient failure on attempt {attempt}/{max_attempts}; "
            f"retrying in {delay:g}s",
            file=sys.stderr,
        )
        sleeper(delay)

    raise AssertionError("unreachable DMG retry state")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume-name", required=True)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        attempt = create_macos_dmg(
            volume_name=args.volume_name,
            source_dir=args.source_dir,
            output=args.output,
            evidence_dir=args.evidence_dir,
            max_attempts=args.max_attempts,
            retry_delay_seconds=args.retry_delay_seconds,
        )
    except (DmgCreationError, ValueError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1
    print(f"DMG created successfully on attempt {attempt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
