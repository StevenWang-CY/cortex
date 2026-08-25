"""Exact outbound-context review sheet for the desktop application.

The sheet is intentionally a three-state interaction: choose sources, inspect
the immutable redacted request, then see the validated response.  Previewing
never calls a provider.  The primary send affordance is enabled only while the
one-time broker handle is live, and the response copy makes clear that model
output is a proposal rather than workspace authority.
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.a11y import (
    set_accessible_description,
    set_accessible_name,
)
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
    BRAND_ACCENT_HOVER,
    BRAND_ACCENT_PRESSED,
    BRAND_ACCENT_TEXT,
    BRAND_DISPLAY_FONT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    RADIUS_BUTTON,
    RADIUS_CARD,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
    SP4,
    SP5,
)

_BG = SEMANTIC_LIGHT["window_bg"]
_CARD = SEMANTIC_LIGHT["control_bg"]
_GROUPED = SEMANTIC_LIGHT["grouped_bg"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
_SECONDARY = CX_TEXT_SECONDARY
_TERTIARY = CX_TEXT_TERTIARY
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_SUCCESS = "#267A4A"
_WARNING = "#9A5B18"

_SOURCE_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "workspace_aggregates",
        "Workspace summary",
        "Activity mode, active app category, and bounded complexity score.",
    ),
    (
        "support_estimate",
        "Support estimate",
        "Derived state label, confidence, and dwell time — never raw camera data.",
    ),
    ("user_goal", "Current goal", "The goal you entered or the active focus goal."),
    (
        "editor_metadata",
        "Editor metadata",
        "File basename, symbol, and error locations; directory paths are removed.",
    ),
    (
        "editor_content",
        "Editor content",
        "Up to 3,000 characters of visible code and three error messages.",
    ),
    (
        "terminal_content",
        "Terminal errors",
        "Up to three detected error excerpts; command history is never included.",
    ),
    (
        "browser_metadata",
        "Browser metadata",
        "Tab titles, topic labels, and origins only — no paths, queries, or fragments.",
    ),
    (
        "browser_content",
        "Active page text",
        "Up to 2,000 characters, only when browser site access is also granted.",
    ),
    (
        "learned_preferences",
        "Learned site preferences",
        "Up to twenty origin-level relevance scores learned on this device.",
    ),
)


def _button_qss(*, primary: bool) -> str:
    if primary:
        return (
            "QPushButton {"
            f" background: {BRAND_ACCENT}; color: {_LABEL}; border: none;"
            f" border-radius: {RADIUS_BUTTON}px; padding: 8px 16px;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; color: #111111; }}"
            f"QPushButton:pressed {{ background: {BRAND_ACCENT_PRESSED}; color: #FFFFFF; }}"
            "QPushButton:disabled { background: rgba(120,120,128,0.24); color: rgba(60,60,67,0.45); }"
        )
    return (
        "QPushButton { background: transparent;"
        f" color: {_SECONDARY}; border: 0.5px solid {_SEPARATOR};"
        f" border-radius: {RADIUS_BUTTON}px; padding: 7px 14px; }}"
        f"QPushButton:hover {{ color: {_LABEL}; background: rgba(0,0,0,0.035); }}"
        f"QPushButton:pressed {{ color: {_LABEL}; background: rgba(0,0,0,0.075); }}"
        "QPushButton:disabled { color: rgba(60,60,67,0.30); }"
    )


class ContextPrivacyDialog(QDialog):
    """Progressive-disclosure UI for one external planner request."""

    status_requested = Signal()
    preview_requested = Signal(dict)
    confirmation_requested = Signal(str, str)
    cancellation_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review external context — Cortex")
        self.setMinimumSize(620, 680)
        self.resize(680, 760)
        self.setModal(True)
        self.setStyleSheet(f"background: {_BG}; color: {_LABEL};")
        self._source_checks: dict[str, QCheckBox] = {}
        self._preview: dict[str, Any] | None = None
        self._retention_url = ""
        self._expiry_timer = QTimer(self)
        self._expiry_timer.setSingleShot(True)
        self._expiry_timer.timeout.connect(self._expire_preview)
        self._build_ui()
        self.rejected.connect(self._discard_current_preview)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(SP5, SP5, SP5, SP5)
        root.setSpacing(SP4)

        header = QHBoxLayout()
        eyebrow = QLabel("External request")
        eyebrow.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        eyebrow.setStyleSheet(f"color: {_SECONDARY}; background: transparent;")
        header.addWidget(eyebrow)
        header.addStretch()
        close = QPushButton("Close")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        close.setStyleSheet(_button_qss(primary=False))
        close.clicked.connect(self.reject)
        header.addWidget(close)
        root.addLayout(header)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_choice_page())
        self._stack.addWidget(self._build_preview_page())
        self._stack.addWidget(self._build_result_page())
        root.addWidget(self._stack, stretch=1)

    def _page_title(self, title: str, detail: str) -> tuple[QLabel, QLabel]:
        title_label = QLabel(title)
        title_label.setFont(mac_native.system_font(FS_TITLE, "regular"))
        title_label.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f" color: {_LABEL}; background: transparent;"
        )
        detail_label = QLabel(detail)
        detail_label.setWordWrap(True)
        detail_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        detail_label.setStyleSheet(f"color: {_SECONDARY}; background: transparent;")
        return title_label, detail_label

    def _build_choice_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SP3)
        title, detail = self._page_title(
            "Choose what the model may see",
            "Every source is off by default. Cortex creates a local, redacted preview first; nothing is sent until you review it and confirm once.",
        )
        layout.addWidget(title)
        layout.addWidget(detail)

        self._posture_label = QLabel("Checking the active privacy posture…")
        self._posture_label.setWordWrap(True)
        self._posture_label.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._posture_label.setStyleSheet(
            f"color: {_SECONDARY}; background: {_GROUPED};"
            f" border-radius: {RADIUS_BUTTON}px; padding: 8px 10px;"
        )
        layout.addWidget(self._posture_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        source_host = QWidget()
        source_layout = QVBoxLayout(source_host)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(SP2)
        for key, label, description in _SOURCE_ROWS:
            card = QFrame()
            card.setStyleSheet(
                f"QFrame {{ background: {_CARD}; border: 0.5px solid {_SEPARATOR};"
                f" border-radius: {RADIUS_BUTTON}px; }}"
            )
            row = QVBoxLayout(card)
            row.setContentsMargins(SP3, SP2, SP3, SP2)
            row.setSpacing(2)
            check = QCheckBox(label)
            check.setChecked(False)
            check.setCursor(Qt.CursorShape.PointingHandCursor)
            check.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
            check.setStyleSheet(
                f"QCheckBox {{ color: {_LABEL}; spacing: 8px; background: transparent; }}"
            )
            set_accessible_description(check, description)
            row.addWidget(check)
            supporting = QLabel(description)
            supporting.setWordWrap(True)
            supporting.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            supporting.setStyleSheet(f"color: {_TERTIARY}; background: transparent;")
            row.addWidget(supporting)
            source_layout.addWidget(card)
            self._source_checks[key] = check
        source_layout.addStretch()
        scroll.setWidget(source_host)
        layout.addWidget(scroll, stretch=1)

        note_label = QLabel("Optional note to the planner")
        note_label.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        note_label.setStyleSheet(f"color: {_SECONDARY}; background: transparent;")
        layout.addWidget(note_label)
        self._extra_note = QPlainTextEdit()
        self._extra_note.setPlaceholderText("Add only what you intend to send…")
        self._extra_note.setMaximumHeight(68)
        self._extra_note.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._extra_note.setStyleSheet(
            f"QPlainTextEdit {{ background: {_CARD}; border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_BUTTON}px; color: {_LABEL}; padding: 7px; }}"
        )
        set_accessible_description(
            self._extra_note,
            "Optional text that will be secret-scanned, redacted, capped, and shown in the exact request preview.",
        )
        layout.addWidget(self._extra_note)

        action_row = QHBoxLayout()
        action_row.addStretch()
        self._review_button = QPushButton("Build private preview")
        self._review_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._review_button.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._review_button.setStyleSheet(_button_qss(primary=True))
        self._review_button.setEnabled(False)
        self._review_button.clicked.connect(self._request_preview)
        set_accessible_description(
            self._review_button,
            "Build the exact redacted request locally. This does not contact the provider.",
        )
        action_row.addWidget(self._review_button)
        layout.addLayout(action_row)
        return page

    def _build_preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SP3)
        title, detail = self._page_title(
            "Review the exact request",
            "Nothing has been sent. This is the complete redacted user prompt that will leave this Mac if you confirm.",
        )
        layout.addWidget(title)
        layout.addWidget(detail)

        self._preview_summary = QLabel("")
        self._preview_summary.setWordWrap(True)
        self._preview_summary.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._preview_summary.setStyleSheet(
            f"color: {_SUCCESS}; background: rgba(38,122,74,0.08);"
            f" border-radius: {RADIUS_BUTTON}px; padding: 8px 10px;"
        )
        layout.addWidget(self._preview_summary)

        retention_card = QFrame()
        retention_card.setStyleSheet(
            f"QFrame {{ background: {_GROUPED}; border-radius: {RADIUS_CARD}px; }}"
        )
        retention_layout = QVBoxLayout(retention_card)
        retention_layout.setContentsMargins(SP3, SP3, SP3, SP3)
        retention_layout.setSpacing(SP2)
        self._destination_label = QLabel("")
        self._destination_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._destination_label.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        retention_layout.addWidget(self._destination_label)
        self._retention_label = QLabel("")
        self._retention_label.setWordWrap(True)
        self._retention_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._retention_label.setStyleSheet(f"color: {_SECONDARY}; background: transparent;")
        retention_layout.addWidget(self._retention_label)
        self._policy_button = QPushButton("Read provider policy ↗")
        self._policy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._policy_button.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._policy_button.setStyleSheet(
            f"QPushButton {{ color: {BRAND_ACCENT_TEXT}; background: transparent; border: none;"
            " text-align: left; padding: 2px 0; }}"
            f"QPushButton:hover {{ color: {BRAND_ACCENT_TEXT}; background: rgba(217,119,87,0.08); }}"
            f"QPushButton:pressed {{ color: {BRAND_ACCENT_TEXT}; background: rgba(217,119,87,0.14); }}"
        )
        self._policy_button.clicked.connect(self._open_retention_policy)
        retention_layout.addWidget(self._policy_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(retention_card)

        prompt_label = QLabel("Exact outbound prompt")
        prompt_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        prompt_label.setStyleSheet(f"color: {_SECONDARY}; background: transparent;")
        layout.addWidget(prompt_label)
        self._prompt_preview = QPlainTextEdit()
        self._prompt_preview.setReadOnly(True)
        self._prompt_preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._prompt_preview.setStyleSheet(
            f"QPlainTextEdit {{ background: {_CARD}; border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_BUTTON}px; color: {_LABEL}; padding: 10px;"
            " font-family: ui-monospace, Menlo, monospace; font-size: 11px; }}"
        )
        set_accessible_name(self._prompt_preview, "Exact outbound model prompt")
        layout.addWidget(self._prompt_preview, stretch=3)

        disclosure_label = QLabel("Included and redacted fields")
        disclosure_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        disclosure_label.setStyleSheet(f"color: {_SECONDARY}; background: transparent;")
        layout.addWidget(disclosure_label)
        self._disclosure_preview = QPlainTextEdit()
        self._disclosure_preview.setReadOnly(True)
        self._disclosure_preview.setMaximumHeight(110)
        self._disclosure_preview.setStyleSheet(
            f"QPlainTextEdit {{ background: {_GROUPED}; border: none;"
            f" border-radius: {RADIUS_BUTTON}px; color: {_SECONDARY}; padding: 8px; }}"
        )
        layout.addWidget(self._disclosure_preview)

        footer = QHBoxLayout()
        self._back_button = QPushButton("Change sources")
        self._back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_button.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._back_button.setStyleSheet(_button_qss(primary=False))
        self._back_button.clicked.connect(self._return_to_sources)
        footer.addWidget(self._back_button)
        footer.addStretch()
        self._send_button = QPushButton("Send this request once")
        self._send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_button.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._send_button.setStyleSheet(_button_qss(primary=True))
        self._send_button.clicked.connect(self._confirm_preview)
        set_accessible_description(
            self._send_button,
            "Send only the exact prompt shown above. The one-time handle is consumed even if the request fails.",
        )
        footer.addWidget(self._send_button)
        layout.addLayout(footer)
        return page

    def _build_result_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SP4)
        layout.addStretch()
        self._result_mark = QLabel("✓")
        self._result_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._result_mark.setFont(mac_native.system_font(32, "semibold"))
        self._result_mark.setStyleSheet(f"color: {_SUCCESS}; background: transparent;")
        layout.addWidget(self._result_mark)
        self._result_title = QLabel("Request sent once")
        self._result_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._result_title.setFont(mac_native.system_font(FS_TITLE, "semibold"))
        self._result_title.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        layout.addWidget(self._result_title)
        self._result_detail = QLabel("")
        self._result_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._result_detail.setWordWrap(True)
        self._result_detail.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._result_detail.setStyleSheet(f"color: {_SECONDARY}; background: transparent;")
        layout.addWidget(self._result_detail)
        self._result_plan = QLabel("")
        self._result_plan.setWordWrap(True)
        self._result_plan.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._result_plan.setStyleSheet(
            f"color: {_LABEL}; background: {_CARD}; border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_CARD}px; padding: 14px;"
        )
        layout.addWidget(self._result_plan)
        close = QPushButton("Done")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        close.setStyleSheet(_button_qss(primary=True))
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()
        return page

    def open_with_selection(self, selection: dict[str, Any] | None = None) -> None:
        self._discard_current_preview()
        selected = selection or {}
        for key, check in self._source_checks.items():
            check.setChecked(bool(selected.get(key, False)))
        self._extra_note.clear()
        self._preview = None
        self._expiry_timer.stop()
        self._stack.setCurrentIndex(0)
        self._posture_label.setText("Checking the active privacy posture…")
        self._posture_label.setStyleSheet(
            f"color: {_SECONDARY}; background: {_GROUPED};"
            f" border-radius: {RADIUS_BUTTON}px; padding: 8px 10px;"
        )
        self._review_button.setEnabled(False)
        self.open()
        self.raise_()
        self.activateWindow()
        self.status_requested.emit()

    def current_selection(self) -> dict[str, bool]:
        result = {key: check.isChecked() for key, check in self._source_checks.items()}
        result["extra_context"] = bool(self._extra_note.toPlainText().strip())
        return result

    def apply_status(self, payload: dict[str, Any]) -> None:
        enabled = bool(payload.get("network_allowed_by_configuration", False))
        mode = str(payload.get("planner_mode") or "no_llm")
        retention = payload.get("retention") if isinstance(payload.get("retention"), dict) else {}
        destination = str(retention.get("destination") or payload.get("provider") or "provider")
        self._retention_url = str(retention.get("documentation_url") or "")
        if enabled:
            self._posture_label.setText(
                f"Ready to preview · destination: {destination}. Previewing remains local."
            )
            self._posture_label.setStyleSheet(
                f"color: {_SUCCESS}; background: rgba(38,122,74,0.08);"
                f" border-radius: {RADIUS_BUTTON}px; padding: 8px 10px;"
            )
            self._review_button.setEnabled(True)
        else:
            friendly_mode = {
                "no_llm": "Local planner",
                "no_content": "No-content planner",
                "external_redacted": "External planner not fully configured",
            }.get(mode, "External planner unavailable")
            self._posture_label.setText(
                f"{friendly_mode} · choose External model, acknowledge the disclosure, and Apply before reviewing a request."
            )
            self._posture_label.setStyleSheet(
                f"color: {_WARNING}; background: rgba(154,91,24,0.08);"
                f" border-radius: {RADIUS_BUTTON}px; padding: 8px 10px;"
            )
            self._review_button.setEnabled(False)

    def _request_preview(self) -> None:
        self._discard_current_preview()
        self._review_button.setEnabled(False)
        self._review_button.setText("Building preview…")
        self.preview_requested.emit(
            {
                "selection": self.current_selection(),
                "extra_context": self._extra_note.toPlainText().strip()[:2_000],
            }
        )

    def apply_preview(self, payload: dict[str, Any]) -> None:
        self._discard_current_preview()
        self._review_button.setText("Build private preview")
        self._review_button.setEnabled(True)
        self._preview = payload
        prompt = str(payload.get("outbound_user_prompt") or "")
        self._prompt_preview.setPlainText(prompt)
        redactions = int(payload.get("redaction_count") or 0)
        omitted = int(payload.get("omitted_field_count") or 0)
        size_bytes = int(payload.get("outbound_utf8_bytes") or 0)
        expires_at_ms = int(payload.get("expires_at_unix_ms") or 0)
        seconds = max(0, int((expires_at_ms / 1000.0) - time.time()))
        self._preview_summary.setText(
            f"{redactions} secret/path redaction{'s' if redactions != 1 else ''} · "
            f"{omitted} fields omitted · {size_bytes / 1024.0:.1f} KB · expires in {seconds}s"
        )
        retention = payload.get("retention") if isinstance(payload.get("retention"), dict) else {}
        self._retention_url = str(retention.get("documentation_url") or "")
        destination = str(retention.get("destination") or payload.get("provider") or "Provider")
        model = str(payload.get("model") or "configured model")
        self._destination_label.setText(f"Destination: {destination} · {model}")
        self._retention_label.setText(str(retention.get("summary") or "Retention varies by provider and account policy."))
        disclosures = payload.get("field_disclosures")
        lines: list[str] = []
        if isinstance(disclosures, list):
            for item in disclosures:
                if not isinstance(item, dict) or item.get("disposition") == "omitted":
                    continue
                disposition = str(item.get("disposition") or "included").upper()
                origin = str(item.get("origin") or "unknown")
                path = str(item.get("field_path") or "field")
                preview = str(item.get("value_preview") or "")
                lines.append(f"{disposition:8}  {origin:8}  {path}  —  {preview}")
        self._disclosure_preview.setPlainText("\n".join(lines) or "No context fields selected.")
        self._send_button.setEnabled(seconds > 0)
        self._send_button.setText("Send this request once")
        self._back_button.setEnabled(True)
        self._stack.setCurrentIndex(1)
        self._expiry_timer.start(max(1, min(seconds * 1000, 300_000)))

    def _confirm_preview(self) -> None:
        preview = self._preview or {}
        preview_id = str(preview.get("preview_id") or "")
        phrase = str(preview.get("confirmation_phrase") or "")
        if not preview_id or not phrase:
            self._expire_preview()
            return
        self._expiry_timer.stop()
        self._send_button.setEnabled(False)
        self._send_button.setText("Sending once…")
        self._back_button.setEnabled(False)
        # Confirmation burns the handle server-side before the provider await.
        # Clear the local copy so closing the sheet cannot race a cancellation.
        self._preview = None
        self.confirmation_requested.emit(preview_id, phrase)

    def apply_confirmation(self, payload: dict[str, Any]) -> None:
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        headline = str(plan.get("headline") or "Validated planner response")
        focus = str(plan.get("primary_focus") or "")
        steps = plan.get("micro_steps") if isinstance(plan.get("micro_steps"), list) else []
        step_text = "\n".join(f"• {str(step)}" for step in steps[:5])
        body = "\n".join(part for part in (headline, focus, step_text) if part)
        self._result_plan.setText(body)
        self._result_detail.setText(
            "The exact preview was consumed and the response passed the local schema and action-vocabulary checks. No workspace action was authorized."
        )
        self._stack.setCurrentIndex(2)

    def apply_error(self, payload: dict[str, Any]) -> None:
        operation = str(payload.get("operation") or "request")
        message = str(payload.get("message") or "The local privacy request failed.")[:500]
        if operation == "cancel":
            return
        if operation == "status":
            self._posture_label.setText(message)
            self._posture_label.setStyleSheet(
                f"color: {_WARNING}; background: rgba(154,91,24,0.08);"
                f" border-radius: {RADIUS_BUTTON}px; padding: 8px 10px;"
            )
            self._review_button.setEnabled(False)
            return
        if operation == "preview":
            self._stack.setCurrentIndex(0)
            self._review_button.setText("Try preview again")
            self._review_button.setEnabled(True)
            self._posture_label.setText(message)
            return
        self._result_mark.setText("!")
        self._result_mark.setStyleSheet(f"color: {_WARNING}; background: transparent;")
        self._result_title.setText("Request was not sent")
        self._result_detail.setText(
            f"{message}\n\nThe one-time preview was consumed. Build a new preview to try again."
        )
        self._result_plan.setText("No model response was accepted and no workspace action was authorized.")
        self._stack.setCurrentIndex(2)

    def _expire_preview(self) -> None:
        self._discard_current_preview()
        self._send_button.setEnabled(False)
        self._send_button.setText("Preview expired")
        self._preview_summary.setText(
            "This one-time preview expired without sending. Change sources and build a fresh preview."
        )

    def _return_to_sources(self) -> None:
        self._discard_current_preview()
        self._stack.setCurrentIndex(0)

    def _discard_current_preview(self) -> None:
        preview = self._preview
        self._preview = None
        self._expiry_timer.stop()
        if not isinstance(preview, dict):
            return
        preview_id = str(preview.get("preview_id") or "")
        if preview_id:
            self.cancellation_requested.emit(preview_id)

    def _open_retention_policy(self) -> None:
        if not self._retention_url:
            return
        target = QUrl(self._retention_url)
        if target.scheme().lower() == "https" and bool(target.host()):
            QDesktopServices.openUrl(target)
