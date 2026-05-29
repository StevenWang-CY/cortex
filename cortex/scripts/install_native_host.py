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

import glob
import json
import os
import shutil
import subprocess
import sys

HOST_NAME = "com.cortex.launcher"
NATIVE_HOST_DIR = os.path.expanduser(
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

# Keywords to identify the Cortex extension in browser profiles
_CORTEX_KEYWORDS = ["cortex", "somatic", "biofeedback", "workspace engine"]


def _find_bundled_framework_python(project_root: str) -> str | None:
    """Resolve the PyInstaller-bundled framework Python inside ``.app``.

    PyInstaller emits ``Python.framework`` under
    ``Contents/Frameworks/Python.framework/Versions/<X.Y>/bin/pythonX.Y``.
    The minor version (``<X.Y>``) tracks whatever interpreter built the
    bundle, so it MUST NOT be hardcoded — bumping the Python pin in
    pyproject.toml would silently break native-messaging launch. We glob
    the ``Versions/*`` directory and pick the highest matching
    ``pythonX.Y`` binary so the path keeps resolving across version bumps.

    Returns an absolute path to an executable Python, or ``None`` if no
    bundled framework Python is present (caller falls back to system
    Python discovery).
    """
    versions_dir = os.path.join(
        project_root,
        "Contents",
        "Frameworks",
        "Python.framework",
        "Versions",
    )
    if not os.path.isdir(versions_dir):
        return None

    candidates: list[tuple[tuple[int, ...], str]] = []
    for version_dir in glob.glob(os.path.join(versions_dir, "*")):
        version_name = os.path.basename(version_dir)
        # Skip the ``Current`` symlink and any non version-numbered entries.
        if not version_name[:1].isdigit():
            continue
        python_bin = os.path.join(version_dir, "bin", f"python{version_name}")
        if os.path.isfile(python_bin) and os.access(python_bin, os.X_OK):
            try:
                sort_key = tuple(int(p) for p in version_name.split("."))
            except ValueError:
                sort_key = (0,)
            candidates.append((sort_key, os.path.abspath(python_bin)))

    if not candidates:
        return None
    # Highest version wins when multiple framework versions coexist.
    candidates.sort()
    return candidates[-1][1]


def _find_python(project_root: str | None = None) -> str:
    """Find an absolute Python path for the native-host shebang.

    Important: in bundled-app mode, ``sys.executable`` points to
    ``.../Cortex.app/Contents/MacOS/Cortex`` (the app executable), not a
    Python interpreter. Native messaging must point to a real Python binary.
    """
    # Explicit override for power users / debugging.
    env_python = os.environ.get("CORTEX_NATIVE_HOST_PYTHON")
    if env_python and os.path.isfile(env_python) and os.access(env_python, os.X_OK):
        return os.path.abspath(env_python)

    requested_app_bundle = bool(project_root and project_root.endswith(".app"))

    # Frozen app OR explicit .app target: do not use sys.executable
    # (bundled binary) and avoid dev-venv coupling.
    if getattr(sys, "frozen", False) or requested_app_bundle:
        # Audit-2 fix: macOS 12.3+ ships ``/usr/bin/python3`` only as a
        # Command Line Tools shim that prompts to install Xcode when
        # exec'd. ``os.path.isfile`` passes on the shim — but native
        # messaging silently fails the first time Chrome tries to run
        # it. Prefer the bundled framework Python (PyInstaller emits
        # one under Contents/Frameworks/) before falling back to system
        # Python, and only use ``/usr/bin/python3`` when we can verify
        # it executes (not just exists).
        if requested_app_bundle and project_root:
            framework_py = _find_bundled_framework_python(project_root)
            if framework_py is not None:
                return framework_py
        for candidate in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        found = shutil.which("python3")
        if found:
            return os.path.abspath(found)
        # ``/usr/bin/python3`` is only a CLT installer stub on a fresh
        # Mac. Only use it after probing that it actually runs.
        usr_bin = "/usr/bin/python3"
        if os.path.isfile(usr_bin):
            try:
                import subprocess

                subprocess.check_call(
                    [usr_bin, "-c", "import sys"],
                    timeout=2.0,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return usr_bin
            except Exception:
                # Falls through — caller should surface a setup error.
                pass
        return "/usr/bin/python3"

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
    return bool(project_root and project_root.endswith(".app"))


def _prepare_host_script(source_script: str, *, project_root: str | None = None) -> str:
    """Prepare the executable host script path for the browser manifest.

    In packaged mode this deliberately copies the script outside
    ``/Applications/Cortex.app`` before patching its shebang. Mutating files
    inside the app bundle can invalidate code signatures and can fail for
    non-admin installs.
    """
    if _is_app_bundle(project_root):
        os.makedirs(NATIVE_HOST_DIR, exist_ok=True)
        host_script = os.path.join(NATIVE_HOST_DIR, "native_host.py")
        shutil.copyfile(source_script, host_script)
    else:
        host_script = source_script

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

    profile_dirs = ["Default"]
    for i in range(1, 10):
        profile_dirs.append(f"Profile {i}")

    for profile in profile_dirs:
        for pref_file in ["Secure Preferences", "Preferences"]:
            pref_path = os.path.join(browser_root, profile, pref_file)
            if not os.path.exists(pref_path):
                continue
            try:
                with open(pref_path) as f:
                    data = json.load(f)
                exts = data.get("extensions", {}).get("settings", {})
                for ext_id, info in exts.items():
                    name = info.get("manifest", {}).get("name", "")
                    path = info.get("path", "")
                    searchable = (name + " " + path).lower()
                    if any(kw in searchable for kw in _CORTEX_KEYWORDS):
                        ids.add(ext_id)
            except (json.JSONDecodeError, KeyError, OSError):
                continue

    return ids


def install(*, project_root: str | None = None) -> bool:
    """Install the native messaging host manifest for all detected browsers.

    Args:
        project_root: Override the project root directory.  Used by the
            desktop app's ConnectionsPanel to pass the canonical
            ``/Applications/Cortex.app`` path instead of the running
            (possibly translocated) path.
    """
    if project_root is not None:
        script_dir = os.path.join(project_root, "Contents", "Resources", "cortex", "scripts")
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    host_script = os.path.join(script_dir, "native_host.py")

    if not os.path.exists(host_script):
        print(f"Error: Native host script not found at {host_script}")
        if project_root is None:
            sys.exit(1)
        return False

    host_script = _prepare_host_script(host_script, project_root=project_root)

    # Collect all extension IDs: fixed + auto-detected from browser profiles
    all_ids: set[str] = {FIXED_EXTENSION_ID}

    print()
    print("Scanning browser profiles for existing Cortex extensions...")
    for browser, browser_root in BROWSER_PROFILES.items():
        if not os.path.isdir(browser_root):
            continue
        found = _scan_browser_for_cortex_ids(browser_root)
        if found:
            all_ids.update(found)
            for eid in found:
                print(f"  Found in {browser}: {eid}")

    allowed_origins = [f"chrome-extension://{eid}/" for eid in sorted(all_ids)]

    print()
    print(f"Extension IDs ({len(all_ids)}): {', '.join(sorted(all_ids))}")
    print()

    manifest = {
        "name": HOST_NAME,
        "description": "Cortex daemon launcher for browser extension",
        "path": host_script,
        "type": "stdio",
        "allowed_origins": allowed_origins,
    }

    installed_browsers = []

    for browser, browser_root in BROWSER_PROFILES.items():
        if not os.path.isdir(browser_root):
            continue

        host_dir = os.path.join(browser_root, "NativeMessagingHosts")
        os.makedirs(host_dir, exist_ok=True)
        manifest_path = os.path.join(host_dir, f"{HOST_NAME}.json")

        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        installed_browsers.append(browser)
        print(f"  Installed for {browser}")

    if not installed_browsers:
        print("  Warning: No Chromium browsers detected!")
        return False
    else:
        print()
        print(f"Installed for {len(installed_browsers)} browser(s). No manual configuration needed.")
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
