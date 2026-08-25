"""Verify a built macOS DMG, its app, architecture, signature, and smoke path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cortex import __version__

_SECRET_PATTERNS = (
    b"sk-ant-",
    b"AWS_SECRET_ACCESS_KEY=",
    b"AWS_BEARER_TOKEN_BEDROCK=",
    b"ANTHROPIC_API_KEY=",
    b"/Users/",
    b"/home/",
    b"C:\\Users\\",
)


class ReleaseVerificationError(RuntimeError):
    """The artifact failed a release invariant."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run(
    command: list[str],
    *,
    check: bool = True,
    timeout: float = 60.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    result = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-8000:],
    }
    if check and completed.returncode != 0:
        raise ReleaseVerificationError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stderr[-2000:]}"
        )
    return result


def _scan_forbidden(root: Path) -> list[str]:
    findings: list[str] = []
    overlap = max(len(pattern) for pattern in _SECRET_PATTERNS) - 1
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as handle:
                tail = b""
                matched: set[bytes] = set()
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    searchable = (tail + block).lower()
                    matched.update(
                        pattern for pattern in _SECRET_PATTERNS if pattern.lower() in searchable
                    )
                    tail = searchable[-overlap:] if overlap else b""
        except OSError:
            continue
        findings.extend(
            f"{path.relative_to(root)} contains {pattern!r}" for pattern in sorted(matched)
        )
    return findings


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def verify(
    artifact: Path,
    *,
    expected_arch: str,
    require_notarized: bool,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise ReleaseVerificationError("macOS artifact verification requires Darwin")
    artifact = artifact.resolve()
    if not artifact.is_file() or artifact.suffix.lower() != ".dmg":
        raise ReleaseVerificationError(f"DMG is missing: {artifact}")
    expected_name = f"Cortex-{__version__}-macos-{expected_arch}.dmg"
    if artifact.name != expected_name:
        raise ReleaseVerificationError(
            f"artifact name {artifact.name!r} does not match {expected_name!r}"
        )

    evidence: dict[str, Any] = {
        "schema_version": "1.0",
        "verified_at_utc": datetime.now(UTC).isoformat(),
        "artifact": str(artifact),
        "artifact_sha256": sha256_file(artifact),
        "expected_arch": expected_arch,
        "require_notarized": require_notarized,
        "commands": [],
    }
    commands: list[dict[str, Any]] = evidence["commands"]
    commands.append(_run(["hdiutil", "verify", str(artifact)], timeout=180.0))

    with tempfile.TemporaryDirectory(prefix="cortex-release-") as raw_mount:
        mount = Path(raw_mount)
        commands.append(
            _run(
                [
                    "hdiutil",
                    "attach",
                    "-nobrowse",
                    "-readonly",
                    "-mountpoint",
                    str(mount),
                    str(artifact),
                ],
                timeout=180.0,
            )
        )
        try:
            app = mount / "Cortex.app"
            plist_path = app / "Contents/Info.plist"
            executable = app / "Contents/MacOS/Cortex"
            if not plist_path.is_file() or not executable.is_file():
                raise ReleaseVerificationError("DMG does not contain a valid Cortex.app")
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)
            required_plist = {
                "CFBundleIdentifier": "com.cortex.daemon",
                "CFBundleShortVersionString": __version__,
                "CFBundleVersion": __version__,
                "LSMinimumSystemVersion": "13.0",
            }
            mismatches = {
                key: {"expected": expected, "actual": plist.get(key)}
                for key, expected in required_plist.items()
                if plist.get(key) != expected
            }
            if mismatches:
                raise ReleaseVerificationError(f"Info.plist mismatch: {mismatches}")
            evidence["info_plist"] = required_plist

            arch_result = _run(["lipo", "-archs", str(executable)])
            commands.append(arch_result)
            architectures = arch_result["stdout"].strip().split()
            if architectures != [expected_arch]:
                raise ReleaseVerificationError(
                    f"expected only architecture {expected_arch}, got {architectures}"
                )
            commands.append(
                _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
            )
            commands.append(_run(["codesign", "-dv", "--verbose=4", str(app)]))

            findings = _scan_forbidden(app)
            if findings:
                raise ReleaseVerificationError(
                    "release bundle contains forbidden credential/personal-path patterns: "
                    + "; ".join(findings[:20])
                )
            evidence["forbidden_pattern_scan"] = "passed"

            smoke_env = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "QT_QPA_PLATFORM": "offscreen",
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            }
            commands.append(
                _run(
                    [str(executable), "--release-smoke"],
                    timeout=45.0,
                    env=smoke_env,
                )
            )
        finally:
            commands.append(_run(["hdiutil", "detach", str(mount)], check=False, timeout=60.0))

    if require_notarized:
        commands.append(_run(["xcrun", "stapler", "validate", str(artifact)]))
        commands.append(
            _run(
                [
                    "spctl",
                    "-a",
                    "-vv",
                    "--type",
                    "open",
                    "--context",
                    "context:primary-signature",
                    str(artifact),
                ]
            )
        )
    evidence["status"] = "passed"
    return evidence


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--expected-arch", choices=("arm64", "x86_64"), default=platform.machine())
    parser.add_argument("--require-notarized", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        evidence = verify(
            args.artifact,
            expected_arch=args.expected_arch,
            require_notarized=args.require_notarized,
        )
    except (OSError, ReleaseVerificationError, subprocess.SubprocessError) as exc:
        print(f"release verification FAILED: {exc}", file=sys.stderr)
        return 1
    if args.output is not None:
        _write_json(args.output, evidence)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
