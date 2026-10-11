"""Real native messaging frames: reject corrupt input without hanging."""

from __future__ import annotations

import io
import json
import os
import select
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex.scripts import native_host


@pytest.mark.parametrize("raw,error", [
    (b"\x10\x00", "native_message_truncated_header"),
    (struct.pack("<I", 30) + b'{"command":"status"}', "native_message_truncated_payload"),
    (struct.pack("<I", 65537), "native_message_too_large"),
])
def test_corrupt_frame_never_dispatches(monkeypatch, raw: bytes, error: str) -> None:
    output = io.BytesIO()
    monkeypatch.setattr(native_host.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(native_host.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(native_host, "log", lambda _message: None)
    monkeypatch.setattr(native_host, "is_daemon_running", lambda: pytest.fail("must not dispatch"))
    native_host.main()
    framed = output.getvalue()
    assert struct.unpack("<I", framed[:4])[0] == len(framed) - 4
    assert json.loads(framed[4:])["error"] == error


def test_oversized_prefix_answers_while_the_browser_keeps_stdin_open(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "from cortex.scripts import native_host; native_host.log=lambda _: None; native_host.main()"],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "HOME": str(tmp_path)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(struct.pack("<I", 2**32 - 1))
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "host blocked draining an oversized message from an open pipe"
        prefix = process.stdout.read(4)
        assert len(prefix) == 4
        body = process.stdout.read(struct.unpack("<I", prefix)[0])
        assert json.loads(body)["error"] == "native_message_too_large"
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_broken_schema_dependency_still_returns_a_private_framed_error(monkeypatch) -> None:
    body = b'{"command":"status"}'
    output = io.BytesIO()
    monkeypatch.setattr(native_host.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(struct.pack("<I", len(body)) + body)))
    monkeypatch.setattr(native_host.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(native_host, "log", lambda _message: None)
    monkeypatch.setattr(native_host, "is_daemon_running", lambda: False)

    def broken(_response):
        raise ImportError("private local path must not escape")

    monkeypatch.setattr(native_host, "_send_validated_response", broken)
    native_host.main()
    raw = output.getvalue()
    assert struct.unpack("<I", raw[:4])[0] == len(raw) - 4
    assert json.loads(raw[4:]) == {
        "command": "error", "status": "error", "error": "native_host_initialization_failed",
    }
