"""Generate checksums and machine-readable provenance for release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cortex import __version__

_ROOT = Path(__file__).resolve().parents[2]


class ReleaseEvidenceError(RuntimeError):
    """Release inputs are incomplete or contradict repository state."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _command(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "unavailable (not found)"
    except OSError as exc:
        return f"unavailable (os error {exc.errno})"
    if completed.returncode != 0:
        return f"unavailable ({completed.returncode})"
    return (completed.stdout or completed.stderr).strip()


def artifact_record(path: Path, *, output_dir: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ReleaseEvidenceError(f"release input is missing: {path}")
    try:
        display_path = str(resolved.relative_to(output_dir.resolve()))
    except ValueError:
        display_path = resolved.name
    return {
        "path": display_path,
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def generate(
    artifact: Path,
    *,
    sboms: tuple[Path, ...],
    verification: Path | None,
    output_dir: Path,
    expected_tag: str | None,
    require_clean: bool,
    checksum_name: str = "SHA256SUMS",
) -> tuple[Path, Path]:
    if (
        not checksum_name
        or Path(checksum_name).name != checksum_name
        or not checksum_name.startswith("SHA256SUMS")
    ):
        raise ReleaseEvidenceError("checksum_name must be a basename beginning with 'SHA256SUMS'")
    output_dir.mkdir(parents=True, exist_ok=True)
    commit = _command(["git", "rev-parse", "HEAD"])
    dirty = bool(_command(["git", "status", "--porcelain"]))
    if require_clean and dirty:
        raise ReleaseEvidenceError("release provenance requires a clean Git checkout")
    actual_tag = _command(["git", "describe", "--tags", "--exact-match"])
    if expected_tag is not None and actual_tag != expected_tag:
        raise ReleaseEvidenceError(
            f"checked-out tag {actual_tag!r} does not match {expected_tag!r}"
        )
    requested = (artifact, *sboms, *((verification,) if verification is not None else ()))
    excluded_names = {"release-metadata.json", checksum_name}
    discovered = tuple(
        path
        for path in sorted(output_dir.iterdir())
        if path.is_file()
        and path.name not in excluded_names
        and not path.name.startswith("SHA256SUMS")
    )
    deduplicated: dict[Path, Path] = {}
    for path in (*requested, *discovered):
        deduplicated[path.resolve()] = path
    inputs = tuple(deduplicated.values())
    records = [artifact_record(path, output_dir=output_dir) for path in inputs]
    basename_counts: dict[str, int] = {}
    for path in inputs:
        basename_counts[path.name] = basename_counts.get(path.name, 0) + 1
    duplicate_basenames = sorted(name for name, count in basename_counts.items() if count > 1)
    if duplicate_basenames:
        raise ReleaseEvidenceError(
            "release checksum inputs require unique basenames: " + ", ".join(duplicate_basenames)
        )
    metadata = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "project": "cortex",
        "version": __version__,
        "git": {"commit": commit, "tag": actual_tag, "dirty": dirty},
        "builder": {
            "os": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "uv": _command(["uv", "--version"]),
            "node": _command(["node", "--version"]),
            "pnpm": _command(["pnpm", "--version"]),
            "xcode": _command(["xcodebuild", "-version"]),
        },
        "inputs": records,
        "reproducibility": {
            "python_lock": "cortex/uv.lock",
            "browser_lock": "cortex/apps/browser_extension/pnpm-lock.yaml",
            "vscode_lock": "cortex/apps/vscode_extension/package-lock.json",
            "note": "Signing/notarization timestamps make the final DMG traceable and repeatable from locked inputs, not byte-for-byte deterministic.",
        },
    }
    metadata_path = output_dir / "release-metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksum_paths = (*inputs, metadata_path)
    checksum_lines = [
        f"{sha256_file(path)}  {path.name}"
        for path in sorted(checksum_paths, key=lambda item: item.name)
    ]
    checksums_path = output_dir / checksum_name
    checksums_path.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return metadata_path, checksums_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, action="append", default=[])
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-tag")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--checksum-name", default="SHA256SUMS")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        metadata, checksums = generate(
            args.artifact,
            sboms=tuple(args.sbom),
            verification=args.verification,
            output_dir=args.output_dir,
            expected_tag=args.expected_tag,
            require_clean=args.require_clean,
            checksum_name=args.checksum_name,
        )
    except (OSError, ReleaseEvidenceError) as exc:
        print(f"release evidence FAILED: {exc}", file=sys.stderr)
        return 1
    print(metadata)
    print(checksums)
    return 0


if __name__ == "__main__":
    sys.exit(main())
