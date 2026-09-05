"""Desktop Shell — Dashboard Window (macOS-native refactor).

Three-segment layout:
    "Dashboard" — Consumer biometrics view (hero numerals in the display
                  serif, terracotta accent, native typography & spacing)
    "History"   — Past sessions and trends
    "Advanced"  — Developer debug view: HR trace, signal quality, scores

Session lifecycle (see ``_ConsumerTab._enter_phase``): the footer control is
"End session" while sensing runs, "Ending…" while the daemon stops, then
"Start session" once it has stopped. Quitting is a separate, explicit
"Quit Cortex" control (footer after a session ends, the recap sheet, the
tray) — ending a session never exits the app.

The visual layer is now driven by:

* :mod:`cortex.apps.desktop_shell.tokens` (emitted from
  ``cortex/libs/design/tokens.yaml``) — semantic palette, 5-step type scale,
  HIG-compliant spacing & radii.
* :mod:`cortex.apps.desktop_shell.mac_native` — system font, safe native
  background tint, unified title bar. Brand identity (terracotta accent +
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
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal

try:
    from PySide6.QtCore import QRectF
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtCore import QRect as QRectF
try:
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtGui import QColor, QFont, QPainter, QPen

try:
    from PySide6.QtGui import QKeySequence, QShortcut
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    class QKeySequence:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

    class QShortcut:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            class _S:
                def connect(self, *_a: object, **_k: object) -> None:
                    return
            self.activated = _S()

        def setContext(self, *_args: object, **_kwargs: object) -> None:
            return

    class QPainterPath:
        def addRoundedRect(self, *_args: object, **_kwargs: object) -> None:
            return

        def moveTo(self, *_args: object, **_kwargs: object) -> None:
            return

        def lineTo(self, *_args: object, **_kwargs: object) -> None:
            return
try:
    from PySide6.QtWidgets import (
        QButtonGroup,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMenu,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - compatibility for lightweight test mocks
    from PySide6.QtWidgets import (
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    class QComboBox(QWidget):
        """Lightweight stub for unit-test harnesses."""

        def __init__(self, *_a: object, **_kw: object) -> None:
            super().__init__()
            self._items: list[tuple[str, object]] = []
            self._current = 0

        def addItem(self, label: str, data: object = None) -> None:
            self._items.append((str(label), data))

        def clear(self) -> None:
            self._items.clear()
            self._current = 0

        def count(self) -> int:
            return len(self._items)

        def itemText(self, idx: int) -> str:
            return self._items[idx][0] if 0 <= idx < len(self._items) else ""

        def itemData(self, idx: int) -> object:
            return self._items[idx][1] if 0 <= idx < len(self._items) else None

        def setCurrentIndex(self, idx: int) -> None:
            self._current = max(0, min(idx, len(self._items) - 1))

        def setVisible(self, visible: bool) -> None:
            pass

        def setEditable(self, editable: bool) -> None:
            pass

        def setFont(self, *_a: object, **_kw: object) -> None:
            pass

        def setMinimumHeight(self, *_a: object) -> None:
            pass

        def setStyleSheet(self, *_a: object) -> None:
            pass

        @property
        def activated(self) -> object:
            class _S:
                def connect(self, *_a: object, **_kw: object) -> None:
                    pass

            return _S()

    class QDialog(QWidget):
        """Lightweight QDialog stub for unit tests."""

        def __init__(self, *_a: object, **_kw: object) -> None:
            super().__init__()

        def exec(self) -> int:
            return 0

    class QFileDialog(QWidget):
        """Lightweight QFileDialog stub for unit tests."""

        @staticmethod
        def getSaveFileName(
            *_a: object, **_kw: object
        ) -> tuple[str, str]:
            return ("", "")

    class QMenu(QWidget):
        """Lightweight stub: tests don't exercise the menu surface."""

        def addAction(self, *_args: object, **_kwargs: object) -> object:
            class _A:
                def setShortcut(self, *_a: object, **_k: object) -> None:
                    return

                def triggered(self) -> None:
                    return
            return _A()

        def exec(self, *_args: object, **_kwargs: object) -> None:
            return

    class QButtonGroup:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def addButton(self, *_args: object, **_kwargs: object) -> None:
            return

        def setExclusive(self, *_args: object, **_kwargs: object) -> None:
            return

    class QLineEdit(QLabel):
        def setPlaceholderText(self, *_args: object, **_kwargs: object) -> None:
            return

    class QScrollArea(QWidget):
        def setWidgetResizable(self, *_args: object, **_kwargs: object) -> None:
            return

        def setWidget(self, *_args: object, **_kwargs: object) -> None:
            return

    class QSizePolicy:
        class Policy:
            Expanding = 0
            Preferred = 0

    class QStackedWidget(QWidget):
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
from cortex.apps.desktop_shell.components import SegmentedControl, status_dot_qss
from cortex.apps.desktop_shell.palette_runtime import active_state_color
from cortex.apps.desktop_shell.tokens import (
    BIO_BLINK,
    BIO_HR,
    BRAND_ACCENT,
    BRAND_ACCENT_DARK,
    BRAND_ACCENT_TEXT,
    BRAND_DISPLAY_FONT,
    BTN_DESTRUCTIVE_QSS,
    BTN_FOCUS_RING,
    BTN_GHOST_QSS,
    BTN_LINK_QSS,
    CARD_QSS,
    CX_BG,
    CX_BG_SECONDARY,
    CX_BORDER_DEFAULT,
    CX_DANGER,
    CX_SUCCESS,
    CX_SURFACE,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    DASHBOARD_MAX_HEIGHT,
    DASHBOARD_WIDTH,
    FONT_MONO,
    FONT_SYSTEM,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_MEDIUM,
    FW_REGULAR,
    FW_SEMIBOLD,
    GOAL_INPUT_HEIGHT,
    HERO_NUMERAL_QSS,
    INPUT_QSS,
    PILL_BUTTON_QSS,
    PILL_QSS,
    RADIUS_CARD,
    RADIUS_PILL,
    SECTION_HEADING_QSS,
    SEMANTIC_LIGHT,
    SP1,
    SP2,
    SP3,
    SP4,
    SP5,
    SP6,
    STATE_TEXT_COLORS,
)
from cortex.apps.desktop_shell.view_models import (
    advanced_state_view,
    consumer_state_view,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# P0 §3.17 — Concepts glossary shared between tooltips + Help → Concepts.
# ---------------------------------------------------------------------------
#
# Single source of truth for the help copy on every quantitative widget in
# the dashboard. Adding a new term here automatically makes it available to
# the ``ConceptsDialog`` below; the dashboard's individual setToolTip(...)
# calls reach in by key.
_CONCEPTS_GLOSSARY: dict[str, str] = {
    "state": (
        "Support estimate: Cortex combines available local activity and sensor "
        "signals into a heuristic workspace-support label. It is not a medical "
        "or psychological assessment."
    ),
    "flow": (
        "Steady activity: a heuristic label for sustained interaction "
        "patterns. It does not measure a nervous-system state."
    ),
    "hyper": (
        "Support may help: a heuristic label for behavior patterns that may "
        "benefit from a workspace suggestion. It is not an arousal diagnosis."
    ),
    "hypo": (
        "Quiet activity: a heuristic label for quieter interaction patterns. "
        "It does not establish disengagement or low arousal."
    ),
    "recovery": (
        "Settling: a temporal transition label after support was indicated; "
        "it is not a physiological recovery measurement."
    ),
    "hr": (
        "BPM: an experimental camera-derived pulse estimate. Accuracy varies "
        "with lighting, motion, camera, and individual conditions; it is not "
        "medical-grade."
    ),
    "perclos": (
        "PERCLOS: estimated eyelid-closure exposure over a recent window. "
        "Cortex has not validated it as a drowsiness or fatigue measure."
    ),
    "blink": (
        "Blink rate: an estimated local count per minute. It is contextual "
        "telemetry, not a fatigue diagnosis."
    ),
    "sqi": (
        "SQI (Signal Quality Index): an algorithmic camera-signal quality "
        "score. It is not measurement confidence or an accuracy guarantee."
    ),
    "calibration": (
        "Calibration: a versioned, quality-gated measured profile with explicit "
        "provenance. It personalizes supported baselines but does not validate a "
        "metric or create calibrated state probabilities."
    ),
}


# ---------------------------------------------------------------------------
# P0 §3.4 — Baseline freshness helpers (shared with the Settings dialog).
# ---------------------------------------------------------------------------


def _active_calibration_measured_at() -> float | None:
    """Measurement time from the canonical active immutable profile."""
    try:
        from cortex.libs.config.settings import get_config
        from cortex.services.capture_service.calibration_store import (
            CalibrationProfileStore,
        )

        config = get_config()
        profile = CalibrationProfileStore(config.storage.path).load_active()
        if profile is None:
            return None
        return profile.created_at_unix_ms / 1000.0
    except Exception:
        logger.debug("active calibration metadata unavailable", exc_info=True)
        return None


def _baseline_age_days(now: float | None = None) -> float | None:
    """Age of the active measured profile, or ``None`` when unavailable."""
    measured_at = _active_calibration_measured_at()
    if measured_at is None:
        return None
    current = now if now is not None else time.time()
    return max(0.0, (current - measured_at) / 86400.0)


_MAX_HR_HISTORY = 120
_MAX_TIMELINE_EVENTS = 50

# F34: how long to keep the Stop button disabled before assuming the daemon
# shutdown is stuck and re-enabling so the user can try again. 10 s matches
# the audit-plan budget; controller's ``daemon_stopped`` signal short-circuits
# this when the daemon actually reports stopped.
_STOP_SAFETY_TIMEOUT_MS = 10_000

# P0 §3.3 / Phase 4.B (#25): max time we wait for a SESSION_RECAP broadcast
# after the user clicks Stop. The daemon itself uses a 5 s wait_for around
# its broadcast; we add 1 s slack so the daemon's broadcast normally wins
# the race. If neither the recap nor the daemon respond inside this window
# the dashboard finalises the stop anyway so the Qt app can exit.
_RECAP_WATCHDOG_MS = 6_000

# Session lifecycle phases for the footer control + state pill. Every
# label the user sees for the session comes from ``_enter_phase`` so the
# button, the pill, and the accessible descriptions can never disagree.
_PHASE_STARTING = "starting"
_PHASE_LIVE = "live"
_PHASE_STOPPING = "stopping"
_PHASE_ENDED = "ended"
_PHASE_DISCONNECTED = "disconnected"

# Connectivity is reported with the info colour, never a state colour, so
# "Connected" cannot be confused with the "Steady activity" estimate.
_CONNECTED_DOT = SEMANTIC_LIGHT["info"]

# Accent-tinted pill button (focus-protection / break / recalibrate chips).
# Same geometry as ``tokens.PILL_BUTTON_QSS`` so a chip can swap tint
# without moving.
_ACCENT_PILL_BUTTON_QSS = (
    "QPushButton {"
    f"  font-family: {FONT_SYSTEM};"
    f"  font-size: {FS_CAPTION}px;"
    f"  font-weight: {FW_MEDIUM};"
    f"  color: {BRAND_ACCENT_TEXT};"
    "  background: rgba(217, 119, 87, 0.14);"
    f"  border-radius: {RADIUS_PILL}px;"
    "  padding: 3px 10px;"
    "  border: 2px solid transparent;"
    "}"
    "QPushButton:hover { background: rgba(217, 119, 87, 0.22); }"
    "QPushButton:pressed { background: rgba(217, 119, 87, 0.32); }"
    f"QPushButton:focus {{ border: {BTN_FOCUS_RING}; }}"
)


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


def _make_history_icon(color_hex: str, size: int = 13) -> object:
    """Draw a small downward pull-down chevron as a ``QIcon``.

    Used for the goal field's trailing recent-goals affordance. Painted
    (not a font glyph) so it renders crisply on every Qt backend
    regardless of SF Symbols availability in unsigned bundles.
    """
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(color_hex))
        pen.setWidthF(1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        w = float(size)
        painter.drawLine(QPointF(w * 0.26, w * 0.40), QPointF(w * 0.50, w * 0.62))
        painter.drawLine(QPointF(w * 0.50, w * 0.62), QPointF(w * 0.74, w * 0.40))
    finally:
        painter.end()
    return QIcon(pm)


# ---------------------------------------------------------------------------
# Global stylesheet — minimal, semantic
# ---------------------------------------------------------------------------

_GLOBAL_QSS = f"""
QWidget#CortexDashboard {{
    /* The NSWindow behind Qt's content view is tinted and pinned to the
       light Aqua appearance by mac_native.apply_vibrancy; off-mac the
       QApplication palette supplies the same light window colour. */
    background-color: transparent;
}}
QLineEdit {{
    selection-background-color: {BRAND_ACCENT};
}}
QToolTip {{
    background-color: {CX_SURFACE};
    color: {CX_TEXT};
    border: 1px solid {CX_BORDER_DEFAULT};
    padding: 4px 8px;
    border-radius: 6px;
}}
/* Popup menus must paint an OPAQUE surface. The main window uses
   vibrancy (translucent) and this stylesheet cascades to child
   ``QMenu`` popups; without an explicit background they render
   see-through on macOS and the items bleed onto the widgets behind
   the popup (observed: recent-goals menu over the Biometrics card).
   Defining it globally hardens every present and future menu. */
QMenu {{
    background-color: {CX_SURFACE};
    color: {CX_TEXT};
    border: 1px solid {CX_BORDER_DEFAULT};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 12px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background-color: {BRAND_ACCENT};
    color: {CX_TEXT};
}}
QMenu::separator {{
    height: 1px;
    background-color: {CX_BORDER_DEFAULT};
    margin: 4px 8px;
}}
"""


# ---------------------------------------------------------------------------
# Segmented control — the shared capsule component (dashboard + history)
# ---------------------------------------------------------------------------

# Historical name kept for call sites and tests; the implementation is the
# shared :class:`cortex.apps.desktop_shell.components.SegmentedControl`.
_MacSegmentedControl = SegmentedControl


# ---------------------------------------------------------------------------
# Tab 1: Consumer Dashboard
# ---------------------------------------------------------------------------

class _ConsumerTab(QWidget):
    """Clean biometrics dashboard — native materials, brand identity intact."""

    # Phase 4.B fix (#1): split the legacy ``stop_requested`` signal into
    # two distinct concerns so the DMG-mode stop deadlock is fixed.
    #
    # * ``daemon_stop_requested`` — emitted IMMEDIATELY on the Stop click
    #   (inside ``_arm_stop``). The controller hears this and schedules
    #   ``daemon.stop()`` so the SESSION_RECAP broadcast pipeline can
    #   actually run. Without this immediate fan-out the daemon never
    #   knows the user wants to stop and the recap sheet is unreachable.
    #
    # * ``gui_quit_requested`` — emitted ONCE from ``_finalize_stop``
    #   (after recap dismiss / watchdog / safety expiry). The controller
    #   hears this and quits the Qt app.
    #
    # The legacy ``stop_requested`` signal is preserved as an ALIAS of
    # ``daemon_stop_requested`` so existing call sites (tests, tray
    # wiring, WS-mode CortexApp) keep working without modification. The
    # alias only fires the daemon-stop emit — quit is gated separately
    # on ``gui_quit_requested``.
    daemon_stop_requested = Signal()
    gui_quit_requested = Signal()
    stop_requested = Signal()
    goal_set = Signal(str)
    # P0 §3.11: bubble quiet-mode menu picks up to the DashboardWindow
    # so the controller forwards them to the daemon's set_quiet_mode.
    quiet_mode_requested = Signal(str, int)
    # P0 §3.10: bubble the "Turn off" auto-focus toast click up to the
    # DashboardWindow so the controller can call disarm_auto_focus.
    auto_focus_disarm_requested = Signal()
    # P0 §3.7 desktop dispatch: bubble the "Take a break?" pill click
    # up so the controller routes to the BiologyBreakOverlay. Payload
    # is the BREAK_RECOMMENDATION dict the daemon broadcast (carries
    # duration_seconds, breathing_pattern) so the overlay's args are
    # available without a second WS round-trip.
    break_pill_clicked = Signal(dict)
    # P0 §3.21 global shortcuts: emitted on Cmd+Shift+R (force a session
    # recap) and Cmd+Shift+D (dismiss the active intervention overlay).
    # The controller forwards via WS / daemon.
    force_recap_requested = Signal()
    dismiss_overlay_requested = Signal()
    # P0 §3.16: bubble the "Undo" toast click up so the controller
    # forwards INTERVENTION_RESTORE to the daemon. Payload is the
    # intervention_id of the action being undone.
    undo_action_requested = Signal(str)
    # Contextual recovery: an offline-camera banner must provide an obvious
    # path back to Cortex Settings instead of relying on discovery of the
    # menu-bar item.
    open_settings_requested = Signal()
    # "Start session" after a session ended (in-process hosts restart the
    # daemon; WS hosts hide the control via ``set_session_restart_available``).
    session_start_requested = Signal()
    # The "Recalibrate" chip is a real control: hosts route it to the same
    # calibration runner Settings uses.
    recalibrate_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: transparent; color: {CX_TEXT};")
        # P0 §3.7: cached BREAK_RECOMMENDATION payload so the click
        # handler can carry it up to the controller. ``apply_break_recommendation``
        # populates this; ``_clear_break_pill`` clears it.
        self._break_recommendation_payload: dict = {}
        self._break_pill_snooze_timer: QTimer | None = None
        # P0 §3.16: ring buffer of recently-applied reversible
        # intervention dispatches so the "Restore previous state" pill
        # surfaces for ~5 min. Entries are
        # (timestamp_monotonic, intervention_id, action_type, applied_count).
        self._reversible_actions: list[tuple[float, str, str, int]] = []
        self._reversible_window_seconds: int = 300
        # Undo toast widget — built lazily inside _show_undo_toast.
        self._undo_toast: QWidget | None = None
        self._undo_toast_timer: QTimer | None = None

        # F34: ``_stopping`` flips to True on End session and back to False
        # when the daemon acknowledges (or the safety timer expires).
        # Coalesces double-clicks at the slot level.
        self._stopping: bool = False
        # Session lifecycle — see ``_enter_phase``.
        self._session_phase: str = _PHASE_DISCONNECTED
        self._quit_after_stop: bool = False
        self._restart_available: bool = True
        self._ended_by_user: bool = False
        self._last_state: str = "UNKNOWN"
        # P0 §3.11: cached quiet-mode state envelope. Mirrors the
        # daemon's QUIET_MODE_STATE broadcast so the capsule re-renders
        # without round-trips.
        self._quiet_mode_state: dict[str, object] = {"kind": "off"}
        # F31: per-widget cache of last applied text + stylesheet so the
        # 2 Hz state broadcast loop does not push identical values through
        # Qt's restyle / paint chain when the user's state is unchanged.
        # Keyed by id(widget) because QWidget is not hashable on every Qt build.
        self._render_cache: dict[int, dict[str, str]] = {}
        # Phase J-3: empty-state flag. Flips False on the first ``update_state``
        # call so the placeholder paragraph in the biometrics card vanishes
        # and the live numerics take over. Sticky across reconnects.
        self._has_received_state: bool = False
        # The "This session" stats reveal only once a real estimate exists.
        self._has_estimate: bool = False

        root = QVBoxLayout(self)
        root.setContentsMargins(SP6, SP5, SP6, SP6)
        root.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, SP5)
        header.setSpacing(SP2)

        # Brand wordmark — the one place (with hero numerals) the display
        # serif is used.
        brand = QLabel("Cortex")
        brand.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-style: italic; font-size: {FS_TITLE}px;"
            f"font-weight: {FW_REGULAR};"
            f"color: {CX_TEXT}; background: transparent;"
        )
        _set_accessible_name(brand, "Cortex")
        header.addWidget(brand)
        header.addStretch()

        # Status pill — capsule with dot + label, sits on the grouped background.
        self._state_badge = QWidget()
        badge_layout = QHBoxLayout(self._state_badge)
        badge_layout.setContentsMargins(10, 3, 12, 3)
        badge_layout.setSpacing(6)

        self._state_dot = QLabel()
        self._state_dot.setFixedSize(7, 7)
        self._state_dot.setStyleSheet(status_dot_qss(CX_TEXT_TERTIARY, size=7))
        badge_layout.addWidget(self._state_dot, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_label = QLabel("Disconnected")
        self._state_label.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._state_label.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        # P0 §3.17: glossary tooltips on every quantitative chrome element.
        try:
            self._state_label.setToolTip(_CONCEPTS_GLOSSARY["state"])
        except Exception:
            pass
        _set_accessible_name(self._state_label, "Cortex status")
        badge_layout.addWidget(self._state_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_badge.setStyleSheet(
            f"background: {CX_BG_SECONDARY}; border-radius: {RADIUS_PILL}px;"
        )
        header.addWidget(self._state_badge, alignment=Qt.AlignmentFlag.AlignVCenter)

        # P0 §3.11: Pause/Quiet capsule — one-click access to the quiet
        # modes. The label mirrors the active mode (e.g. "Quiet · 28m").
        self._quiet_capsule = QPushButton("Pause")
        try:
            self._quiet_capsule.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._quiet_capsule.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        try:
            self._quiet_capsule.setFlat(True)
            self._quiet_capsule.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._quiet_capsule.setStyleSheet(PILL_BUTTON_QSS)
        _set_accessible_name(self._quiet_capsule, "Pause or quiet Cortex")
        _set_accessible_description(
            self._quiet_capsule,
            "Opens a menu to snooze, quiet, or pause Cortex. "
            "Shortcut: Command + Shift + Slash.",
        )
        self._quiet_capsule.clicked.connect(self._on_quiet_capsule_clicked)
        header.addWidget(
            self._quiet_capsule, alignment=Qt.AlignmentFlag.AlignVCenter,
        )

        # P0 §3.11: ⌘⇧/ opens the same menu the capsule does.
        try:
            self._quiet_shortcut = QShortcut(
                QKeySequence("Ctrl+Shift+/"), self,
            )
            try:
                self._quiet_shortcut.setContext(
                    Qt.ShortcutContext.ApplicationShortcut,
                )
            except Exception:
                pass
            self._quiet_shortcut.activated.connect(
                self._on_quiet_capsule_clicked,
            )
        except Exception:
            logger.debug("QShortcut setup failed", exc_info=True)

        # P0 §3.21 global shortcuts:
        #   Cmd+Shift+P → toggle quiet capsule (pause/resume)
        #   Cmd+Shift+R → request a manual session recap
        #   Cmd+Shift+D → dismiss the active intervention overlay
        def _install_shortcut(seq: str, slot: object) -> None:
            try:
                sc = QShortcut(QKeySequence(seq), self)
                try:
                    sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
                except Exception:
                    pass
                sc.activated.connect(slot)
            except Exception:
                logger.debug("QShortcut %s setup failed", seq, exc_info=True)

        _install_shortcut(
            "Ctrl+Shift+P",
            lambda: self.quiet_mode_requested.emit(
                "off" if self._quiet_mode_state.get("kind") not in ("off", None) else "pause",
                0,
            ),
        )
        _install_shortcut(
            "Ctrl+Shift+R",
            lambda: getattr(self, "force_recap_requested", None)
            and self.force_recap_requested.emit(),
        )
        _install_shortcut(
            "Ctrl+Shift+D",
            lambda: getattr(self, "dismiss_overlay_requested", None)
            and self.dismiss_overlay_requested.emit(),
        )

        # ── Ambient chips (footer meta strip) ─────────────────────────
        # Each chip is hidden until its render slot has real data; every
        # pressable chip is a real button with hover/pressed/focus.

        # P0 §3.10: auto-armed focus protection. Click → disarm.
        self._focus_protection_pill = QPushButton("")
        try:
            self._focus_protection_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._focus_protection_pill.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._focus_protection_pill.setStyleSheet(_ACCENT_PILL_BUTTON_QSS)
        self._focus_protection_pill.setVisible(False)
        _set_accessible_name(self._focus_protection_pill, "Turn off focus protection")
        self._focus_protection_pill.clicked.connect(
            self.auto_focus_disarm_requested.emit,
        )

        # P0 §3.15: LLM cost meter. Hidden until the daemon reports a cost
        # (``apply_cost_update``) — a permanent "$—" would be a placeholder
        # pretending to be data.
        self._cost_pill = QLabel("")
        self._cost_pill.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._cost_pill.setObjectName("CortexCostPill")
        self._cost_pill.setStyleSheet(f"QLabel#CortexCostPill {{ {PILL_QSS} }}")
        try:
            self._cost_pill.setToolTip(
                "LLM spend today — Settings → Budget sets a daily cap."
            )
        except Exception:
            pass
        self._cost_pill.setVisible(False)
        _set_accessible_name(self._cost_pill, "LLM spend today")
        # Cache the last applied cost so we don't restyle on every poll.
        self._cost_last_value: float = -1.0
        self._cost_budget_warned: bool = False

        # Opt-in elapsed-focus reminder. Hidden unless the user's preferred
        # interval is reached; no biometric or inferred-stress input can
        # surface this control.
        self._break_pill = QPushButton("Take a break?")
        try:
            self._break_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._break_pill.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._break_pill.setStyleSheet(_ACCENT_PILL_BUTTON_QSS)
        self._break_pill.setVisible(False)
        _set_accessible_name(self._break_pill, "Take a break")
        try:
            self._break_pill.clicked.connect(self._on_break_pill_clicked)
        except Exception:
            logger.debug("break pill connect failed", exc_info=True)

        # P0 §3.4 — measured-profile freshness. A real control: clicking it
        # starts recalibration. Hidden when no profile exists or it is fresh.
        self._baseline_pill = QPushButton("")
        try:
            self._baseline_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._baseline_pill.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._baseline_pill.setStyleSheet(_ACCENT_PILL_BUTTON_QSS)
        self._baseline_pill.setVisible(False)
        _set_accessible_name(self._baseline_pill, "Recalibrate measured profile")
        _set_accessible_description(
            self._baseline_pill,
            "Your measured profile is more than 30 days old. Starts the guided "
            "recalibration; nothing is saved until you review it.",
        )
        try:
            self._baseline_pill.clicked.connect(self.recalibrate_requested.emit)
        except Exception:
            logger.debug("baseline pill connect failed", exc_info=True)

        # P0 §3.16: "Restore previous state" — undoes the most recent
        # verified reversible workspace change. Lives in the strip (hidden
        # until a receipt arrives) instead of being conjured as a parentless
        # window.
        self._restore_pill: QPushButton | None = QPushButton("Restore previous state")
        try:
            self._restore_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._restore_pill.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._restore_pill.setStyleSheet(PILL_BUTTON_QSS)
        self._restore_pill.setVisible(False)
        _set_accessible_name(self._restore_pill, "Restore previous workspace state")
        _set_accessible_description(
            self._restore_pill,
            "Undoes the most recent workspace change Cortex applied.",
        )
        try:
            self._restore_pill.clicked.connect(self._on_restore_pill_clicked)
        except Exception:
            logger.debug("restore pill connect failed", exc_info=True)

        self._meta_strip = QHBoxLayout()
        self._meta_strip.setContentsMargins(0, 0, 0, SP3)
        self._meta_strip.setSpacing(SP2)
        self._meta_strip.addStretch()
        for chip in (
            self._restore_pill,
            self._focus_protection_pill,
            self._break_pill,
            self._baseline_pill,
            self._cost_pill,
        ):
            self._meta_strip.addWidget(chip, alignment=Qt.AlignmentFlag.AlignVCenter)

        root.addLayout(header)
        # Run an initial freshness check so the chip is correct on first
        # paint. The controller / main app also calls refresh on a
        # completed calibration.
        try:
            self.refresh_baseline_freshness()
        except Exception:
            logger.debug("initial baseline freshness check failed", exc_info=True)

        # F16 (Phase-4 audit): envelope-level health warning strip,
        # mirrored from the daemon's ``payload["capture"]["stale"]`` and
        # ``payload["store"]["degraded"]`` flags. Hidden by default.
        self._health_banner = QLabel("")
        self._health_banner.setObjectName("CortexHealthBanner")
        self._health_banner.setWordWrap(True)
        self._health_banner.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._health_banner.setStyleSheet(
            "QLabel#CortexHealthBanner {"
            f"  color: {CX_DANGER};"
            "  background: rgba(215, 0, 21, 0.08);"
            f"  border: 1px solid {CX_DANGER};"
            f"  border-radius: {RADIUS_CARD}px;"
            "  padding: 6px 10px;"
            "}"
        )
        self._health_banner.setOpenExternalLinks(False)
        self._health_banner.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self._health_banner.linkActivated.connect(
            lambda href: (
                self.open_settings_requested.emit()
                if href == "cortex-settings"
                else None
            )
        )
        self._health_banner.setVisible(False)
        _set_accessible_name(self._health_banner, "Health warning")
        root.addWidget(self._health_banner)

        # ── Goal input ────────────────────────────────────────────────
        self._goal_input = QLineEdit()
        self._goal_input.setPlaceholderText("What are you working on?")
        self._goal_input.setMinimumHeight(GOAL_INPUT_HEIGHT)
        # Mirror the browser-extension popup and the backend ``GoalSet``
        # schema upper bound.
        self._goal_input.setMaxLength(500)
        _set_accessible_name(self._goal_input, "Goal")
        _set_accessible_description(
            self._goal_input,
            "Tell Cortex what you're working on so suggestions match your intent.",
        )
        self._goal_input.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        # Constant 1px border: focus changes colour only, so the text never
        # shifts by a pixel when the field takes focus.
        self._goal_input.setStyleSheet(INPUT_QSS)
        # F19: placeholder colour via QPalette (the QSS selector silently
        # no-ops on some Qt 6.x builds).
        try:
            from PySide6.QtGui import QColor, QPalette

            placeholder_palette = self._goal_input.palette()
            placeholder_palette.setColor(
                QPalette.ColorRole.PlaceholderText,
                QColor(CX_TEXT_TERTIARY),
            )
            self._goal_input.setPalette(placeholder_palette)
        except Exception:
            pass
        # F33: debounce the goal-set emission (held Return auto-repeats).
        self._goal_debounce_pending = False

        def _schedule_goal_emit() -> None:
            if self._goal_debounce_pending:
                return
            self._goal_debounce_pending = True
            QTimer.singleShot(150, _fire_goal_emit)

        def _fire_goal_emit() -> None:
            self._goal_debounce_pending = False
            text = self._goal_input.text().strip()
            self.goal_set.emit(text)
            # P0 §3.13: persist the goal to the on-disk recent-goals store.
            if text:
                try:
                    from cortex.libs.store.goal_store import add_goal
                    add_goal(text)
                    self._refresh_recent_goals_dropdown()
                except Exception:
                    logger.debug("goal_store add_goal failed", exc_info=True)

        self._goal_input.returnPressed.connect(_schedule_goal_emit)
        # Exposed for tests so they can drive the coalescer deterministically.
        self._schedule_goal_emit = _schedule_goal_emit
        self._fire_goal_emit = _fire_goal_emit

        # P0 §3.13: recent goals behind a trailing pull-down glyph inside
        # the goal field. Hidden until the store has at least one goal.
        self._goal_history_action = None
        try:
            from PySide6.QtGui import QAction  # noqa: F401  (presence check)

            icon = _make_history_icon(CX_TEXT_SECONDARY)
            action = self._goal_input.addAction(
                icon, QLineEdit.ActionPosition.TrailingPosition
            )
            action.setToolTip("Recent goals")
            action.triggered.connect(self._open_recent_goals_menu)
            action.setVisible(False)
            self._goal_history_action = action
        except Exception:
            logger.debug("recent-goals affordance init failed", exc_info=True)
        root.addWidget(self._goal_input)
        try:
            self._refresh_recent_goals_dropdown()
        except Exception:
            logger.debug("initial recent goals refresh failed", exc_info=True)
        root.addSpacing(SP5)

        # ── Biometrics card ───────────────────────────────────────────
        # NB: Qt's ``QFrame`` selector matches every subclass (QLabel etc.),
        # so every card stylesheet is scoped by objectName.
        bio_card = QFrame()
        bio_card.setObjectName("CortexBioCard")
        bio_card.setStyleSheet(f"QFrame#CortexBioCard {{ {CARD_QSS} }}")
        bio_inner = QVBoxLayout(bio_card)
        bio_inner.setContentsMargins(SP5, SP4, SP5, SP4)
        bio_inner.setSpacing(SP3)

        bio_heading = QLabel("Biometrics")
        bio_heading.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        bio_heading.setStyleSheet(SECTION_HEADING_QSS)
        bio_inner.addWidget(bio_heading)

        # Phase J-3: empty state before the first capture frame. The copy
        # says what is actually happening — the camera has not delivered a
        # frame yet — rather than instructing the user to do something.
        self._bio_empty_state = QLabel("Waiting for the camera… Live biometrics appear as soon as frames arrive.")
        self._bio_empty_state.setObjectName("CortexBioEmptyState")
        self._bio_empty_state.setWordWrap(True)
        self._bio_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bio_empty_state.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._bio_empty_state.setStyleSheet(
            "QLabel#CortexBioEmptyState {"
            f"  color: {CX_TEXT_TERTIARY};"
            "  background: transparent;"
            "  padding: 2px 0 6px 0;"
            "}"
        )
        _set_accessible_name(self._bio_empty_state, "Biometrics empty state")
        bio_inner.addWidget(self._bio_empty_state)

        bio_row = QHBoxLayout()
        bio_row.setSpacing(0)
        bio_row.setContentsMargins(0, 0, 0, 0)

        self._bpm_label = QLabel("--")
        self._blk_label = QLabel("--")

        # Channel tints stay as 6 px dots (data identity); the caption itself
        # uses the secondary text token so it clears AA at 11 px.
        bio_specs = [
            (self._bpm_label, "BPM", BIO_HR, "hr"),
            (self._blk_label, "BLK", BIO_BLINK, "blink"),
        ]
        for val_widget, title, color, glossary_key in bio_specs:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)

            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # Hero numeral — the display serif's one non-wordmark use.
            val_widget.setStyleSheet(HERO_NUMERAL_QSS)
            _set_accessible_name(val_widget, f"{title} value")
            tip = _CONCEPTS_GLOSSARY.get(glossary_key)
            if tip:
                try:
                    val_widget.setToolTip(tip)
                except Exception:
                    pass

            heading_row = QHBoxLayout()
            heading_row.setContentsMargins(0, 0, 0, 0)
            heading_row.setSpacing(SP1)
            heading_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot = QLabel()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet(status_dot_qss(color, size=6))
            heading = QLabel(title)
            heading.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
            heading.setStyleSheet(
                f"color: {CX_TEXT_SECONDARY}; background: transparent; border: none;"
            )
            if tip:
                try:
                    heading.setToolTip(tip)
                except Exception:
                    pass
            heading_row.addWidget(dot, alignment=Qt.AlignmentFlag.AlignVCenter)
            heading_row.addWidget(heading, alignment=Qt.AlignmentFlag.AlignVCenter)
            col.addWidget(val_widget)
            col.addLayout(heading_row)
            bio_row.addLayout(col, stretch=1)

        # Numerics and the status banner share a fixed height so the card
        # never reflows when the first reading lands.
        _BIO_SWAP_HEIGHT = 96
        self._bio_numerics = QWidget()
        self._bio_numerics.setStyleSheet("background: transparent;")
        self._bio_numerics.setLayout(bio_row)
        self._bio_numerics.setFixedHeight(_BIO_SWAP_HEIGHT)
        bio_inner.addWidget(self._bio_numerics)

        # Contextual status banner ("Camera offline …" / "Looking for your
        # face…" / "Reading your pulse…") shown while ``heart_rate`` is None.
        self._bio_status_label = QLabel("")
        self._bio_status_label.setObjectName("CortexBioStatus")
        self._bio_status_label.setWordWrap(True)
        self._bio_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bio_status_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._bio_status_label.setStyleSheet(
            "QLabel#CortexBioStatus {"
            f"  color: {CX_TEXT_SECONDARY};"
            "  background: transparent;"
            "  padding: 0 8px;"
            "}"
        )
        self._bio_status_label.setFixedHeight(_BIO_SWAP_HEIGHT)
        self._bio_status_label.setVisible(False)
        _set_accessible_name(self._bio_status_label, "Biometrics status")
        bio_inner.addWidget(self._bio_status_label)
        root.addWidget(bio_card)
        root.addSpacing(SP4)

        # ── Connections row — dot + "Chrome · Off" text, never colour alone ──
        conn_row = QHBoxLayout()
        conn_row.setContentsMargins(SP2, 0, SP2, 0)
        conn_row.setSpacing(SP3)

        self._conn_dots: dict[str, QLabel] = {}
        self._conn_labels: dict[str, QLabel] = {}
        for name in ("Chrome", "Edge", "Editor"):
            dot = QLabel()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet(status_dot_qss(CX_TEXT_TERTIARY, size=6))
            lbl = QLabel(f"{name} · Off")
            lbl.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            lbl.setStyleSheet(f"color: {CX_TEXT_SECONDARY}; background: transparent;")
            _set_accessible_name(lbl, f"{name} extension: off")
            conn_row.addWidget(dot, alignment=Qt.AlignmentFlag.AlignVCenter)
            conn_row.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
            self._conn_dots[name] = dot
            self._conn_labels[name] = lbl

        conn_row.addStretch()

        self._connect_btn = QPushButton("Connect…")
        self._connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _set_accessible_name(self._connect_btn, "Open Connections panel")
        _set_accessible_description(
            self._connect_btn, "Opens the window that links your browser and editor.",
        )
        try:
            self._connect_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._connect_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._connect_btn.setStyleSheet(BTN_LINK_QSS)
        conn_row.addWidget(self._connect_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(conn_row)
        root.addSpacing(SP5)

        # ── Divider (hairline, system separator) ───────────────────────
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {CX_BORDER_DEFAULT};")
        root.addWidget(divider)
        root.addSpacing(SP5)

        # ── This session — hidden until a real estimate exists ────────
        # The three numbers are exactly what the dashboard measures from the
        # STATE_UPDATE stream since this session started: steady-activity
        # time, the longest contiguous steady run, and how many nudges were
        # shown. Nothing here is a placeholder or a guess.
        self._session_stats = QWidget()
        self._session_stats.setStyleSheet("background: transparent;")
        stats_col = QVBoxLayout(self._session_stats)
        stats_col.setContentsMargins(0, 0, 0, 0)
        stats_col.setSpacing(SP3)
        today_label = QLabel("This session")
        today_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        today_label.setStyleSheet(SECTION_HEADING_QSS)
        stats_col.addWidget(today_label)

        today_row = QHBoxLayout()
        today_row.setSpacing(0)

        self._today_focus = QLabel("0m")
        self._today_best = QLabel("0s")
        self._today_blocked = QLabel("0")

        for val_widget, title in [
            (self._today_focus, "Steady"),
            (self._today_best, "Longest steady"),
            (self._today_blocked, "Nudges shown"),
        ]:
            col = QVBoxLayout()
            col.setSpacing(2)
            col.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            val_widget.setFont(mac_native.system_font(FS_TITLE, "semibold"))
            val_widget.setStyleSheet(f"color: {CX_TEXT}; background: transparent;")
            _set_accessible_name(val_widget, title)
            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            heading.setStyleSheet(f"color: {CX_TEXT_SECONDARY}; background: transparent;")
            col.addWidget(val_widget)
            col.addWidget(heading)
            today_row.addLayout(col, stretch=1)

        stats_col.addLayout(today_row)
        self._session_stats.setVisible(False)
        root.addWidget(self._session_stats)
        root.addStretch()

        # Footer meta strip (ambient chips) just above the session control.
        root.addLayout(self._meta_strip)

        # ── Session control ─────────────────────────────────────────────
        # "End session" stops sensing and shows the recap; Cortex stays
        # open. "Quit Cortex" appears only once the session has ended and
        # names its consequence. Cmd+Q belongs to Quit alone (app menu /
        # tray), never to this button.
        root.addSpacing(SP4)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(SP2)

        self._quit_btn = QPushButton("Quit Cortex")
        self._quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._quit_btn.setMinimumHeight(36)
        self._quit_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._quit_btn.setStyleSheet(BTN_DESTRUCTIVE_QSS)
        try:
            self._quit_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        _set_accessible_name(self._quit_btn, "Quit Cortex")
        _set_accessible_description(
            self._quit_btn,
            "Quits Cortex. Sensing is already off; the menu-bar icon closes "
            "until you relaunch.",
        )
        try:
            self._quit_btn.setToolTip("Quits Cortex and closes the menu-bar icon")
        except Exception:
            pass
        self._quit_btn.setVisible(False)
        self._quit_btn.clicked.connect(self._on_quit_clicked)
        footer.addWidget(self._quit_btn)

        self._stop_btn = QPushButton("End session")
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setMinimumHeight(36)
        self._stop_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._stop_btn.setStyleSheet(BTN_GHOST_QSS)
        try:
            self._stop_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._stop_safety_timer = QTimer(self)
        self._stop_safety_timer.setSingleShot(True)
        self._stop_safety_timer.setInterval(_STOP_SAFETY_TIMEOUT_MS)
        self._stop_safety_timer.timeout.connect(self._stop_safety_expired)
        self._stop_btn.clicked.connect(self._on_primary_clicked)
        footer.addWidget(self._stop_btn, stretch=1)
        root.addLayout(footer)
        self._enter_phase(_PHASE_DISCONNECTED)

        # F55: explicit tab-order chain: Goal → Connect → Quit → session control.
        _set_tab_order(self._goal_input, self._connect_btn)
        _set_tab_order(self._connect_btn, self._quit_btn)
        _set_tab_order(self._quit_btn, self._stop_btn)

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

    def refresh_baseline_freshness(self) -> None:
        """P0 §3.4 — refresh the measured-profile chip in the meta strip.

        Hidden when no profile exists (don't shame users during onboarding)
        or it is fresh. Past 30 days the chip becomes a real control:
        "Profile is N days old · Recalibrate" starts recalibration."""
        pill = getattr(self, "_baseline_pill", None)
        if pill is None:
            return
        try:
            age = _baseline_age_days()
        except Exception:
            return
        if age is None or age <= 30.0:
            pill.setVisible(False)
            return
        days = int(age)
        pill.setText(f"Profile is {days} days old · Recalibrate")
        try:
            pill.setToolTip(
                "Measured baselines drift over time. Starts the guided "
                "recalibration; nothing is saved until you review it."
            )
        except Exception:
            pass
        pill.setVisible(True)

    # ── P0 §3.15: LLM cost meter ────────────────────────────────────

    def apply_cost_update(
        self,
        cost_today: float,
        budget: float = 0.0,
    ) -> None:
        """Render the cost pill from a COST_RESPONSE payload.

        ``cost_today`` is the running daily spend in USD; ``budget`` is
        the configured daily cap (0.0 = unlimited). At 80% of budget
        we emit a one-shot toast so the user can pre-empt the
        kill-switch. The pill is subdued (tertiary label) until 50% of
        budget then warms up to the accent so the user notices.
        """
        try:
            cost = max(0.0, float(cost_today))
        except (TypeError, ValueError):
            cost = 0.0
        try:
            cap = max(0.0, float(budget))
        except (TypeError, ValueError):
            cap = 0.0

        ratio = (cost / cap) if cap > 0 else 0.0
        # Compose the visible string. Below $0.005 we show "$—" so the
        # initial empty state doesn't lie ("$0.00 today" is misleading
        # when the daemon hasn't reported any data yet).
        text = "$—" if cost < 0.005 and self._cost_last_value < 0 else f"${cost:.2f}"
        if cap > 0:
            text = f"{text} / ${cap:.2f}"
        if cost == self._cost_last_value:
            return
        self._cost_last_value = cost
        try:
            self._cost_pill.setText(text)
            color = BRAND_ACCENT_TEXT if ratio >= 0.80 else CX_TEXT_SECONDARY
            self._cost_pill.setStyleSheet(
                f"QLabel#CortexCostPill {{ {PILL_QSS} color: {color}; }}"
            )
            # The chip exists only once the daemon has reported real spend.
            self._cost_pill.setVisible(True)
        except Exception:
            logger.debug("cost pill update failed", exc_info=True)
        # One-shot 80% threshold toast.
        if cap > 0 and ratio >= 0.80 and not self._cost_budget_warned:
            self._cost_budget_warned = True
            toast = getattr(self, "_toast", None)
            if toast is not None:
                try:
                    toast.show_info(
                        "Approaching daily LLM budget.",
                        f"You've used ${cost:.2f} of your ${cap:.2f} cap today.",
                    )
                except Exception:
                    logger.debug("toast budget warn failed", exc_info=True)

    # ── P0 §3.13: recent goals dropdown ─────────────────────────────

    def _refresh_recent_goals_dropdown(self) -> None:
        """Show/hide the goal field's recent-goals affordance based on
        whether the on-disk store has any goals.

        Idempotent — safe to call from the input return-pressed handler
        (after each new goal is persisted) and from the constructor.
        Kept under its historical name (call sites unchanged); the inline
        pull-down affordance replaced the old combobox.
        """
        action = getattr(self, "_goal_history_action", None)
        if action is None:
            return
        try:
            from cortex.libs.store.goal_store import load_goals
            goals = load_goals()
        except Exception:
            logger.debug("load_goals failed; hiding affordance", exc_info=True)
            goals = []
        try:
            action.setVisible(bool(goals))
        except Exception:
            logger.debug("recent goals affordance toggle failed", exc_info=True)

    def _open_recent_goals_menu(self) -> None:
        """Open a native menu of recent goals below the goal field.

        Built fresh on each open from the on-disk store so it always
        reflects the latest history. Selecting an item fills the field and
        emits ``goal_set`` — identical downstream wiring to the old
        combobox path.
        """
        try:
            from cortex.libs.store.goal_store import load_goals
            goals = load_goals()[:8]
        except Exception:
            logger.debug("load_goals failed; no menu", exc_info=True)
            return
        if not goals:
            return
        try:
            menu = QMenu(self._goal_input)
            menu.setObjectName("RecentGoalsMenu")
            # Force an opaque surface directly on the popup instance. The
            # global QMenu rule already covers this, but the popup is a
            # separate top-level window on macOS and the app-wide cascade
            # can miss it under vibrancy — without an opaque background the
            # menu renders see-through and its items bleed onto the card
            # behind it. Setting the stylesheet on the instance guarantees
            # the paint regardless of cascade.
            try:
                menu.setAttribute(
                    Qt.WidgetAttribute.WA_TranslucentBackground, False
                )
            except Exception:
                pass
            menu.setStyleSheet(
                f"QMenu#RecentGoalsMenu {{"
                f" background-color: {CX_SURFACE};"
                f" color: {CX_TEXT};"
                f" border: 1px solid {CX_BORDER_DEFAULT};"
                f" border-radius: 8px; padding: 4px; }}"
                f"QMenu#RecentGoalsMenu::item {{"
                f" padding: 6px 12px; border-radius: 5px; }}"
                f"QMenu#RecentGoalsMenu::item:selected {{"
                f" background-color: {BRAND_ACCENT}; color: {CX_TEXT}; }}"
            )
            menu.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            for g in goals:
                title = g.title
                label = (title[:46] + "…") if len(title) > 47 else title
                act = menu.addAction(label)
                act.setData(g.id)
                act.triggered.connect(
                    lambda _checked=False, gid=g.id, t=title:
                    self._on_recent_goal_chosen(str(gid), t)
                )
            field = self._goal_input
            pos = field.mapToGlobal(field.rect().bottomLeft())
            menu.exec(pos)
        except Exception:
            logger.debug("recent goals menu failed", exc_info=True)

    def _on_recent_goal_chosen(self, goal_id: str, title: str) -> None:
        """Apply a goal chosen from the recent-goals menu: fill the field,
        mark it used (so it sorts to the top next time), and emit
        ``goal_set`` so the daemon picks it up.
        """
        try:
            self._goal_input.setText(title)
        except Exception:
            logger.debug("goal_input setText failed", exc_info=True)
        if goal_id:
            try:
                from cortex.libs.store.goal_store import mark_used
                mark_used(goal_id)
            except Exception:
                logger.debug("mark_used failed", exc_info=True)
        try:
            self.goal_set.emit(title)
        except Exception:
            logger.debug("goal_set emit failed", exc_info=True)

    # ── P0 §3.11 / §3.10: quiet-mode + auto-focus surfaces ──────────

    def _on_quiet_capsule_clicked(self) -> None:
        """Open the Pause/Quiet menu at the capsule's anchor point.

        Three actions:
          1. Snooze 15 min — overlay-only suppression for 15 min.
          2. Quiet for session — overlay-only suppression for the
             daemon's default ``quiet_mode_minutes`` (typically 30).
          3. Pause all sensing — releases the camera, indefinite.

        When any mode is already active, an extra "Off" item appears
        first so the user can disarm without leaving the menu.
        """
        try:
            menu = QMenu(self)
        except Exception:
            logger.debug("QMenu construction failed", exc_info=True)
            return
        active = (self._quiet_mode_state or {}).get("kind", "off")

        def _add(label: str, kind: str, minutes: int = 0) -> None:
            try:
                action = menu.addAction(label)
                if action is not None and hasattr(action, "triggered"):
                    action.triggered.connect(
                        lambda _checked=False, k=kind, m=minutes:
                            self.quiet_mode_requested.emit(k, m),
                    )
            except Exception:
                logger.debug("menu action wiring failed", exc_info=True)

        if active != "off":
            _add("Turn off (resume)", "off", 0)
            try:
                menu.addSeparator()
            except Exception:
                pass

        _add("Snooze 15 min", "snooze_15", 15)
        _add("Quiet for session", "quiet_session", 0)
        _add("Pause all sensing", "pause", 0)

        try:
            from PySide6.QtCore import QPoint  # local import for test stubs

            anchor = self._quiet_capsule.mapToGlobal(
                QPoint(0, self._quiet_capsule.height()),
            )
            menu.exec(anchor)
        except Exception:
            # In the lightweight test stub QMenu.exec is a no-op.
            logger.debug("quiet menu exec failed", exc_info=True)

    def apply_quiet_mode_state(self, payload: dict) -> None:
        """P0 §3.11: render the pause capsule (label + colour) from
        the daemon's QUIET_MODE_STATE broadcast.
        """
        if not isinstance(payload, dict):
            return
        self._quiet_mode_state = dict(payload)
        kind = str(payload.get("kind", "off"))
        duration = payload.get("duration_minutes")
        labels = {
            "off": "Pause",
            "snooze_15": "Snoozed",
            "quiet_session": "Quiet",
            "pause": "Paused",
        }
        label = labels.get(kind, "Pause")
        if kind != "off" and isinstance(duration, int) and duration > 0:
            label = f"{label} · {duration}m"
        try:
            self._quiet_capsule.setText(label)
        except Exception:
            logger.debug("quiet capsule label update failed", exc_info=True)
        try:
            self._quiet_capsule.setStyleSheet(
                _ACCENT_PILL_BUTTON_QSS if kind != "off" else PILL_BUTTON_QSS
            )
        except Exception:
            pass

    def apply_auto_focus_state(self, armed: bool, preset: str = "") -> None:
        """P0 §3.10: show / hide the focus-protection toast pill."""
        try:
            if armed:
                label_preset = preset.capitalize() if preset else "Auto-armed"
                self._focus_protection_pill.setText(
                    f"Focus protected · {label_preset} · Turn off"
                )
                self._focus_protection_pill.setVisible(True)
            else:
                self._focus_protection_pill.setVisible(False)
        except Exception:
            logger.debug("focus protection pill update failed", exc_info=True)

    def apply_break_recommendation(self, payload: dict) -> None:
        """Render only the opt-in elapsed-focus reminder contract."""

        if payload.get("basis") != "elapsed_focus":
            self._clear_break_pill()
            return
        self._break_recommendation_payload = dict(payload)
        try:
            elapsed = float(payload.get("elapsed_focus_seconds", 0.0))
            minutes = max(1, round(elapsed / 60.0))
            self._break_pill.setText(f"Take a break? · {minutes} min")
            self._break_pill.setToolTip(
                "Your preferred active-work interval was reached. "
                "This reminder does not use camera or biometric estimates."
            )
            self._break_pill.setVisible(True)
        except Exception:
            logger.debug("break pill update failed", exc_info=True)

    def _on_break_pill_clicked(self) -> None:
        """Bubble the cached BREAK_RECOMMENDATION payload up to the host."""
        payload = dict(self._break_recommendation_payload)
        try:
            self.break_pill_clicked.emit(payload)
        except Exception:
            logger.debug("break_pill_clicked emit failed", exc_info=True)
        self._clear_break_pill()

    def _clear_break_pill(self) -> None:
        """Hide the break pill + clear cached payload."""
        try:
            self._break_pill.setVisible(False)
        except Exception:
            pass
        self._break_recommendation_payload = {}
        if self._break_pill_snooze_timer is not None:
            try:
                self._break_pill_snooze_timer.stop()
            except Exception:
                pass
            self._break_pill_snooze_timer = None

    # ── P0 §3.16: undo toast + restore pill ─────────────────────────

    # Compatibility symbol retained for downstream imports. It is deliberately
    # empty: an action name is not restoration evidence. Only a verified exact
    # transaction receipt may surface Undo/Restore.
    _DESKTOP_REVERSIBLE_ACTIONS: frozenset[str] = frozenset()

    def apply_intervention_applied(self, payload: dict) -> None:
        """Render the Undo toast on a reversible INTERVENTION_APPLIED.

        This compatibility entry point requires ``transaction_verified=True``
        plus ``is_reversible=True``. Callers may not infer reversibility from
        action names or optimistic adapter acknowledgements.
        """
        if not isinstance(payload, dict):
            return
        action_type = str(payload.get("action_type") or "")
        intervention_id = str(payload.get("intervention_id") or "")
        try:
            applied = int(payload.get("mutations_applied_count") or 0)
        except (TypeError, ValueError):
            applied = 0
        is_reversible = payload.get("is_reversible")
        if (
            payload.get("transaction_verified") is not True
            or is_reversible is not True
            or applied <= 0
            or not intervention_id
        ):
            return
        import time as _time
        now = _time.monotonic()
        if any(
            entry[1] == intervention_id
            for entry in self._reversible_actions
        ):
            return
        self._reversible_actions.append(
            (now, intervention_id, action_type, applied)
        )
        # Trim entries older than the configured window so the restore
        # pill clears naturally.
        cutoff = now - self._reversible_window_seconds
        self._reversible_actions = [
            entry for entry in self._reversible_actions
            if entry[0] >= cutoff
        ]
        self._show_undo_toast(intervention_id, action_type, applied)
        self._refresh_restore_pill()

    def apply_intervention_transaction_state(self, payload: dict) -> None:
        """Project verified transaction outcomes into the Undo affordance."""

        if not isinstance(payload, dict):
            return
        intervention_id = str(payload.get("intervention_id") or "")
        state = str(payload.get("state") or "")
        if not intervention_id:
            return
        if state in {"restored", "failed", "restore_failed"}:
            self._reversible_actions = [
                entry for entry in self._reversible_actions
                if entry[1] != intervention_id
            ]
            if self._undo_toast_timer is not None:
                try:
                    self._undo_toast_timer.stop()
                except Exception:
                    pass
                self._undo_toast_timer = None
            if self._undo_toast is not None:
                try:
                    self._undo_toast.deleteLater()
                except Exception:
                    pass
                self._undo_toast = None
            self._refresh_restore_pill()
            return
        if state != "applied":
            return
        raw_results = payload.get("receipt_results")
        results = raw_results if isinstance(raw_results, list) else []
        restorable = [
            item for item in results
            if isinstance(item, dict)
            and item.get("reversible") is True
            and item.get("status") in {"succeeded", "already_complete"}
        ]
        if not restorable:
            return
        self.apply_intervention_applied({
            "intervention_id": intervention_id,
            "action_type": "workspace_change",
            "mutations_applied_count": len(restorable),
            "is_reversible": True,
            "transaction_verified": True,
        })

    def _show_undo_toast(
        self,
        intervention_id: str,
        action_type: str,
        applied_count: int,
    ) -> None:
        """Gmail-style toast at the bottom of the dashboard with a 5 s
        countdown. Clicking Undo emits ``undo_action_requested``.
        """
        action_label = action_type.replace("_", " ")
        if action_type == "close_tab":
            verb = f"Closed {applied_count} tab" + ("s" if applied_count != 1 else "")
        elif action_type == "group_tabs":
            verb = f"Grouped {applied_count} tab" + ("s" if applied_count != 1 else "")
        else:
            verb = action_label.capitalize()
        # Tear down any prior toast.
        if self._undo_toast is not None:
            try:
                self._undo_toast.deleteLater()
            except Exception:
                pass
            self._undo_toast = None
        if self._undo_toast_timer is not None:
            try:
                self._undo_toast_timer.stop()
            except Exception:
                pass
            self._undo_toast_timer = None
        toast = QFrame(self)
        toast.setObjectName("CortexUndoToast")
        toast.setStyleSheet(
            "QFrame#CortexUndoToast {"
            "  background: rgba(28, 28, 30, 0.94);"
            f"  border-radius: {RADIUS_PILL}px;"
            "  padding: 6px 14px;"
            "}"
            "QLabel { color: white; background: transparent; }"
            "QPushButton {"
            "  background: transparent;"
            f"  color: {BRAND_ACCENT_DARK};"
            "  border: none;"
            "  padding: 2px 8px;"
            f"  font-weight: {FW_SEMIBOLD};"
            "}"
            "QPushButton:hover { color: white; }"
        )
        row = QHBoxLayout(toast)
        row.setContentsMargins(SP3, SP2, SP2, SP2)
        row.setSpacing(SP3)
        msg = QLabel(f"{verb} · ")
        msg.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        countdown = QLabel("5s")
        countdown.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        undo = QPushButton("Undo")
        undo.setCursor(Qt.CursorShape.PointingHandCursor)
        undo.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        row.addWidget(msg)
        row.addWidget(countdown)
        row.addWidget(undo)

        remaining = {"sec": 5}

        def _on_undo(_checked: bool = False) -> None:
            try:
                self.undo_action_requested.emit(intervention_id)
            except Exception:
                logger.debug("undo emit failed", exc_info=True)
            _dismiss()

        def _dismiss() -> None:
            try:
                if self._undo_toast_timer is not None:
                    self._undo_toast_timer.stop()
                    self._undo_toast_timer = None
            except Exception:
                pass
            try:
                toast.deleteLater()
            except Exception:
                pass
            self._undo_toast = None

        def _tick() -> None:
            remaining["sec"] -= 1
            if remaining["sec"] <= 0:
                _dismiss()
                return
            try:
                countdown.setText(f"{remaining['sec']}s")
            except Exception:
                _dismiss()

        try:
            undo.clicked.connect(_on_undo)
        except Exception:
            pass
        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(_tick)
        timer.start()
        self._undo_toast_timer = timer

        # Anchor toast at the bottom-center of the dashboard widget.
        try:
            toast.adjustSize()
            x = (self.width() - toast.width()) // 2
            y = self.height() - toast.height() - 24
            toast.move(max(SP3, x), max(SP3, y))
            toast.show()
            toast.raise_()
        except Exception:
            logger.debug("toast positioning failed", exc_info=True)
        self._undo_toast = toast

    def _refresh_restore_pill(self) -> None:
        """Show / hide the "Restore previous state" chip based on the
        sliding-window membership of recently reversible actions."""
        import time as _time
        now = _time.monotonic()
        cutoff = now - self._reversible_window_seconds
        self._reversible_actions = [
            entry for entry in self._reversible_actions
            if entry[0] >= cutoff
        ]
        pill = getattr(self, "_restore_pill", None)
        if pill is None:
            return
        try:
            pill.setVisible(bool(self._reversible_actions))
        except Exception:
            pass

    def _on_restore_pill_clicked(self) -> None:
        """User wants to restore — undo the most recent reversible action."""
        if not self._reversible_actions:
            self._refresh_restore_pill()
            return
        _ts, intervention_id, _action_type, _applied = self._reversible_actions[-1]
        try:
            self.undo_action_requested.emit(intervention_id)
        except Exception:
            logger.debug("undo emit failed", exc_info=True)
        # Drop the entry we just undid; the pill auto-hides if it was
        # the only one in the window.
        self._reversible_actions = self._reversible_actions[:-1]
        self._refresh_restore_pill()

    def update_state(self, payload: dict) -> None:
        view = consumer_state_view(payload)
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

        # F16 (Phase-4 audit): drive the health banner from the
        # envelope-level flags the daemon stamps on STATE_UPDATE. The
        # capture-stale message wins over the store-degraded one
        # because a dead camera is more user-actionable than a
        # degraded SQLite store.
        try:
            if view.health_message is not None:
                health_text = view.health_message
                if health_text.startswith("Camera offline"):
                    health_text = (
                        f"{health_text} · "
                        f'<a style="color: {CX_DANGER};" '
                        'href="cortex-settings">Open Settings</a>'
                    )
                self._set_text_if_changed(self._health_banner, health_text)
                self._health_banner.setVisible(True)
            else:
                self._health_banner.setVisible(False)
        except Exception:
            # Mock widgets in unit tests may not expose setVisible /
            # setText; the visibility flag isn't load-bearing.
            pass

        # A state frame means the daemon is live, whatever the connection
        # signal said last.
        if self._session_phase in (_PHASE_STARTING, _PHASE_DISCONNECTED):
            self._enter_phase(_PHASE_LIVE)

        state = view.state
        self._last_state = state
        self._render_state_badge(state, view.label)

        hr = view.heart_rate
        blink = view.blink_rate

        # When no heart-rate has landed yet, swap the BPM/HRV/BLK row for
        # a contextual status line so the user can tell apart "camera off"
        # from "camera on, still warming up". The daemon stamps
        # ``payload["capture"]`` on every STATE_UPDATE; older daemons
        # that lack the field fall through to the "Reading your pulse…"
        # default, which is the most benign of the three states.
        if hr is None:
            self._set_text_if_changed(
                self._bio_status_label,
                view.biometrics_status or "Reading your pulse…",
            )
            try:
                self._bio_status_label.setVisible(True)
                self._bio_numerics.setVisible(False)
            except Exception:
                pass
        else:
            try:
                self._bio_status_label.setVisible(False)
                self._bio_numerics.setVisible(True)
            except Exception:
                pass
            self._set_text_if_changed(self._bpm_label, f"{hr:.0f}")
            self._set_text_if_changed(
                self._blk_label, f"{blink:.1f}" if blink is not None else "--"
            )

        # "This session" stats accumulate steady-activity seconds, the
        # longest contiguous steady run, and nudges shown. The section is
        # revealed only once a real estimate has arrived.
        try:
            self._accumulate_today_stats(state)
            if state != "UNKNOWN" and not self._has_estimate:
                self._has_estimate = True
                self._session_stats.setVisible(True)
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
            _CLIENT_TYPE_TO_DOT = {
                "chrome": "Chrome",
                "edge": "Edge",
                "vscode": "Editor",
            }
            for client_type, dot_name in _CLIENT_TYPE_TO_DOT.items():
                self.set_extension_connected(
                    dot_name,
                    client_type in view.connected_surfaces,
                )
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
        # Format steady activity (h:mm) and Best (m:ss / h:mm) compactly.
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
        self._set_text_if_changed(self._today_best, best_text)
        self._set_text_if_changed(
            self._today_blocked, str(self._today_intervention_count)
        )

    def record_intervention_seen(self) -> None:
        """Invoked by the parent dashboard when an intervention is
        broadcast so the "Nudges shown" counter advances.
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
        """Update a Chrome / Edge / Editor connection row: dot colour, the
        "· On / Off" text, and the accessible name together.

        The visible word is short because three rows plus the Connect control
        share one 380 px line; "Connected" overflowed it on the v0.4.0 local
        build. The accessible name keeps the full word.

        ``name`` is matched case-insensitively against the constructed
        keys ("Chrome", "Edge", "Editor"). Unknown names are ignored.
        """
        key_match = None
        for key in self._conn_dots:
            if key.lower() == (name or "").lower():
                key_match = key
                break
        if key_match is None:
            return
        dot = self._conn_dots[key_match]
        label = self._conn_labels.get(key_match)
        color = CX_SUCCESS if connected else CX_TEXT_TERTIARY
        status = "On" if connected else "Off"
        try:
            self._set_style_if_changed(dot, status_dot_qss(color, size=6))
        except Exception:
            pass
        if label is not None:
            self._set_text_if_changed(label, f"{key_match} · {status}")
            self._set_style_if_changed(
                label,
                f"color: {CX_TEXT if connected else CX_TEXT_SECONDARY};"
                " background: transparent;",
            )
            _set_accessible_name(
                label,
                f"{key_match} extension: {'connected' if connected else 'off'}",
            )

    def set_connected(self, connected: bool) -> None:
        # G3 (audit-prod): when the daemon connection drops, gray every
        # extension row too — they can't possibly be alive without the
        # daemon. This keeps the dashboard's connection story coherent.
        if connected:
            self._ended_by_user = False
            self._enter_phase(_PHASE_LIVE)
            return
        for name in list(self._conn_dots.keys()):
            self.set_extension_connected(name, False)
        if self._session_phase == _PHASE_STOPPING:
            # The daemon went away while we were ending the session: that
            # is the acknowledgement, whichever host mode we run in.
            self.notify_daemon_stopped()
        elif self._session_phase == _PHASE_ENDED:
            return
        else:
            self._enter_phase(_PHASE_DISCONNECTED)

    def set_starting(self) -> None:
        """Render a truthful pre-readiness state during daemon startup."""
        for name in list(self._conn_dots.keys()):
            self.set_extension_connected(name, False)
        self._enter_phase(_PHASE_STARTING)

    def set_session_restart_available(self, available: bool) -> None:
        """Whether this host can start a new session after one ends.

        The in-process app restarts its daemon; the WebSocket dev shell
        cannot start a daemon, so its ended state offers Quit only.
        """
        self._restart_available = bool(available)
        self._enter_phase(self._session_phase)

    def apply_palette_change(self) -> None:
        """Re-render state colours after the accessibility palette changed."""
        if self._session_phase == _PHASE_LIVE and self._has_received_state:
            label = self._render_cache.get(id(self._state_label), {}).get("text", "")
            self._render_state_badge(self._last_state, label or "")

    def _render_state_badge(self, state: str, label: str) -> None:
        """Dot + label for a live state estimate (palette-aware)."""
        if state == "UNKNOWN":
            dot_color = CX_TEXT_TERTIARY
            text_color = CX_TEXT_SECONDARY
        else:
            dot_color = active_state_color(state)
            text_color = STATE_TEXT_COLORS.get(state, CX_TEXT_SECONDARY)
        self._set_style_if_changed(self._state_dot, status_dot_qss(dot_color, size=7))
        self._set_text_if_changed(self._state_label, label)
        self._set_style_if_changed(
            self._state_label, f"color: {text_color}; background: transparent;"
        )

    # ------------------------------------------------------------------
    # Session lifecycle — one place decides every label
    # ------------------------------------------------------------------

    def _enter_phase(self, phase: str) -> None:
        previous = self._session_phase
        self._session_phase = phase
        btn = self._stop_btn
        quit_btn = getattr(self, "_quit_btn", None)
        restart = self._restart_available

        def _pill(text: str, color: str = CX_TEXT_TERTIARY) -> None:
            self._set_text_if_changed(self._state_label, text)
            self._set_style_if_changed(self._state_dot, status_dot_qss(color, size=7))
            self._set_style_if_changed(
                self._state_label,
                f"color: {CX_TEXT_SECONDARY}; background: transparent;",
            )

        show_quit = False
        if phase == _PHASE_STARTING:
            btn.setText("Starting…")
            btn.setEnabled(False)
            _set_accessible_name(btn, "Starting session")
            _set_accessible_description(btn, "Cortex is starting its sensors.")
            _pill("Starting…")
        elif phase == _PHASE_LIVE:
            btn.setText("End session")
            btn.setEnabled(True)
            _set_accessible_name(btn, "End session")
            _set_accessible_description(
                btn,
                "Stops sensing and shows a summary of this session. "
                "Cortex stays open.",
            )
            if previous != _PHASE_LIVE:
                _pill("Connected", _CONNECTED_DOT)
        elif phase == _PHASE_STOPPING:
            btn.setText("Ending…")
            btn.setEnabled(False)
            _set_accessible_name(btn, "Ending session")
            _set_accessible_description(btn, "Cortex is stopping its sensors.")
            _pill("Ending session…")
        elif phase == _PHASE_ENDED:
            show_quit = True
            if restart:
                btn.setText("Start session")
                btn.setEnabled(True)
                _set_accessible_name(btn, "Start session")
                _set_accessible_description(
                    btn, "Starts the camera and input sensing again.",
                )
            else:
                btn.setText("Session ended")
                btn.setEnabled(False)
                _set_accessible_name(btn, "Session ended")
                _set_accessible_description(
                    btn, "Sensing is off. Relaunch the daemon to start again.",
                )
            _pill("Session ended")
        else:  # disconnected
            show_quit = True
            if restart:
                btn.setText("Start session")
                btn.setEnabled(True)
                _set_accessible_name(btn, "Start session")
                _set_accessible_description(
                    btn, "Starts the camera and input sensing.",
                )
            else:
                btn.setText("Disconnected")
                btn.setEnabled(False)
                _set_accessible_name(btn, "Disconnected")
                _set_accessible_description(
                    btn, "Cortex is not connected to its daemon.",
                )
            _pill("Disconnected")
        if quit_btn is not None:
            try:
                quit_btn.setVisible(show_quit)
            except Exception:
                pass

    def _on_primary_clicked(self) -> None:
        """Footer control: End session while live, Start session after."""
        if self._session_phase == _PHASE_LIVE:
            self._arm_stop()
        elif self._session_phase in (_PHASE_ENDED, _PHASE_DISCONNECTED):
            self._request_session_start()

    def _request_session_start(self) -> None:
        if not self._restart_available:
            return
        self._enter_phase(_PHASE_STARTING)
        try:
            self.session_start_requested.emit()
        except Exception:
            logger.debug("session_start_requested.emit raised", exc_info=True)

    def _on_quit_clicked(self) -> None:
        """Explicit quit after the session has ended — no recap pending."""
        try:
            self.gui_quit_requested.emit()
        except Exception:
            logger.debug("gui_quit_requested.emit raised", exc_info=True)

    # ------------------------------------------------------------------
    # F34 — End-session state machine
    # ------------------------------------------------------------------

    def _handle_stop_clicked(self) -> None:
        """Thin wrapper preserved for external call sites. Delegates to
        :meth:`_arm_stop` so the dashboard's two-phase stop flow (P0
        §3.3) is the single source of truth."""
        self._arm_stop()

    def _arm_stop(self, *, quit_after: bool = False) -> None:
        """P0 §3.3 phase 1 — ask the daemon to stop and wait for the
        SESSION_RECAP broadcast.

        ``quit_after`` records the route the user chose: False for "End
        session" (Cortex stays open once the recap is consumed), True for
        a user-initiated quit (tray, Cmd+Q). Whichever it is, the recap
        sheet's "View full report" keeps Cortex open and its "Quit
        Cortex" quits — the user can always change their mind on the
        sheet.

        * ``daemon_stop_requested`` fires IMMEDIATELY so the controller
          can schedule ``daemon.stop()`` and the SESSION_RECAP pipeline
          runs.
        * The safety + recap watchdogs arm so the GUI doesn't wedge if
          either the recap or the daemon never report back.
        * The route completes in :meth:`_finalize_stop` (recap dismissed
          / watchdog / safety / daemon ack without recap).

        Double clicks are coalesced via ``self._stopping``.
        """
        if getattr(self, "_stopping", False):
            return
        self._stopping = True
        self._quit_after_stop = bool(quit_after)
        self._ended_by_user = True
        self._enter_phase(_PHASE_STOPPING)
        self._stop_safety_timer.start()
        # Recap-watchdog: if no SESSION_RECAP arrives in
        # ``_RECAP_WATCHDOG_MS`` ms, proceed with quit anyway. Matches
        # the daemon's own 5 s broadcast timeout with a small slack so
        # we lose to the daemon by default, not the other way around.
        if getattr(self, "_recap_watchdog", None) is None:
            self._recap_watchdog = QTimer(self)
            self._recap_watchdog.setSingleShot(True)
            self._recap_watchdog.setInterval(_RECAP_WATCHDOG_MS)
            self._recap_watchdog.timeout.connect(self._on_recap_watchdog_expired)
        self._recap_finalised = False
        self._recap_watchdog.start()
        # Phase 4.B fix (#1): emit the daemon-stop request IMMEDIATELY.
        # Without this, the controller never schedules ``daemon.stop()``
        # and the SESSION_RECAP pipeline never runs.
        try:
            self.daemon_stop_requested.emit()
        except Exception:
            logger.debug("daemon_stop_requested.emit raised", exc_info=True)
        # Preserve the legacy alias so existing call sites (tests, tray
        # wiring, WS-mode CortexApp) keep working. The legacy contract
        # now means "ask the daemon to stop" — quit is gated separately
        # on ``gui_quit_requested``.
        try:
            self.stop_requested.emit()
        except Exception:
            logger.debug("stop_requested.emit (legacy alias) raised", exc_info=True)

    def _on_recap_watchdog_expired(self) -> None:
        """Called when the 6 s recap watchdog fires without a recap.

        Short sessions (<90 s) never trigger SESSION_RECAP server-side,
        so this is the expected path for them. Proceeds straight to
        :meth:`_finalize_stop`.
        """
        logger.info("Recap watchdog expired; finalising stop without recap sheet")
        self._finalize_stop()

    def _finalize_stop(self, *, quit: bool | None = None) -> None:
        """P0 §3.3 phase 2 — complete the route the user chose now that
        the recap has been consumed (or its watchdog expired).

        ``quit`` overrides the route recorded by :meth:`_arm_stop`: the
        recap sheet passes False for "View full report" (stay open) and
        True for "Quit Cortex". Idempotent; safe to call from the
        recap-sheet handlers, the watchdog, the safety timer, or the
        daemon-ack path.
        """
        if getattr(self, "_recap_finalised", False):
            return
        self._recap_finalised = True
        if getattr(self, "_recap_watchdog", None) is not None:
            try:
                self._recap_watchdog.stop()
            except Exception:
                pass
        should_quit = self._quit_after_stop if quit is None else bool(quit)
        self._quit_after_stop = should_quit
        if should_quit:
            try:
                self.gui_quit_requested.emit()
            except Exception:
                logger.debug("gui_quit_requested.emit raised", exc_info=True)
        elif not self._stopping:
            # The daemon already acknowledged; the session is over and
            # Cortex stays open.
            self._enter_phase(_PHASE_ENDED)

    def _stop_safety_expired(self) -> None:
        """F34 safety net: if the daemon never reports stopped, finish the
        route and re-enable the footer control so the user is not wedged.
        """
        logger.warning(
            "End-session safety timeout fired; finalising without daemon ack"
        )
        self._finalize_stop()
        self.notify_daemon_stopped()

    def notify_daemon_stopped(self) -> None:
        """Called when the daemon confirms shutdown (controller wires this;
        the connection-dropped path calls it too). Idempotent.

        If the recap watchdog is still armed no recap can arrive any more,
        so the route is completed here instead of waiting out the 6 s.
        """
        self._stop_safety_timer.stop()
        self._stopping = False
        watchdog = getattr(self, "_recap_watchdog", None)
        if watchdog is not None:
            try:
                pending = bool(watchdog.isActive())
            except Exception:
                pending = False
            if pending:
                self._finalize_stop()
        if not self._quit_after_stop or not getattr(self, "_recap_finalised", False):
            self._enter_phase(_PHASE_ENDED)


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
        painter.setBrush(QColor(CX_SURFACE))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), RADIUS_CARD, RADIUS_CARD)
        painter.drawPath(path)

        painter.setPen(QPen(QColor(CX_BORDER_DEFAULT), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        if len(self._values) < 2:
            painter.setPen(QColor(CX_TEXT_TERTIARY))
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

        painter.setPen(QColor(CX_TEXT))
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
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        layout.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(5)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {CX_BG_SECONDARY};"
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
            f"color: {CX_TEXT_TERTIARY}; background: transparent;"
        )
        layout.addWidget(self._val_label)

    def set_value(self, quality: float) -> None:
        pct = int(quality * 100)
        self._bar.setValue(pct)
        self._val_label.setText(f"{pct}%")
        if quality >= 0.7:
            color = CX_SUCCESS
        elif quality >= 0.4:
            color = SEMANTIC_LIGHT["warning"]
        else:
            color = CX_DANGER
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {CX_BG_SECONDARY};"
            f" border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {color};"
            f" border-radius: 2px; }}"
        )

    def set_subcomponents(
        self,
        *,
        luminance: float | None = None,
        motion_penalty: float | None = None,
        face_loss_rate: float | None = None,
    ) -> None:
        """P0 §3.18: cache sub-component values so the tooltip surfaces
        the per-channel breakdown without requiring an extra widget.

        Values fall in [0, 1]. ``None`` means the upstream
        STATE_UPDATE did not include the field — we render "—".
        Recommendation copy is attached when a sub-component is in a
        problematic range:

        * Luminance below 0.35 → "Move toward a window."
        * Motion penalty above 0.5 → "Centre your face."
        * Face-loss rate above 0.3 → "Stay in frame."
        """
        def _fmt(v: float | None) -> str:
            return "—" if v is None else f"{v * 100:.0f}%"

        lines = [
            f"Luminance: {_fmt(luminance)}",
            f"Motion penalty: {_fmt(motion_penalty)}",
            f"Face-loss rate: {_fmt(face_loss_rate)}",
        ]
        if isinstance(luminance, (int, float)) and luminance < 0.35:
            lines.append("→ Move toward a window for better lighting.")
        if isinstance(motion_penalty, (int, float)) and motion_penalty > 0.5:
            lines.append("→ Hold still and centre your face.")
        if isinstance(face_loss_rate, (int, float)) and face_loss_rate > 0.3:
            lines.append("→ Stay in frame so Cortex can read your face.")
        tip = "\n".join(lines)
        try:
            self.setToolTip(tip)
            self._bar.setToolTip(tip)
            self._val_label.setToolTip(tip)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tab 2: Advanced
# ---------------------------------------------------------------------------

class _AdvancedTab(QWidget):
    """Developer debug view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: transparent; color: {CX_TEXT};")
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
            "Waiting for the camera — signal quality, heart-rate trace, and "
            "support scores appear once frames arrive."
        )
        self._empty_state.setObjectName("CortexAdvancedEmptyState")
        self._empty_state.setWordWrap(True)
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._empty_state.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._empty_state.setStyleSheet(
            "QLabel#CortexAdvancedEmptyState {"
            f"  color: {CX_TEXT_SECONDARY};"
            f"  background: {CX_BG_SECONDARY};"
            f"  border-radius: {RADIUS_CARD}px;"
            "  padding: 10px 14px;"
            "}"
        )
        _set_accessible_name(self._empty_state, "Advanced tab empty state")
        layout.addWidget(self._empty_state)

        # Explicitly describe unavailable evidence or a safety fallback.
        self._degraded_badge = QLabel("No actionable estimate yet")
        self._degraded_badge.setObjectName("CortexDegradedBadge")
        self._degraded_badge.setFont(
            mac_native.system_font(FS_FOOTNOTE, "semibold")
        )
        # Warm terracotta hint without recoloring the whole tab.
        self._degraded_badge.setStyleSheet(
            "QLabel#CortexDegradedBadge {"
            f"  color: {BRAND_ACCENT_TEXT};"
            "  background-color: rgba(217, 119, 87, 0.10);"
            "  border: 1px solid rgba(217, 119, 87, 0.35);"
            f"  border-radius: {RADIUS_CARD}px;"
            "  padding: 4px 10px;"
            "}"
        )
        self._degraded_badge.setVisible(False)
        layout.addWidget(self._degraded_badge)

        sq_label = QLabel("Signal quality")
        sq_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        sq_label.setStyleSheet(SECTION_HEADING_QSS)
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
        hr_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(hr_label)
        self._hr_plot = HRTracePlot()
        layout.addWidget(self._hr_plot)

        scores_label = QLabel("Support scores")
        scores_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        scores_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(scores_label)

        scores_grid = QGridLayout()
        scores_grid.setVerticalSpacing(6)
        self._score_bars: dict[str, QProgressBar] = {}
        self._score_labels: dict[str, QLabel] = {}
        self._score_states: dict[str, str] = {}
        for i, (name, display_name, state_key) in enumerate([
            ("flow", "Steady", "FLOW"),
            ("hyper", "Support", "HYPER"),
            ("hypo", "Quiet", "HYPO"),
            ("recovery", "Settling", "RECOVERY"),
        ]):
            color = active_state_color(state_key)
            self._score_states[name] = state_key
            lbl = QLabel(display_name)
            lbl.setFixedWidth(72)
            lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            lbl.setStyleSheet(
                f"color: {CX_TEXT_SECONDARY}; background: transparent;"
            )
            scores_grid.addWidget(lbl, i, 0)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedHeight(5)
            bar.setTextVisible(False)
            bar.setStyleSheet(
                f"QProgressBar {{ background: {CX_BG_SECONDARY}; border: none;"
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
                f"color: {CX_TEXT_TERTIARY}; background: transparent;"
            )
            scores_grid.addWidget(val_lbl, i, 2)
            self._score_bars[name] = bar
            self._score_labels[name] = val_lbl
        layout.addLayout(scores_grid)

        meta_row = QHBoxLayout()
        self._confidence_lbl = QLabel("Evidence strength: --")
        self._confidence_lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._confidence_lbl.setStyleSheet(
            f"color: {CX_TEXT_TERTIARY}; background: transparent;"
        )
        self._dwell_lbl = QLabel("Dwell: --")
        self._dwell_lbl.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._dwell_lbl.setStyleSheet(
            f"color: {CX_TEXT_TERTIARY}; background: transparent;"
        )
        meta_row.addWidget(self._confidence_lbl)
        meta_row.addStretch()
        meta_row.addWidget(self._dwell_lbl)
        layout.addLayout(meta_row)

        tl_label = QLabel("Timeline")
        tl_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        tl_label.setStyleSheet(SECTION_HEADING_QSS)
        layout.addWidget(tl_label)
        self._timeline_text = QLabel("No events yet")
        self._timeline_text.setWordWrap(True)
        self._timeline_text.setStyleSheet(
            f"font-family: {FONT_MONO};"
            f"font-size: {FS_CAPTION}px; color: {CX_TEXT_SECONDARY};"
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

    def apply_palette_change(self) -> None:
        """Restyle the support-score bars after the palette changed."""
        for name, bar in self._score_bars.items():
            color = active_state_color(self._score_states.get(name, ""))
            try:
                bar.setStyleSheet(
                    f"QProgressBar {{ background: {CX_BG_SECONDARY}; border: none;"
                    f" border-radius: 2px; }}"
                    f"QProgressBar::chunk {{ background: {color};"
                    f" border-radius: 2px; }}"
                )
            except Exception:
                logger.debug("score bar restyle failed", exc_info=True)

    def update_state(self, payload: dict) -> None:
        # Phase J-3: first frame retires the empty-state panel.
        if not self._has_received_state:
            self._has_received_state = True
            try:
                self._empty_state.setVisible(False)
            except Exception:
                pass

        view = advanced_state_view(payload)
        scores = view.scores
        sig_q = view.signal_quality
        evidence_strength = view.evidence_strength
        coverage = view.evidence_coverage
        dwell = view.dwell_seconds
        state = view.state
        badge_text = view.degraded_message or ""
        self._set_text_if_changed(self._degraded_badge, badge_text)
        self._degraded_badge.setVisible(bool(badge_text))

        self._physio_q.set_value(sig_q.get("physio", 0.0))
        self._kine_q.set_value(sig_q.get("kinematics", 0.0))
        self._tele_q.set_value(sig_q.get("telemetry", 0.0))

        # P0 §3.18: feed the physio bar's per-component breakdown into
        # its tooltip. The fields are optional on STATE_UPDATE — passing
        # ``None`` for a missing key renders "—" instead of fabricating
        # a value. The underlying schema (cortex/libs/schemas/state.py)
        # gates these on physio_sqi presence; the dashboard does not.
        try:
            sqi_detail = sig_q.get("physio_subcomponents") or {}
            self._physio_q.set_subcomponents(
                luminance=sqi_detail.get("luminance"),
                motion_penalty=sqi_detail.get("motion_penalty"),
                face_loss_rate=sqi_detail.get("face_loss_rate"),
            )
        except Exception:
            logger.debug("physio SQI subcomponent update failed", exc_info=True)

        hr = view.heart_rate
        if hr is not None:
            self._hr_plot.add_value(hr)

        for name in ("flow", "hyper", "hypo", "recovery"):
            val = scores.get(name, 0.0)
            if name in self._score_bars:
                # F31: avoid pushing identical values through Qt's
                # progress-bar / label paint chain on every 2 Hz tick.
                self._set_value_if_changed(self._score_bars[name], int(val * 100))
                self._set_text_if_changed(self._score_labels[name], f"{val:.2f}")

        self._set_text_if_changed(
            self._confidence_lbl,
            f"Evidence strength: {evidence_strength:.0%}",
        )
        self._set_text_if_changed(
            self._dwell_lbl,
            f"Coverage: {coverage:.0%} · Dwell: {dwell:.1f}s",
        )

        if not self._timeline_events or self._timeline_events[-1]["state"] != state:
            elapsed = time.monotonic() - self._session_start
            self._timeline_events.append({
                "time": elapsed,
                "state": state,
                "evidence_strength": evidence_strength,
            })
            if len(self._timeline_events) > _MAX_TIMELINE_EVENTS:
                self._timeline_events = self._timeline_events[-_MAX_TIMELINE_EVENTS:]
            lines = []
            for ev in reversed(self._timeline_events[-8:]):
                t = ev["time"]
                m, s = int(t // 60), t % 60
                lines.append(
                    f"{m:02d}:{s:04.1f}  {ev['state']:<10} "
                    f"{ev['evidence_strength']:.0%} evidence"
                )
            self._timeline_text.setText("\n".join(lines) if lines else "No events yet")


# ---------------------------------------------------------------------------
# P0 §3.17 — Concepts dialog (Help → Concepts).
# ---------------------------------------------------------------------------


class ConceptsDialog(QDialog):
    """Small modal dialog listing every term in ``_CONCEPTS_GLOSSARY``.

    Reuses the same glossary used by setToolTip across the dashboard so
    there is exactly one place to edit help copy.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        try:
            self.setWindowTitle("Concepts")
        except Exception:
            pass
        try:
            self.setMinimumWidth(440)
        except Exception:
            pass
        try:
            self.setStyleSheet(f"background: {CX_BG};")
        except Exception:
            pass
        layout = QVBoxLayout(self)
        try:
            layout.setContentsMargins(SP5, SP5, SP5, SP5)
            layout.setSpacing(SP3)
        except Exception:
            pass
        try:
            title = QLabel("Concepts")
            title.setFont(mac_native.system_font(FS_TITLE, "semibold"))
            title.setStyleSheet(f"color: {CX_TEXT}; background: transparent;")
            layout.addWidget(title)
        except Exception:
            pass
        for key, body in _CONCEPTS_GLOSSARY.items():
            try:
                term = QLabel(key.upper())
                term.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
                term.setStyleSheet(
                    f"color: {BRAND_ACCENT_TEXT}; background: transparent;"
                )
                layout.addWidget(term)
                desc = QLabel(body)
                desc.setWordWrap(True)
                desc.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
                desc.setStyleSheet(
                    f"color: {CX_TEXT_SECONDARY}; background: transparent;"
                )
                layout.addWidget(desc)
            except Exception:
                continue
        try:
            close_btn = QPushButton("Close")
            close_btn.setStyleSheet(BTN_GHOST_QSS)
            close_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            _set_accessible_name(close_btn, "Close concepts")
            close_btn.clicked.connect(self.accept)
            layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main Dashboard Window
# ---------------------------------------------------------------------------

class DashboardWindow(QWidget):
    """Two-tab dashboard with native chrome.

    Uses a segmented control + stacked widget instead of QTabWidget — the
    macOS convention for two-segment top-level navigation.
    """

    # E.1 / Phase 4.B (#1): re-emit user-intent signals from the
    # consumer tab. The signal split fixes the DMG stop deadlock:
    #
    # * ``daemon_stop_requested`` — emitted on Stop click; tells the
    #   controller to schedule ``daemon.stop()`` (or send the WS
    #   SHUTDOWN frame). Does NOT quit Qt.
    # * ``gui_quit_requested`` — emitted after recap dismiss / watchdog;
    #   tells the controller to quit the Qt app.
    # * ``stop_requested`` — legacy alias of ``daemon_stop_requested``,
    #   preserved for tests and existing wiring. Triggers daemon stop
    #   only — never quit.
    daemon_stop_requested = Signal()
    gui_quit_requested = Signal()
    stop_requested = Signal()
    goal_set = Signal(str)
    # P0 §3.1 / §3.2: re-emit history-tab user intent so the controller
    # can route them to the daemon via WS (or in-process direct calls).
    history_requested = Signal(object, int)  # since, limit
    detail_requested = Signal(str)  # session_id
    trends_requested = Signal(str, bool)  # window, refresh
    # P0 §3.11: emitted when the user clicks an item in the Pause/Quiet
    # menu (next to the state badge) OR triggers ⌘⇧/. Payload is the
    # kind ("snooze_15"/"quiet_session"/"pause"/"off") and an optional
    # duration override in minutes (0 = use daemon default).
    quiet_mode_requested = Signal(str, int)
    # P0 §3.10: emitted when the user clicks the "Turn off" link on
    # the daemon-armed focus protection toast. Cleared in the daemon
    # via ``disarm_auto_focus``.
    auto_focus_disarm_requested = Signal()
    # P0 §3.3 (Wave-2 P1): emitted when the user dismisses the recap
    # card OR the in-process recap watchdog fires. The controller
    # forwards this to ``daemon.acknowledge_session_recap()`` which
    # releases the daemon's stop() wait — without this, the daemon's
    # 5 s recap-dismiss timeout fires unnecessarily on every stop.
    recap_dismissed_ack = Signal(str)  # session_id (may be empty)
    # P0 §3.7 desktop dispatch: re-emitted from consumer tab's break pill.
    break_pill_clicked = Signal(dict)
    # P0 §3.16: re-emitted from consumer tab's undo toast / restore pill.
    undo_action_requested = Signal(str)
    # P0 §3.21 global shortcuts re-emit.
    force_recap_requested = Signal()
    dismiss_overlay_requested = Signal()
    open_settings_requested = Signal()
    # Session lifecycle re-emits (see ``_ConsumerTab``).
    session_start_requested = Signal()
    recalibrate_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._connected = False
        self._last_recap_payload: dict = {}
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
        # P0 §3.1: add a third "History" segment between Dashboard and
        # Advanced so the user can browse past sessions and trends.
        # Audit fix: if the History tab module fails to import (test stubs
        # or partial Qt), we hide its segment entirely rather than
        # surfacing a broken tab that lands on Advanced.
        seg_container = QHBoxLayout()
        seg_container.setContentsMargins(SP6, SP3, SP6, SP3)
        # Probe the History tab availability before building the segments.
        # The actual instance is constructed below; we only need to know
        # whether the import resolves.
        try:
            from cortex.apps.desktop_shell.history_tab import (
                HistoryTab as _HistoryTabProbe,  # noqa: F401
            )
            _history_segment_available = True
        except Exception:
            _history_segment_available = False
        if _history_segment_available:
            self._seg = SegmentedControl(["Dashboard", "History", "Advanced"])
        else:
            self._seg = SegmentedControl(["Dashboard", "Advanced"])
        seg_container.addWidget(self._seg, stretch=1)
        layout.addLayout(seg_container)

        # Phase J-2: the status toast FLOATS over the content (a plain
        # child widget positioned by ``_place_toast``), so surfacing an
        # error never reflows the page. Hidden until ``show_error`` /
        # ``show_info_toast``. Construction is guarded so the legacy mock
        # harness that swaps out PySide6 keeps the dashboard importable.
        try:
            from cortex.apps.desktop_shell.components import Toast
            self._toast: Toast | None = Toast(self)
        except Exception:  # pragma: no cover - mock harness without Toast
            logger.debug("Toast widget unavailable; skipping", exc_info=True)
            self._toast = None

        self._stack = QStackedWidget()
        self._consumer = _ConsumerTab()
        # P0 §3.1 + §3.2: lazy-import the History tab so a degraded test
        # harness that swaps out PySide6 keeps the dashboard importable.
        try:
            from cortex.apps.desktop_shell.history_tab import HistoryTab
            self._history_tab: Any = HistoryTab()
        except Exception:  # pragma: no cover - test stubs / partial Qt
            logger.debug("HistoryTab unavailable; skipping", exc_info=True)
            self._history_tab = None
        self._advanced = _AdvancedTab()
        self._timeline_events = self._advanced._timeline_events
        self._stack.addWidget(self._consumer)
        if self._history_tab is not None:
            self._stack.addWidget(self._history_tab)
        self._stack.addWidget(self._advanced)
        layout.addWidget(self._stack, stretch=1)

        self._seg.selection_changed.connect(self._stack.setCurrentIndex)

        # E.1 / Phase 4.B (#1): forward both halves of the consumer tab's
        # stop flow so the controller can wire them independently.
        # ``daemon_stop_requested`` (and its legacy alias
        # ``stop_requested``) tells the controller to ask the daemon to
        # stop; ``gui_quit_requested`` tells it to quit Qt.
        self._consumer.daemon_stop_requested.connect(self.daemon_stop_requested.emit)
        self._consumer.gui_quit_requested.connect(self.gui_quit_requested.emit)
        self._consumer.stop_requested.connect(self.stop_requested.emit)
        self._consumer.goal_set.connect(self.goal_set.emit)
        # P0 §3.11 / §3.10: bubble pause/quiet menu + auto-focus disarm
        # picks to the controller so the daemon's set_quiet_mode /
        # disarm_auto_focus are invoked.
        self._consumer.quiet_mode_requested.connect(
            self.quiet_mode_requested.emit,
        )
        self._consumer.auto_focus_disarm_requested.connect(
            self.auto_focus_disarm_requested.emit,
        )
        # P0 §3.7 desktop dispatch + §3.16 undo.
        if hasattr(self._consumer, "break_pill_clicked"):
            self._consumer.break_pill_clicked.connect(
                self.break_pill_clicked.emit,
            )
        if hasattr(self._consumer, "undo_action_requested"):
            self._consumer.undo_action_requested.connect(
                self.undo_action_requested.emit,
            )
        # P0 §3.21 global shortcut re-emit.
        if hasattr(self._consumer, "force_recap_requested"):
            self._consumer.force_recap_requested.connect(
                self.force_recap_requested.emit,
            )
        if hasattr(self._consumer, "dismiss_overlay_requested"):
            self._consumer.dismiss_overlay_requested.connect(
                self.dismiss_overlay_requested.emit,
            )
        if hasattr(self._consumer, "open_settings_requested"):
            self._consumer.open_settings_requested.connect(
                self.open_settings_requested.emit,
            )
        if hasattr(self._consumer, "session_start_requested"):
            self._consumer.session_start_requested.connect(
                self.session_start_requested.emit,
            )
        if hasattr(self._consumer, "recalibrate_requested"):
            self._consumer.recalibrate_requested.connect(
                self.recalibrate_requested.emit,
            )

        # P0 §3.1 + §3.2: forward history-tab outgoing signals so the
        # controller can route them to the daemon via WS or direct call.
        if self._history_tab is not None:
            self._history_tab.history_requested.connect(self.history_requested.emit)
            self._history_tab.detail_requested.connect(self.detail_requested.emit)
            self._history_tab.trends_requested.connect(self.trends_requested.emit)

        # P0 §3.3: recap sheet — lazy-created on first SESSION_RECAP.
        self._recap_sheet: Any = None

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
            mac_native.apply_vibrancy(self)
        except Exception:
            logger.debug("native chrome application failed", exc_info=True)
        self._place_toast()

    def resizeEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().resizeEvent(event)
        self._place_toast()

    def _place_toast(self) -> None:
        """Float the toast just below the segmented control."""
        toast = getattr(self, "_toast", None)
        if toast is None:
            return
        try:
            top = self._seg.geometry().bottom() + SP2
            toast.set_top_offset(max(SP3, top))
            toast.reposition()
        except Exception:
            logger.debug("toast placement failed", exc_info=True)

    # -- Public update methods (signature-stable) ------------------------

    def update_state(self, payload: dict) -> None:
        self._consumer.update_state(payload)
        self._advanced.update_state(payload)

    def refresh_baseline_freshness(self) -> None:
        """P0 §3.4 — proxy down to the consumer tab's pill. Called by
        the controller after a successful calibration run."""
        if self._consumer is not None and hasattr(
            self._consumer, "refresh_baseline_freshness"
        ):
            try:
                self._consumer.refresh_baseline_freshness()
            except Exception:
                logger.debug("baseline freshness refresh failed", exc_info=True)

    def set_connected(self, connected: bool) -> None:
        self._connected = connected
        self._consumer.set_connected(connected)

    def set_starting(self) -> None:
        self._connected = False
        self._consumer.set_starting()

    def set_session_restart_available(self, available: bool) -> None:
        """Forward the host's restart capability to the session control."""
        if self._consumer is not None and hasattr(
            self._consumer, "set_session_restart_available"
        ):
            self._consumer.set_session_restart_available(available)

    def apply_palette_change(self) -> None:
        """Re-render state colours on every tab after a palette swap."""
        for tab in (self._consumer, self._advanced):
            if tab is not None and hasattr(tab, "apply_palette_change"):
                try:
                    tab.apply_palette_change()
                except Exception:
                    logger.debug("palette re-render failed", exc_info=True)

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

    # ------------------------------------------------------------------
    # P0 §3.1 / §3.2 / §3.3 — public apply slots for incoming WS frames.
    # ------------------------------------------------------------------

    def apply_session_list(self, payload: dict) -> None:
        """Route a ``SESSION_LIST`` payload to the History tab."""
        if self._history_tab is not None:
            try:
                self._history_tab.apply_session_list(payload)
            except Exception:
                logger.debug("apply_session_list failed", exc_info=True)

    def apply_session_detail(self, payload: dict) -> None:
        """Route a ``SESSION_DETAIL`` payload to the History tab."""
        if self._history_tab is not None:
            try:
                self._history_tab.apply_session_detail(payload)
            except Exception:
                logger.debug("apply_session_detail failed", exc_info=True)

    def apply_trends(self, payload: dict) -> None:
        """Route a ``TRENDS_PAYLOAD`` payload to the History tab."""
        if self._history_tab is not None:
            try:
                self._history_tab.apply_trends(payload)
            except Exception:
                logger.debug("apply_trends failed", exc_info=True)

    def apply_session_recap(self, payload: dict) -> None:
        """P0 §3.3 — surface the slide-up recap sheet on SESSION_RECAP.

        Lazy-constructs the sheet on first call, then asks it to render
        the payload. Connects its lifecycle signals to the consumer
        tab's two-phase stop flow so the daemon shutdown only completes
        after the user dismisses (or the autohide fires).

        Phase 4.B fix (#16): an empty payload ``{}`` is the synthetic
        short-session signal from the daemon (Phase 4.A #34). Treat it
        as an instant finalise — do NOT open the recap sheet for
        sessions that produced no report.

        Phase 4.B fix (#23): only open the sheet when the consumer tab
        is mid-stop. A SESSION_RECAP arriving outside the stop flow
        (e.g. from a Chrome popup's REQUEST_SESSION_RECAP) must not
        surprise the desktop user with an unexpected slide-up sheet.

        Phase 4.B fix (#24): cancel the consumer tab's recap watchdog
        on recap arrival so the daemon's 5 s broadcast and the UI's
        6 s watchdog don't race.
        """
        if not isinstance(payload, dict):
            logger.debug(
                "apply_session_recap: payload was %s; ignoring", type(payload)
            )
            return
        is_stopping = bool(
            self._consumer is not None and getattr(self._consumer, "_stopping", False)
        )
        # Empty payload = short-session synthetic recap. Finalise the
        # stop flow directly without opening the sheet. Outside the
        # stop flow this is just a no-op (nothing to surface).
        if not payload.get("session_id"):
            if is_stopping and self._consumer is not None:
                try:
                    self._consumer._finalize_stop()
                except Exception:
                    logger.debug(
                        "short-session finalize_stop failed", exc_info=True
                    )
            return
        # Late SESSION_RECAP outside the stop flow (e.g. popup-driven
        # REQUEST_SESSION_RECAP): drop on the floor for the desktop
        # shell; the dashboard's History tab is the canonical surface
        # for past sessions.
        if not is_stopping:
            logger.debug(
                "apply_session_recap: not stopping; ignoring late recap "
                "for session_id=%s",
                payload.get("session_id"),
            )
            return
        # Cancel the 6 s recap watchdog now that we know the recap is
        # about to render. Prevents a race where the watchdog fires
        # right as the sheet starts animating in.
        if self._consumer is not None:
            watchdog = getattr(self._consumer, "_recap_watchdog", None)
            if watchdog is not None:
                try:
                    watchdog.stop()
                except Exception:
                    logger.debug(
                        "recap watchdog stop on recap arrival failed",
                        exc_info=True,
                    )
        self._last_recap_payload = dict(payload)
        if self._recap_sheet is None:
            try:
                from cortex.apps.desktop_shell.recap_sheet import RecapSheet
                self._recap_sheet = RecapSheet(self)
                self._recap_sheet.dismissed.connect(self._on_recap_dismissed)
                self._recap_sheet.view_full_report.connect(
                    self._on_recap_view_full,
                )
                quit_signal = getattr(self._recap_sheet, "quit_requested", None)
                if quit_signal is not None:
                    quit_signal.connect(self._on_recap_quit)
            except Exception:
                logger.debug("Failed to construct RecapSheet", exc_info=True)
                self._recap_sheet = None
                # Without the sheet, we can't honour the recap contract;
                # just finalise the stop so the daemon proceeds.
                if self._consumer is not None:
                    try:
                        self._consumer._finalize_stop()
                    except Exception:
                        logger.debug("fallback finalize_stop failed", exc_info=True)
                return
        quit_pending = bool(getattr(self._consumer, "_quit_after_stop", False))
        try:
            self._recap_sheet.show_report(payload, quit_pending=quit_pending)
        except TypeError:
            # Older / stub sheets without the keyword.
            try:
                self._recap_sheet.show_report(payload)
            except Exception:
                logger.debug("show_report failed", exc_info=True)
        except Exception:
            logger.debug("show_report failed", exc_info=True)
            if self._consumer is not None:
                try:
                    self._consumer._finalize_stop()
                except Exception:
                    logger.debug("fallback finalize_stop failed", exc_info=True)
        # Phase 4.B fix (#17): a successful recap means a new session
        # row is now on disk. Force the History tab to drop its
        # auto-request memo so the next visit re-fetches and the user
        # sees the just-finished session at the top.
        if self._history_tab is not None and hasattr(
            self._history_tab, "force_refresh"
        ):
            try:
                self._history_tab.force_refresh()
            except Exception:
                logger.debug(
                    "history force_refresh on recap arrival failed",
                    exc_info=True,
                )

    # ── P0 §3.11 / §3.10: quiet-mode + auto-focus surfaces ──────────

    def apply_quiet_mode_state(self, payload: dict) -> None:
        """P0 §3.11: forward QUIET_MODE_STATE payload to the consumer
        tab's capsule + the dashboard-level surfaces. The consumer
        owns the actual UI; the dashboard delegates."""
        if self._consumer is not None and hasattr(
            self._consumer, "apply_quiet_mode_state",
        ):
            try:
                self._consumer.apply_quiet_mode_state(payload)
            except Exception:
                logger.debug(
                    "consumer apply_quiet_mode_state failed", exc_info=True,
                )

    def apply_auto_focus_state(self, armed: bool, preset: str = "") -> None:
        """P0 §3.10: forward to the consumer tab's focus-protection pill."""
        if self._consumer is not None and hasattr(
            self._consumer, "apply_auto_focus_state",
        ):
            try:
                self._consumer.apply_auto_focus_state(armed, preset)
            except Exception:
                logger.debug(
                    "consumer apply_auto_focus_state failed", exc_info=True,
                )

    def apply_break_recommendation(self, payload: dict) -> None:
        """P0 §3.7 desktop dispatch: forward to consumer tab's break pill."""
        if self._consumer is not None and hasattr(
            self._consumer, "apply_break_recommendation",
        ):
            try:
                self._consumer.apply_break_recommendation(payload)
            except Exception:
                logger.debug(
                    "consumer apply_break_recommendation failed",
                    exc_info=True,
                )

    def apply_intervention_applied(self, payload: dict) -> None:
        """Forward a verified compatibility projection to the consumer tab."""
        if self._consumer is not None and hasattr(
            self._consumer, "apply_intervention_applied",
        ):
            try:
                self._consumer.apply_intervention_applied(payload)
            except Exception:
                logger.debug(
                    "consumer apply_intervention_applied failed",
                    exc_info=True,
                )

    def apply_intervention_transaction_state(self, payload: dict) -> None:
        """Forward exact transaction outcomes to the Undo/Restore surface."""

        if self._consumer is not None and hasattr(
            self._consumer, "apply_intervention_transaction_state",
        ):
            try:
                self._consumer.apply_intervention_transaction_state(payload)
            except Exception:
                logger.debug(
                    "consumer transaction-state projection failed",
                    exc_info=True,
                )

    def apply_cost_update(self, cost_today: float, budget: float = 0.0) -> None:
        """P0 §3.15: forward LLM cost data to consumer tab's pill."""
        if self._consumer is not None and hasattr(
            self._consumer, "apply_cost_update",
        ):
            try:
                self._consumer.apply_cost_update(cost_today, budget)
            except Exception:
                logger.debug(
                    "consumer apply_cost_update failed", exc_info=True,
                )

    def show_concepts_dialog(self) -> None:
        """P0 §3.17: open the Concepts glossary dialog. Wired into the
        Help menu (or hosted by the controller through ``main_app``).
        """
        try:
            dialog = ConceptsDialog(self)
            dialog.exec()
        except Exception:
            logger.debug("Concepts dialog failed to open", exc_info=True)

    def _on_recap_dismissed(self) -> None:
        """RecapSheet was closed (manual / autohide) — proceed with the
        actual daemon shutdown emit. The consumer tab's ``_finalize_stop``
        is idempotent so calling it from both this path and the
        watchdog is safe.

        Wave-2 P1 (P0 §3.3): also bubble a ``recap_dismissed_ack``
        signal so the controller can call
        ``daemon.acknowledge_session_recap()`` — that releases the
        daemon's ``stop()`` wait early instead of letting the 5 s
        dismissal-ACK timeout fire on every shutdown.
        """
        # Echo the session id from the currently-displayed recap sheet
        # if available; the daemon's ack flips its event
        # unconditionally so a missing id is harmless.
        session_id: str = ""
        try:
            if self._recap_sheet is not None and hasattr(
                self._recap_sheet, "current_session_id"
            ):
                session_id = str(self._recap_sheet.current_session_id() or "")
        except Exception:
            logger.debug("recap_sheet.current_session_id raised", exc_info=True)
        try:
            self.recap_dismissed_ack.emit(session_id)
        except Exception:
            logger.debug("recap_dismissed_ack.emit raised", exc_info=True)
        if self._consumer is not None:
            try:
                self._consumer._finalize_stop()
            except Exception:
                logger.debug("finalize_stop on dismiss failed", exc_info=True)

    def _on_recap_view_full(self, session_id: str) -> None:
        """User chose ``View full report``: keep Cortex open, switch to
        the History tab, and show the report from the recap payload
        already in hand — the daemon has stopped, so nothing is requested
        from it. Viewing cancels any pending quit.
        """
        if self._consumer is not None:
            try:
                self._consumer._finalize_stop(quit=False)
            except Exception:
                logger.debug("finalize_stop on view_full failed", exc_info=True)
        history_index = 1 if self._history_tab is not None else -1
        if history_index < 0:
            return
        if hasattr(self._seg, "set_selected"):
            try:
                self._seg.set_selected(history_index)
            except Exception:
                logger.debug("SegmentedControl.set_selected failed", exc_info=True)
        else:
            try:
                self._stack.setCurrentIndex(history_index)
            except Exception:
                logger.debug("switch to History tab failed", exc_info=True)
        report = self._last_recap_payload
        if str(report.get("session_id") or "") != str(session_id or ""):
            report = {}
        try:
            self._history_tab.open_detail(session_id, report=report or None)
        except TypeError:
            try:
                self._history_tab.open_detail(session_id)
            except Exception:
                logger.debug("HistoryTab.open_detail failed", exc_info=True)
        except Exception:
            logger.debug("HistoryTab.open_detail failed", exc_info=True)

    def _on_recap_quit(self) -> None:
        """User chose ``Quit Cortex`` on the recap sheet."""
        if self._consumer is not None:
            try:
                self._consumer._finalize_stop(quit=True)
            except Exception:
                logger.debug("finalize_stop on recap quit failed", exc_info=True)

    # Phase J-2 ----------------------------------------------------------

    def show_error(self, title: str, body: str, cid: str = "") -> None:
        """Surface a daemon error in the floating toast.

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
        self._place_toast()
        self._toast.show_error(title, body, cid)

    def show_info_toast(self, title: str, body: str = "") -> None:
        """B2 (audit-prod): surface a positive / status message in the
        top-bar toast (e.g. "Cortex is now using your LLM"). Reuses the
        Phase J-2 toast widget with empty cid slot."""
        if self._toast is None or not hasattr(self._toast, "show_info"):
            logger.info("Toast unavailable; info toast skipped: %s", title)
            return
        try:
            self._place_toast()
            self._toast.show_info(title, body)
        except Exception:
            logger.debug("show_info_toast failed", exc_info=True)
