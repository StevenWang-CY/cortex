"""Reusable desktop-shell widgets.

Phase J-2: small ``Toast`` widget for surfacing daemon errors in the
dashboard top bar with the F19 correlation-id quoted back to the user.

Why a dedicated module
======================

Both the dashboard and (future) the settings dialog need a transient
status surface. Keeping the widget here — rather than nested inside
``dashboard.py`` — means a future surface (e.g. connections panel,
settings sync) can import the same ``Toast`` without dragging in the
dashboard's tab plumbing.

Contract
========

* Construct with a parent widget; the toast positions itself inside that
  parent at the top, centred horizontally.
* Call :meth:`Toast.show_error` with a title, body, and correlation id.
  The cid renders inline (``ref: <cid>``) and is selectable so the user
  can copy it into a support ticket.
* Toast auto-dismisses after 8 s (the default — overridable via the
  ``duration_ms`` constructor arg). A close button always allows manual
  dismissal so power users don't wait through the cooldown.
* No animation in this module — the toast appears immediately. Phase J-4
  owns animation for the overlay; the toast prioritises readability over
  motion (and the Reduce Motion preference would mute any animation here
  anyway).
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
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
    CX_TEXT_SECONDARY,
    FONT_MONO,
    FS_CAPTION,
    FS_FOOTNOTE,
    RADIUS_BUTTON,
    RADIUS_CARD,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
)

logger = logging.getLogger(__name__)


# 8 s default: long enough to read a two-line error + copy the cid, short
# enough that a stale toast doesn't pile up if the user is mid-task. The
# audit plan pins this number for the test contract.
DEFAULT_TOAST_DURATION_MS: Final[int] = 8_000


class Toast(QFrame):
    """Top-bar status surface for daemon errors. Phase J-2.

    Rendering order top-to-bottom:

    * Title row: bold heading + close (×) button.
    * Body paragraph: word-wrapped, secondary tint.
    * Reference row: ``ref: <cid>`` rendered in the mono font, selectable
      with ``TextSelectableByMouse`` so the user can copy the id into a
      support ticket.

    The widget is hidden at construction time; ``show_error`` triggers
    visibility, populates the slots, and arms the auto-dismiss timer.
    """

    dismissed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        duration_ms: int = DEFAULT_TOAST_DURATION_MS,
    ) -> None:
        super().__init__(parent)
        self._duration_ms = duration_ms
        self._current_cid: str = ""

        self.setObjectName("CortexToast")
        # Scope the stylesheet to the objectName so the warm-amber error
        # background doesn't bleed onto child QLabels.
        self.setStyleSheet(
            "QFrame#CortexToast {"
            "  background: rgba(217, 119, 87, 0.10);"
            "  border: 0.5px solid rgba(217, 119, 87, 0.45);"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Toasts are an interactive surface; keyboard users can tab to
        # the close button. The container itself should not steal focus.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP3, SP3, SP3, SP3)
        outer.setSpacing(SP2)

        # Title row.
        title_row = QHBoxLayout()
        title_row.setSpacing(SP2)
        self._title_label = QLabel("")
        self._title_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._title_label.setStyleSheet(
            f"color: {SEMANTIC_LIGHT['label_primary']}; background: transparent;"
        )
        # Title text is selectable too — the user might want to copy a
        # specific error code from the title.
        self._title_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_accessible_name(self._title_label, "Error title")
        title_row.addWidget(self._title_label, stretch=1)

        self._close_btn = QPushButton("×")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._close_btn.setStyleSheet(
            "QPushButton {"
            "  border: none; background: transparent;"
            f"  color: {CX_TEXT_SECONDARY};"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "}"
            "QPushButton:hover { background: rgba(0, 0, 0, 0.06); }"
        )
        # Strong focus so a keyboard user can tab to the close affordance.
        self._close_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        set_accessible_name(self._close_btn, "Dismiss error toast")
        set_accessible_description(
            self._close_btn,
            "Close this error notification.",
        )
        self._close_btn.clicked.connect(self._dismiss)
        title_row.addWidget(self._close_btn, alignment=Qt.AlignmentFlag.AlignTop)

        outer.addLayout(title_row)

        # Body paragraph.
        self._body_label = QLabel("")
        self._body_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._body_label.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        self._body_label.setWordWrap(True)
        self._body_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_accessible_name(self._body_label, "Error details")
        outer.addWidget(self._body_label)

        # Reference row — the cid lives here, mono font for grep-readiness,
        # selectable so the user can copy it into a support ticket.
        ref_row = QHBoxLayout()
        ref_row.setSpacing(SP2)
        ref_label = QLabel("ref:")
        ref_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        ref_label.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        ref_row.addWidget(ref_label)

        self._cid_label = QLabel("")
        self._cid_label.setObjectName("CortexToastCid")
        # The cid is what support engineers grep for; render it in mono
        # so character ambiguity (O/0, I/l) is avoided.
        self._cid_label.setStyleSheet(
            f"font-family: {FONT_MONO};"
            f"font-size: {FS_CAPTION}px;"
            f"color: {BRAND_ACCENT};"
            "background: transparent;"
        )
        # CRITICAL: the cid must be selectable. The audit's user-research
        # finding was that users could see the cid but not copy it.
        self._cid_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_accessible_name(self._cid_label, "Correlation id")
        set_accessible_description(
            self._cid_label,
            "Reference identifier — copy this into a support ticket "
            "so the team can find the matching log entry.",
        )
        ref_row.addWidget(self._cid_label)
        ref_row.addStretch()
        outer.addLayout(ref_row)

        # Auto-dismiss timer.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._duration_ms)
        self._timer.timeout.connect(self._dismiss)

        # Toasts start hidden — show_error reveals.
        self.hide()
        set_accessible_name(self, "Error notification")

    def show_error(self, title: str, body: str, cid: str) -> None:
        """Populate the slots and reveal the toast.

        The cid is rendered as-is. If the daemon failed to mint one,
        callers pass the empty string and the ref row simply shows
        ``ref:`` with nothing after — the user still sees the error;
        only the support-correlation handoff is unavailable. The toast
        never invents a cid.
        """
        self._current_cid = cid or ""
        self._title_label.setText(title or "Error")
        self._body_label.setText(body or "")
        self._cid_label.setText(self._current_cid)
        # Arm the auto-dismiss; restart from scratch if already running so
        # back-to-back errors get their full 8 s read budget.
        self._timer.start(self._duration_ms)
        self.show()
        self.raise_()
        logger.info(
            "Toast surfaced",
            extra={
                "correlation_id": self._current_cid,
                "title": title,
            },
        )

    def show_info(self, title: str, body: str) -> None:
        """B2 (audit-prod): info-toast variant for success / status
        messages (e.g. "Cortex is now using your LLM" after BYOK reload).

        Reuses the same slots as ``show_error`` — only the title/body
        copy and the absence of a cid distinguish the two. The toast
        component itself is style-neutral; callers decide the message
        tone via the title text.
        """
        self._current_cid = ""
        self._title_label.setText(title or "")
        self._body_label.setText(body or "")
        self._cid_label.setText("")
        self._timer.start(self._duration_ms)
        self.show()
        self.raise_()
        logger.info("Toast surfaced (info): %s — %s", title, body)

    @property
    def current_cid(self) -> str:
        """The cid currently displayed (empty string if none / dismissed)."""
        return self._current_cid

    def is_cid_selectable(self) -> bool:
        """Test affordance: confirm the cid label is selectable. Phase J-2
        contract test pins this so a future stylesheet refactor cannot
        silently drop ``TextSelectableByMouse`` (the audit-finding root
        cause was a non-selectable cid)."""
        flags = self._cid_label.textInteractionFlags()
        return bool(flags & Qt.TextInteractionFlag.TextSelectableByMouse)

    def _dismiss(self) -> None:
        self._timer.stop()
        self.hide()
        self._current_cid = ""
        self.dismissed.emit()


__all__ = ["DEFAULT_TOAST_DURATION_MS", "Toast"]
