#!/usr/bin/env python3
"""
Cortex Native Messaging Host for Chrome Extension.

Chrome calls this script via Native Messaging when the extension needs
to launch the Cortex daemon. It reads a JSON request, spawns the daemon
as a detached background process (if not already running), and replies
with status.

Protocol: Chrome native messaging uses 4-byte little-endian length prefix
followed by JSON payload.
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import os
import time


def read_message() -> dict:
    """Read a native messaging request from stdin."""
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) < 4:
        return {}
    length = struct.unpack("<I", raw_length)[0]
    data = sys.stdin.buffer.read(length)
    return json.loads(data.decode("utf-8"))


def send_message(msg: dict) -> None:
    """Send a native messaging response to stdout."""
    encoded = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def is_daemon_running(port: int = 9473) -> bool:
    """Check if the Cortex daemon is already listening on its WebSocket port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def launch_daemon() -> dict:
    """Launch the Cortex daemon as a detached background process."""
    if is_daemon_running():
        return {"status": "already_running"}

    # Find the project root (this script is at cortex/scripts/native_host.py)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(script_dir))

    # Launch daemon detached from this process
    try:
        log_path = os.path.join(project_root, "cortex_daemon.log")
        log_file = open(log_path, "a")

        subprocess.Popen(
            [sys.executable, "-m", "cortex.scripts.run_dev"],
            cwd=project_root,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return {"status": "error", "error": str(e)}

    # Wait for the daemon to start listening (up to 8 seconds)
    for _ in range(16):
        time.sleep(0.5)
        if is_daemon_running():
            return {"status": "launched"}

    return {"status": "timeout", "error": "Daemon started but port 9473 not yet ready"}


def main() -> None:
    msg = read_message()
    command = msg.get("command", "launch")

    if command == "launch":
        result = launch_daemon()
    elif command == "status":
        result = {"status": "running" if is_daemon_running() else "stopped"}
    else:
        result = {"status": "error", "error": f"Unknown command: {command}"}

    send_message(result)


if __name__ == "__main__":
    main()
