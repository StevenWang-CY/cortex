"""Reusable desktop-shell widgets and copy helpers.

Why a dedicated module
======================

Every surface (dashboard, settings, connections, history, onboarding) needs
the same small vocabulary: a transient status toast, a capsule segmented
control, a disclosure that keeps rarely-used controls out of the way, a
status pill whose colour never carries meaning alone, and one way to say
"3 hours ago". Keeping them here — rather than duplicated inside each
window module — means the recipes in :mod:`cortex.apps.desktop_shell.tokens`
are applied once and the surfaces cannot drift apart.

Contracts
=========

* :class:`Toast` floats over its parent's content (manual geometry at the
  top) so surfacing an error never reflows the page. It fades in and out
  over ``DURATION_FAST`` (160 ms), instantly under Reduce Motion, and the
  8 s auto-dismiss pauses while the pointer hovers the toast.
* :class:`SegmentedControl` is the single capsule tab control; selection
  changes are emitted exactly once per user gesture and arrow keys move
  between segments when one has keyboard focus.
* :class:`Disclosure` toggles a body widget behind a labelled header.
* :func:`format_relative_age` is the only relative-time formatter in the
  shell (settings freshness row and history staleness caption share it).
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# The opacity fade is real-PySide6 only; the legacy stubbed harness omits
# the animation classes. Guarded at import time so every surface that
# imports this module stays importable there; the toast simply snaps.
try:
    from PySide6.QtCore import QEasingCurve, QPropertyAnimation
    from PySide6.QtWidgets import QGraphicsOpacityEffect

    _ANIMATION_AVAILABLE = True
except ImportError:  # pragma: no cover - lightweight stubs
    QEasingCurve = None
    QPropertyAnimation = None
    QGraphicsOpacityEffect = None
    _ANIMATION_AVAILABLE = False

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.a11y import (
    set_accessible_description,
    set_accessible_name,
)
from cortex.apps.desktop_shell.tokens import (
    BTN_SEGMENT_QSS,
    CX_BG_SECONDARY,
    CX_BORDER_DEFAULT,
    CX_DANGER,
    CX_DANGER_TEXT,
    CX_INFO_TEXT,
    CX_SUCCESS_TEXT,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_WARNING_TEXT,
    DURATION_FAST,
    FONT_MONO,
    FONT_SYSTEM,
    FS_CAPTION,
    FS_FOOTNOTE,
    FW_MEDIUM,
    FW_SEMIBOLD,
    RADIUS_BUTTON,
    RADIUS_CARD,
    RADIUS_PILL,
    SP2,
    SP3,
    SP6,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# Text-overflow helpers (UI-redesign overlap hardening)
# ─────────────────────────────────────────────────────────────────────────
#
# Two complementary tools so no label can ever overlap a neighbour or clip
# its container:
#
# * ``wrap_capped`` — multi-line word-wrap with an optional hard max width.
#   Use for paragraphs / help copy / descriptions where reflowing onto a
#   second line is acceptable and preferable to truncation.
# * ``install_elide`` — single-line tail-elision (``"This month's flo…"``)
#   that recomputes on every resize. Use for headers / pills / inline
#   values where wrapping would break the row rhythm. The FULL text is
#   preserved in the accessible name + tooltip so nothing is lost to
#   sighted-mouse or VoiceOver users; only the on-screen glyphs shorten.


def wrap_capped(label: QLabel, max_width: int | None = None) -> QLabel:
    """Enable word-wrap on ``label`` (+ an optional hard ``max_width``).

    Returns the label for fluent use. Safe against lightweight Qt stubs —
    any AttributeError from a test double is swallowed.
    """
    try:
        label.setWordWrap(True)
        if max_width is not None:
            label.setMaximumWidth(int(max_width))
    except Exception:  # pragma: no cover - lightweight stub
        logger.debug("wrap_capped: widget does not support wrap/max-width")
    return label


class _ElideFilter(QObject):
    """Event filter that keeps a single-line ``QLabel`` tail-elided to its
    current width. Installed by :func:`install_elide`; one filter per label.

    The full text is the source of truth (``set_full_text`` updates it);
    the label's *displayed* text is always the elided projection. The
    accessible name and tooltip mirror the full text so assistive tech and
    hover-discovery never lose information.
    """

    def __init__(self, label: QLabel, mode: Qt.TextElideMode) -> None:
        super().__init__(label)
        self._label = label
        self._mode = mode
        self._full = label.text()
        # Allow the label to shrink below its natural text width so the
        # layout can hand it a constrained rect (otherwise the minimum
        # size hint pins it to the full string and it clips instead).
        try:
            label.setMinimumWidth(0)
            sp = label.sizePolicy()
            sp.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            label.setSizePolicy(sp)
        except Exception:  # pragma: no cover - lightweight stub
            pass
        label.installEventFilter(self)
        self._apply()

    def set_full_text(self, text: str) -> None:
        self._full = text or ""
        self._apply()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self._apply()
        return False

    def _apply(self) -> None:
        try:
            fm = QFontMetrics(self._label.font())
            avail = max(0, self._label.width())
            elided = fm.elidedText(self._full, self._mode, avail)
            if elided != self._label.text():
                self._label.setText(elided)
            # Keep full text reachable for mouse-hover + VoiceOver even when
            # the on-screen text is shortened.
            if elided != self._full:
                self._label.setToolTip(self._full)
                try:
                    self._label.setAccessibleName(self._full)
                except Exception:
                    pass
        except Exception:  # pragma: no cover - lightweight stub
            logger.debug("elide filter apply failed", exc_info=True)


def install_elide(
    label: QLabel,
    mode: Qt.TextElideMode | None = None,
) -> _ElideFilter | None:
    """Make ``label`` tail-elide (default) to its width on every resize.

    Returns the filter so callers can push new text via
    ``filter.set_full_text(...)``; returns ``None`` if the widget can't host
    the filter (lightweight test stub). The full string stays in the
    tooltip + accessible name.
    """
    try:
        if mode is None:
            mode = Qt.TextElideMode.ElideRight
        return _ElideFilter(label, mode)
    except Exception:  # pragma: no cover - lightweight stub
        logger.debug("install_elide: could not attach filter")
        return None


# ─────────────────────────────────────────────────────────────────────────
# Relative time — the one formatter shared by settings + history
# ─────────────────────────────────────────────────────────────────────────


def format_relative_age(delta_seconds: float, *, compact: bool = False) -> str:
    """Render an age in seconds as ``just now`` / ``3 hours ago`` / ``3h ago``.

    Negative deltas (clock drift, future timestamps) clamp to ``just now``
    so the UI never says "in 30 s".
    """
    delta = max(0.0, float(delta_seconds))
    minute = 60.0
    hour = 60.0 * minute
    day = 24.0 * hour
    if delta < minute:
        return "just now"
    if delta < hour:
        n = int(delta // minute)
        unit = "m" if compact else (" minute" if n == 1 else " minutes")
        return f"{n}{unit} ago"
    if delta < day:
        n = int(delta // hour)
        unit = "h" if compact else (" hour" if n == 1 else " hours")
        return f"{n}{unit} ago"
    n = int(delta // day)
    unit = "d" if compact else (" day" if n == 1 else " days")
    return f"{n}{unit} ago"


# ─────────────────────────────────────────────────────────────────────────
# Status pill recipe — colour + copy, never colour alone
# ─────────────────────────────────────────────────────────────────────────

_PILL_TONES: Final[dict[str, tuple[str, str]]] = {
    "neutral": (CX_TEXT_SECONDARY, CX_BG_SECONDARY),
    "success": (CX_SUCCESS_TEXT, "rgba(48, 178, 87, 0.12)"),
    "warning": (CX_WARNING_TEXT, "rgba(217, 161, 0, 0.14)"),
    "danger": (CX_DANGER_TEXT, "rgba(215, 0, 21, 0.10)"),
    "info": (CX_INFO_TEXT, "rgba(10, 132, 255, 0.10)"),
}


def status_pill_qss(tone: str = "neutral") -> str:
    """QSS for a ``QLabel`` status pill whose text tint clears WCAG AA.

    ``tone`` is one of ``neutral`` / ``success`` / ``warning`` / ``danger``
    / ``info``. The fill tints are the semantic colours at low alpha; the
    foreground is always the contrast-safe *text* token, never the raw
    fill (systemGreen is 2.8:1 on white).
    """
    fg, bg = _PILL_TONES.get(tone, _PILL_TONES["neutral"])
    return (
        f"color: {fg}; background: {bg}; border: none;"
        f" border-radius: {RADIUS_PILL}px; padding: 3px 10px;"
        f" font-family: {FONT_SYSTEM}; font-size: {FS_CAPTION}px;"
        f" font-weight: {FW_MEDIUM};"
    )


def status_dot_qss(color: str, *, size: int = 6) -> str:
    """QSS for a small round status dot. Pair it with text — never alone."""
    return (
        f"background: {color}; border-radius: {size // 2}px;"
        " border: none;"
    )


# ─────────────────────────────────────────────────────────────────────────
# Segmented control (dashboard tabs + history sub-navigation)
# ─────────────────────────────────────────────────────────────────────────


class SegmentedControl(QWidget):
    """Capsule segmented control matching ``NSSegmentedControl.capsule``.

    Emits ``selection_changed(int)`` once per selection change, whether the
    change came from a click, an arrow key, or :meth:`set_selected`.
    """

    selection_changed = Signal(int)

    def __init__(
        self,
        labels: list[str],
        parent: QWidget | None = None,
        *,
        role_suffix: str = "tab",
    ) -> None:
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        self._selected = 0
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        track = QFrame()
        track.setObjectName("CortexSegTrack")
        track.setStyleSheet(
            f"#CortexSegTrack {{ background: {CX_BG_SECONDARY};"
            f" border: 1px solid {CX_BORDER_DEFAULT};"
            f" border-radius: {RADIUS_CARD}px; }}"
        )
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(3, 3, 3, 3)
        track_layout.setSpacing(2)
        for index, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            btn.setStyleSheet(BTN_SEGMENT_QSS)
            set_accessible_name(btn, f"{label} {role_suffix}")
            set_accessible_description(btn, f"Switch to the {label} view.")
            btn.clicked.connect(lambda _checked=False, i=index: self.set_selected(i))
            btn.installEventFilter(self)
            self._buttons.append(btn)
            track_layout.addWidget(btn, stretch=1)
        outer.addWidget(track, stretch=1)
        if self._buttons:
            self._buttons[0].setChecked(True)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        # Left/Right move the selection while a segment has keyboard focus.
        if event.type() == QEvent.Type.KeyPress and obj in self._buttons:
            key = getattr(event, "key", lambda: None)()
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                step = -1 if key == Qt.Key.Key_Left else 1
                target = max(0, min(len(self._buttons) - 1, self._selected + step))
                self.set_selected(target)
                self._buttons[target].setFocus(Qt.FocusReason.TabFocusReason)
                return True
        return super().eventFilter(obj, event)

    def selected_index(self) -> int:
        return self._selected

    def set_selected(self, index: int, *, emit: bool = True) -> None:
        """Activate a segment. Out-of-range indices are ignored."""
        if not self._buttons or index < 0 or index >= len(self._buttons):
            logger.debug("SegmentedControl.set_selected: index %d out of range", index)
            return
        for i, b in enumerate(self._buttons):
            b.setChecked(i == index)
        changed = index != self._selected
        self._selected = index
        if emit and (changed or True):
            # A repeated click on the selected segment re-emits so a stacked
            # widget that was navigated programmatically can resync; the
            # signal is idempotent for every listener in the shell.
            self.selection_changed.emit(index)


# ─────────────────────────────────────────────────────────────────────────
# Disclosure — keeps rarely-used controls out of the reading order
# ─────────────────────────────────────────────────────────────────────────


class Disclosure(QWidget):
    """A labelled header that shows/hides a body widget.

    The header is a real checkable button (keyboard reachable, focus ring,
    press feedback) whose accessible name says what it reveals.
    """

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        body: QWidget,
        *,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._body = body
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SP2)
        self._header = QPushButton()
        self._header.setCheckable(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._header.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._header.setStyleSheet(
            "QPushButton {"
            "  text-align: left;"
            f"  padding: 4px 12px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {CX_TEXT_SECONDARY};"
            "  border: 2px solid transparent;"
            "}"
            f"QPushButton:hover {{ color: {CX_TEXT}; }}"
            "QPushButton:pressed { background: rgba(0,0,0,0.05); }"
            f"QPushButton:focus {{ border-color: {CX_BORDER_DEFAULT}; }}"
        )
        set_accessible_name(self._header, f"Show {title}")
        set_accessible_description(
            self._header, f"Expands or collapses the {title} controls."
        )
        try:
            self._header.toggled.connect(self._on_toggled)
        except AttributeError:  # pragma: no cover - stub harness
            pass
        layout.addWidget(self._header)
        layout.addWidget(body)
        self._header.setChecked(bool(expanded))
        self._on_toggled(bool(expanded))

    def _on_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        chevron = "⌄" if checked else "›"
        self._header.setText(f"{chevron}  {self._title}")
        set_accessible_name(
            self._header, f"{'Hide' if checked else 'Show'} {self._title}"
        )
        self.toggled.emit(checked)

    def is_expanded(self) -> bool:
        return self._header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self._header.setChecked(bool(expanded))


# ─────────────────────────────────────────────────────────────────────────
# Toast
# ─────────────────────────────────────────────────────────────────────────

# 8 s default: long enough to read a two-line error + copy the cid, short
# enough that a stale toast doesn't pile up if the user is mid-task. The
# audit plan pins this number for the test contract.
DEFAULT_TOAST_DURATION_MS: Final[int] = 8_000


def _reduced_motion() -> bool:
    try:
        return bool(mac_native.prefers_reduced_motion())
    except Exception:
        return False


class Toast(QFrame):
    """Floating status surface for daemon errors and confirmations.

    Rendering order top-to-bottom:

    * Title row: bold heading + close (×) button.
    * Body paragraph: word-wrapped, secondary tint.
    * Reference row: ``ref: <cid>`` rendered in the mono font, selectable
      with ``TextSelectableByMouse`` so the user can copy the id into a
      support ticket.

    The widget is a *floating* child of its parent (not part of any layout)
    positioned along the top edge by :meth:`reposition`; showing it never
    changes the geometry of the content underneath.
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
        self._mode = "error"
        self._top_offset = SP3

        self.setObjectName("CortexToast")
        self._apply_palette("error")
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
            f"color: {CX_TEXT}; background: transparent;"
        )
        self._title_label.setWordWrap(True)
        self._title_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        set_accessible_name(self._title_label, "Error title")
        title_row.addWidget(self._title_label, stretch=1)

        self._close_btn = QPushButton("×")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._close_btn.setStyleSheet(
            "QPushButton {"
            "  border: 2px solid transparent; background: transparent;"
            f"  color: {CX_TEXT_SECONDARY};"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "}"
            "QPushButton:hover { background: rgba(0, 0, 0, 0.06); }"
            "QPushButton:pressed { background: rgba(0, 0, 0, 0.12); }"
            f"QPushButton:focus {{ border-color: {CX_TEXT_SECONDARY}; }}"
        )
        # Strong focus so a keyboard user can tab to the close affordance.
        self._close_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        set_accessible_name(self._close_btn, "Dismiss notification")
        set_accessible_description(self._close_btn, "Close this notification.")
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
        set_accessible_name(self._body_label, "Notification details")
        outer.addWidget(self._body_label)

        # Reference row — the cid lives here, mono font for grep-readiness,
        # selectable so the user can copy it into a support ticket.
        self._ref_row = QWidget()
        self._ref_row.setStyleSheet("background: transparent;")
        ref_row = QHBoxLayout(self._ref_row)
        ref_row.setContentsMargins(0, 0, 0, 0)
        ref_row.setSpacing(SP2)
        ref_label = QLabel("ref:")
        ref_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        ref_label.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        ref_row.addWidget(ref_label)

        self._cid_label = QLabel("")
        self._cid_label.setObjectName("CortexToastCid")
        self._cid_label.setStyleSheet(
            f"font-family: {FONT_MONO};"
            f"font-size: {FS_CAPTION}px;"
            f"color: {CX_TEXT}; font-weight: {FW_SEMIBOLD};"
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
        outer.addWidget(self._ref_row)

        # Auto-dismiss timer.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._duration_ms)
        self._timer.timeout.connect(self._dismiss)

        # Opacity effect + one reusable, interruptible animation. Absent
        # (stub harness / animation failure) the toast snaps in and out.
        self._opacity: QGraphicsOpacityEffect | None = None
        self._fade: QPropertyAnimation | None = None
        self._fading_out = False
        if _ANIMATION_AVAILABLE:
            try:
                effect = QGraphicsOpacityEffect(self)
                effect.setOpacity(1.0)
                self.setGraphicsEffect(effect)
                fade = QPropertyAnimation(effect, b"opacity", self)
                fade.setDuration(DURATION_FAST)
                fade.setEasingCurve(QEasingCurve.Type.OutCubic)
                fade.finished.connect(self._on_fade_finished)
                self._opacity = effect
                self._fade = fade
            except Exception:  # pragma: no cover - defensive
                logger.debug("toast fade unavailable; snapping", exc_info=True)
                self._opacity = None
                self._fade = None

        # Toasts start hidden — show_error / show_info reveal.
        self.hide()
        set_accessible_name(self, "Error notification")

    # -- geometry ---------------------------------------------------------

    def set_top_offset(self, top: int) -> None:
        """Vertical inset from the parent's top edge (e.g. below a tab bar)."""
        self._top_offset = max(0, int(top))
        if self.isVisible():
            self.reposition()

    def reposition(self) -> None:
        """Pin the toast along the top edge of its parent, inset by SP6.

        Called by the owner on every resize and by ``show_*`` so the toast
        never participates in the parent's layout.
        """
        parent = self.parentWidget()
        if parent is None:
            return
        width = max(120, parent.width() - 2 * SP6)
        self.setFixedWidth(width)
        self.adjustSize()
        self.move(SP6, self._top_offset)
        self.raise_()

    # -- public API -------------------------------------------------------

    def show_error(self, title: str, body: str, cid: str) -> None:
        """Populate the slots and reveal the toast.

        The cid is rendered as-is. If the daemon failed to mint one,
        callers pass the empty string and the ref row is hidden — the
        user still sees the error; only the support-correlation handoff
        is unavailable. The toast never invents a cid.
        """
        self._current_cid = cid or ""
        self._apply_palette("error")
        set_accessible_name(self, "Error notification")
        set_accessible_name(self._title_label, "Error title")
        self._title_label.setText(title or "Error")
        self._body_label.setText(body or "")
        self._cid_label.setText(self._current_cid)
        self._ref_row.setVisible(bool(self._current_cid))
        self._present()
        logger.info(
            "Toast surfaced",
            extra={"correlation_id": self._current_cid, "title": title},
        )

    def show_info(self, title: str, body: str) -> None:
        """Success / status variant (e.g. "Cortex is now using your LLM")."""
        self._current_cid = ""
        self._apply_palette("info")
        set_accessible_name(self, "Notification")
        set_accessible_name(self._title_label, "Notification title")
        self._title_label.setText(title or "")
        self._body_label.setText(body or "")
        self._cid_label.setText("")
        self._ref_row.setVisible(False)
        self._present()
        logger.info("Toast surfaced (info): %s — %s", title, body)

    @property
    def current_cid(self) -> str:
        """The cid currently displayed (empty string if none / dismissed)."""
        return self._current_cid

    @property
    def mode(self) -> str:
        """``"error"`` or ``"info"`` — whichever palette is applied."""
        return self._mode

    def is_cid_selectable(self) -> bool:
        """Test affordance: confirm the cid label is selectable."""
        flags = self._cid_label.textInteractionFlags()
        return bool(flags & Qt.TextInteractionFlag.TextSelectableByMouse)

    # -- internals --------------------------------------------------------

    def _present(self) -> None:
        # Arm the auto-dismiss; restart from scratch if already running so
        # back-to-back messages get their full read budget.
        self._timer.start(self._duration_ms)
        # ``isHidden`` rather than ``isVisible``: the toast is a child of
        # the dashboard, which may itself not be on screen yet.
        was_visible = not self.isHidden()
        self._fading_out = False
        fade = self._fade
        effect = self._opacity
        if fade is not None:
            fade.stop()
        self.reposition()
        self.show()
        self.raise_()
        if fade is None or effect is None or _reduced_motion():
            if effect is not None:
                effect.setOpacity(1.0)
            return
        fade.setStartValue(effect.opacity() if was_visible else 0.0)
        fade.setEndValue(1.0)
        fade.start()

    def _apply_palette(self, mode: str) -> None:
        """Swap the frame tint to match the semantic: danger for errors,
        the contrast-safe success text tint for confirmations."""
        self._mode = "info" if mode == "info" else "error"
        if self._mode == "info":
            bg = "rgba(27, 107, 60, 0.08)"
            border = "rgba(27, 107, 60, 0.45)"
            accent = CX_SUCCESS_TEXT
        else:
            bg = "rgba(215, 0, 21, 0.06)"
            border = "rgba(215, 0, 21, 0.45)"
            accent = CX_DANGER
        self.setStyleSheet(
            "QFrame#CortexToast {"
            f"  background: {bg};"
            f"  border: 1px solid {border};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
            f"QFrame#CortexToast QLabel#CortexToastTitleAccent {{ color: {accent}; }}"
        )

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        # Hover pauses the auto-dismiss so the user can read and copy.
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        if not self.isHidden() and not self._fading_out:
            self._timer.start(self._duration_ms)
        super().leaveEvent(event)

    def _dismiss(self) -> None:
        self._timer.stop()
        self._current_cid = ""
        if self.isHidden():
            return
        # The user's decision is final now; the fade is presentation only.
        self.dismissed.emit()
        fade = self._fade
        effect = self._opacity
        # No fade when nothing is on screen to fade (parent hidden) — the
        # animation would only delay the state change.
        if fade is None or effect is None or _reduced_motion() or not self.isVisible():
            if fade is not None:
                fade.stop()
            self._fading_out = False
            self.hide()
            if effect is not None:
                effect.setOpacity(1.0)
            return
        self._fading_out = True
        fade.stop()
        fade.setStartValue(effect.opacity())
        fade.setEndValue(0.0)
        fade.start()

    def _on_fade_finished(self) -> None:
        if self._fading_out:
            self._fading_out = False
            self.hide()
            if self._opacity is not None:
                self._opacity.setOpacity(1.0)


__all__ = [
    "DEFAULT_TOAST_DURATION_MS",
    "Disclosure",
    "SegmentedControl",
    "Toast",
    "format_relative_age",
    "install_elide",
    "status_dot_qss",
    "status_pill_qss",
    "wrap_capped",
]
