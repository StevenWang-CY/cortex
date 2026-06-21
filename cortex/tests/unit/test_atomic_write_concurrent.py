"""Concurrency correctness test for atomic_write_text (Item: ATOMIC-WRITE).

Two threads each write a DISTINCT large JSON payload to the SAME destination
path concurrently.  After joining, the file must:
  * parse as valid JSON, and
  * exactly equal ONE of the two payloads (no byte interleaving).

With the old fixed-name temp file (path.with_suffix('.tmp')) both threads
share a single fd, interleave bytes, and the resulting file is corrupt.
With the fix (tempfile.mkstemp per call) each write is isolated.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from cortex.libs.utils.atomic_write import atomic_write_json


def _make_payload(marker: str, size: int = 4096) -> dict[str, object]:
    """Return a large JSON-serialisable dict whose content uniquely identifies it."""
    return {"marker": marker, "data": marker * size}


def test_concurrent_writes_produce_valid_json(tmp_path: Path) -> None:
    """After two concurrent atomic writes the file is valid JSON = one of the payloads."""
    dest = tmp_path / "output.json"

    payload_a = _make_payload("AAAA")
    payload_b = _make_payload("BBBB")

    errors: list[Exception] = []

    def write_a() -> None:
        try:
            atomic_write_json(dest, payload_a)
        except Exception as exc:
            errors.append(exc)

    def write_b() -> None:
        try:
            atomic_write_json(dest, payload_b)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=write_a)
    t2 = threading.Thread(target=write_b)

    # Start both threads as close together as possible.
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # No exceptions from either writer.
    assert not errors, f"Writer threads raised: {errors}"

    # The file must exist.
    assert dest.exists(), "Destination file was not created"

    # Must parse as valid JSON — interleaved bytes produce a JSONDecodeError.
    raw = dest.read_text(encoding="utf-8")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"File is not valid JSON after concurrent writes: {exc}\n\nFile content (first 200 chars): {raw[:200]!r}")

    # The result must exactly match one of the two payloads (not a mix).
    assert result in (payload_a, payload_b), (
        f"File contents match neither payload_a nor payload_b.\n"
        f"marker in result: {result.get('marker', '<missing>')!r}"
    )


def test_concurrent_writes_stress(tmp_path: Path) -> None:
    """Stress test: 8 concurrent writers, file stays valid JSON after all complete."""
    dest = tmp_path / "stress.json"
    n_threads = 8
    payloads = [_make_payload(f"T{i:02d}") for i in range(n_threads)]
    errors: list[Exception] = []
    barrier = threading.Barrier(n_threads)

    def writer(idx: int) -> None:
        # Synchronise all threads to start writing at the same moment.
        barrier.wait(timeout=5)
        try:
            atomic_write_json(dest, payloads[idx])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"Writer threads raised: {errors}"
    assert dest.exists()

    raw = dest.read_text(encoding="utf-8")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"File corrupt after stress concurrent writes: {exc}")

    assert result in payloads, "File does not match any writer's payload"
