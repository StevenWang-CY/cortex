#!/usr/bin/env python3
"""
Install the Cortex native messaging host for Chrome.

Registers the native messaging host manifest so the Chrome extension
can launch the Cortex daemon via chrome.runtime.sendNativeMessage().

Usage:
    python -m cortex.scripts.install_native_host [--extension-id ID]

If --extension-id is not provided, it will scan Chrome profiles to
auto-detect the Cortex extension, or prompt the user.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HOST_NAME = "com.cortex.launcher"
NATIVE_HOST_DIR = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/NativeMessagingHosts"
)


def find_extension_id() -> str | None:
    """Try to auto-detect the Cortex extension ID from Chrome profiles."""
    chrome_base = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    for profile in ["Default", "Profile 1", "Profile 2", "Profile 3"]:
        prefs_path = os.path.join(chrome_base, profile, "Preferences")
        if not os.path.exists(prefs_path):
            continue
        try:
            with open(prefs_path) as f:
                prefs = json.load(f)
            extensions = prefs.get("extensions", {}).get("settings", {})
            for ext_id, info in extensions.items():
                name = info.get("manifest", {}).get("name", "")
                path = info.get("path", "")
                if "cortex" in name.lower() or "somatic" in name.lower() or "cortex" in path.lower():
                    return ext_id
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def install(extension_id: str) -> None:
    """Install the native messaging host manifest."""
    # Path to the native host script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    host_script = os.path.join(script_dir, "native_host.py")

    if not os.path.exists(host_script):
        print(f"Error: Native host script not found at {host_script}")
        sys.exit(1)

    # Ensure the script is executable
    os.chmod(host_script, 0o755)

    manifest = {
        "name": HOST_NAME,
        "description": "Cortex daemon launcher for Chrome extension",
        "path": host_script,
        "type": "stdio",
        "allowed_origins": [
            f"chrome-extension://{extension_id}/"
        ],
    }

    os.makedirs(NATIVE_HOST_DIR, exist_ok=True)
    manifest_path = os.path.join(NATIVE_HOST_DIR, f"{HOST_NAME}.json")

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Installed native messaging host:")
    print(f"  Manifest: {manifest_path}")
    print(f"  Host:     {host_script}")
    print(f"  Extension ID: {extension_id}")
    print()
    print("Done! The Chrome extension can now launch the Cortex daemon.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Cortex native messaging host")
    parser.add_argument(
        "--extension-id",
        help="Chrome extension ID (from chrome://extensions)",
    )
    args = parser.parse_args()

    ext_id = args.extension_id

    if not ext_id:
        ext_id = find_extension_id()

    if not ext_id:
        print("Could not auto-detect extension ID.")
        print("Please find it at chrome://extensions (enable Developer mode)")
        print("and run:")
        print(f"  python -m cortex.scripts.install_native_host --extension-id YOUR_ID")
        sys.exit(1)

    install(ext_id)


if __name__ == "__main__":
    main()
