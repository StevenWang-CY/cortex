"""Desktop Shell — Dashboard Window (macOS-native refactor).

Two-tab layout:
    Tab 1 "Dashboard" — Consumer biometrics view (Cormorant numerics, terracotta
                        accent, native typography & spacing)
    Tab 2 "Advanced"  — Developer debug view: HR trace, signal quality, scores

The visual layer is now driven by:

* :mod:`cortex.apps.desktop_shell.tokens` (emitted from
  ``cortex/libs/design/tokens.yaml``) — semantic palette, 5-step type scale,
  HIG-compliant spacing & radii.
* :mod:`cortex.apps.desktop_shell.mac_native` — system font, NSVisualEffectView
  vibrancy, unified title bar. Brand identity (terracotta accent +
  Cormorant Garamond wordmark/numerics + ECG heartbeat motif) is preserved on
  top of native materials.

All public Signals, slots, and update methods are byte-identical to the
pre-refactor implementation so :mod:`cortex.apps.desktop_shell.controller`
and :mod:`cortex.apps.desktop_shell.main` do not need to change.
"""

from __future__ import annotations

import collections
import logging
import time

from PySide6.QtCore import Qt, QTimer, Signal

try:
    from PySide6.QtCore import QRectF
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtCore import QRect as QRectF
try:
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtGui import QColor, QFont, QPainter, QPen

    class QPainterPath:  # type: ignore[override]
        def addRoundedRect(self, *_args: object, **_kwargs: object) -> None:
            return

        def moveTo(self, *_args: object, **_kwargs: object) -> None:
            return

        def lineTo(self, *_args: object, **_kwargs: object) -> None:
            return
try:
    from PySide6.QtWidgets import (
        QButtonGroup,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtWidgets import (  # type: ignore[attr-defined]
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    class QButtonGroup:  # type: ignore[override]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def addButton(self, *_args: object, **_kwargs: object) -> None:
            return

        def setExclusive(self, *_args: object, **_kwargs: object) -> None:
            return

    class QLineEdit(QLabel):  # type: ignore[override]
        def setPlaceholderText(self, *_args: object, **_kwargs: object) -> None:
            return

    class QScrollArea(QWidget):  # type: ignore[override]
        def setWidgetResizable(self, *_args: object, **_kwargs: object) -> None:
            return

        def setWidget(self, *_args: object, **_kwargs: object) -> None:
            return

    class QSizePolicy:  # type: ignore[override]
        class Policy:
            Expanding = 0
            Preferred = 0

    class QStackedWidget(QWidget):  # type: ignore[override]
        def addWidget(self, *_args: object, **_kwargs: object) -> None:
            return

        def setCurrentIndex(self, *_args: object, **_kwargs: object) -> None:
            return

# Tab widget compatibility shim retained for test harness even though the new
# dashboard uses a segmented control + QStackedWidget. Some downstream tests
# still reference QTabWidget at import time.
try:
    from PySide6.QtWidgets import QTabWidget  # noqa: F401 - re-exported
except ImportError:  # pragma: no cover
    pass

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BIO_BLINK,
    BIO_HR,
    BIO_HRV,
    BRAND_ACCENT,
    BRAND_DISPLAY_FONT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    DASHBOARD_MAX_HEIGHT,
    DASHBOARD_WIDTH,
    FONT_MONO,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_HERO_NUMERIC,
    FS_TITLE,
    FW_REGULAR,
    FW_SEMIBOLD,
    RADIUS_CARD,
    RADIUS_PILL,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
    SP4,
    SP5,
    SP6,
    STATE_COLORS,
    STATE_LABELS,
)

logger = logging.getLogger(__name__)

_MAX_HR_HISTORY = 120
_MAX_TIMELINE_EVENTS = 50

# F34: how long to keep the Stop button disabled before assuming the daemon
# shutdown is stuck and re-enabling so the user can try again. 10 s matches
# the audit-plan budget; controller's ``daemon_stopped`` signal short-circuits
# this when the daemon actually reports stopped.
_STOP_SAFETY_TIMEOUT_MS = 10_000

# Resolved semantic colors. These hex strings are dev-mode fallbacks; on
# macOS, ``mac_native`` re-tints widgets at runtime when the user toggles
# light/dark mode (see :func:`mac_native.install_appearance_observer`).
_WINDOW_BG = SEMANTIC_LIGHT["window_bg"]
_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_GROUPED_BG = SEMANTIC_LIGHT["grouped_bg"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
# F55 + audit-w2: the warm-greyscale label tints now live in the token
# registry. Tertiary is the AA-passing "#6B6661" (~5.4:1 on #FFFFFF) — was
# previously a sub-AA value (3.98:1). Source-of-truth: tokens.py.
_LABEL_SECONDARY = CX_TEXT_SECONDARY
_LABEL_TERTIARY = CX_TEXT_TERTIARY
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_DANGER = SEMANTIC_LIGHT["danger"]


def _set_accessible_name(widget: object, name: str) -> None:
    """Wrapper for ``setAccessibleName`` that no-ops cleanly when the
    target widget is a lightweight test stub without that method (F55)."""
    fn = getattr(widget, "setAccessibleName", None)
    if callable(fn):
        try:
            fn(name)
        except Exception:
            pass


def _set_accessible_description(widget: object, description: str) -> None:
    """Wrapper for ``setAccessibleDescription`` — see :func:`_set_accessible_name`."""
    fn = getattr(widget, "setAccessibleDescription", None)
    if callable(fn):
        try:
            fn(description)
        except Exception:
            pass


def _set_tab_order(first: object, second: object) -> None:
    """Wrapper for ``QWidget.setTabOrder`` that degrades cleanly when
    PySide6 has been swapped out for the lightweight test stubs (F55)."""
    fn = getattr(QWidget, "setTabOrder", None)
    if callable(fn):
        try:
            fn(first, second)
        except Exception:
            pass


def _system(point_size: float, weight: str = "regular") -> str:
    """Return a Qt stylesheet font-family value resolving to the system font.

    Used inside QSS strings where a literal stack is required. The companion
    helper :func:`mac_native.system_font` returns an actual ``QFont`` for use
    with ``setFont()`` calls.
    """
    return '-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif'


# ---------------------------------------------------------------------------
# Global stylesheet — minimal, semantic
# ---------------------------------------------------------------------------

_GLOBAL_QSS = f"""
QWidget#CortexDashboard {{
    background-color: transparent;
}}
QLineEdit {{
    selection-background-color: {BRAND_ACCENT};
}}
QToolTip {{
    background-color: {_CONTROL_BG};
    color: {_LABEL};
    border: 1px solid {_SEPARATOR};
    padding: 4px 8px;
    border-radius: 6px;
}}
"""


# ---------------------------------------------------------------------------
# Native-style segmented control (capsule pill, two segments)
# ---------------------------------------------------------------------------

class _MacSegmentedControl(QWidget):
    """Two-segment capsule pill matching ``NSSegmentedControl.capsule`` look.

    Emits ``selection_changed(int)`` when the user clicks a segment. Used in
    place of the previous ``QTabWidget`` underline-accent bar (which is a
    Chrome/Material pattern, not a Mac one).
    """

    selection_changed = Signal(int)

    def __init__(self, labels: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        track = QFrame()
        track.setObjectName("_seg_track")
        track.setStyleSheet(
            f"#_seg_track {{ background: {_GROUPED_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: 8px; }}"
        )
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(3, 3, 3, 3)
        track_layout.setSpacing(2)
        for index, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
            # Phase J-5 a11y sweep: segmented-control buttons need
            # explicit accessible names (the visible label is the
            # text but VoiceOver also needs the role context to
            # announce "tab — Dashboard, selected"), and StrongFocus
            # so the keyboard tab cycle reaches them rather than the
            # default WheelFocus which excludes them from tabbing.
            _set_accessible_name(btn, f"{label} tab")
            _set_accessible_description(btn, f"Switch to the {label} view.")
            try:
                btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            except Exception:
                pass
            btn.setStyleSheet(
                "QPushButton {"
                "  padding: 4px 14px;"
                "  border-radius: 6px;"
                "  background: transparent;"
                f"  color: {_LABEL_SECONDARY};"
                "  border: none;"
                "}"
                f"QPushButton:hover {{ color: {_LABEL}; }}"
                "QPushButton:checked {"
                f"  background: {_CONTROL_BG};"
                f"  color: {_LABEL};"
                f"  font-weight: {FW_SEMIBOLD};"
                "}"
            )
            btn.clicked.connect(lambda _checked=False, i=index: self._on_clicked(i))
            self._group.addButton(btn, index)
            self._buttons.append(btn)
            track_layout.addWidget(btn, stretch=1)
        outer.addWidget(track, stretch=1)
        if self._buttons:
            self._buttons[0].setChecked(True)

    def _on_clicked(self, index: int) -> None:
        for i, b in enumerate(self._buttons):
            b.setChecked(i == index)
        self.selection_changed.emit(index)


# ---------------------------------------------------------------------------
# Tab 1: Consumer Dashboard
# ---------------------------------------------------------------------------

class _ConsumerTab(QWidget):
    """Clean biometrics dashboard — native materials, brand identity intact."""

    # E.1: surface user intent for the daemon orchestrator. The shell only
    # owns the widgets; the parent dashboard re-emits these signals so the
    # desktop app (in-process or WebSocket mode) can route them to
    # ``RuntimeDaemon._handle_user_action`` and to ``_shutdown_daemon``.
    stop_requested = Signal()
    goal_set = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: transparent; color: {_LABEL};")

        # F34: state machine for the Stop button. ``_stopping`` flips to True
        # on first click and back to False on ``notify_daemon_stopped`` (or
        # the safety-timer expiry). Coalesces double-clicks at the slot level.
        self._stopping: bool = False
        # F31: per-widget cache of last applied text + stylesheet so the
        # 2 Hz state broadcast loop does not push identical values through
        # Qt's restyle / paint chain when the user's state is unchanged.
        # Keyed by id(widget) because QWidget is not hashable on every Qt build.
        self._render_cache: dict[int, dict[str, str]] = {}
        # Phase J-3: empty-state flag. Flips False on the first ``update_state``
        # call so the placeholder paragraph in the biometrics card vanishes
        # and the live BPM / HRV / BLK numerics take over. The flag is sticky
        # — once Cortex has rendered live data, subsequent reconnects keep
        # the live UI (the dashboard reuses cached values rather than
        # collapsing back to "no data yet").
        self._has_received_state: bool = False

        root = QVBoxLayout(self)
        root.setContentsMargins(SP6, SP5, SP6, SP6)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, SP5)

        # Brand wordmark (preserved — Cormorant italic, terracotta is the
        # signature contrast). HIG section-heading conventions don't apply to
        # the wordmark; it's the app identity.
        brand = QLabel("Cortex")
        brand.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-style: italic; font-size: {FS_TITLE}px;"
            f"font-weight: {FW_REGULAR};"
            f"color: {_LABEL}; background: transparent;"
        )
        header.addWidget(brand)
        header.addStretch()

        # State pill — capsule with dot + label, sits on the grouped background.
        self._state_badge = QWidget()
        badge_layout = QHBoxLayout(self._state_badge)
        badge_layout.setContentsMargins(10, 3, 12, 3)
        badge_layout.setSpacing(6)

        self._state_dot = QLabel()
        self._state_dot.setFixedSize(7, 7)
        self._state_dot.setStyleSheet(
            f"background: {_LABEL_TERTIARY}; border-radius: 3px;"
        )
        badge_layout.addWidget(self._state_dot, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_label = QLabel("Disconnected")
        self._state_label.setFont(mac_native.system_font(FS_FOOTNOTE - 1, "medium"))
        self._state_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        badge_layout.addWidget(self._state_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_badge.setStyleSheet(
            f"background: {_GROUPED_BG}; border-radius: {RADIUS_PILL}px;"
        )
        header.addWidget(self._state_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        # ── Goal input — minimum width, flexible (HIG: avoid fixed sizes) ──
        self._goal_input = QLineEdit()
        self._goal_input.setPlaceholderText("What are you working on?")
        self._goal_input.setMinimumHeight(36)
        # F55: accessible name + description for VoiceOver / screen
        # readers. Wrapped because the legacy MockQLineEdit stub in
        # test_desktop_shell.py does not expose these QWidget methods.
        _set_accessible_name(self._goal_input, "Goal")
        _set_accessible_description(
            self._goal_input,
            "Tell Cortex what you're working on so suggestions match your intent.",
        )
        self._goal_input.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._goal_input.setStyleSheet(
            "QLineEdit {"
            f"  padding: 0 {SP4}px;"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: 6px;"
            f"  color: {_LABEL};"
            f"  background: {_CONTROL_BG};"
            "}"
            f"QLineEdit:focus {{ border: 1.5px solid {BRAND_ACCENT}; }}"
            f"QLineEdit::placeholder {{ color: {_LABEL_TERTIARY}; }}"
        )
        # E.1: emit goal_set when the user hits return.
        # F33: debounce the goal-set emission. A held-down Return key fires
        # ``returnPressed`` repeatedly (Qt key auto-repeat); without a
        # coalescer the daemon receives N rapid-fire goals, the LLM kicks
        # off N planner calls, and the user pays the latency + cost of
        # the bursts. Schedule a single 150 ms singleShot per burst and
        # ignore subsequent presses while one is pending — the emit reads
        # the latest input text at fire time, so the user still gets the
        # value they typed last.
        self._goal_debounce_pending = False

        def _schedule_goal_emit() -> None:
            if self._goal_debounce_pending:
                return
            self._goal_debounce_pending = True
            QTimer.singleShot(150, _fire_goal_emit)

        def _fire_goal_emit() -> None:
            self._goal_debounce_pending = False
            self.goal_set.emit(self._goal_input.text().strip())

        self._goal_input.returnPressed.connect(_schedule_goal_emit)
        # Expose the scheduler for tests so they can drive the coalescer
        # deterministically (the QTimer.singleShot path needs an event
        # loop tick which the offscreen test harness provides via
        # ``QApplication.processEvents``).
        self._schedule_goal_emit = _schedule_goal_emit
        self._fire_goal_emit = _fire_goal_emit
        root.addWidget(self._goal_input)
        root.addSpacing(SP5)

        # ── Biometrics inset section (no shadow, hairline border) ──
        # NB: Qt's stylesheet selector ``QFrame`` matches QFrame *and every
        # subclass* (incl. QLabel, QLCDNumber, QStackedWidget). Without the
        # objectName scope, the card's white-background / hairline-border /
        # 8px-radius leak into every QLabel descendant, which scrambles
        # text rendering (see the Connections panel regression). All six
        # card stylesheets in desktop_shell are scoped this way.
        bio_card = QFrame()
        bio_card.setObjectName("CortexBioCard")
        bio_card.setStyleSheet(
            "QFrame#CortexBioCard {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        bio_inner = QVBoxLayout(bio_card)
        bio_inner.setContentsMargins(SP5, SP4, SP5, SP4)
        bio_inner.setSpacing(SP3)

        # Sentence-case section heading (HIG) — no letter-spacing, secondary color.
        bio_heading = QLabel("Biometrics")
        bio_heading.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        bio_heading.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        bio_inner.addWidget(bio_heading)

        # Phase J-3: empty-state placeholder. Pre-first-frame the BPM /
        # HRV / BLK numerics carry placeholder "--" glyphs but that
        # reads as "stuck at zero" rather than "we haven't started yet".
        # The placeholder paragraph below sets the expectation: nothing
        # is broken; the daemon simply hasn't captured a frame. Hidden
        # the moment ``update_state`` arrives.
        self._bio_empty_state = QLabel(
            "Start a session to see your biometrics."
        )
        self._bio_empty_state.setObjectName("CortexBioEmptyState")
        self._bio_empty_state.setWordWrap(True)
        self._bio_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bio_empty_state.setFont(
            mac_native.system_font(FS_CAPTION, "regular")
        )
        self._bio_empty_state.setStyleSheet(
            "QLabel#CortexBioEmptyState {"
            f"  color: {_LABEL_TERTIARY};"
            "  background: transparent;"
            "  padding: 2px 0 6px 0;"
            "  font-style: italic;"
            "}"
        )
        _set_accessible_name(self._bio_empty_state, "Biometrics empty state")
        bio_inner.addWidget(self._bio_empty_state)

        bio_row = QHBoxLayout()
        bio_row.setSpacing(0)

        self._bpm_label = QLabel("--")
        self._hrv_label = QLabel("--")
        self._blk_label = QLabel("--")

        for val_widget, title, color in [
            (self._bpm_label, "BPM", BIO_HR),
            (self._hrv_label, "HRV", BIO_HRV),
            (self._blk_label, "BLK", BIO_BLINK),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)

            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # Brand identity — Cormorant numerics, terracotta channel
            # accents — preserved across the macOS refactor.
            val_widget.setStyleSheet(
                f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
                f"font-size: {FS_HERO_NUMERIC}px;"
                f"font-weight: {FW_REGULAR};"
                f"color: {_LABEL};"
                f"background: transparent; border: none;"
            )

            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
            heading.setStyleSheet(
                f"color: {color}; background: transparent; border: none;"
            )
            col.addWidget(val_widget)
            col.addWidget(heading)
            bio_row.addLayout(col, stretch=1)

        bio_inner.addLayout(bio_row)
        root.addWidget(bio_card)
        root.addSpacing(SP4)

        # ── Connections row ───────────────────────────────────────────
        conn_row = QHBoxLayout()
        conn_row.setContentsMargins(SP2, 0, SP2, 0)
        conn_row.setSpacing(SP4)

        self._conn_dots: dict[str, QLabel] = {}
        for name in ("Chrome", "Edge", "Editor"):
            dot = QLabel()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet(
                f"background: {_LABEL_TERTIARY}; border-radius: 3px;"
            )
            lbl = QLabel(name)
            lbl.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            lbl.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            conn_row.addWidget(dot, alignment=Qt.AlignmentFlag.AlignVCenter)
            conn_row.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
            self._conn_dots[name] = dot

        conn_row.addStretch()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # F55: accessible name for VoiceOver.
        _set_accessible_name(self._connect_btn, "Open Connections panel")
        # Phase J-5: QPushButton defaults to TabFocus on most platforms
        # but macOS Qt builds occasionally inherit WheelFocus, which
        # silently excludes the button from the keyboard tab cycle.
        # StrongFocus is the union of Tab + Click + Wheel and is the
        # safe default for any user-driven control.
        try:
            self._connect_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._connect_btn.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._connect_btn.setStyleSheet(
            "QPushButton {"
            f"  color: {BRAND_ACCENT};"
            f"  background: transparent;"
            f"  border: none;"
            f"  padding: 4px 0;"
            "}"
            f"QPushButton:hover {{ color: {_LABEL}; }}"
        )
        conn_row.addWidget(self._connect_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(conn_row)
        root.addSpacing(SP5)

        # ── Divider (hairline, system separator) ───────────────────────
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {_SEPARATOR};")
        root.addWidget(divider)
        root.addSpacing(SP5)

        # ── Today stats — sentence-case, no letter-spacing ────────────
        today_label = QLabel("Today")
        today_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        today_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        root.addWidget(today_label)
        root.addSpacing(SP3)

        today_row = QHBoxLayout()
        today_row.setSpacing(0)

        self._today_focus = QLabel("--")
        self._today_sessions = QLabel("--")
        self._today_best = QLabel("--")
        self._today_blocked = QLabel("--")

        for val_widget, title in [
            (self._today_focus, "Focus"),
            (self._today_sessions, "Sessions"),
            (self._today_best, "Best"),
            (self._today_blocked, "Blocked"),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setStyleSheet(
                f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
                f"font-size: {FS_TITLE}px;"
                f"color: {_LABEL};"
                f"background: transparent;"
            )
            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            heading.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            col.addWidget(val_widget)
            col.addWidget(heading)
            today_row.addLayout(col, stretch=1)

        root.addLayout(today_row)
        root.addStretch()

        # ── Stop button (HIG destructive role) ─────────────────────────
        root.addSpacing(SP4)
        self._stop_btn = QPushButton("Stop Cortex")
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setMinimumHeight(36)  # HIG tap target ≥ 44 once font padding factored
        self._stop_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._stop_btn.setShortcut("Ctrl+Q")  # VoiceOver picks this up
        _set_accessible_name(self._stop_btn, "Stop Cortex")
        # Phase J-5: ensure the destructive Stop button is keyboard
        # reachable on every Qt build. The shortcut alone doesn't put
        # the button into the tab cycle.
        try:
            self._stop_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._stop_btn.setStyleSheet(
            "QPushButton {"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  background: {_CONTROL_BG};"
            f"  color: {_DANGER};"
            f"  border-radius: 8px;"
            f"  padding: 6px 14px;"
            "}"
            f"QPushButton:hover {{ background: rgba(215, 0, 21, 0.06); }}"
            f"QPushButton:pressed {{ background: rgba(215, 0, 21, 0.12); }}"
        )
        # E.1: emit stop_requested so the parent dashboard re-emits and the
        # app-level handler calls _shutdown_daemon.
        # F34: clicking the button transitions to the "stopping" state.
        # The button disables itself, displays "Stopping…", and arms a safety
        # timer that re-enables after `_STOP_SAFETY_TIMEOUT_MS` even if the
        # daemon never reports `daemon_stopped`. Double-click coalesces to a
        # single emission because the second click hits a disabled button.
        self._stop_safety_timer = QTimer(self)
        self._stop_safety_timer.setSingleShot(True)
        self._stop_safety_timer.setInterval(_STOP_SAFETY_TIMEOUT_MS)
        self._stop_safety_timer.timeout.connect(self._stop_safety_expired)
        self._stop_btn.clicked.connect(self._handle_stop_clicked)
        root.addWidget(self._stop_btn)

        # F55: explicit tab-order chain. Without setTabOrder, Qt falls
        # back to widget-creation order which is usually right but is not
        # contractual — a single re-ordering of constructor lines can
        # silently scramble VoiceOver / keyboard navigation. The chain
        # below is the canonical reading order: Goal → Connect → Stop.
        _set_tab_order(self._goal_input, self._connect_btn)
        _set_tab_order(self._connect_btn, self._stop_btn)

    # -- Public update methods (preserved byte-identical) ----------------

    def _set_text_if_changed(self, widget: QLabel, text: str) -> bool:
        """Call ``widget.setText`` only when the value differs from the
        last applied text. Returns True if a write occurred. F31."""
        slot = self._render_cache.setdefault(id(widget), {})
        if slot.get("text") == text:
            return False
        slot["text"] = text
        widget.setText(text)
        return True

    def _set_style_if_changed(self, widget: QWidget, qss: str) -> bool:
        """Call ``widget.setStyleSheet`` only when the QSS differs from
        the last applied stylesheet. Returns True if a write occurred. F31."""
        slot = self._render_cache.setdefault(id(widget), {})
        if slot.get("style") == qss:
            return False
        slot["style"] = qss
        widget.setStyleSheet(qss)
        return True

    def update_state(self, payload: dict) -> None:
        # Phase J-3: first frame retires the empty state. The flag is
        # sticky so a transient WS disconnect doesn't collapse the UI
        # back to "no data yet" — the rendered numerics carry the last
        # known reading, which is more useful than a placeholder.
        if not self._has_received_state:
            self._has_received_state = True
            try:
                self._bio_empty_state.setVisible(False)
            except Exception:
                # Lightweight mock widgets may not expose setVisible —
                # the flag itself is what the contract pins on.
                pass

        state = payload.get("state", "FLOW")
        color = STATE_COLORS.get(state, _LABEL_TERTIARY)
        label = STATE_LABELS.get(state, state)
        self._set_style_if_changed(
            self._state_dot, f"background: {color}; border-radius: 3px;"
        )
        self._set_text_if_changed(self._state_label, label)
        self._set_style_if_changed(
            self._state_label, f"color: {color}; background: transparent;"
        )

        bio = payload.get("biometrics", {})
        hr = bio.get("heart_rate")
        hrv = bio.get("hrv_rmssd")
        blink = bio.get("blink_rate")
        self._set_text_if_changed(
            self._bpm_label, f"{hr:.0f}" if hr is not None else "--"
        )
        self._set_text_if_changed(
            self._hrv_label, f"{hrv:.0f}" if hrv is not None else "--"
        )
        self._set_text_if_changed(
            self._blk_label, f"{blink:.1f}" if blink is not None else "--"
        )

        # Audit-2 fix: drive the Today panel from accumulated session
        # stats instead of leaving the placeholder "--" labels. We
        # accumulate FLOW seconds, count interventions seen, and track
        # the longest contiguous FLOW streak. Approximation is
        # acceptable here — these are at-a-glance numerics, not
        # research data — and is better than dead UI.
        try:
            self._accumulate_today_stats(state)
        except Exception:
            # Don't let a stats bug crash state rendering.
            pass

        # G1 (audit-prod): the daemon stamps the currently-IDENTIFY-ed
        # client types into every STATE_UPDATE. Map daemon-side names
        # (chrome / edge / vscode) onto the dashboard's dot keys
        # (Chrome / Edge / Editor) and update each in turn. The mapping
        # is deliberately one-way; a daemon-side type that the dashboard
        # doesn't render is silently dropped.
        try:
            connected = payload.get("connected_clients")
            if isinstance(connected, list):
                _CLIENT_TYPE_TO_DOT = {
                    "chrome": "Chrome",
                    "edge": "Edge",
                    "vscode": "Editor",
                }
                connected_set = {str(c).lower() for c in connected}
                for ct, dot_name in _CLIENT_TYPE_TO_DOT.items():
                    self.set_extension_connected(dot_name, ct in connected_set)
        except Exception:
            pass

    # G3 (audit-prod): seconds without a STATE_UPDATE before we consider
    # the prior session ended (daemon stopped / network blip / sleep).
    _TODAY_SESSION_GAP_SECONDS = 1800.0  # 30 min

    def _reset_today_stats(self) -> None:
        """Reset every Today/* accumulator. Called on a long gap between
        STATE_UPDATEs, on a local-date rollover, or when the daemon
        connection drops (so the user doesn't see yesterday's numbers
        mixed into today's). Idempotent.
        """
        import time as _t

        self._today_last_tick = _t.monotonic()
        self._today_flow_seconds = 0.0
        self._today_current_streak = 0.0
        self._today_best_streak = 0.0
        self._today_intervention_count = 0
        self._today_session_yday = _t.localtime().tm_yday
        self._today_session_started_at = self._today_last_tick
        try:
            self._set_text_if_changed(self._today_focus, "0m")
            self._set_text_if_changed(self._today_sessions, "1")
            self._set_text_if_changed(self._today_best, "0s")
            self._set_text_if_changed(self._today_blocked, "0")
        except Exception:
            pass

    def _accumulate_today_stats(self, state: str) -> None:
        import time as _t

        now = _t.monotonic()
        # Lazy-init counters on first frame (the dashboard widget
        # constructor doesn't see ``time.monotonic`` to avoid early
        # import side effects).
        if not hasattr(self, "_today_last_tick"):
            self._reset_today_stats()

        # G3 (audit-prod): if the daemon went away for a while (>30 min
        # gap) OR the calendar date rolled over, reset all accumulators
        # so the user sees fresh numbers, not yesterday's tail.
        gap = now - self._today_last_tick
        yday_now = _t.localtime().tm_yday
        if (
            gap > self._TODAY_SESSION_GAP_SECONDS
            or yday_now != getattr(self, "_today_session_yday", yday_now)
        ):
            self._reset_today_stats()

        dt = max(0.0, min(now - self._today_last_tick, 2.0))
        self._today_last_tick = now
        if state == "FLOW":
            self._today_flow_seconds += dt
            self._today_current_streak += dt
            if self._today_current_streak > self._today_best_streak:
                self._today_best_streak = self._today_current_streak
        else:
            self._today_current_streak = 0.0
        # Format Focus (h:mm) and Best (m:ss / h:mm) compactly.
        focus_m = int(self._today_flow_seconds // 60)
        focus_h, focus_m = divmod(focus_m, 60)
        focus_text = f"{focus_h}h{focus_m:02d}" if focus_h else f"{focus_m}m"
        best_m = int(self._today_best_streak // 60)
        best_h, best_m = divmod(best_m, 60)
        best_text = (
            f"{best_h}h{best_m:02d}"
            if best_h
            else (f"{best_m}m" if best_m else f"{int(self._today_best_streak)}s")
        )
        self._set_text_if_changed(self._today_focus, focus_text)
        self._set_text_if_changed(self._today_sessions, "1")
        self._set_text_if_changed(self._today_best, best_text)
        self._set_text_if_changed(
            self._today_blocked, str(self._today_intervention_count)
        )

    def record_intervention_seen(self) -> None:
        """Audit-2 fix: invoked by the parent dashboard when an
        intervention is broadcast so the Today/Blocked counter advances.
        """
        if not hasattr(self, "_today_intervention_count"):
            self._today_intervention_count = 0
        self._today_intervention_count += 1
        try:
            self._set_text_if_changed(
                self._today_blocked, str(self._today_intervention_count)
            )
        except Exception:
            pass

    def set_extension_connected(self, name: str, connected: bool) -> None:
        """Audit-2 fix: update the Chrome / Edge / Editor connection dots.

        ``name`` is matched case-insensitively against the constructed
        keys ("Chrome", "Edge", "Editor"). Unknown names are ignored.
        """
        dot = None
        for key, widget in self._conn_dots.items():
            if key.lower() == (name or "").lower():
                dot = widget
                break
        if dot is None:
            return
        color = BRAND_ACCENT if connected else _LABEL_TERTIARY
        try:
            self._set_style_if_changed(
                dot, f"background: {color}; border-radius: 3px;"
            )
        except Exception:
            pass

    def set_connected(self, connected: bool) -> None:
        # G3 (audit-prod): when the daemon connection drops, gray every
        # extension dot too — they can't possibly be alive without the
        # daemon. This keeps the dashboard's connection story coherent.
        if not connected:
            for name in list(self._conn_dots.keys()):
                self.set_extension_connected(name, False)
        if connected:
            self._set_text_if_changed(self._state_label, "Connected")
            self._set_style_if_changed(
                self._state_dot,
                f"background: {BRAND_ACCENT}; border-radius: 3px;",
            )
        else:
            self._set_text_if_changed(self._state_label, "Disconnected")
            self._set_style_if_changed(
                self._state_dot,
                f"background: {_LABEL_TERTIARY}; border-radius: 3px;",
            )

    # ------------------------------------------------------------------
    # F34 — Stop button state machine
    # ------------------------------------------------------------------

    def _handle_stop_clicked(self) -> None:
        """Disable the Stop button, swap its text to "Stopping…", arm the
        safety timer, then emit ``stop_requested`` exactly once. Double-clicks
        are silently coalesced because the second click lands on a disabled
        button (the ``clicked`` signal still fires in some Qt versions when a
        button transitions to disabled mid-event, so we also guard with
        ``self._stopping``)."""
        if getattr(self, "_stopping", False):
            return
        self._stopping = True
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText("Stopping…")
        self._stop_safety_timer.start()
        self.stop_requested.emit()

    def _stop_safety_expired(self) -> None:
        """F34 safety net: if the daemon never reports stopped, re-enable
        the button so the user can try again rather than be wedged."""
        logger.warning(
            "Stop button safety timeout fired; re-enabling without daemon ack"
        )
        self.notify_daemon_stopped()

    def notify_daemon_stopped(self) -> None:
        """Called when the daemon confirms shutdown (controller wires this).
        Idempotent — safe to call from both the daemon-ack path and the
        safety-timer path."""
        self._stop_safety_timer.stop()
        self._stopping = False
        self._stop_btn.setEnabled(True)
        self._stop_btn.setText("Stop Cortex")


# ---------------------------------------------------------------------------
# HR Trace Plot — brand accent trace, system separator grid
# ---------------------------------------------------------------------------

class HRTracePlot(QWidget):
    """Rolling HR trace. Grid lines use the system separator color; the trace
    itself is the brand accent (terracotta) — the ECG identity preserved."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: collections.deque[float] = collections.deque(maxlen=_MAX_HR_HISTORY)
        self.setMinimumHeight(120)
        self.setMinimumWidth(300)

    def add_value(self, hr: float) -> None:
        self._values.append(hr)
        self.update()

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad = 8

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_CONTROL_BG))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), RADIUS_CARD, RADIUS_CARD)
        painter.drawPath(path)

        painter.setPen(QPen(QColor(0, 0, 0, 24), 1))  # ~ system separator 15%
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        if len(self._values) < 2:
            painter.setPen(QColor(_LABEL_TERTIARY))
            painter.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Waiting for HR data...")
            painter.end()
            return

        min_hr = max(40.0, min(self._values) - 5)
        max_hr = min(180.0, max(self._values) + 5)
        hr_range = max(max_hr - min_hr, 10.0)

        painter.setPen(QPen(QColor(0, 0, 0, 12), 1))  # ~ tertiary label
        for tick in range(int(min_hr), int(max_hr) + 1, 10):
            y = pad + (h - 2 * pad) - int((tick - min_hr) / hr_range * (h - 2 * pad))
            painter.drawLine(pad, y, w - pad, y)

        pen = QPen(QColor(BRAND_ACCENT), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        vals = list(self._values)
        n = len(vals)
        for i in range(1, n):
            x1 = pad + int((i - 1) / max(n - 1, 1) * (w - 2 * pad))
            x2 = pad + int(i / max(n - 1, 1) * (w - 2 * pad))
            y1 = pad + (h - 2 * pad) - int((vals[i - 1] - min_hr) / hr_range * (h - 2 * pad))
            y2 = pad + (h - 2 * pad) - int((vals[i] - min_hr) / hr_range * (h - 2 * pad))
            painter.drawLine(x1, y1, x2, y2)

        painter.setPen(QColor(_LABEL))
        f = mac_native.system_font(FS_FOOTNOTE, "semibold")
        if isinstance(f, QFont):
            painter.setFont(f)
        painter.drawText(w - 80, h - 12, f"{vals[-1]:.0f} BPM")

        painter.end()


# ---------------------------------------------------------------------------
# Signal quality bar
# ---------------------------------------------------------------------------

class _SignalQualityBar(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self._label = QLabel(label)
        self._label.setFixedWidth(76)
        self._label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {_GROUPED_BG};"
            f" border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {BRAND_ACCENT};"
            f" border-radius: 2px; }}"
        )
        layout.addWidget(self._bar)

        self._val_label = QLabel("0%")
        self._val_label.setFixedWidth(36)
        self._val_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._val_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._val_label.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        layout.addWidget(self._val_label)

    def set_value(self, quality: float) -> None:
        pct = int(quality * 100)
        self._bar.setValue(pct)
        self._val_label.setText(f"{pct}%")
        if quality >= 0.7:
            color = SEMANTIC_LIGHT["success"]
        elif quality >= 0.4:
            color = BIO_BLINK
        else:
            color = _DANGER
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {_GROUPED_BG};"
            f" border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {color};"
            f" border-radius: 2px; }}"
        )


# ---------------------------------------------------------------------------
# Tab 2: Advanced
# ---------------------------------------------------------------------------

class _AdvancedTab(QWidget):
    """Developer debug view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: transparent; color: {_LABEL};")
        self._timeline_events: list[dict] = []
        self._session_start = time.monotonic()
        # F31: render-cache per widget; only setText / setValue when the
        # value differs from the last applied write.
        self._render_cache: dict[int, dict[str, object]] = {}
        # Phase J-3: empty-state flag. Before the first capture frame
        # arrives the developer-debug widgets are uninformative (all
        # bars at zero, plot blank, scores all 0.00). The empty-state
        # panel below sets expectations; ``update_state`` flips the flag
        # and hides it.
        self._has_received_state: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP6, SP5, SP6, SP6)
        layout.setSpacing(SP4)

        # Phase J-3: empty-state panel at the top of the advanced tab.
        # Communicates "we haven't started yet" before any state arrives
        # so the developer (and curious user) doesn't read the zero bars
        # as "Cortex is broken". Hidden once update_state arrives.
        self._empty_state = QLabel(
            "Start a session to populate signal quality, heart-rate "
            "trace, and state scores."
        )
        self._empty_state.setObjectName("CortexAdvancedEmptyState")
        self._empty_state.setWordWrap(True)
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._empty_state.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._empty_state.setStyleSheet(
            "QLabel#CortexAdvancedEmptyState {"
            f"  color: {_LABEL_TERTIARY};"
            f"  background: {_GROUPED_BG};"
            f"  border-radius: {RADIUS_CARD}px;"
            "  padding: 10px 14px;"
            "  font-style: italic;"
            "}"
        )
        _set_accessible_name(self._empty_state, "Advanced tab empty state")
        layout.addWidget(self._empty_state)

        # F18 (audit): a small badge that surfaces when the daemon falls
        # back to synthetic state inference. Hidden by default so the
        # happy path remains visually unchanged.
        self._degraded_badge = QLabel(
            "Cortex degraded — classifier unavailable"
        )
        self._degraded_badge.setObjectName("CortexDegradedBadge")
        self._degraded_badge.setFont(
            mac_native.system_font(FS_FOOTNOTE, "semibold")
        )
        # Warm terracotta hint without recoloring the whole tab.
        self._degraded_badge.setStyleSheet(
            "QLabel#CortexDegradedBadge {"
            "  color: #B25430;"  # deep terracotta, WCAG-AA on grouped bg
            "  background-color: rgba(217, 119, 87, 0.10);"
            "  border: 0.5px solid rgba(217, 119, 87, 0.35);"
            "  border-radius: 6px;"
            "  padding: 4px 10px;"
            "}"
        )
        self._degraded_badge.setVisible(False)
        layout.addWidget(self._degraded_badge)

        sq_label = QLabel("Signal quality")
        sq_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        sq_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(sq_label)

        self._physio_q = _SignalQualityBar("Physio")
        self._kine_q = _SignalQualityBar("Kinematics")
        self._tele_q = _SignalQualityBar("Telemetry")
        layout.addWidget(self._physio_q)
        layout.addWidget(self._kine_q)
        layout.addWidget(self._tele_q)
        layout.addSpacing(SP2)

        hr_label = QLabel("Heart rate")
        hr_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        hr_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(hr_label)
        self._hr_plot = HRTracePlot()
        layout.addWidget(self._hr_plot)

        scores_label = QLabel("State scores")
        scores_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        scores_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(scores_label)

        scores_grid = QGridLayout()
        scores_grid.setVerticalSpacing(6)
        self._score_bars: dict[str, QProgressBar] = {}
        self._score_labels: dict[str, QLabel] = {}
        for i, (name, color) in enumerate([
            ("flow", STATE_COLORS["FLOW"]),
            ("hyper", STATE_COLORS["HYPER"]),
            ("hypo", STATE_COLORS["HYPO"]),
            ("recovery", STATE_COLORS["RECOVERY"]),
        ]):
            lbl = QLabel(name.capitalize())
            lbl.setFixedWidth(72)
            lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            lbl.setStyleSheet(
                f"color: {_LABEL_SECONDARY}; background: transparent;"
            )
            scores_grid.addWidget(lbl, i, 0)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(5)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {_GROUPED_BG}; border: none;"
                f" border-radius: 2px; }}"
                f"QProgressBar::chunk {{ background: {color};"
                f" border-radius: 2px; }}"
            )
            scores_grid.addWidget(bar, i, 1)
            val_lbl = QLabel("0.00")
            val_lbl.setFixedWidth(36)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val_lbl.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            val_lbl.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            scores_grid.addWidget(val_lbl, i, 2)
            self._score_bars[name] = bar
            self._score_labels[name] = val_lbl
        layout.addLayout(scores_grid)

        meta_row = QHBoxLayout()
        self._confidence_lbl = QLabel("Confidence: --")
        self._confidence_lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._confidence_lbl.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        self._dwell_lbl = QLabel("Dwell: --")
        self._dwell_lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._dwell_lbl.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        meta_row.addWidget(self._confidence_lbl)
        meta_row.addStretch()
        meta_row.addWidget(self._dwell_lbl)
        layout.addLayout(meta_row)

        tl_label = QLabel("Timeline")
        tl_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        tl_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(tl_label)
        self._timeline_text = QLabel("No events yet")
        self._timeline_text.setWordWrap(True)
        self._timeline_text.setStyleSheet(
            f"font-family: {FONT_MONO};"
            f"font-size: {FS_CAPTION}px; color: {_LABEL_SECONDARY};"
            f"background: transparent; line-height: 1.6;"
        )
        self._timeline_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._timeline_text)
        layout.addStretch()

    def _set_text_if_changed(self, widget: QLabel, text: str) -> bool:
        slot = self._render_cache.setdefault(id(widget), {})
        if slot.get("text") == text:
            return False
        slot["text"] = text
        widget.setText(text)
        return True

    def _set_value_if_changed(self, widget: QProgressBar, value: int) -> bool:
        slot = self._render_cache.setdefault(id(widget), {})
        if slot.get("value") == value:
            return False
        slot["value"] = value
        widget.setValue(value)
        return True

    def update_state(self, payload: dict) -> None:
        # Phase J-3: first frame retires the empty-state panel.
        if not self._has_received_state:
            self._has_received_state = True
            try:
                self._empty_state.setVisible(False)
            except Exception:
                pass

        scores = payload.get("scores", {})
        sig_q = payload.get("signal_quality", {})
        confidence = payload.get("confidence", 0.0)
        dwell = payload.get("dwell_seconds", 0.0)
        state = payload.get("state", "FLOW")
        bio = payload.get("biometrics", {})

        # F18 (audit): surface the daemon's degraded state. ``degraded``
        # and ``source`` are mirrored from the ``StateInferResponse``
        # envelope onto every WS STATE_UPDATE payload by
        # ``WebSocketServer._make_state_update`` (audit Wave-2 fix), so
        # the same reader works for both the /state/infer round-trip and
        # the live WS stream. The literal ``fallback`` is the only value
        # ``source`` takes when the classifier is unavailable; treating
        # it as the trigger keeps the badge from flipping on a healthy
        # ``classifier_source="rule"`` debug field (which lives on the
        # same payload but is unrelated to envelope-level degradation).
        is_degraded = bool(payload.get("degraded", False)) or (
            payload.get("source") == "fallback"
        )
        self._degraded_badge.setVisible(is_degraded)

        self._physio_q.set_value(sig_q.get("physio", 0.0))
        self._kine_q.set_value(sig_q.get("kinematics", 0.0))
        self._tele_q.set_value(sig_q.get("telemetry", 0.0))

        hr = bio.get("heart_rate")
        if hr is not None:
            self._hr_plot.add_value(hr)

        for name in ("flow", "hyper", "hypo", "recovery"):
            val = scores.get(name, 0.0)
            if name in self._score_bars:
                # F31: avoid pushing identical values through Qt's
                # progress-bar / label paint chain on every 2 Hz tick.
                self._set_value_if_changed(self._score_bars[name], int(val * 100))
                self._set_text_if_changed(self._score_labels[name], f"{val:.2f}")

        self._set_text_if_changed(self._confidence_lbl, f"Confidence: {confidence:.0%}")
        self._set_text_if_changed(self._dwell_lbl, f"Dwell: {dwell:.1f}s")

        if not self._timeline_events or self._timeline_events[-1]["state"] != state:
            elapsed = time.monotonic() - self._session_start
            self._timeline_events.append({
                "time": elapsed, "state": state, "confidence": confidence,
            })
            if len(self._timeline_events) > _MAX_TIMELINE_EVENTS:
                self._timeline_events = self._timeline_events[-_MAX_TIMELINE_EVENTS:]
            lines = []
            for ev in reversed(self._timeline_events[-8:]):
                t = ev["time"]
                m, s = int(t // 60), t % 60
                lines.append(f"{m:02d}:{s:04.1f}  {ev['state']:<10} {ev['confidence']:.0%}")
            self._timeline_text.setText("\n".join(lines) if lines else "No events yet")


# ---------------------------------------------------------------------------
# Main Dashboard Window
# ---------------------------------------------------------------------------

class DashboardWindow(QWidget):
    """Two-tab dashboard with native chrome.

    Uses a segmented control + stacked widget instead of QTabWidget — the
    macOS convention for two-segment top-level navigation.
    """

    # E.1: re-emit user-intent signals from the consumer tab.
    stop_requested = Signal()
    goal_set = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._connected = False
        self.setObjectName("CortexDashboard")
        self.setWindowTitle("Cortex")
        # HIG: minimum width, flexible. Macs at 1024×768 still fit comfortably.
        self.setMinimumWidth(DASHBOARD_WIDTH)
        self.setMaximumWidth(DASHBOARD_WIDTH + 60)
        self.setMaximumHeight(DASHBOARD_MAX_HEIGHT)
        self.setStyleSheet(_GLOBAL_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Segmented control sits at the top under the unified title bar.
        seg_container = QHBoxLayout()
        seg_container.setContentsMargins(SP6, SP3, SP6, SP3)
        self._seg = _MacSegmentedControl(["Dashboard", "Advanced"])
        seg_container.addWidget(self._seg, stretch=1)
        layout.addLayout(seg_container)

        # Phase J-2: error toast lives under the segmented control so it
        # is visible from either tab. Hidden until ``show_error`` is
        # called. Lazy-import the Toast helper at construction time so a
        # legacy mock harness that swaps out PySide6 doesn't crash on
        # module import — the toast is itself test-stub-tolerant.
        try:
            from cortex.apps.desktop_shell.components import Toast
            toast_container = QHBoxLayout()
            toast_container.setContentsMargins(SP6, 0, SP6, SP3)
            self._toast: Toast | None = Toast(self)
            toast_container.addWidget(self._toast, stretch=1)
            layout.addLayout(toast_container)
        except Exception:  # pragma: no cover - mock harness without Toast
            logger.debug("Toast widget unavailable; skipping", exc_info=True)
            self._toast = None

        self._stack = QStackedWidget()
        self._consumer = _ConsumerTab()
        self._advanced = _AdvancedTab()
        self._timeline_events = self._advanced._timeline_events
        self._stack.addWidget(self._consumer)
        self._stack.addWidget(self._advanced)
        layout.addWidget(self._stack, stretch=1)

        self._seg.selection_changed.connect(self._stack.setCurrentIndex)

        # E.1: forward consumer-tab signals to outer subscribers.
        self._consumer.stop_requested.connect(self.stop_requested.emit)
        self._consumer.goal_set.connect(self.goal_set.emit)

    # -- Lifecycle hook for native chrome --------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        # On first show, snap to the centre of whatever screen the user
        # currently has so a stale geometry from a previous multi-monitor
        # session can't strand the window at e.g. x=2412 on a 1728-wide
        # display. Subsequent shows respect wherever the user dragged it.
        if not getattr(self, "_positioned_once", False):
            try:
                screen = self.screen()
                if screen is not None:
                    geo = screen.availableGeometry()
                    self.move(
                        geo.x() + (geo.width() - self.width()) // 2,
                        geo.y() + (geo.height() - self.height()) // 3,
                    )
            except Exception:
                pass
            self._positioned_once = True
        # Apply native materials once winId() is valid. Re-applying on each
        # show is cheap and idempotent.
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="window_background")
        except Exception:
            logger.debug("native chrome application failed", exc_info=True)

    # -- Public update methods (signature-stable) ------------------------

    def update_state(self, payload: dict) -> None:
        self._consumer.update_state(payload)
        self._advanced.update_state(payload)

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._consumer.set_connected(connected)

    def set_extension_connected(self, name: str, connected: bool) -> None:
        """Audit-2 fix: update the Chrome / Edge / Editor connection
        indicator dots in the consumer tab. Callers invoke this from
        controller / main when an extension IDENTIFY arrives."""
        if self._consumer is not None and hasattr(
            self._consumer, "set_extension_connected"
        ):
            self._consumer.set_extension_connected(name, connected)

    def record_intervention_seen(self) -> None:
        """Audit-2 fix: forward intervention-broadcast events to the
        consumer-tab counter so the Today/Blocked numeric advances."""
        if self._consumer is not None and hasattr(
            self._consumer, "record_intervention_seen"
        ):
            self._consumer.record_intervention_seen()

    # F34 -----------------------------------------------------------------

    def notify_daemon_stopped(self) -> None:
        """Re-enable the Stop button. Forwarded to the consumer tab; called
        from ``controller._on_daemon_stopped`` (or test fixtures)."""
        if self._consumer is not None:
            self._consumer.notify_daemon_stopped()

    def set_stop_safety_timeout_ms(self, ms: int) -> None:
        """Allow tests (or future settings) to shorten the safety-timer
        budget. ``_STOP_SAFETY_TIMEOUT_MS`` is the production default."""
        if self._consumer is not None:
            self._consumer._stop_safety_timer.setInterval(int(ms))

    # Phase J-2 ----------------------------------------------------------

    def show_error(self, title: str, body: str, cid: str = "") -> None:
        """Surface a daemon error in the top-bar toast.

        ``cid`` is the F19 correlation id quoted back to the user so a
        support engineer can grep the daemon log for the matching entry.
        When the daemon failed to mint one (or the call site didn't have
        it bound) the empty string is acceptable — the toast still shows
        the title + body, only the support-handoff slot is empty.
        """
        if self._toast is None:
            logger.warning(
                "Toast unavailable; error not surfaced: %s — %s [cid=%s]",
                title, body, cid,
            )
            return
        self._toast.show_error(title, body, cid)

    def show_info_toast(self, title: str, body: str = "") -> None:
        """B2 (audit-prod): surface a positive / status message in the
        top-bar toast (e.g. "Cortex is now using your LLM"). Reuses the
        Phase J-2 toast widget with empty cid slot."""
        if self._toast is None or not hasattr(self._toast, "show_info"):
            logger.info("Toast unavailable; info toast skipped: %s", title)
            return
        try:
            self._toast.show_info(title, body)
        except Exception:
            logger.debug("show_info_toast failed", exc_info=True)
