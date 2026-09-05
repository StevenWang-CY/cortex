"""Desktop Shell — End-of-Session Recap Sheet (P0 §3.3).

A frameless slide-up sheet anchored to the bottom of the dashboard window.
Surfaces the just-finalised :class:`SessionReport` in a single, calm card
once the daemon has ended the session. The flow is:

1. User ends the session (dashboard "End session") or asks to quit (tray
   "Quit Cortex", Cmd+Q) → ``_ConsumerTab._arm_stop`` arms the recap
   watchdog and asks the daemon to stop.
2. Daemon finishes the session report, broadcasts ``SESSION_RECAP``.
3. Controller relays the payload to ``DashboardWindow.apply_session_recap``
   which constructs this sheet and animates it up over the dashboard.
4. The user picks one of three explicit routes:

   * ``View full report`` → ``view_full_report`` carries the ``session_id``
     and the dashboard opens the History detail **from the recap payload
     already in hand**. Cortex stays open; nothing is requested from the
     stopped daemon.
   * ``Quit Cortex`` → ``quit_requested`` — the only route that exits the
     app. When the user *started* with Quit, the primary button already
     reads "Quit Cortex" and closing the sheet completes that quit.
   * ``Close`` / Escape / the 12 s autohide → ``dismissed`` — finishes the
     route the user chose at the start (end session and stay open, or
     quit).

The sheet uses the shared design tokens (hero numerals in the display
serif, warm terracotta accent, card radius, 200 ms OutCubic motion with an
instant Reduce Motion path) so it reads as a continuation of the dashboard,
not a separate window. The entrance and exit share one interruptible
animation: an exit can start mid-entrance and simply retargets.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
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
from cortex.apps.desktop_shell.components import install_elide
from cortex.apps.desktop_shell.tokens import (
    BTN_ACCENT_QSS,
    BTN_DESTRUCTIVE_QSS,
    BTN_GHOST_QSS,
    CARD_QSS,
    CX_TEXT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    DURATION_FAST,
    DURATION_NORMAL,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    HERO_NUMERAL_QSS,
    SP2,
    SP3,
    SP4,
    SP5,
)

logger = logging.getLogger(__name__)


# Auto-dismiss after this many milliseconds unless the user hovers /
# clicks. The 12 s budget mirrors the design-doc spec.
_AUTOHIDE_MS = 12_000

# Fixed sheet geometry — kept independent of DASHBOARD_WIDTH so the sheet
# always looks like a discrete card even if the dashboard ever gains a
# resizable mode.
_SHEET_WIDTH = 360
_SHEET_HEIGHT = 236
_SHEET_BOTTOM_INSET = SP4  # gap below the sheet at its resting position


def _safe_call(target: Any, *args: Any, **kwargs: Any) -> Any:
    """Tolerate stub widgets without a given method (CI / test harness)."""
    try:
        return target(*args, **kwargs)
    except Exception:
        return None


def _reduced_motion_active() -> bool:
    """Thin wrapper around :func:`mac_native.prefers_reduced_motion`.

    Isolated so the recap sheet's animation paths read a single boolean
    and the test harness can monkeypatch a single helper. Returns False
    on any AppKit/platform error so a probing failure still animates.
    """
    try:
        return bool(mac_native.prefers_reduced_motion())
    except Exception:
        logger.debug(
            "prefers_reduced_motion probe failed; defaulting to motion-on",
            exc_info=True,
        )
        return False


class _Stat(QFrame):
    """One stat tile: caption on top, numeric below."""

    def __init__(
        self,
        caption: str,
        value: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent; border: none;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self._caption = QLabel(caption)
        self._caption.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._caption.setStyleSheet(
            f"color: {CX_TEXT_TERTIARY}; background: transparent;"
        )
        install_elide(self._caption)
        self._value = QLabel(value)
        self._value.setFont(mac_native.system_font(FS_BODY, "semibold"))
        self._value.setStyleSheet(
            f"color: {CX_TEXT}; background: transparent;"
        )
        install_elide(self._value)
        layout.addWidget(self._caption)
        layout.addWidget(self._value)


class RecapSheet(QWidget):
    """Slide-up end-of-session recap card (P0 §3.3).

    Construction is deliberately cheap — the widget hides itself until
    :meth:`show_report` is called with the broadcast payload. Re-showing
    with a new payload rebuilds the inner layout in place.
    """

    view_full_report = Signal(str)
    """Emitted with ``session_id`` when the user asks to see the report.
    Cortex stays open."""

    quit_requested = Signal()
    """Emitted when the user explicitly chooses to quit Cortex from the
    sheet. Never emitted by Close, Escape, or the autohide."""

    dismissed = Signal()
    """Emitted exactly once when the sheet is fully hidden, regardless of
    whether the user clicked Close, hit Esc, or the autohide fired."""

    def __init__(self, parent: QWidget | None = None) -> None:
        # The parent is intentional: it anchors positioning to the dashboard
        # and gives Qt a sane reparent target on close. WindowStaysOnTopHint
        # ensures the sheet floats above the dashboard's stacked widget.
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        # The sheet is its own popup-style top-level — set translucency so
        # the rounded corners don't get a square OS-painted backdrop.
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        except Exception:
            pass
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(_SHEET_WIDTH, _SHEET_HEIGHT)
        set_accessible_name(self, "Session recap")
        set_accessible_description(
            self,
            "Summary of the session that just ended. Escape closes the sheet.",
        )
        self.hide()

        self._session_id: str = ""
        self._dismissed_once = False
        self._closing = False
        self._quit_pending = False
        self._rest_pos: QPoint | None = None
        self._start_pos: QPoint | None = None

        self._autohide = QTimer(self)
        self._autohide.setSingleShot(True)
        self._autohide.setInterval(_AUTOHIDE_MS)
        self._autohide.timeout.connect(self._on_autohide)

        # One reusable, interruptible position animation. Entrance and
        # exit both retarget it; ``_on_anim_finished`` reads ``_closing``
        # to decide whether the finished run was an exit.
        self._anim: QPropertyAnimation | None = None
        try:
            self._anim = QPropertyAnimation(self, b"pos", self)
            self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._anim.finished.connect(self._on_anim_finished)
        except Exception:
            self._anim = None

        self._card = QFrame(self)
        self._card.setObjectName("RecapCard")
        self._card.setGeometry(0, 0, _SHEET_WIDTH, _SHEET_HEIGHT)
        self._card.setStyleSheet(f"#RecapCard {{ {CARD_QSS} }}")

        # Outer column ─────────────────────────────────────────────
        col = QVBoxLayout(self._card)
        col.setContentsMargins(SP5, SP5, SP5, SP5)
        col.setSpacing(SP3)

        # Hero numeral — the one place outside the wordmark where the
        # display serif is allowed. Pixel-sized from the type scale.
        self._headline = QLabel("--")
        self._headline.setStyleSheet(HERO_NUMERAL_QSS)
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_accessible_name(self._headline, "Session length")

        self._subtext = QLabel("")
        self._subtext.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._subtext.setStyleSheet(
            f"color: {CX_TEXT_SECONDARY}; background: transparent;"
        )
        self._subtext.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtext.setWordWrap(True)

        # Stats row ───────────────────────────────────────────────
        self._stats_row = QHBoxLayout()
        self._stats_row.setContentsMargins(0, 0, 0, 0)
        self._stats_row.setSpacing(SP4)
        self._stat_widgets: list[_Stat] = []

        # Buttons ─────────────────────────────────────────────────
        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.setSpacing(SP2)

        self._quit_btn = QPushButton("Quit Cortex")
        self._quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._quit_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._quit_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._quit_btn.setStyleSheet(BTN_DESTRUCTIVE_QSS)
        self._quit_btn.clicked.connect(self._on_quit_clicked)
        set_accessible_name(self._quit_btn, "Quit Cortex")
        set_accessible_description(
            self._quit_btn,
            "Closes Cortex. Sensing has already stopped; the menu-bar icon "
            "goes away until you relaunch.",
        )
        _safe_call(self._quit_btn.setToolTip, "Closes Cortex and the menu-bar icon")

        self._view_btn = QPushButton("View full report")
        self._view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._view_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._view_btn.setStyleSheet(BTN_GHOST_QSS)
        self._view_btn.clicked.connect(self._on_view_clicked)
        set_accessible_name(self._view_btn, "View full session report")
        set_accessible_description(
            self._view_btn,
            "Opens this session in the History tab. Cortex stays open.",
        )

        self._close_btn = QPushButton("Close")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._close_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._close_btn.setStyleSheet(BTN_ACCENT_QSS)
        self._close_btn.clicked.connect(self._on_close_clicked)

        buttons_row.addWidget(self._quit_btn)
        buttons_row.addStretch(1)
        buttons_row.addWidget(self._view_btn)
        buttons_row.addWidget(self._close_btn)

        col.addWidget(self._headline)
        col.addWidget(self._subtext)
        col.addLayout(self._stats_row, stretch=1)
        col.addStretch(1)
        col.addLayout(buttons_row)
        self._apply_route_copy()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_report(self, report: dict, *, quit_pending: bool = False) -> None:
        """Render ``report`` (raw ``SessionReport.model_dump`` payload)
        then slide the sheet up over the parent dashboard.

        ``quit_pending`` is True when the user started this stop by asking
        to quit: the primary button then reads "Quit Cortex" (closing the
        sheet completes the quit) and the separate quit control is hidden.

        Safe to call repeatedly — each call rebuilds the stats row and
        restarts the autohide timer. The ``dismissed`` signal will only
        fire once between :meth:`show_report` and the eventual close.
        """
        if not isinstance(report, dict):
            logger.debug("RecapSheet.show_report: payload was %s, skipping", type(report))
            # A non-dict payload (mis-routed broadcast) must not leave the
            # dashboard waiting on the recap watchdog.
            self._emit_dismissed()
            return
        # Phase 4.B fix (#10): an empty payload (no session_id) is the
        # synthetic short-session signal from the daemon. Treat it as an
        # immediate dismiss so the dashboard's two-phase stop completes
        # without the 6 s watchdog having to fire.
        if not report.get("session_id"):
            logger.debug(
                "RecapSheet.show_report: empty payload (no session_id); dismissing"
            )
            self._emit_dismissed()
            return
        self._dismissed_once = False
        self._closing = False
        self._quit_pending = bool(quit_pending)
        self._session_id = str(report.get("session_id") or "")
        self._apply_route_copy()

        # Headline: total minutes (round nearest int).
        duration_s = float(report.get("duration_seconds") or 0.0)
        total_min = max(0, int(round(duration_s / 60.0)))
        self._headline.setText(f"{total_min} min")

        # Subtext: flow minutes + flow percentage. ``flow_pct`` is
        # clamped to [0, 100] so a daemon-side rounding glitch or stale
        # cached payload cannot render "112%" in flow.
        flow_s = float(report.get("time_in_flow_seconds") or 0.0)
        flow_min = max(0, int(round(flow_s / 60.0)))
        flow_pct = float(report.get("flow_percentage") or 0.0)
        flow_pct = max(0.0, min(100.0, flow_pct))
        self._subtext.setText(
            f"Session ended  ·  {flow_min}m steady  ({flow_pct:.0f}%)"
        )

        # Five stats.
        self._rebuild_stats(report)

        # Position and slide up.
        self._position_relative_to_parent()
        self.show()
        self.raise_()
        # Activate so the sheet owns keyboard focus — required for the
        # Esc / Enter handlers below to receive the keypress without
        # the user clicking the sheet first. (The sheet is a deliberate,
        # user-initiated modal moment, unlike the intervention overlay.)
        try:
            self.activateWindow()
        except Exception:
            logger.debug("RecapSheet.activateWindow raised", exc_info=True)
        try:
            self._close_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        except Exception:
            pass

        self._animate_in()
        self._autohide.start()

    def force_dismiss(self) -> None:
        """Programmatic close path (e.g. controller's outer watchdog
        decides to bail). Emits ``dismissed`` exactly once."""
        self._on_close_clicked()

    def current_session_id(self) -> str:
        """The currently-displayed session id (``""`` before any report)."""
        return self._session_id

    @property
    def quit_pending(self) -> bool:
        return self._quit_pending

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_route_copy(self) -> None:
        """Make the buttons say exactly what closing the sheet does."""
        if self._quit_pending:
            self._close_btn.setText("Quit Cortex")
            self._close_btn.setStyleSheet(BTN_DESTRUCTIVE_QSS)
            set_accessible_name(self._close_btn, "Quit Cortex")
            set_accessible_description(
                self._close_btn,
                "Finishes quitting Cortex. Sensing has already stopped.",
            )
            self._quit_btn.setVisible(False)
        else:
            self._close_btn.setText("Close")
            self._close_btn.setStyleSheet(BTN_ACCENT_QSS)
            set_accessible_name(self._close_btn, "Close recap")
            set_accessible_description(
                self._close_btn,
                "Closes this summary. Cortex stays open with the session ended.",
            )
            self._quit_btn.setVisible(True)

    def _rebuild_stats(self, report: dict) -> None:
        # Tear down any prior stat widgets so a re-show with fresh data
        # doesn't accumulate them.
        for i in reversed(range(self._stats_row.count())):
            item = self._stats_row.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                self._stats_row.removeWidget(w)
                w.deleteLater()
        self._stat_widgets = []

        flow_pct = float(report.get("flow_percentage") or 0.0)
        flow_pct = max(0.0, min(100.0, flow_pct))
        avg_hr = report.get("avg_hr_bpm")
        breaks = int(report.get("breaks_taken") or 0)
        distraction_domains = report.get("top_distraction_domains") or []
        if not isinstance(distraction_domains, list):
            distraction_domains = []
        distractions = len(distraction_domains)
        # "Spikes" counts state_transitions whose to_state is HYPER — how
        # many times the estimate moved into "support may help".
        transitions = report.get("state_transitions") or []
        if not isinstance(transitions, list):
            transitions = []
        spikes = sum(
            1
            for t in transitions
            if isinstance(t, dict) and str(t.get("to_state", "")).upper() == "HYPER"
        )

        stats: list[tuple[str, str]] = [("Steady", f"{flow_pct:.0f}%")]
        # ``avg_hr_bpm`` is an average over the session, not a peak.
        if isinstance(avg_hr, (int, float)):
            stats.append(("Avg pulse", f"{int(round(avg_hr))} bpm"))
        stats.append(("Breaks", str(breaks)))
        stats.append(("Distractions", str(distractions)))
        stats.append(("Spikes", str(spikes)))

        for caption, value in stats:
            tile = _Stat(caption, value)
            self._stats_row.addWidget(tile, stretch=1)
            self._stat_widgets.append(tile)

    def _position_relative_to_parent(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            parent_geo = parent.geometry()
            parent_top_left = parent.mapToGlobal(QPoint(0, 0))
        except Exception:
            return
        x = parent_top_left.x() + (parent_geo.width() - _SHEET_WIDTH) // 2
        # Resting Y: just above the parent's bottom edge.
        rest_y = parent_top_left.y() + parent_geo.height() - _SHEET_HEIGHT - _SHEET_BOTTOM_INSET
        # Below-bottom start Y for the slide-in animation.
        start_y = parent_top_left.y() + parent_geo.height() + 4
        self.move(x, start_y)
        self._rest_pos = QPoint(x, rest_y)
        self._start_pos = QPoint(x, start_y)

    def _retarget(self, end: QPoint, duration_ms: int) -> bool:
        """Stop any in-flight run and animate from the current position.

        Returns False when the animation system is unavailable so the
        caller can snap instead.
        """
        anim = self._anim
        if anim is None:
            return False
        try:
            anim.stop()
            anim.setDuration(duration_ms)
            anim.setStartValue(self.pos())
            anim.setEndValue(end)
            anim.start()
            return True
        except Exception:
            logger.debug("recap animation retarget failed", exc_info=True)
            return False

    def _animate_in(self) -> None:
        if self._rest_pos is None:
            return
        # Respect Accessibility → Reduce motion: snap to the resting
        # position so VOR-sensitive users don't get a slide animation.
        if _reduced_motion_active() or not self._retarget(self._rest_pos, DURATION_NORMAL):
            try:
                if self._anim is not None:
                    self._anim.stop()
                self.move(self._rest_pos)
            except Exception:
                logger.debug("reduced-motion snap (in) failed", exc_info=True)

    def _animate_out(self) -> None:
        if self._start_pos is None:
            self._finish_close()
            return
        # Exit is faster than entry (motion contract) and interrupts any
        # in-flight entrance: the same animation object is retargeted from
        # wherever the sheet currently is.
        if _reduced_motion_active() or not self._retarget(self._start_pos, DURATION_FAST):
            self._finish_close()

    def _on_anim_finished(self) -> None:
        if self._closing:
            self._finish_close()

    def _finish_close(self) -> None:
        try:
            if self._anim is not None:
                self._anim.stop()
        except Exception:
            pass
        self.hide()
        self._emit_dismissed()

    def _emit_dismissed(self) -> None:
        if self._dismissed_once:
            return
        self._dismissed_once = True
        try:
            self.dismissed.emit()
        except Exception:
            logger.debug("RecapSheet dismissed.emit raised", exc_info=True)

    def _on_view_clicked(self) -> None:
        self._autohide.stop()
        # Emit the view signal BEFORE dismissing so the dashboard can
        # switch tabs and open the detail while the sheet animates away.
        try:
            self.view_full_report.emit(self._session_id)
        except Exception:
            logger.debug("view_full_report.emit raised", exc_info=True)
        self._on_close_clicked()

    def _on_quit_clicked(self) -> None:
        self._autohide.stop()
        try:
            self.quit_requested.emit()
        except Exception:
            logger.debug("quit_requested.emit raised", exc_info=True)
        self._on_close_clicked()

    def _on_close_clicked(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._autohide.stop()
        self._animate_out()

    def _on_autohide(self) -> None:
        # Treat exactly like a manual close click.
        self._on_close_clicked()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def enterEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        # Hover cancels the autohide so the user has time to read.
        self._autohide.stop()
        try:
            super().enterEvent(event)
        except Exception:
            pass

    def leaveEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        # Restart the autohide on leave so unattended sheets still close.
        if not self._closing:
            self._autohide.start()
        try:
            super().leaveEvent(event)
        except Exception:
            pass

    def mousePressEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        # Any click anywhere on the card cancels the autohide too.
        self._autohide.stop()
        try:
            super().mousePressEvent(event)
        except Exception:
            pass

    def keyPressEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        try:
            key = event.key()
        except Exception:
            key = None
        if key == Qt.Key.Key_Escape:
            self._on_close_clicked()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focused = self.focusWidget()
            if focused is self._view_btn:
                self._on_view_clicked()
            elif focused is self._quit_btn:
                self._on_quit_clicked()
            else:
                self._on_close_clicked()
            event.accept()
            return
        try:
            super().keyPressEvent(event)
        except Exception:
            pass


__all__ = ["RecapSheet"]
