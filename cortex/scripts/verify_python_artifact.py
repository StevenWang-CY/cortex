"""Fail a release when the Cortex wheel is empty, incomplete, or overbroad."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REQUIRED_MEMBERS = frozenset(
    {
        "cortex/__init__.py",
        "cortex/_version.py",
        "cortex/application/clock.py",
        "cortex/apps/desktop_shell/main.py",
        "cortex/libs/config/defaults.yaml",
        "cortex/models/face_landmarker.task",
        "cortex/scripts/run_dev.py",
        "cortex/services/runtime_daemon.py",
        "cortex/storage/database.py",
        "cortex/storage/migrations/0001_initial.sql",
    }
)
FORBIDDEN_PREFIXES = (
    "./",
    "tests/",
    "cortex/tests/",
    "cortex/apps/browser_extension/",
    "cortex/apps/vscode_extension/",
)


def verify_wheel(path: Path) -> tuple[int, int]:
    """Return member/byte counts after enforcing the release allowlist."""

    if not path.is_file() or path.suffix != ".whl":
        raise ValueError(f"wheel path does not exist or is not a .whl: {path}")
    with zipfile.ZipFile(path) as archive:
        infos = tuple(archive.infolist())
        names = {info.filename for info in infos if not info.is_dir()}
    missing = sorted(REQUIRED_MEMBERS - names)
    if missing:
        raise ValueError(f"wheel is missing required runtime members: {missing}")
    forbidden = sorted(
        name
        for name in names
        if name.startswith(FORBIDDEN_PREFIXES)
        or name.endswith((".pyc", ".DS_Store", ".env"))
        or "/__pycache__/" in name
    )
    if forbidden:
        raise ValueError(f"wheel contains forbidden source/artifacts: {forbidden[:20]}")
    metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
    if len(metadata) != 1:
        raise ValueError("wheel must contain exactly one distribution METADATA file")
    return len(names), sum(info.file_size for info in infos if not info.is_dir())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        members, unpacked_bytes = verify_wheel(args.wheel)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"python artifact verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"python artifact verified: {args.wheel.name} "
        f"({members} files, {unpacked_bytes} unpacked bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

