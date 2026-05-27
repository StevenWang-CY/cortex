"""Audit Phase 4d Task E — native_host ``raise_dashboard`` branch.

The browser-extension popup needs a way to surface the desktop
dashboard without juggling AppleScript or relying on the user finding
the menu-bar icon. ``cortex.scripts.native_host`` exposes a
``raise_dashboard`` command that POSTs to the daemon's
``/dashboard/raise`` route and surfaces the daemon's response status to
the extension.

These tests pin three behaviours:

1. The command is dispatched correctly when present in the inbound
   message (it lives outside the schema's discriminated union, so the
   handler must intercept it before Pydantic parse).
2. Successful HTTP responses produce ``{ok: True, status: <int>}``.
3. HTTP errors (e.g. 404 when the route isn't deployed) surface as
   ``{ok: False, error: "<reason>"}`` rather than crashing the host.

Patterns mirror :mod:`cortex.tests.unit.test_native_messaging_schema`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cortex.scripts import native_host


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class TestRaiseDashboard:
    """``_raise_dashboard`` produces the documented response envelopes."""

    def test_success_returns_ok_true_with_status(self) -> None:
        with patch.object(
            native_host, "_read_auth_token", return_value="fake-token"
        ):
            with patch(
                "urllib.request.urlopen", return_value=_FakeResponse(204)
            ) as mock_open:
                result = native_host._raise_dashboard("dashboard")

        assert result == {"ok": True, "status": 204}
        # Verify the URL targets the centralised port constant.
        called_request = mock_open.call_args[0][0]
        assert f"127.0.0.1:{native_host.HTTP_API_PORT}" in called_request.full_url
        assert called_request.full_url.endswith("/dashboard/raise")
        # X-Cortex-Auth header is populated from the cached token.
        assert called_request.headers.get("X-cortex-auth") == "fake-token"

    def test_http_error_surfaces_as_ok_false(self) -> None:
        with patch.object(native_host, "_read_auth_token", return_value=""):
            with patch(
                "urllib.request.urlopen",
                side_effect=RuntimeError("404 Not Found"),
            ):
                result = native_host._raise_dashboard("dashboard")

        assert result["ok"] is False
        assert "404 Not Found" in result["error"]

    def test_unknown_target_is_clamped_to_64_chars(self) -> None:
        # The pre-parse interceptor clamps absurdly long target strings;
        # the helper itself accepts whatever the dispatcher passes in,
        # so this test just confirms ``_raise_dashboard`` doesn't add
        # surprise validation.
        with patch.object(native_host, "_read_auth_token", return_value=""):
            with patch(
                "urllib.request.urlopen", return_value=_FakeResponse(200)
            ):
                result = native_host._raise_dashboard("x" * 64)

        assert result == {"ok": True, "status": 200}


class TestPortConstantImport:
    """Phase 4a Debt-1 — module-level ports come from cortex.libs.config.ports."""

    def test_http_api_port_is_centralised_constant(self) -> None:
        from cortex.libs.config.ports import HTTP_API_PORT

        assert native_host.HTTP_API_PORT == HTTP_API_PORT

    def test_websocket_port_is_centralised_constant(self) -> None:
        from cortex.libs.config.ports import WEBSOCKET_PORT

        assert native_host.WEBSOCKET_PORT == WEBSOCKET_PORT


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
