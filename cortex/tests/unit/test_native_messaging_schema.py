"""Audit F14 + F37 — schema validation for native-messaging payloads.

The native host previously decoded inbound JSON without a structural
check; an 8 MB length cap was the only guardrail. ``launch_daemon``
then accepted any ``project_root`` the message provided, so a hostile
extension could push the daemon CWD into an attacker-chosen location.

The fix is :mod:`cortex.libs.schemas.native_messaging`, a Pydantic
discriminated-union schema with a tight 64 KB size cap and a
``project_root`` allowlist (``~/Desktop``, ``~/Documents``,
``~/Projects``, ``/Applications/Cortex.app``, plus an env-configurable
list). These tests pin that contract.
"""

from __future__ import annotations

import json

from cortex.libs.schemas.native_messaging import (
    MAX_MESSAGE_BYTES,
    GetAuthTokenMessage,
    LaunchMessage,
    StatusMessage,
    StopMessage,
    parse_native_message,
)


def _encode(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class TestValidCommands:
    """Each legitimate command parses cleanly."""

    def test_valid_launch_without_project_root(self) -> None:
        result = parse_native_message(_encode({"command": "launch"}))
        assert result.error is None, result.detail
        assert isinstance(result.message, LaunchMessage)
        assert result.message.project_root is None

    def test_valid_launch_with_allowlisted_project_root(
        self, tmp_path, monkeypatch
    ) -> None:
        # Add tmp_path's parent to the env-configurable allowlist so the
        # validator accepts it.
        monkeypatch.setenv(
            "CORTEX_NATIVE_HOST_PROJECT_ROOTS", str(tmp_path.parent)
        )
        result = parse_native_message(
            _encode({"command": "launch", "project_root": str(tmp_path)})
        )
        assert result.error is None, result.detail
        assert isinstance(result.message, LaunchMessage)
        assert result.message.project_root == str(tmp_path.resolve())

    def test_comma_separated_allowlist_extends_roots(
        self, tmp_path, monkeypatch
    ) -> None:
        """Finding-10: ``CORTEX_NATIVE_HOST_PROJECT_ROOTS`` is COMMA-separated
        (matching the documented ``.env.example`` example). A two-entry,
        comma-separated value must register BOTH roots. With the legacy
        ``:`` split this whole string was treated as one (non-existent)
        path and neither entry was honoured.
        """
        root_a = tmp_path / "alpha"
        root_b = tmp_path / "beta"
        root_a.mkdir()
        root_b.mkdir()
        monkeypatch.setenv(
            "CORTEX_NATIVE_HOST_PROJECT_ROOTS",
            f"{root_a},{root_b}",
        )
        # The SECOND entry must be honoured — proves the comma split, not
        # a single-path interpretation of the whole string.
        result = parse_native_message(
            _encode({"command": "launch", "project_root": str(root_b)})
        )
        assert result.error is None, result.detail
        assert isinstance(result.message, LaunchMessage)
        assert result.message.project_root == str(root_b.resolve())

    def test_valid_stop(self) -> None:
        result = parse_native_message(_encode({"command": "stop"}))
        assert result.error is None
        assert isinstance(result.message, StopMessage)

    def test_valid_status(self) -> None:
        result = parse_native_message(_encode({"command": "status"}))
        assert result.error is None
        assert isinstance(result.message, StatusMessage)

    def test_valid_get_auth_token(self) -> None:
        result = parse_native_message(_encode({"command": "get_auth_token"}))
        assert result.error is None
        assert isinstance(result.message, GetAuthTokenMessage)


class TestRejections:
    """Adversarial inputs are refused with structured errors."""

    def test_oversized_message_rejected(self) -> None:
        # MAX_MESSAGE_BYTES is 64 KB; build a 100 KB payload.
        oversized = b"x" * (MAX_MESSAGE_BYTES + 100)
        result = parse_native_message(oversized)
        assert result.message is None
        assert result.error == "message_too_large"
        assert result.detail is not None
        assert f"max={MAX_MESSAGE_BYTES}" in result.detail

    def test_project_root_outside_allowlist_rejected(
        self, tmp_path, monkeypatch
    ) -> None:
        # Clear the env override so only the default allowlist applies.
        monkeypatch.delenv("CORTEX_NATIVE_HOST_PROJECT_ROOTS", raising=False)
        # ``tmp_path`` (typically under /private/var/folders/) lives
        # outside ~/Desktop, ~/Documents, ~/Projects, and Cortex.app.
        result = parse_native_message(
            _encode({"command": "launch", "project_root": str(tmp_path)})
        )
        assert result.message is None
        assert result.error == "invalid_message"
        assert result.detail is not None
        # The validator's reason should propagate through Pydantic.
        assert "project_root_outside_allowlist" in result.detail

    def test_unknown_command_rejected(self) -> None:
        result = parse_native_message(
            _encode({"command": "exfil", "victim": "user"})
        )
        assert result.message is None
        assert result.error == "invalid_message"

    def test_malformed_json_does_not_crash(self) -> None:
        result = parse_native_message(b"{not valid json at all")
        assert result.message is None
        assert result.error == "malformed_json"
        assert result.detail is not None

    def test_non_object_payload_rejected(self) -> None:
        # JSON arrays are valid JSON but not legal native-host payloads.
        result = parse_native_message(b'["launch"]')
        assert result.message is None
        assert result.error == "not_an_object"

    def test_invalid_encoding_rejected(self) -> None:
        # Lone continuation bytes are not valid UTF-8.
        result = parse_native_message(b"\xff\xfe\xfd not utf8")
        assert result.message is None
        assert result.error == "invalid_encoding"

    def test_extra_fields_rejected(self) -> None:
        # ``extra="forbid"`` keeps tampered extensions from smuggling
        # in attributes the dispatcher might later trust.
        result = parse_native_message(
            _encode({"command": "stop", "smuggle": "payload"})
        )
        assert result.message is None
        assert result.error == "invalid_message"

    def test_project_root_pointing_at_file_rejected(self, tmp_path) -> None:
        target = tmp_path / "not_a_dir.txt"
        target.write_text("x")
        result = parse_native_message(
            _encode({"command": "launch", "project_root": str(target)})
        )
        assert result.message is None
        assert result.error == "invalid_message"
        assert result.detail is not None
        assert "project_root_not_a_directory" in result.detail


class TestSize:
    """The 64 KB cap is enforced; legitimate messages are well below it."""

    def test_message_at_cap_size_passes_size_gate(self) -> None:
        # Build a payload that is *exactly* at the 64 KB cap so the size
        # gate has to let it through and we confirm it doesn't fire
        # first; the schema-level ``extra="forbid"`` reject is the
        # expected failure mode.
        prefix = '{"command":"status","_pad":"'
        suffix = '"}'
        padding = " " * (MAX_MESSAGE_BYTES - len(prefix) - len(suffix))
        padded = (prefix + padding + suffix).encode("utf-8")
        assert len(padded) == MAX_MESSAGE_BYTES
        result = parse_native_message(padded)
        # ``extra="forbid"`` rejects the ``_pad`` key — that's a feature,
        # not a bug; we're testing the *size gate* did not fire first.
        assert result.error == "invalid_message", result.detail
        assert result.error != "message_too_large"

    def test_one_byte_over_cap_is_rejected_by_size_gate(self) -> None:
        oversized = b"x" * (MAX_MESSAGE_BYTES + 1)
        result = parse_native_message(oversized)
        assert result.error == "message_too_large"

    def test_legitimate_messages_are_tiny(self) -> None:
        for payload in (
            {"command": "launch"},
            {"command": "stop"},
            {"command": "status"},
            {"command": "get_auth_token"},
        ):
            assert len(_encode(payload)) < 64
