#!/usr/bin/env python3
"""
Install the Cortex native messaging host for all Chromium browsers.

Registers the native messaging host manifest so the browser extension
can launch the Cortex daemon via chrome.runtime.sendNativeMessage().

The extension uses a fixed key in its manifest, giving it a deterministic
ID across all machines and browsers. The installer also auto-detects any
existing Cortex extension IDs from browser profiles so it works even if
the extension was loaded before the key was added.

Usage:
    python -m cortex.scripts.install_native_host
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOST_NAME = "com.cortex.launcher"
PACKAGED_HOST_EXECUTABLE = "CortexNativeHost"
DEVELOPMENT_HOST_DIR = os.path.expanduser(
    "~/Library/Application Support/Cortex/NativeMessaging"
)

# Deterministic extension ID derived from the fixed key in package.json.
# This never changes regardless of where the extension is loaded from.
FIXED_EXTENSION_ID = "khbaagicippibonmgcnhpbagjloilknd"

# Browser data directories on macOS (profile root, not NativeMessagingHosts)
BROWSER_PROFILES = {
    "Google Chrome": os.path.expanduser(
        "~/Library/Application Support/Google/Chrome"
    ),
    "Microsoft Edge": os.path.expanduser(
        "~/Library/Application Support/Microsoft Edge"
    ),
    "Chromium": os.path.expanduser(
        "~/Library/Application Support/Chromium"
    ),
    "Brave": os.path.expanduser(
        "~/Library/Application Support/BraveSoftware/Brave-Browser"
    ),
    "Vivaldi": os.path.expanduser(
        "~/Library/Application Support/Vivaldi"
    ),
    "Arc": os.path.expanduser(
        "~/Library/Application Support/Arc/User Data"
    ),
    "Opera": os.path.expanduser(
        "~/Library/Application Support/com.operasoftware.Opera"
    ),
}

_BROWSER_ALIASES = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
}

# Keywords to identify the Cortex extension in browser profiles
_CORTEX_KEYWORDS = ["cortex", "somatic", "biofeedback", "workspace engine"]
_EXTENSION_ID_PATTERN = re.compile(r"^[a-p]{32}$")


@dataclass(frozen=True)
class NativeHostVerification:
    """Observable state for one browser's native-host registration.

    A manifest merely existing is not proof that Chrome or Edge can execute its
    target.  The desktop shell consumes this result so it can distinguish an
    absent manifest, a malformed/stale manifest, and a host that completed a
    real framed protocol round-trip.
    """

    browser: str
    manifest_path: Path
    manifest_valid: bool
    host_path: Path | None
    host_executable: bool
    host_responded: bool
    extension_ids: tuple[str, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.manifest_valid and self.host_executable and self.host_responded


def _find_python(project_root: str | None = None) -> str:
    """Find the absolute development interpreter for a source host script."""

    if getattr(sys, "frozen", False) or _is_app_bundle(project_root):
        raise RuntimeError(
            "Packaged native messaging must use CortexNativeHost, not an "
            "external Python interpreter"
        )

    # Explicit override for power users / debugging.
    env_python = os.environ.get("CORTEX_NATIVE_HOST_PYTHON")
    if env_python and os.path.isfile(env_python) and os.access(env_python, os.X_OK):
        return os.path.abspath(env_python)

    # Dev checkout: prefer local venv.
    dev_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    venv_python = os.path.join(dev_root, ".venv", "bin", "python")
    if os.path.isfile(venv_python) and os.access(venv_python, os.X_OK):
        return os.path.abspath(venv_python)

    # Non-frozen fallback.
    return os.path.abspath(sys.executable)


def _patch_shebang(script_path: str, python_path: str) -> None:
    """Rewrite the shebang line to use an absolute Python path.

    Chrome invokes native messaging hosts directly — /usr/bin/env won't
    resolve inside Chrome's restricted PATH.
    """
    with open(script_path) as f:
        lines = f.readlines()

    if not lines:
        return

    new_shebang = f"#!{python_path}\n"
    if lines[0].startswith("#!"):
        lines[0] = new_shebang
    else:
        lines.insert(0, new_shebang)

    with open(script_path, "w") as f:
        f.writelines(lines)


def _is_app_bundle(project_root: str | None) -> bool:
    if not project_root:
        return False
    return Path(project_root.rstrip(os.sep)).suffix.lower() == ".app"


def _packaged_host_path(project_root: str) -> str:
    """Return the signed native-host executable embedded in ``Cortex.app``."""

    return os.path.abspath(
        os.path.join(
            project_root,
            "Contents",
            "MacOS",
            PACKAGED_HOST_EXECUTABLE,
        )
    )


def _probe_native_host(host_path: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """Require a real native-messaging ``status`` round-trip.

    Chrome and Edge execute the manifest's path directly and exchange framed
    JSON over stdio.  Running the same contract here catches missing dynamic
    libraries, invalid shebangs, broken imports, non-executable files, and
    malformed stdout before a manifest is ever reported as installed.  The
    ``status`` command is side-effect-free: it does not launch Cortex or touch
    camera hardware.
    """

    request_body = json.dumps({"command": "status"}, separators=(",", ":")).encode(
        "utf-8"
    )
    framed_request = struct.pack("<I", len(request_body)) + request_body
    try:
        completed = subprocess.run(
            [
                host_path,
                f"chrome-extension://{FIXED_EXTENSION_ID}/",
            ],
            input=framed_request,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"native host could not be executed: {exc}") from exc

    stderr = completed.stderr.decode("utf-8", errors="replace")[-4096:].strip()
    if completed.returncode != 0:
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(
            f"native host exited with code {completed.returncode}{detail}"
        )
    if len(completed.stdout) < 4:
        detail = f"; stderr: {stderr}" if stderr else ""
        raise RuntimeError(f"native host returned no framed response{detail}")

    response_length = struct.unpack("<I", completed.stdout[:4])[0]
    response_bytes = completed.stdout[4:]
    if response_length != len(response_bytes):
        raise RuntimeError(
            "native host response length mismatch "
            f"(declared {response_length}, received {len(response_bytes)})"
        )
    try:
        response = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("native host returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise RuntimeError("native host response was not an object")
    if response.get("command") != "status" or response.get("status") not in {
        "running",
        "stopped",
    }:
        raise RuntimeError(f"native host returned an invalid status response: {response!r}")
    return response


def _prepare_host_script(source_script: str, *, project_root: str | None = None) -> str:
    """Prepare the executable host script path for the browser manifest.

    Packaged releases use a dedicated PyInstaller executable embedded in the
    signed app.  They must never fall back to a Homebrew/Xcode interpreter:
    those interpreters do not contain the frozen ``cortex`` package.  Source
    checkouts retain the absolute-shebang development path.
    """
    if _is_app_bundle(project_root):
        assert project_root is not None
        packaged_host = _packaged_host_path(project_root)
        if not os.path.isfile(packaged_host):
            raise RuntimeError(
                "Packaged native host is missing from Cortex.app: "
                f"{packaged_host}"
            )
        if not os.access(packaged_host, os.X_OK):
            raise RuntimeError(f"Packaged native host is not executable: {packaged_host}")
        _probe_native_host(packaged_host)
        print(f"Native host: {packaged_host}")
        print("Runtime:     self-contained signed executable")
        return packaged_host

    os.makedirs(DEVELOPMENT_HOST_DIR, exist_ok=True)
    host_script = os.path.join(DEVELOPMENT_HOST_DIR, "native_host.py")
    shutil.copyfile(source_script, host_script)

    python_path = _find_python(project_root=project_root)
    _patch_shebang(host_script, python_path)
    os.chmod(host_script, 0o755)
    print(f"Native host: {host_script}")
    print(f"Python:      {python_path}")

    # Probe that the patched Python can import the cortex package.
    # ``native_host.py`` swallows its own ImportError inside a broad
    # try/except (and writes only to native_host_debug.log), so a
    # mis-installed venv would otherwise fail silently the first time
    # Chrome opens the host — surface the error here instead.
    try:
        subprocess.run(
            [python_path, "-c", "import cortex.libs.schemas.native_messaging"],
            check=True,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = ""
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            stderr_bytes = exc.stderr if isinstance(exc.stderr, bytes) else b""
            try:
                stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                stderr = ""
        raise RuntimeError(
            "Native host python lacks the cortex package "
            f"({python_path}) — install with `pip install -e cortex` "
            f"first. underlying error: {exc}; stderr: {stderr}"
        ) from exc
    return host_script


def _scan_browser_for_cortex_ids(browser_root: str) -> set[str]:
    """Scan a browser's profiles for existing Cortex extension IDs.

    Checks both Preferences and Secure Preferences across all profiles.
    """
    ids: set[str] = set()

    root = Path(browser_root)
    profile_dirs = ["Default"]
    try:
        profile_dirs.extend(
            sorted(
                entry.name
                for entry in root.iterdir()
                if entry.is_dir() and entry.name.startswith("Profile ")
            )
        )
    except OSError:
        pass

    for profile in profile_dirs:
        for pref_file in ["Secure Preferences", "Preferences"]:
            pref_path = os.path.join(browser_root, profile, pref_file)
            if not os.path.exists(pref_path):
                continue
            try:
                with open(pref_path) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                extensions = data.get("extensions")
                if not isinstance(extensions, dict):
                    continue
                exts = extensions.get("settings")
                if not isinstance(exts, dict):
                    continue
                for ext_id, info in exts.items():
                    if (
                        not isinstance(ext_id, str)
                        or _EXTENSION_ID_PATTERN.fullmatch(ext_id) is None
                        or not isinstance(info, dict)
                    ):
                        continue
                    # The release key makes this ID deterministic. Recognise it
                    # even when Chromium stores a localised ``__MSG_*`` name or
                    # omits the unpacked path from Preferences.
                    if ext_id == FIXED_EXTENSION_ID:
                        ids.add(ext_id)
                        continue
                    embedded_manifest = info.get("manifest")
                    name = (
                        embedded_manifest.get("name", "")
                        if isinstance(embedded_manifest, dict)
                        else ""
                    )
                    path = info.get("path", "")
                    if not isinstance(name, str) or not isinstance(path, str):
                        continue
                    searchable = (name + " " + path).lower()
                    if any(kw in searchable for kw in _CORTEX_KEYWORDS):
                        ids.add(ext_id)
            except (json.JSONDecodeError, KeyError, OSError):
                continue

    return ids


def _canonical_browser_name(browser: str) -> str:
    canonical = _BROWSER_ALIASES.get(browser.strip().lower(), browser.strip())
    if canonical not in BROWSER_PROFILES:
        supported = ", ".join(sorted(BROWSER_PROFILES))
        raise ValueError(f"Unsupported Chromium browser {browser!r}; expected one of {supported}")
    return canonical


def _selected_browsers(target_browsers: Iterable[str] | None) -> tuple[str, ...]:
    if target_browsers is None:
        return tuple(
            browser
            for browser, browser_root in BROWSER_PROFILES.items()
            if os.path.isdir(browser_root)
        )
    canonical = {_canonical_browser_name(browser) for browser in target_browsers}
    return tuple(browser for browser in BROWSER_PROFILES if browser in canonical)


def _manifest_path(browser: str) -> Path:
    canonical = _canonical_browser_name(browser)
    return (
        Path(BROWSER_PROFILES[canonical])
        / "NativeMessagingHosts"
        / f"{HOST_NAME}.json"
    )


def _write_manifest_atomic(path: Path, manifest: dict[str, Any]) -> None:
    """Write one host manifest atomically with browser-readable permissions."""

    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _trusted_host_paths() -> set[Path]:
    """Return the only host locations Cortex itself can provision."""

    return {
        Path(DEVELOPMENT_HOST_DIR).resolve() / "native_host.py",
        (
            Path("/Applications/Cortex.app/Contents/MacOS")
            / PACKAGED_HOST_EXECUTABLE
        ).resolve(),
    }


def verify_browser_installation(
    browser: str,
    *,
    expected_host_path: str | Path | None = None,
) -> NativeHostVerification:
    """Validate one browser manifest and execute its registered host.

    This is deliberately stronger than ``Path.exists()``.  It checks the
    manifest contract, every Cortex origin currently found in that browser,
    executable permissions, and a framed ``status`` response from the exact
    path the browser will launch.
    """

    canonical = _canonical_browser_name(browser)
    browser_root = BROWSER_PROFILES[canonical]
    manifest_path = _manifest_path(canonical)
    extension_ids = tuple(sorted(_scan_browser_for_cortex_ids(browser_root)))
    required_ids = {FIXED_EXTENSION_ID, *extension_ids}
    required_origins = {f"chrome-extension://{extension_id}/" for extension_id in required_ids}

    if not manifest_path.is_file():
        return NativeHostVerification(
            browser=canonical,
            manifest_path=manifest_path,
            manifest_valid=False,
            host_path=None,
            host_executable=False,
            host_responded=False,
            extension_ids=extension_ids,
            error="native messaging manifest is missing",
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return NativeHostVerification(
            browser=canonical,
            manifest_path=manifest_path,
            manifest_valid=False,
            host_path=None,
            host_executable=False,
            host_responded=False,
            extension_ids=extension_ids,
            error=f"native messaging manifest is unreadable: {exc}",
        )

    raw_host_path = payload.get("path") if isinstance(payload, dict) else None
    host_path = Path(raw_host_path) if isinstance(raw_host_path, str) else None
    origins = payload.get("allowed_origins") if isinstance(payload, dict) else None
    origin_set = set(origins) if isinstance(origins, list) and all(
        isinstance(origin, str) for origin in origins
    ) else set()
    trusted_paths = (
        {Path(expected_host_path).resolve()}
        if expected_host_path is not None
        else _trusted_host_paths()
    )
    normalized_host_path = (
        host_path.resolve() if host_path is not None and host_path.is_absolute() else None
    )
    manifest_valid = bool(
        isinstance(payload, dict)
        and payload.get("name") == HOST_NAME
        and payload.get("type") == "stdio"
        and host_path is not None
        and host_path.is_absolute()
        and normalized_host_path in trusted_paths
        and origin_set == required_origins
        and isinstance(origins, list)
        and len(origins) == len(origin_set)
    )
    if not manifest_valid:
        return NativeHostVerification(
            browser=canonical,
            manifest_path=manifest_path,
            manifest_valid=False,
            host_path=host_path,
            host_executable=False,
            host_responded=False,
            extension_ids=extension_ids,
            error="native messaging manifest has stale or invalid fields",
        )

    assert host_path is not None
    host_executable = host_path.is_file() and os.access(host_path, os.X_OK)
    if not host_executable:
        return NativeHostVerification(
            browser=canonical,
            manifest_path=manifest_path,
            manifest_valid=True,
            host_path=host_path,
            host_executable=False,
            host_responded=False,
            extension_ids=extension_ids,
            error=f"registered native host is missing or not executable: {host_path}",
        )
    try:
        _probe_native_host(str(host_path))
    except RuntimeError as exc:
        return NativeHostVerification(
            browser=canonical,
            manifest_path=manifest_path,
            manifest_valid=True,
            host_path=host_path,
            host_executable=True,
            host_responded=False,
            extension_ids=extension_ids,
            error=str(exc),
        )
    return NativeHostVerification(
        browser=canonical,
        manifest_path=manifest_path,
        manifest_valid=True,
        host_path=host_path,
        host_executable=True,
        host_responded=True,
        extension_ids=extension_ids,
    )


def install(
    *,
    project_root: str | None = None,
    target_browsers: Iterable[str] | None = None,
) -> bool:
    """Install the native messaging host manifest for all detected browsers.

    Args:
        project_root: Override the project root directory. Used by the
            desktop app's ConnectionsPanel to pass the canonical
            ``/Applications/Cortex.app`` path instead of the running
            (possibly translocated) path.
        target_browsers: Optional browser display names. When supplied, the
            corresponding user-data roots are created even before first browser
            launch. This prevents a Connect Chrome action from succeeding only
            because an unrelated Edge profile happened to exist.
    """
    if project_root is not None:
        # Packaged mode resolves the dedicated executable in Contents/MacOS.
        # ``source_script`` remains an inert compatibility argument to keep the
        # source/dev preparation helper's API narrow.
        host_script = os.path.join(
            project_root,
            "Contents",
            "Resources",
            "cortex",
            "scripts",
            "native_host.py",
        )
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        host_script = os.path.join(script_dir, "native_host.py")

    if project_root is None and not os.path.exists(host_script):
        print(f"Error: Native host script not found at {host_script}")
        sys.exit(1)

    host_script = _prepare_host_script(host_script, project_root=project_root)

    # Collect browser-local extension IDs. A Chrome-only legacy extension ID
    # must never be authorised in Edge (or vice versa).
    detected_ids: dict[str, set[str]] = {}

    print()
    print("Scanning browser profiles for existing Cortex extensions...")
    for browser, browser_root in BROWSER_PROFILES.items():
        if not os.path.isdir(browser_root):
            continue
        found = _scan_browser_for_cortex_ids(browser_root)
        if found:
            detected_ids[browser] = found
            for eid in found:
                print(f"  Found in {browser}: {eid}")
    print()

    selected_browsers = _selected_browsers(target_browsers)
    installed_browsers: list[str] = []

    for browser in selected_browsers:
        browser_ids = {FIXED_EXTENSION_ID, *detected_ids.get(browser, set())}
        allowed_origins = [
            f"chrome-extension://{extension_id}/"
            for extension_id in sorted(browser_ids)
        ]
        manifest = {
            "name": HOST_NAME,
            "description": "Cortex daemon launcher for browser extension",
            "path": host_script,
            "type": "stdio",
            "allowed_origins": allowed_origins,
        }
        manifest_path = _manifest_path(browser)
        _write_manifest_atomic(manifest_path, manifest)

        installed_browsers.append(browser)
        print(f"  Installed for {browser}")

    if not installed_browsers:
        print("  Warning: No Chromium browsers detected!")
        return False
    else:
        print()
        failed_verifications: list[str] = []
        for browser in installed_browsers:
            verification = verify_browser_installation(
                browser,
                expected_host_path=host_script,
            )
            if not verification.ok:
                failed_verifications.append(
                    f"{browser}: {verification.error or 'verification failed'}"
                )
        if failed_verifications:
            raise RuntimeError(
                "Native messaging installation did not pass its protocol probe: "
                + "; ".join(failed_verifications)
            )
        print(f"Installed and verified for {len(installed_browsers)} browser(s).")
        print()
        print("IMPORTANT: Restart your browser (Cmd+Q, reopen) for changes to take effect.")
        return True


def main() -> int:
    """Entry point. Returns 0 on success, 1 when no browsers were
    detected so the build script / CI can fail fast instead of silently
    shipping a daemon with no extension wiring.
    """
    if not install():
        print(
            "ERROR: Native messaging host could NOT be installed (no "
            "supported browsers detected).\n"
            "       Install Chrome / Edge / Brave / Vivaldi / Arc and "
            "re-run this script.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
