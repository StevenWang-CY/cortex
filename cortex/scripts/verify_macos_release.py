"""Verify a built macOS DMG, its app, architecture, signature, and smoke path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cortex import __version__

_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "anthropic-api-key",
        re.compile(rb"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{16,512}(?![A-Za-z0-9_-])"),
    ),
    (
        "openai-api-key",
        re.compile(
            rb"(?<![A-Za-z0-9_-])sk-(?!ant-)(?:proj-|svcacct-)?"
            rb"[A-Za-z0-9_-]{20,512}"
            rb"(?![A-Za-z0-9_-])"
        ),
    ),
    (
        "aws-access-key-id",
        re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    ),
    (
        "credential-assignment",
        re.compile(
            rb"\b(?:AWS_SECRET_ACCESS_KEY|AWS_BEARER_TOKEN_BEDROCK|ANTHROPIC_API_KEY)"
            rb"\s*=\s*['\"]?[A-Za-z0-9_./+=-]{16,512}",
            re.IGNORECASE,
        ),
    ),
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "github-token",
        re.compile(
            rb"(?<![A-Za-z0-9])(?:"
            rb"gh[pour]_[A-Za-z0-9]{36,255}|"
            rb"ghs_[A-Za-z0-9_.-]{20,2048}|"
            rb"github_pat_[A-Za-z0-9_]{22,255}"
            rb")(?![A-Za-z0-9_.-])"
        ),
    ),
    (
        "slack-token",
        re.compile(
            rb"(?<![A-Za-z0-9])(?:xox(?:a|b|p|r|s)|xapp|xwfp)-"
            rb"[A-Za-z0-9-]{10,512}(?![A-Za-z0-9-])"
        ),
    ),
)
# Must exceed the longest accepted credential form so a match cannot evade the
# scanner by straddling two 1 MiB reads.
_SCAN_OVERLAP_BYTES = 4096
_NON_PERSONAL_BUILD_USERS = frozenset({"root", "runner", "runneradmin"})


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


def _default_personal_roots() -> tuple[str, ...]:
    """Return local home roots worth treating as personal build metadata.

    GitHub-hosted runner homes are intentionally generic and routinely appear
    in debug strings embedded by third-party wheels. A developer's own home is
    not generic and must never leak into a distributable bundle.
    """

    roots: list[str] = []
    for variable in ("HOME", "USERPROFILE"):
        value = os.environ.get(variable, "").strip().rstrip("/\\")
        if not value:
            continue
        username = re.split(r"[/\\]", value)[-1].casefold()
        if username in _NON_PERSONAL_BUILD_USERS:
            continue
        if value not in roots:
            roots.append(value)
    return tuple(roots)


def _scan_forbidden(
    root: Path,
    *,
    personal_roots: tuple[str, ...] | None = None,
) -> list[str]:
    """Scan for complete credentials and non-generic local home paths.

    Prefix-only matching is deliberately rejected: Cortex ships redaction
    rules containing strings such as ``sk-ant-``, while native dependencies
    contain generic ``/Users/runner`` debug paths. Credential expressions are
    length-bounded and path checks target only the actual non-generic build
    homes (or explicit roots supplied by a test/caller).
    """

    findings: list[str] = []
    roots = _default_personal_roots() if personal_roots is None else personal_roots
    encoded_roots = tuple(
        (candidate + separator).encode("utf-8")
        for root_value in roots
        if (candidate := root_value.strip().rstrip("/\\"))
        for separator in ("/", "\\")
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        try:
            with path.open("rb") as handle:
                tail = b""
                matched_credentials: set[str] = set()
                matched_local_path = False
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    searchable = tail + block
                    matched_credentials.update(
                        name
                        for name, pattern in _CREDENTIAL_PATTERNS
                        if pattern.search(searchable) is not None
                    )
                    if any(root_bytes in searchable for root_bytes in encoded_roots):
                        matched_local_path = True
                    tail = searchable[-_SCAN_OVERLAP_BYTES:]
        except OSError as exc:
            findings.append(f"{relative} could not be scanned ({type(exc).__name__})")
            continue
        findings.extend(
            f"{relative} matches credential rule {name}" for name in sorted(matched_credentials)
        )
        if matched_local_path:
            findings.append(f"{relative} contains a non-generic local home path")
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
