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
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer, Signal

if TYPE_CHECKING:
    from pathlib import Path

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
from cortex.apps.desktop_shell.tokens import (
    BIO_BLINK,
    BIO_HR,
    BIO_HRV,
    BRAND_ACCENT,
    BRAND_ACCENT_DARK,
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
        "Cognitive state: Cortex infers FLOW (deep focus), HYPER (overwhelmed), "
        "HYPO (idle), or RECOVERY (winding back to focus) from biometrics + "
        "activity. The pill turns warmer when sustained HYPER is detected."
    ),
    "flow": "FLOW: sustained deep focus — your nervous system is regulated and engaged.",
    "hyper": "HYPER: elevated arousal — heart rate up, attention scattered. Cortex offers a calming intervention.",
    "hypo": "HYPO: low arousal — long pauses, low engagement. Cortex offers a re-engagement nudge.",
    "recovery": "RECOVERY: post-HYPER cool-down — Cortex eases interventions while your nervous system settles.",
    "hr": "BPM (Heart rate): beats per minute, inferred from your face using rPPG. Not medical-grade.",
    "hrv": (
        "HRV (Heart Rate Variability): variation between heartbeats. Higher HRV "
        "correlates with a calmer, more adaptive nervous system."
    ),
    "perclos": (
        "PERCLOS: percentage of eyelid closure, averaged over a minute. "
        "A proxy for drowsiness / cognitive fatigue."
    ),
    "blink": "Blink rate: blinks per minute. Elevated rates can flag screen fatigue.",
    "sqi": (
        "SQI (Signal Quality Index): how confident Cortex is in its biometric "
        "readout. Drops when lighting, motion, or face position degrade the signal."
    ),
    "stress_integral": (
        "Stress integral: a running area-under-curve of elevated arousal. When it "
        "crosses a threshold, Cortex suggests a paced break."
    ),
    "calibration": (
        "Calibration: a 2-minute capture that locks in your resting baselines so "
        "Cortex can detect *your* shift, not the population average."
    ),
}


# ---------------------------------------------------------------------------
# P0 §3.4 — Baseline freshness helpers (shared with the Settings dialog).
# ---------------------------------------------------------------------------


def _baseline_default_path() -> Path:
    """Resolve `storage/baselines/default.json` via the active config.
    Lazy-imported so the dashboard stays importable under test stubs."""
    from pathlib import Path

    try:
        from cortex.libs.config.settings import get_config

        return Path(get_config().storage.path) / "baselines" / "default.json"
    except Exception:
        return Path("storage") / "baselines" / "default.json"


def _baseline_age_days(now: float | None = None) -> float | None:
    """Age of the default baseline file in days, or None if missing."""
    target = _baseline_default_path()
    try:
        if not target.exists():
            return None
        current = now if now is not None else time.time()
        return max(0.0, (current - target.stat().st_mtime) / 86400.0)
    except OSError:
        return None


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

# Resolved semantic colors. These hex strings are dev-mode fallbacks; on
# macOS, the window chrome (background tint + titlebar) re-adapts to the
# user's light/dark setting at runtime via the appearance observer wired
# at app startup (see :func:`mac_native.install_appearance_observer`,
# invoked from ``CortexApp.run`` / ``CortexAppController``). These Qt
# stylesheet token values stay fixed; the native NSWindow background
# colour follows the system appearance.
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
/* Popup menus must paint an OPAQUE surface. The main window uses
   vibrancy (translucent) and this stylesheet cascades to child
   ``QMenu`` popups; without an explicit background they render
   see-through on macOS and the items bleed onto the widgets behind
   the popup (observed: recent-goals menu over the Biometrics card).
   Defining it globally hardens every present and future menu. */
QMenu {{
    background-color: {_CONTROL_BG};
    color: {_LABEL};
    border: 1px solid {_SEPARATOR};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 12px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background-color: {BRAND_ACCENT};
    color: #FFFFFF;
}}
QMenu::separator {{
    height: 1px;
    background-color: {_SEPARATOR};
    margin: 4px 8px;
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

    def set_selected(self, index: int) -> None:
        """Programmatically activate a segment and emit
        ``selection_changed`` so subscribers (e.g. the QStackedWidget)
        re-sync.

        Phase 4.B fix (#22): provides a public API for the dashboard's
        ``_on_recap_view_full`` so it no longer has to reach into the
        private ``_buttons`` list. Out-of-range indices are clamped
        defensively; a negative or oversized index becomes a no-op
        rather than raising. The emitted ``selection_changed`` signal
        is the same one the user click path emits — listeners cannot
        tell the difference.
        """
        if not self._buttons:
            return
        if index < 0 or index >= len(self._buttons):
            logger.debug(
                "_MacSegmentedControl.set_selected: index %d out of range",
                index,
            )
            return
        self._on_clicked(index)


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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: transparent; color: {_LABEL};")
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

        # F34: state machine for the Stop button. ``_stopping`` flips to True
        # on first click and back to False on ``notify_daemon_stopped`` (or
        # the safety-timer expiry). Coalesces double-clicks at the slot level.
        self._stopping: bool = False
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
        # P0 §3.17: glossary tooltips on every quantitative chrome
        # element. Help text taken verbatim from _CONCEPTS_GLOSSARY
        # below so a single edit point keeps the in-app tooltip + the
        # Concepts dialog in lockstep.
        try:
            self._state_label.setToolTip(_CONCEPTS_GLOSSARY["state"])
        except Exception:
            pass
        badge_layout.addWidget(self._state_label, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._state_badge.setStyleSheet(
            f"background: {_GROUPED_BG}; border-radius: {RADIUS_PILL}px;"
        )
        header.addWidget(self._state_badge, alignment=Qt.AlignmentFlag.AlignVCenter)

        # P0 §3.11: Pause/Quiet capsule — one-click access to the three
        # quiet modes (Snooze 15, Quiet for session, Pause) plus an
        # Off entry to clear any active mode. Lives next to the state
        # badge so the user can disarm in a single gesture. The
        # capsule's label mirrors the active mode (e.g. "Quiet · 28m")
        # so the user always sees what's on.
        self._quiet_capsule = QPushButton("Pause")
        try:
            self._quiet_capsule.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._quiet_capsule.setFont(
            mac_native.system_font(FS_FOOTNOTE - 1, "medium")
        )
        try:
            self._quiet_capsule.setFlat(True)
        except Exception:
            pass
        self._quiet_capsule.setStyleSheet(
            "QPushButton {"
            f"  background: {_GROUPED_BG};"
            f"  color: {_LABEL_SECONDARY};"
            f"  border-radius: {RADIUS_PILL}px;"
            "  padding: 3px 12px;"
            "  margin-left: 8px;"
            "  border: none;"
            "}"
            f"QPushButton:hover {{ background: rgba(0,0,0,0.05); color: {_LABEL}; }}"
        )
        try:
            _set_accessible_name(
                self._quiet_capsule, "Pause or quiet Cortex",
            )
            _set_accessible_description(
                self._quiet_capsule,
                "Opens a menu to snooze, quiet, or pause Cortex. "
                "Shortcut: Command + Shift + Slash.",
            )
        except Exception:
            pass
        self._quiet_capsule.clicked.connect(self._on_quiet_capsule_clicked)
        header.addWidget(
            self._quiet_capsule, alignment=Qt.AlignmentFlag.AlignVCenter,
        )

        # P0 §3.11: ⌘⇧/ keyboard shortcut. The shortcut opens the same
        # menu the capsule does so muscle-memory power users can
        # disarm without leaving their keyboard.
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
        # All three are application-scoped so the user can fire them
        # while focused in another app (popup, settings dialog, etc.).
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

        # P0 §3.10: auto-armed focus protection toast. Hidden by
        # default; revealed via :meth:`apply_quiet_mode_state` when
        # the daemon emits START_FOCUS_AUTO (or QUIET_MODE_STATE with
        # an auto-armed kind). Click → emits
        # ``auto_focus_disarm_requested``.
        self._focus_protection_pill = QPushButton("")
        try:
            self._focus_protection_pill.setCursor(
                Qt.CursorShape.PointingHandCursor,
            )
        except Exception:
            pass
        self._focus_protection_pill.setFont(
            mac_native.system_font(FS_CAPTION, "medium")
        )
        self._focus_protection_pill.setStyleSheet(
            "QPushButton {"
            f"  background: {BRAND_ACCENT}1A;"
            f"  color: {BRAND_ACCENT};"
            f"  border-radius: {RADIUS_PILL}px;"
            "  padding: 3px 10px;"
            "  margin-left: 8px;"
            "  border: none;"
            "}"
            "QPushButton:hover { background: rgba(217,119,87,0.20); }"
        )
        self._focus_protection_pill.setVisible(False)
        self._focus_protection_pill.clicked.connect(
            self.auto_focus_disarm_requested.emit,
        )
        # UI redesign: the ambient chips (focus-protection, cost, break,
        # baseline) move OUT of the top bar into a footer meta strip (built
        # near the Stop button below) so the header reads as brand + state +
        # quiet only. The widgets keep their attribute names so every render
        # slot (apply_auto_focus_state / cost update / apply_break_recommendation
        # / refresh_baseline_freshness) updates them unchanged.

        # P0 §3.15: LLM cost meter pill. Subdued unless near budget. The
        # daemon publishes the running daily total via a COST_RESPONSE
        # WS message (Phase 4b owns the plumbing); the desktop polls
        # COST_REQUEST every ~60 s and re-renders. Until Phase 4b lands
        # the message handler, the pill falls back to "$—".
        self._cost_pill = QLabel("$—")
        self._cost_pill.setFont(
            mac_native.system_font(FS_CAPTION, "medium"),
        )
        self._cost_pill.setObjectName("CortexCostPill")
        self._cost_pill.setStyleSheet(
            "QLabel#CortexCostPill {"
            f"  color: {_LABEL_TERTIARY};"
            f"  background: {_GROUPED_BG};"
            f"  border-radius: {RADIUS_PILL}px;"
            "  padding: 3px 10px;"
            "  margin-left: 8px;"
            "}"
        )
        try:
            self._cost_pill.setToolTip(
                "LLM spend today — click Settings → Budget to set a daily cap."
            )
        except Exception:
            pass
        # (added to the footer meta strip below, not the header)
        # Cache the last applied cost so we don't restyle on every poll.
        self._cost_last_value: float = -1.0
        self._cost_budget_warned: bool = False

        # P0 §3.7 desktop dispatch: "Take a break?" soft pill. Hidden by
        # default; surfaced via :meth:`apply_break_recommendation` when
        # the daemon emits BREAK_RECOMMENDATION (stress integral
        # threshold reached). Click → emits ``break_pill_clicked`` which
        # the controller routes to the BiologyBreakOverlay; right-click
        # snoozes the pill for 5 minutes. The pill auto-clears after a
        # break completes or 10 minutes of no engagement.
        self._break_pill = QPushButton("Take a break?")
        try:
            self._break_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._break_pill.setFont(
            mac_native.system_font(FS_CAPTION, "medium"),
        )
        self._break_pill.setStyleSheet(
            "QPushButton {"
            f"  background: rgba(217, 119, 87, 0.18);"
            f"  color: {BRAND_ACCENT};"
            f"  border-radius: {RADIUS_PILL}px;"
            "  padding: 3px 10px;"
            "  margin-left: 8px;"
            "  border: none;"
            "}"
            "QPushButton:hover { background: rgba(217,119,87,0.30); }"
        )
        self._break_pill.setVisible(False)
        try:
            self._break_pill.clicked.connect(self._on_break_pill_clicked)
        except Exception:
            logger.debug("break pill connect failed", exc_info=True)
        # (added to the footer meta strip below, not the header)

        # P0 §3.4 — baseline freshness pill. Hidden when the baseline
        # file doesn't exist (so we don't shame the user mid-onboarding)
        # and quiet when fresh. >30 days old it surfaces a subtle
        # "Recalibrate?" link.
        self._baseline_pill = QLabel("")
        self._baseline_pill.setFont(
            mac_native.system_font(FS_CAPTION, "medium")
        )
        self._baseline_pill.setVisible(False)
        self._baseline_pill.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: {_GROUPED_BG};"
            f" border-radius: {RADIUS_PILL}px; padding: 3px 10px;"
        )
        # (added to the footer meta strip below, not the header)

        # UI redesign: footer meta strip — the four ambient chips
        # (focus-protection, break, baseline, cost) right-aligned in a
        # single subtle row. Each is hidden until its render slot reveals
        # it; cost shows "$—" quietly. This declutters the top bar while
        # keeping every signal reachable. Widget objects + attribute names
        # are unchanged, so all render slots keep working.
        self._meta_strip = QHBoxLayout()
        self._meta_strip.setContentsMargins(0, 0, 0, SP3)
        self._meta_strip.setSpacing(SP2)
        self._meta_strip.addStretch()
        self._meta_strip.addWidget(
            self._focus_protection_pill, alignment=Qt.AlignmentFlag.AlignVCenter
        )
        self._meta_strip.addWidget(
            self._break_pill, alignment=Qt.AlignmentFlag.AlignVCenter
        )
        self._meta_strip.addWidget(
            self._baseline_pill, alignment=Qt.AlignmentFlag.AlignVCenter
        )
        self._meta_strip.addWidget(
            self._cost_pill, alignment=Qt.AlignmentFlag.AlignVCenter
        )

        root.addLayout(header)
        # Run an initial freshness check so the pill is correct on first
        # paint. The controller / main app also calls refresh on a
        # completed calibration.
        try:
            self.refresh_baseline_freshness()
        except Exception:
            logger.debug("initial baseline freshness check failed", exc_info=True)

        # F16 (Phase-4 audit): envelope-level health warning strip,
        # mirrored from the daemon's ``payload["capture"]["stale"]`` and
        # ``payload["store"]["degraded"]`` flags. Hidden by default;
        # ``update_state`` flips visibility on receipt of a STATE_UPDATE
        # carrying either flag. Uses the existing danger token (no new
        # palette).
        self._health_banner = QLabel("")
        self._health_banner.setObjectName("CortexHealthBanner")
        self._health_banner.setWordWrap(True)
        self._health_banner.setFont(
            mac_native.system_font(FS_CAPTION, "regular")
        )
        self._health_banner.setStyleSheet(
            "QLabel#CortexHealthBanner {"
            f"  color: {_DANGER};"
            f"  background: rgba(215, 0, 21, 0.10);"
            f"  border: 1px solid {_DANGER};"
            f"  border-radius: 6px;"
            f"  padding: 6px 10px;"
            "}"
        )
        self._health_banner.setVisible(False)
        _set_accessible_name(self._health_banner, "Health warning")
        root.addWidget(self._health_banner)

        # ── Goal input — minimum width, flexible (HIG: avoid fixed sizes) ──
        self._goal_input = QLineEdit()
        self._goal_input.setPlaceholderText("What are you working on?")
        self._goal_input.setMinimumHeight(36)
        # Mirror the browser-extension popup (popup.tsx) and the backend
        # ``GoalSet`` schema upper bound so the desktop input can never
        # accumulate more characters than the daemon will accept.
        self._goal_input.setMaxLength(500)
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
        )
        # F19 (Phase-4 audit/UI 4.6): drive placeholder color via
        # QPalette.PlaceholderText instead of the
        # ``QLineEdit::placeholder`` QSS selector. The QSS form silently
        # no-ops on some Qt 6.x builds (the parser accepts
        # ``::placeholder`` but never wires it to the actual paint
        # path), while the palette role is the documented Qt API and
        # works on every backend.
        try:
            from PySide6.QtGui import QColor, QPalette

            placeholder_palette = self._goal_input.palette()
            placeholder_palette.setColor(
                QPalette.ColorRole.PlaceholderText,
                QColor(_LABEL_TERTIARY),
            )
            self._goal_input.setPalette(placeholder_palette)
        except Exception:
            # Headless test envs may not have a real platform plugin —
            # the placeholder colour is non-functional in that case,
            # which is harmless for unit tests.
            pass
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
            text = self._goal_input.text().strip()
            self.goal_set.emit(text)
            # P0 §3.13: persist the goal to the on-disk recent-goals
            # store so the dropdown picks it up on next open. Failures
            # are non-fatal — we never want a write error to swallow
            # the daemon-bound goal_set emission.
            if text:
                try:
                    from cortex.libs.store.goal_store import add_goal
                    add_goal(text)
                    self._refresh_recent_goals_dropdown()
                except Exception:
                    logger.debug("goal_store add_goal failed", exc_info=True)

        self._goal_input.returnPressed.connect(_schedule_goal_emit)
        # Expose the scheduler for tests so they can drive the coalescer
        # deterministically (the QTimer.singleShot path needs an event
        # loop tick which the offscreen test harness provides via
        # ``QApplication.processEvents``).
        self._schedule_goal_emit = _schedule_goal_emit
        self._fire_goal_emit = _fire_goal_emit

        # P0 §3.13 / UI redesign: recent goals live behind a trailing
        # pull-down glyph INSIDE the goal field — the macOS "field with a
        # built-in menu" pattern (Safari address bar / NSComboButton),
        # replacing the stock QComboBox that read as dated. One clean
        # control; the menu renders with native vibrancy. The affordance
        # is hidden until the on-disk store has at least one goal so a
        # first-run user still sees a blank field.
        self._goal_history_action = None
        try:
            from PySide6.QtGui import QAction  # noqa: F401  (presence check)

            icon = _make_history_icon(_LABEL_SECONDARY)
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
        # Populate from disk on first paint (silently).
        try:
            self._refresh_recent_goals_dropdown()
        except Exception:
            logger.debug("initial recent goals refresh failed", exc_info=True)
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
        bio_row.setContentsMargins(0, 0, 0, 0)

        self._bpm_label = QLabel("--")
        self._hrv_label = QLabel("--")
        self._blk_label = QLabel("--")

        # P0 §3.17: tooltip key per biometric channel so the dialog +
        # the inline tooltips share a single source of truth.
        bio_specs = [
            (self._bpm_label, "BPM", BIO_HR, "hr"),
            (self._hrv_label, "HRV", BIO_HRV, "hrv"),
            (self._blk_label, "BLK", BIO_BLINK, "blink"),
        ]
        for val_widget, title, color, glossary_key in bio_specs:
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
            try:
                tip = _CONCEPTS_GLOSSARY.get(glossary_key)
                if tip:
                    val_widget.setToolTip(tip)
            except Exception:
                pass

            heading = QLabel(title)
            heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
            heading.setStyleSheet(
                f"color: {color}; background: transparent; border: none;"
            )
            try:
                tip = _CONCEPTS_GLOSSARY.get(glossary_key)
                if tip:
                    heading.setToolTip(tip)
            except Exception:
                pass
            col.addWidget(val_widget)
            col.addWidget(heading)
            bio_row.addLayout(col, stretch=1)

        # Wrap the numerics row in a container so we can swap it for a
        # status banner ("Reading your pulse…" / "Camera offline" / …)
        # while the rPPG window fills. Both the numerics container and
        # the status banner share an explicit fixed height so the card
        # doesn't reflow when the first reading lands. The value (96 px)
        # matches the natural height of the populated numerics row at
        # default Mac font sizes (FS_NUMERIC value + FS_CAPTION heading
        # + bio_row padding).
        _BIO_SWAP_HEIGHT = 96
        self._bio_numerics = QWidget()
        self._bio_numerics.setStyleSheet("background: transparent;")
        self._bio_numerics.setLayout(bio_row)
        self._bio_numerics.setFixedHeight(_BIO_SWAP_HEIGHT)
        bio_inner.addWidget(self._bio_numerics)

        # Contextual status banner. Shown only when ``heart_rate`` is
        # ``None`` post-first-STATE_UPDATE; the message is driven by
        # ``payload["capture"]`` (camera frames flowing, face detected).
        # Three states:
        #   • camera offline (no frames)        → "Camera offline …"
        #   • frames flowing, no face yet       → "Looking for your face…"
        #   • frames flowing, face, no rPPG yet → "Reading your pulse…"
        self._bio_status_label = QLabel("")
        self._bio_status_label.setObjectName("CortexBioStatus")
        self._bio_status_label.setWordWrap(True)
        self._bio_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bio_status_label.setFont(
            mac_native.system_font(FS_CAPTION, "regular")
        )
        # No vertical padding here — height is pinned via ``setFixedHeight``
        # below to match ``_bio_numerics`` exactly, with the label's own
        # ``AlignCenter`` keeping the message vertically centred.
        self._bio_status_label.setStyleSheet(
            "QLabel#CortexBioStatus {"
            f"  color: {_LABEL_SECONDARY};"
            "  background: transparent;"
            "  padding: 0 8px;"
            "  font-style: italic;"
            "}"
        )
        self._bio_status_label.setFixedHeight(_BIO_SWAP_HEIGHT)
        self._bio_status_label.setVisible(False)
        _set_accessible_name(self._bio_status_label, "Biometrics status")
        bio_inner.addWidget(self._bio_status_label)
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

        # UI redesign: footer meta strip (ambient chips: focus-protection,
        # break, baseline, cost) sits just above the Stop button, pushed to
        # the bottom by the stretch above so the top bar stays uncluttered.
        root.addLayout(self._meta_strip)

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

    def refresh_baseline_freshness(self) -> None:
        """P0 §3.4 — refresh the freshness pill next to the state badge.

        Hidden when the baseline file does not exist (don't shame users
        during onboarding). When >30 days old we surface a quiet
        "Recalibrate?" prompt; otherwise the pill stays hidden so the
        dashboard chrome remains uncluttered."""
        pill = getattr(self, "_baseline_pill", None)
        if pill is None:
            return
        try:
            age = _baseline_age_days()
        except Exception:
            return
        if age is None:
            pill.setVisible(False)
            return
        if age > 30.0:
            pill.setText("Stale baseline · Recalibrate?")
            pill.setStyleSheet(
                f"color: {BRAND_ACCENT}; background: {_GROUPED_BG};"
                f" border-radius: {RADIUS_PILL}px; padding: 3px 10px;"
                " margin-left: 8px;"
            )
            pill.setVisible(True)
        else:
            pill.setVisible(False)

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
            if ratio >= 0.80:
                color = BRAND_ACCENT
            elif ratio >= 0.50:
                color = _LABEL_SECONDARY
            else:
                color = _LABEL_TERTIARY
            self._cost_pill.setStyleSheet(
                "QLabel#CortexCostPill {"
                f"  color: {color};"
                f"  background: {_GROUPED_BG};"
                f"  border-radius: {RADIUS_PILL}px;"
                "  padding: 3px 10px;"
                "  margin-left: 8px;"
                "}"
            )
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
                f" background-color: {_CONTROL_BG};"
                f" color: {_LABEL};"
                f" border: 1px solid {_SEPARATOR};"
                f" border-radius: 8px; padding: 4px; }}"
                f"QMenu#RecentGoalsMenu::item {{"
                f" padding: 6px 12px; border-radius: 5px; }}"
                f"QMenu#RecentGoalsMenu::item:selected {{"
                f" background-color: {BRAND_ACCENT}; color: #FFFFFF; }}"
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
        if kind != "off":
            try:
                self._quiet_capsule.setStyleSheet(
                    "QPushButton {"
                    f"  background: {BRAND_ACCENT}22;"
                    f"  color: {BRAND_ACCENT};"
                    f"  border-radius: {RADIUS_PILL}px;"
                    "  padding: 3px 12px;"
                    "  margin-left: 8px;"
                    "  border: none;"
                    "}"
                    "QPushButton:hover { background: rgba(217,119,87,0.28); }"
                )
            except Exception:
                pass
        else:
            try:
                self._quiet_capsule.setStyleSheet(
                    "QPushButton {"
                    f"  background: {_GROUPED_BG};"
                    f"  color: {_LABEL_SECONDARY};"
                    f"  border-radius: {RADIUS_PILL}px;"
                    "  padding: 3px 12px;"
                    "  margin-left: 8px;"
                    "  border: none;"
                    "}"
                    f"QPushButton:hover {{ background: rgba(0,0,0,0.05); color: {_LABEL}; }}"
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
        """P0 §3.7 desktop dispatch: surface the "Take a break?" pill.

        Idempotent: if the pill is already visible, the payload is
        refreshed (so a higher-urgency recommendation can over-write a
        prior low one) but no flicker is introduced.
        """
        if not isinstance(payload, dict):
            return
        self._break_recommendation_payload = dict(payload)
        urgency = str(payload.get("urgency") or "low").lower()
        label_by_urgency = {
            "low": "Take a break?",
            "medium": "Time for a break",
            "high": "Break recommended now",
        }
        try:
            self._break_pill.setText(label_by_urgency.get(urgency, "Take a break?"))
            self._break_pill.setToolTip(
                str(payload.get("reason") or "")
                or "Stress integral threshold reached. Click for a paced break."
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

    # Mirror of cortex/services/intervention_engine/executor.py::_REVERSE_ACTIONS.
    # Membership controls when the Undo toast + Restore pill surface.
    # We could ship the daemon-side `is_reversible: bool` on every
    # INTERVENTION_APPLIED payload (Phase 4b), but pre-empting that with
    # a local mirror keeps the desktop UX functional in isolation.
    _DESKTOP_REVERSIBLE_ACTIONS: frozenset[str] = frozenset({
        "hide_tabs_except_active",
        "collapse_before_error",
        "fold_except_current",
        "dim_background",
        "show_overlay",
        "close_tab",
        "group_tabs",
        "bookmark_and_close",
    })

    def apply_intervention_applied(self, payload: dict) -> None:
        """Render the Undo toast on a reversible INTERVENTION_APPLIED.

        Payload keys (matches the daemon's contract):
        ``intervention_id``, ``action_type``, ``mutations_applied_count``,
        and optional ``is_reversible`` (Phase 4b will start stamping
        this; until then we fall back to ``action_type`` membership in
        :data:`_DESKTOP_REVERSIBLE_ACTIONS`).
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
        if not isinstance(is_reversible, bool):
            is_reversible = action_type in self._DESKTOP_REVERSIBLE_ACTIONS
        if not is_reversible or applied <= 0 or not intervention_id:
            return
        import time as _time
        now = _time.monotonic()
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
        """Show / hide the "Restore previous state" pill based on the
        sliding-window membership of recently reversible actions."""
        # The pill itself is created lazily so the dashboard's header
        # doesn't grow another permanent widget when no reversible
        # action has happened yet.
        import time as _time
        now = _time.monotonic()
        cutoff = now - self._reversible_window_seconds
        self._reversible_actions = [
            entry for entry in self._reversible_actions
            if entry[0] >= cutoff
        ]
        # Lazily build the pill.
        if not hasattr(self, "_restore_pill") or self._restore_pill is None:
            pill = QPushButton("Restore previous state")
            try:
                pill.setCursor(Qt.CursorShape.PointingHandCursor)
            except Exception:
                pass
            pill.setFont(mac_native.system_font(FS_CAPTION, "medium"))
            pill.setStyleSheet(
                "QPushButton {"
                f"  background: {_GROUPED_BG};"
                f"  color: {_LABEL_SECONDARY};"
                f"  border-radius: {RADIUS_PILL}px;"
                "  padding: 3px 10px;"
                "  border: none;"
                "}"
                f"QPushButton:hover {{ background: rgba(0,0,0,0.05); color: {_LABEL}; }}"
            )
            try:
                pill.clicked.connect(self._on_restore_pill_clicked)
            except Exception:
                pass
            try:
                # Add to header row if available.
                self._header_layout.addWidget(  # type: ignore[attr-defined]
                    pill, alignment=Qt.AlignmentFlag.AlignVCenter,
                )
            except Exception:
                # No header reference — keep the pill detached; the
                # consumer can show()/hide() it programmatically without
                # blowing up.
                pass
            self._restore_pill: QPushButton | None = pill
        try:
            self._restore_pill.setVisible(bool(self._reversible_actions))
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
            capture_envelope = payload.get("capture") or {}
            store_envelope = payload.get("store") or {}
            capture_stale = bool(capture_envelope.get("stale", False))
            store_degraded = bool(store_envelope.get("degraded", False))
            if capture_stale:
                self._set_text_if_changed(
                    self._health_banner,
                    "Camera offline — frames are not flowing",
                )
                self._health_banner.setVisible(True)
            elif store_degraded:
                self._set_text_if_changed(
                    self._health_banner,
                    "Storage degraded — sessions may not persist",
                )
                self._health_banner.setVisible(True)
            else:
                self._health_banner.setVisible(False)
        except Exception:
            # Mock widgets in unit tests may not expose setVisible /
            # setText; the visibility flag isn't load-bearing.
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

        # When no heart-rate has landed yet, swap the BPM/HRV/BLK row for
        # a contextual status line so the user can tell apart "camera off"
        # from "camera on, still warming up". The daemon stamps
        # ``payload["capture"]`` on every STATE_UPDATE; older daemons
        # that lack the field fall through to the "Reading your pulse…"
        # default, which is the most benign of the three states.
        if hr is None:
            capture = payload.get("capture") or {}
            frames_flowing = bool(capture.get("frames_flowing", True))
            face_detected = bool(capture.get("face_detected", True))
            if not frames_flowing:
                status_text = (
                    "Camera offline — check System Settings → Privacy "
                    "& Security → Camera"
                )
            elif not face_detected:
                status_text = "Looking for your face…"
            else:
                # Camera + face are both healthy; the rPPG sliding window
                # is filling. First HR usually lands inside ~25 s.
                status_text = "Reading your pulse…"
            self._set_text_if_changed(self._bio_status_label, status_text)
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
        """Thin wrapper preserved for external call sites. Delegates to
        :meth:`_arm_stop` so the dashboard's two-phase stop flow (P0
        §3.3) is the single source of truth."""
        self._arm_stop()

    def _arm_stop(self) -> None:
        """P0 §3.3 phase 1 — fire the daemon-stop request, disarm the
        Stop affordance, and *wait* for the SESSION_RECAP broadcast
        before quitting Qt.

        Phase 4.B fix (#1): the previous implementation did NOT emit
        any signal here. The daemon therefore never received a stop
        request, the SESSION_RECAP pipeline never ran, and the recap
        sheet was unreachable — every click ended in the 6 s watchdog
        firing followed by a hard quit. The new contract:

        * ``daemon_stop_requested`` fires IMMEDIATELY. The controller
          schedules ``daemon.stop()`` on its asyncio loop; this kicks
          off the SESSION_RECAP broadcast pipeline (or short-session
          synthetic empty-payload broadcast).
        * The safety + recap watchdogs arm so the GUI doesn't wedge
          if either the recap or the daemon never report back.
        * ``gui_quit_requested`` is deferred to :meth:`_finalize_stop`,
          which fires when the recap is dismissed / the watchdog
          expires / the safety timer expires.

        Double clicks are coalesced via ``self._stopping``.
        """
        if getattr(self, "_stopping", False):
            return
        self._stopping = True
        self._stop_btn.setEnabled(False)
        self._stop_btn.setText("Stopping…")
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

    def _finalize_stop(self) -> None:
        """P0 §3.3 phase 2 — quit the Qt app now that the recap has been
        consumed (or its watchdog expired). Idempotent; safe to call
        from the recap-sheet dismiss handler, the recap-watchdog, the
        safety timer, or the controller's own shutdown path.

        Phase 4.B fix (#1): emits ``gui_quit_requested`` rather than
        the legacy ``stop_requested``. The daemon-stop signal was
        already fired from :meth:`_arm_stop`; this signal is solely
        about quitting the GUI now that the user has seen the recap.
        """
        if getattr(self, "_recap_finalised", False):
            return
        self._recap_finalised = True
        if getattr(self, "_recap_watchdog", None) is not None:
            try:
                self._recap_watchdog.stop()
            except Exception:
                pass
        try:
            self.gui_quit_requested.emit()
        except Exception:
            logger.debug("gui_quit_requested.emit raised", exc_info=True)

    def _stop_safety_expired(self) -> None:
        """F34 safety net: if the daemon never reports stopped, re-enable
        the button so the user can try again rather than be wedged.

        Also force-finalises the recap flow so a missed SESSION_RECAP
        cannot wedge the user inside ``_arm_stop`` indefinitely.
        """
        logger.warning(
            "Stop button safety timeout fired; re-enabling without daemon ack"
        )
        # Finalise before re-enabling so the daemon does receive the
        # stop request even on the slow path.
        self._finalize_stop()
        self.notify_daemon_stopped()

    def notify_daemon_stopped(self) -> None:
        """Called when the daemon confirms shutdown (controller wires this).
        Idempotent — safe to call from both the daemon-ack path and the
        safety-timer path."""
        self._stop_safety_timer.stop()
        if getattr(self, "_recap_watchdog", None) is not None:
            try:
                self._recap_watchdog.stop()
            except Exception:
                pass
        self._stopping = False
        self._recap_finalised = True  # No more emits expected.
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
            self.setStyleSheet(f"background: {_WINDOW_BG};")
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
            title.setStyleSheet(f"color: {_LABEL}; background: transparent;")
            layout.addWidget(title)
        except Exception:
            pass
        for key, body in _CONCEPTS_GLOSSARY.items():
            try:
                term = QLabel(key.upper())
                term.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
                term.setStyleSheet(
                    f"color: {BRAND_ACCENT}; background: transparent;"
                )
                layout.addWidget(term)
                desc = QLabel(body)
                desc.setWordWrap(True)
                desc.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
                desc.setStyleSheet(
                    f"color: {_LABEL_SECONDARY}; background: transparent;"
                )
                layout.addWidget(desc)
            except Exception:
                continue
        try:
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.accept)
            layout.addWidget(close_btn)
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
            self._seg = _MacSegmentedControl(["Dashboard", "History", "Advanced"])
        else:
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
            mac_native.apply_vibrancy(self, material="window_background")
        except Exception:
            logger.debug("native chrome application failed", exc_info=True)

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
        if self._recap_sheet is None:
            try:
                from cortex.apps.desktop_shell.recap_sheet import RecapSheet
                self._recap_sheet = RecapSheet(self)
                self._recap_sheet.dismissed.connect(self._on_recap_dismissed)
                self._recap_sheet.view_full_report.connect(
                    self._on_recap_view_full,
                )
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
        try:
            self._recap_sheet.show_report(payload)
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
        """P0 §3.16: forward INTERVENTION_APPLIED to consumer tab's undo toast."""
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
        """User clicked ``View full report →``. Switch to the History
        tab, request the detail, then continue with the shutdown like
        a normal dismiss.

        Phase 4.B fix (#22): uses the public
        ``_MacSegmentedControl.set_selected`` API rather than reaching
        into the private ``_buttons`` list. The set_selected call
        emits ``selection_changed`` itself, which drives the
        ``QStackedWidget``'s ``setCurrentIndex`` via the existing
        connection — so we no longer need a separate manual switch.
        """
        # Compute the History tab index defensively because the
        # ``HistoryTab`` may be absent in degraded mock harnesses.
        history_index = 1 if self._history_tab is not None else -1
        if history_index >= 0:
            # Prefer the public set_selected API; fall back to the
            # private stack-index path if the segmented control is a
            # mock stub without set_selected.
            if hasattr(self._seg, "set_selected"):
                try:
                    self._seg.set_selected(history_index)
                except Exception:
                    logger.debug(
                        "_MacSegmentedControl.set_selected failed",
                        exc_info=True,
                    )
            else:
                try:
                    self._stack.setCurrentIndex(history_index)
                except Exception:
                    logger.debug("switch to History tab failed", exc_info=True)
            try:
                self._history_tab.open_detail(session_id)
            except Exception:
                logger.debug("HistoryTab.open_detail failed", exc_info=True)
        # Continue with the shutdown — user already consumed the recap.
        if self._consumer is not None:
            try:
                self._consumer._finalize_stop()
            except Exception:
                logger.debug("finalize_stop on view_full failed", exc_info=True)

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
