"""Desktop privacy-sheet contracts: fail-closed defaults and exact gestures."""

from __future__ import annotations

import time
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from cortex.apps.desktop_shell.context_privacy_controller import (
    ContextPrivacyController,
)
from cortex.apps.desktop_shell.context_privacy_dialog import ContextPrivacyDialog
from cortex.apps.desktop_shell.settings import SettingsDialog


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_settings_external_mode_requires_revision_bound_acknowledgement(
    qt_app: QApplication,
) -> None:
    del qt_app
    settings = SettingsDialog()
    defaults = settings.get_settings()
    assert defaults["llm_planner_mode"] == "no_llm"
    assert defaults["external_context_enabled"] is False
    assert defaults["llm_context_consent_revision"] == ""

    settings._llm_privacy_mode.setCurrentIndex(2)
    unacknowledged = settings.get_settings()
    assert unacknowledged["llm_planner_mode"] == "external_redacted"
    assert unacknowledged["external_context_enabled"] is False

    settings._external_context_ack.setChecked(True)
    acknowledged = settings.get_settings()
    assert acknowledged["external_context_enabled"] is True
    assert acknowledged["llm_context_consent_revision"] == "context-disclosure-v1"


def test_privacy_sheet_previews_before_one_time_send(qt_app: QApplication) -> None:
    del qt_app
    dialog = ContextPrivacyDialog()
    dialog.apply_status(
        {
            "planner_mode": "external_redacted",
            "network_allowed_by_configuration": True,
            "provider": "direct",
            "retention": {
                "destination": "Anthropic API",
                "documentation_url": "https://example.test/privacy",
            },
        }
    )
    assert dialog._review_button.isEnabled()

    previews: list[dict[str, Any]] = []
    dialog.preview_requested.connect(previews.append)
    dialog._source_checks["editor_content"].setChecked(True)
    dialog._request_preview()
    assert previews == [
        {
            "selection": {
                "workspace_aggregates": False,
                "support_estimate": False,
                "user_goal": False,
                "editor_metadata": False,
                "editor_content": True,
                "terminal_content": False,
                "browser_metadata": False,
                "browser_content": False,
                "learned_preferences": False,
                "extra_context": False,
            },
            "extra_context": "",
        }
    ]

    dialog.apply_preview(
        {
            "preview_id": "ctx_abcdefghijklmnopqrstuvwxyz0123456789",
            "confirmation_phrase": "SEND PREVIEWED CONTEXT ONCE",
            "expires_at_unix_ms": int(time.time() * 1000) + 60_000,
            "outbound_user_prompt": "<WORKSPACE_CONTEXT>redacted</WORKSPACE_CONTEXT>",
            "outbound_utf8_bytes": 56,
            "redaction_count": 2,
            "omitted_field_count": 10,
            "provider": "direct",
            "model": "test-model",
            "retention": {
                "destination": "Anthropic API",
                "summary": "Account policy must be verified.",
                "documentation_url": "https://example.test/privacy",
            },
            "field_disclosures": [
                {
                    "field_path": "editor_context.visible_code",
                    "origin": "editor",
                    "disposition": "redacted",
                    "value_preview": "[REDACTED:SECRET]",
                }
            ],
        }
    )
    assert dialog._stack.currentIndex() == 1
    assert dialog._send_button.isEnabled()
    assert "redacted" in dialog._prompt_preview.toPlainText()

    confirmations: list[tuple[str, str]] = []
    dialog.confirmation_requested.connect(
        lambda preview_id, phrase: confirmations.append((preview_id, phrase))
    )
    dialog._confirm_preview()
    assert confirmations == [
        (
            "ctx_abcdefghijklmnopqrstuvwxyz0123456789",
            "SEND PREVIEWED CONTEXT ONCE",
        )
    ]
    assert not dialog._send_button.isEnabled()

    dialog.apply_confirmation(
        {
            "sent": True,
            "authority_granted": False,
            "plan": {
                "headline": "Take one small step",
                "primary_focus": "Read the current error",
                "micro_steps": ["Inspect the first stack frame"],
            },
        }
    )
    assert dialog._stack.currentIndex() == 2
    assert "No workspace action was authorized" in dialog._result_detail.text()


def test_loopback_controller_never_accepts_raw_context_from_view(
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qt_app
    controller = ContextPrivacyController()
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        controller,
        "_start",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )
    controller.preview_current(
        {
            "selection": {"browser_content": True},
            "extra_context": "user note",
            "task_context": {"visible_code": "must never cross this boundary"},
        }
    )
    assert len(calls) == 1
    operation, method, path, body, kwargs = calls[0]
    assert (operation, method, path) == (
        "preview",
        "POST",
        "/privacy/context/preview/current",
    )
    assert body == {
        "selection": {"browser_content": True},
        "extra_context": "user note",
    }
    assert "task_context" not in body
    assert kwargs == {"timeout": 8.0}

    controller.cancel_preview("ctx_abcdefghijklmnopqrstuvwxyz0123456789")
    assert calls[1][:3] == (
        "cancel",
        "DELETE",
        "/privacy/context/preview/ctx_abcdefghijklmnopqrstuvwxyz0123456789",
    )
    assert calls[1][3] is None
    assert calls[1][4] == {"timeout": 5.0}


def test_changing_sources_explicitly_cancels_prepared_preview(
    qt_app: QApplication,
) -> None:
    del qt_app
    dialog = ContextPrivacyDialog()
    cancelled: list[str] = []
    dialog.cancellation_requested.connect(cancelled.append)
    dialog.apply_preview(
        {
            "preview_id": "ctx_abcdefghijklmnopqrstuvwxyz0123456789",
            "confirmation_phrase": "SEND PREVIEWED CONTEXT ONCE",
            "expires_at_unix_ms": int(time.time() * 1000) + 60_000,
            "outbound_user_prompt": "No selected content",
            "outbound_utf8_bytes": 19,
            "field_disclosures": [],
        }
    )
    dialog._return_to_sources()
    assert cancelled == ["ctx_abcdefghijklmnopqrstuvwxyz0123456789"]
    assert dialog._preview is None
    assert dialog._stack.currentIndex() == 0
