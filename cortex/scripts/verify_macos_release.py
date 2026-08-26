"""Verify a built macOS DMG, its app, architecture, signature, and smoke path."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import platform
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
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
        # AWS documentation reserves values ending in EXAMPLE for public
        # fixtures (for example AKIAIOSFODNN7EXAMPLE). Those fixtures ship in
        # boto3/botocore and are not credentials.
        re.compile(
            rb"(?<![A-Z0-9])(?:AKIA|ASIA)(?![0-9A-Z]{9}EXAMPLE)"
            rb"[0-9A-Z]{16}(?![A-Z0-9])"
        ),
    ),
    (
        "credential-assignment",
        re.compile(
            # AWS secret access keys are exactly 40 base64-like characters.
            # The bearer token is variable length but is still an opaque value,
            # not a dotted Python expression or same-named SDK constant.
            rb"\b(?:"
            rb"AWS_SECRET_ACCESS_KEY\s*=\s*['\"]?[A-Za-z0-9/+=]{40}"
            rb"(?![A-Za-z0-9/+=])|"
            rb"AWS_BEARER_TOKEN_BEDROCK\s*=\s*['\"]?"
            rb"(?!AWS_BEARER_TOKEN_BEDROCK\b)[A-Za-z0-9_+/=-]{20,512}"
            rb"(?![A-Za-z0-9_+/=-])"
            rb")",
            re.IGNORECASE,
        ),
    ),
    (
        "private-key",
        # Crypto/TLS libraries legitimately embed PEM parser marker strings.
        # Require a complete PEM payload with a matching footer.
        re.compile(
            rb"-----BEGIN (?P<kind>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?"
            rb"PRIVATE KEY)-----\r?\n"
            rb"(?:(?:Proc-Type|DEK-Info):[^\r\n]{1,200}\r?\n){0,2}(?:\r?\n)?"
            rb"(?:"
            # Compact PKCS#8 Ed25519 keys can have one 64-character line.
            rb"[A-Za-z0-9+/]{40,76}={0,2}\r?\n|"
            # Larger keys have at least two lines; the final line may be short.
            rb"[A-Za-z0-9+/]{16,76}\r?\n"
            rb"(?:[A-Za-z0-9+/]{4,76}={0,2}\r?\n){1,511}"
            rb")"
            rb"-----END (?P=kind)-----"
        ),
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
# Must exceed the longest accepted generic credential form (including a
# complete PEM block) so a match cannot evade the scanner by straddling two
# 1 MiB reads.
_SCAN_OVERLAP_BYTES = 32 * 1024
_TEXT_SAMPLE_BYTES = 64 * 1024
_NON_PERSONAL_BUILD_USERS = frozenset({"root", "runner", "runneradmin"})
_DEEP_SIGNATURE_TIMEOUT_SECONDS = 300.0
_DETACH_NORMAL_ATTEMPTS = 3
_DETACH_RETRY_DELAY_SECONDS = 0.5
_SENSITIVE_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "APPLE_DEVELOPER_ID_P12_BASE64",
    "APPLE_DEVELOPER_ID_P12_PASSWORD",
    "APPLE_NOTARY_KEY_P8_BASE64",
    "APPLE_ID_USERNAME",
    "APPLE_ID_APP_PASSWORD",
    "APPLE_TEMP_KEYCHAIN_PASSWORD",
    "TEMP_KEYCHAIN_PASSWORD",
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


def _default_sensitive_values() -> tuple[tuple[str, bytes], ...]:
    """Return non-trivial secret values explicitly present in the build env.

    Exact values are scanned in every file, including opaque native binaries.
    Labels may be reported, but values are never included in findings or logs.
    """

    values: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for name in _SENSITIVE_ENV_NAMES:
        raw_value = os.environ.get(name, "")
        encoded = raw_value.encode("utf-8")
        # Short values create unacceptable collision risk in native binaries.
        if len(encoded) < 8 or encoded in seen:
            continue
        seen.add(encoded)
        values.append((name, encoded))
    return tuple(values)


def _is_probably_text(data: bytes) -> bool:
    """Conservatively identify UTF-8 text for generic signature matching.

    Opaque Mach-O/shared-library bytes can randomly contain token-shaped ASCII
    and crypto libraries intentionally contain PEM parser markers. Exact build
    secrets and personal roots are still checked in all bytes; only generic
    provider signatures are limited to text-like members.
    """

    sample = data[:_TEXT_SAMPLE_BYTES]
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    controls = sum(byte < 32 and byte not in b"\t\n\r\f\b" for byte in sample)
    return controls / len(sample) <= 0.01


def _scan_forbidden(
    root: Path,
    *,
    personal_roots: tuple[str, ...] | None = None,
    sensitive_values: tuple[tuple[str, bytes], ...] | None = None,
) -> list[str]:
    """Scan for exact secrets, complete credentials, and local home paths.

    Prefix-only matching is deliberately rejected: Cortex ships redaction
    rules containing strings such as ``sk-ant-``, while native dependencies
    contain generic ``/Users/runner`` debug paths and credential-parser marker
    strings. Exact build secrets and personal paths are checked in all bytes.
    Generic provider expressions are length-bounded and checked only in
    UTF-8/text-like members, avoiding random opaque-binary collisions. Path
    checks target only the actual non-generic build homes (or explicit roots
    supplied by a test/caller).
    """

    findings: list[str] = []
    roots = _default_personal_roots() if personal_roots is None else personal_roots
    secrets = _default_sensitive_values() if sensitive_values is None else sensitive_values
    encoded_roots = tuple(
        (candidate + separator).encode("utf-8")
        for root_value in roots
        if (candidate := root_value.strip().rstrip("/\\"))
        for separator in ("/", "\\")
    )
    overlap_bytes = max(
        _SCAN_OVERLAP_BYTES,
        max((len(value) - 1 for _, value in secrets), default=0),
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        try:
            with path.open("rb") as handle:
                first_block = handle.read(1024 * 1024)
                scan_generic_credentials = _is_probably_text(first_block)
                tail = b""
                matched_credentials: set[str] = set()
                matched_exact_secrets: set[str] = set()
                matched_local_path = False
                blocks = itertools.chain(
                    (first_block,) if first_block else (),
                    iter(lambda: handle.read(1024 * 1024), b""),
                )
                for block in blocks:
                    searchable = tail + block
                    if scan_generic_credentials:
                        matched_credentials.update(
                            name
                            for name, pattern in _CREDENTIAL_PATTERNS
                            if pattern.search(searchable) is not None
                        )
                    matched_exact_secrets.update(
                        label for label, value in secrets if value in searchable
                    )
                    if any(root_bytes in searchable for root_bytes in encoded_roots):
                        matched_local_path = True
                    tail = searchable[-overlap_bytes:]
        except OSError as exc:
            findings.append(f"{relative} could not be scanned ({type(exc).__name__})")
            continue
        findings.extend(
            f"{relative} matches credential rule {name}" for name in sorted(matched_credentials)
        )
        findings.extend(
            f"{relative} contains exact sensitive value {label}"
            for label in sorted(matched_exact_secrets)
        )
        if matched_local_path:
            findings.append(f"{relative} contains a non-generic local home path")
    return findings


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _notarized_container_verification_commands(artifact: Path) -> tuple[list[str], ...]:
    """Return the fail-closed verification order for a notarized outer DMG."""

    artifact_path = str(artifact)
    return (
        ["codesign", "--verify", "--strict", "--verbose=2", artifact_path],
        ["codesign", "-dv", "--verbose=4", artifact_path],
        ["xcrun", "stapler", "validate", artifact_path],
        [
            "spctl",
            "-a",
            "-vv",
            "--type",
            "open",
            "--context",
            "context:primary-signature",
            artifact_path,
        ],
    )


def _mounted_app_signature_verification(app: Path) -> tuple[list[str], float]:
    """Return the deep app verification command and its bounded timeout.

    Intel release bundles contain thousands of nested native members and have
    exceeded the generic 60-second subprocess budget on hosted macOS runners.
    Five minutes remains bounded by the workflow deadline while allowing the
    authoritative deep verification to finish instead of misreporting a slow
    valid signature as a signing failure.
    """

    return (
        ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
        _DEEP_SIGNATURE_TIMEOUT_SECONDS,
    )


def _verify_installer_layout(mount: Path) -> dict[str, str]:
    """Require the Finder-visible drag-to-Applications installation contract.

    The former ``create-dmg`` path added this link as a presentation side
    effect, while ``hdiutil`` only packaged the supplied staging directory. An
    image containing just ``Cortex.app`` is technically mountable while
    silently dropping the primary installation affordance users expect.
    """

    applications_link = mount / "Applications"
    if not applications_link.is_symlink():
        raise ReleaseVerificationError("DMG does not contain the required Applications symlink")
    link_target = os.readlink(applications_link)
    if link_target != "/Applications":
        raise ReleaseVerificationError(
            f"DMG Applications symlink targets {link_target!r}, expected '/Applications'"
        )
    return {"applications_link": link_target}


def _detach_mounted_dmg(
    mount: Path,
    *,
    normal_attempts: int = _DETACH_NORMAL_ATTEMPTS,
    retry_delay_seconds: float = _DETACH_RETRY_DELAY_SECONDS,
) -> list[dict[str, Any]]:
    """Detach a read-only verification mount without traversing it on failure.

    ``hdiutil detach`` can transiently return ``Resource busy`` after deep
    signature scans or a packaged-app launch. A ``TemporaryDirectory`` must
    not own that mount point: its recursive cleanup otherwise walks the still
    mounted, read-only application and fails on files such as
    ``_CodeSignature/CodeResources``. Retry normal detaches, use one bounded
    forced detach for this disposable read-only image, and fail explicitly if
    the volume is still mounted.
    """

    if normal_attempts < 1:
        raise ValueError("normal_attempts must be positive")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must be non-negative")

    results: list[dict[str, Any]] = []
    for attempt in range(normal_attempts):
        result = _run(
            ["hdiutil", "detach", str(mount)],
            check=False,
            timeout=60.0,
        )
        results.append(result)
        if result["returncode"] == 0 or not os.path.ismount(mount):
            return results
        if attempt + 1 < normal_attempts and retry_delay_seconds:
            time.sleep(retry_delay_seconds)

    forced_result = _run(
        ["hdiutil", "detach", "-force", str(mount)],
        check=False,
        timeout=60.0,
    )
    results.append(forced_result)
    if forced_result["returncode"] == 0 or not os.path.ismount(mount):
        return results

    stderr = str(forced_result.get("stderr", ""))[-2000:]
    raise ReleaseVerificationError(
        f"could not detach read-only DMG mount after {normal_attempts} normal "
        f"attempts and one forced attempt: {mount}\n{stderr}"
    )


def _remove_detached_mountpoint(mount: Path) -> None:
    """Remove only the empty directory revealed after a successful detach."""

    if os.path.ismount(mount):
        raise ReleaseVerificationError(
            f"refusing to remove a mount point that is still attached: {mount}"
        )
    try:
        mount.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ReleaseVerificationError(
            f"detached DMG mount point was not removable as an empty directory: {mount}"
        ) from exc


def _reserve_local_port() -> int:
    """Ask the kernel for an available loopback port for a launch probe."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _tail_text(path: Path, limit: int = 8000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def _probe_frozen_startup(
    executable: Path,
    *,
    base_env: dict[str, str],
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    """Launch the complete packaged app and require a healthy runtime.

    The probe uses an isolated HOME/storage directory and the explicit
    headless-hardware boundary. It therefore exercises QApplication,
    CortexAppController, CortexDaemon construction, SQLite migration, service
    registration, and both local servers without touching a user's state,
    camera, or input-monitoring authority. A resource-only smoke cannot prove
    these paths.
    """

    with tempfile.TemporaryDirectory(prefix="cortex-startup-probe-") as raw_probe:
        probe_root = Path(raw_probe)
        home = probe_root / "home"
        storage = home / "Library" / "Application Support" / "Cortex" / "Data"
        home.mkdir(parents=True)
        storage.mkdir(parents=True)
        stdout_path = probe_root / "stdout.log"
        stderr_path = probe_root / "stderr.log"
        http_port = _reserve_local_port()
        ws_port = _reserve_local_port()
        while ws_port == http_port:
            ws_port = _reserve_local_port()

        env = dict(base_env)
        env.update(
            {
                "HOME": str(home),
                "CORTEX_STORAGE__PATH": str(storage),
                "CORTEX_API__HOST": "127.0.0.1",
                "CORTEX_API__PORT": str(http_port),
                "CORTEX_API__WS_PORT": str(ws_port),
                # Defense in depth if the explicit headless hardware boundary
                # ever regresses; this index must never resolve to a device.
                "CORTEX_CAPTURE__DEVICE_ID": "2147483647",
                "CORTEX_REDIS__ENABLED": "false",
                "CORTEX_HEADLESS_STARTUP": "1",
            }
        )
        command = [str(executable)]
        health_url = f"http://127.0.0.1:{http_port}/health"
        health_payload: dict[str, Any] | None = None
        process: subprocess.Popen[str]
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=probe_root,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
            deadline = time.monotonic() + timeout_seconds
            failure: str | None = None
            while time.monotonic() < deadline:
                returncode = process.poll()
                if returncode is not None:
                    failure = f"packaged app exited before health check (code {returncode})"
                    break
                try:
                    with urllib.request.urlopen(health_url, timeout=0.75) as response:
                        decoded = json.loads(response.read().decode("utf-8"))
                    if isinstance(decoded, dict):
                        health_payload = decoded
                except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
                    time.sleep(0.20)
                    continue
                if health_payload is not None:
                    raw_services = health_payload.get("services")
                    services = raw_services if isinstance(raw_services, dict) else {}
                    if (
                        health_payload.get("status") == "healthy"
                        and health_payload.get("version") == __version__
                        and services.get("support_model_registry") == "up"
                    ):
                        break
                time.sleep(0.20)
            else:
                failure = f"packaged app did not become healthy within {timeout_seconds:.0f}s"

            if health_payload is None and failure is None:
                failure = "packaged app returned no health payload"

            process.terminate()
            try:
                returncode = process.wait(timeout=30.0)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=5.0)
                if failure is None:
                    failure = "packaged app did not stop within 30s after SIGTERM"

        stdout = _tail_text(stdout_path)
        stderr = _tail_text(stderr_path)
        startup_log = _tail_text(home / "Library" / "Logs" / "Cortex" / "startup.log")
        last_error = _tail_text(
            home / "Library" / "Logs" / "Cortex" / "last-startup-error.txt"
        )
        fatal_markers = (
            "startup.failed",
            "Task was destroyed but it is pending",
            "Failed to execute script",
            "Could not parse stylesheet",
            "Replace uses of missing font family",
            "AVCaptureDeviceTypeExternal is deprecated for Continuity Cameras",
        )
        combined_diagnostics = "\n".join((stdout, stderr, startup_log))
        if failure is None and returncode != 0:
            failure = f"packaged app returned non-zero after graceful stop: {returncode}"
        if failure is None and last_error:
            failure = "packaged app produced a startup-failure diagnostic despite health"
        if failure is None:
            matched_markers = [
                marker for marker in fatal_markers if marker in combined_diagnostics
            ]
            if matched_markers:
                failure = "packaged app emitted fatal cleanup/startup markers: " + ", ".join(
                    matched_markers
                )
        required_log_markers = (
            "startup.ready name=qt_event_loop",
            "Cortex daemon started",
            "Cortex daemon stopped",
        )
        if failure is None:
            missing_markers = [
                marker for marker in required_log_markers if marker not in startup_log
            ]
            if missing_markers:
                failure = "packaged startup log lacks lifecycle evidence: " + ", ".join(
                    missing_markers
                )
        if failure is None:
            for port in (http_port, ws_port):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                    client.settimeout(0.5)
                    if client.connect_ex(("127.0.0.1", port)) == 0:
                        failure = f"packaged app left port {port} listening after exit"
                        break
        result: dict[str, Any] = {
            "command": command,
            "returncode": returncode,
            "http_port": http_port,
            "ws_port": ws_port,
            "health": health_payload,
            "stdout": stdout,
            "stderr": stderr,
            "startup_log": startup_log,
            "last_startup_error": last_error,
        }
        if failure is not None:
            raise ReleaseVerificationError(
                f"{failure}\nstdout:\n{stdout[-2000:]}\nstderr:\n{stderr[-2000:]}\n"
                f"startup log:\n{startup_log[-3000:]}\nlast error:\n{last_error[-3000:]}"
            )
        return result


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

    # The mount point deliberately is not owned by TemporaryDirectory. If a
    # transiently busy image remains attached, recursive tempfile cleanup
    # would traverse the read-only volume and obscure the real detach error.
    mount = Path(tempfile.mkdtemp(prefix="cortex-release-"))
    attached = False
    try:
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
        attached = True
        app = mount / "Cortex.app"
        plist_path = app / "Contents/Info.plist"
        executable = app / "Contents/MacOS/Cortex"
        if not plist_path.is_file() or not executable.is_file():
            raise ReleaseVerificationError("DMG does not contain a valid Cortex.app")
        evidence["installer_layout"] = _verify_installer_layout(mount)
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        required_plist = {
            "CFBundleIdentifier": "com.cortex.daemon",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "13.0",
            "NSCameraUseContinuityCameraDeviceType": True,
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
        signature_command, signature_timeout = _mounted_app_signature_verification(app)
        commands.append(_run(signature_command, timeout=signature_timeout))
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
        commands.append(
            _probe_frozen_startup(
                executable,
                base_env=smoke_env,
            )
        )
    finally:
        detach_error: ReleaseVerificationError | None = None
        # A failed attach can still leave a partially mounted image. Inspect
        # the kernel-visible mount state as well as the successful-return flag.
        if attached or os.path.ismount(mount):
            try:
                commands.extend(_detach_mounted_dmg(mount))
            except ReleaseVerificationError as exc:
                detach_error = exc
        if not os.path.ismount(mount):
            _remove_detached_mountpoint(mount)
        if detach_error is not None:
            raise detach_error

    if require_notarized:
        # A stapled ticket proves Apple accepted the submission, but it is not
        # the outer disk image's Developer ID signature. Verify both before
        # asking Gatekeeper to assess the exact distributable container.
        commands.extend(
            _run(command)
            for command in _notarized_container_verification_commands(artifact)
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
