"""Desktop Shell — End-of-Session Recap Sheet (P0 §3.3).

A frameless slide-up sheet anchored to the bottom of the dashboard window.
Surfaces the just-finalised :class:`SessionReport` in a single, calm card
before the daemon actually shuts down. The flow is:

1. User clicks Stop → ``DashboardWindow._arm_stop`` disables the button
   and arms a 6 s recap-watchdog.
2. Daemon finishes the session report, broadcasts ``SESSION_RECAP``.
3. Controller relays the payload to ``DashboardWindow.apply_session_recap``
   which constructs this sheet and animates it up over the dashboard.
4. User clicks ``Close`` (or the 12 s autohide fires) → ``dismissed``
   → ``_finalize_stop`` → ``stop_requested.emit()`` actually shuts down.
5. Alternatively user clicks ``View full report →`` → ``view_full_report``
   carries the ``session_id``; the dashboard switches to the History tab,
   requests detail, then proceeds with shutdown.

The sheet uses the same design tokens (Cormorant Garamond display numerals,
warm terracotta accent, 10 px card radius, OutCubic 200 ms motion) as the
rest of the desktop shell so it feels like a continuation of the dashboard,
not a separate window.
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

try:
    from PySide6.QtGui import QKeyEvent
except ImportError:  # pragma: no cover - test stubs
    QKeyEvent = object  # type: ignore[assignment,misc]

try:
    from PySide6.QtWidgets import (
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - lightweight stubs
    from PySide6.QtWidgets import (  # type: ignore[attr-defined]
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
    BRAND_ACCENT_HOVER,
    BRAND_DISPLAY_FONT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    DURATION_NORMAL,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_LARGE_TITLE,
    FW_REGULAR,
    RADIUS_BUTTON,
    RADIUS_CARD,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
    SP4,
    SP5,
)

logger = logging.getLogger(__name__)


# Auto-dismiss after this many milliseconds unless the user hovers /
# clicks. The 12 s budget mirrors the design-doc spec.
_AUTOHIDE_MS = 12_000

# Reduced-motion bypass: when the user has Accessibility → Reduce motion
# enabled the slide animation is replaced by an instant snap. Promoted
# to a named constant so the bypass path is greppable.
_REDUCED_MOTION_DURATION_MS = 0

# Fixed sheet geometry — kept independent of DASHBOARD_WIDTH so the sheet
# always looks like a discrete card even if the dashboard ever gains a
# resizable mode.
_SHEET_WIDTH = 360
_SHEET_HEIGHT = 220
_SHEET_BOTTOM_INSET = SP4  # gap below the sheet at its resting position

_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
_LABEL_SECONDARY = CX_TEXT_SECONDARY
_LABEL_TERTIARY = CX_TEXT_TERTIARY


def _safe_call(target: Any, *args: Any, **kwargs: Any) -> Any:
    """Tolerate stub widgets without a given method (CI / test harness)."""
    try:
        return target(*args, **kwargs)
    except Exception:
        return None


def _reduced_motion_active() -> bool:
    """Thin wrapper around :func:`mac_native.prefers_reduced_motion`.

    Isolated so the recap sheet's animation paths read a single boolean
    and the test harness can monkeypatch a single helper. Mirrors
    :class:`OverlayWindow`'s approach in ``overlay.py``. Returns False
    on any AppKit/platform error so a probing failure still animates
    (motion is the wrong fail-open here, but matches the rest of the
    shell's behaviour and avoids accidentally skipping motion on a
    misconfigured macOS install).
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
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        self._value = QLabel(value)
        self._value.setFont(mac_native.system_font(FS_BODY, "semibold"))
        self._value.setStyleSheet(
            f"color: {_LABEL}; background: transparent;"
        )
        layout.addWidget(self._caption)
        layout.addWidget(self._value)


class RecapSheet(QWidget):
    """Slide-up end-of-session recap card (P0 §3.3).

    Construction is deliberately cheap — the widget hides itself until
    :meth:`show_report` is called with the broadcast payload. Re-showing
    with a new payload rebuilds the inner layout in place (recap sheets
    are rare events, so the simplicity is cheap and avoids cache bugs).
    """

    view_full_report = Signal(str)
    """Emitted with ``session_id`` when the user clicks the primary CTA."""

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
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        except Exception:
            pass
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(_SHEET_WIDTH, _SHEET_HEIGHT)
        self.hide()

        self._session_id: str = ""
        self._dismissed_once = False
        self._anim: QPropertyAnimation | None = None
        self._closing = False

        self._autohide = QTimer(self)
        self._autohide.setSingleShot(True)
        self._autohide.setInterval(_AUTOHIDE_MS)
        self._autohide.timeout.connect(self._on_autohide)

        self._card = QFrame(self)
        self._card.setObjectName("RecapCard")
        self._card.setGeometry(0, 0, _SHEET_WIDTH, _SHEET_HEIGHT)
        self._card.setStyleSheet(
            f"#RecapCard {{ background: {_CONTROL_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_CARD + 2}px; }}"
        )

        # Outer column ─────────────────────────────────────────────
        col = QVBoxLayout(self._card)
        col.setContentsMargins(SP5, SP5, SP5, SP5)
        col.setSpacing(SP3)

        self._headline = QLabel("--")
        headline_font = mac_native.system_font(FS_LARGE_TITLE, "regular")
        # Override family to the brand serif for the numeric headline; we
        # do this via stylesheet rather than QFont so the family-stack
        # fallback resolves correctly when Cormorant is missing on host.
        self._headline.setFont(headline_font)
        self._headline.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT};"
            f" font-size: {FS_LARGE_TITLE}pt;"
            f" font-weight: {FW_REGULAR};"
            f" color: {_LABEL};"
            " background: transparent;"
        )
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._subtext = QLabel("")
        self._subtext.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._subtext.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        self._subtext.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Stats row ───────────────────────────────────────────────
        self._stats_row = QHBoxLayout()
        self._stats_row.setContentsMargins(0, 0, 0, 0)
        self._stats_row.setSpacing(SP4)

        self._stat_widgets: list[_Stat] = []

        # Buttons ─────────────────────────────────────────────────
        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)
        buttons_row.setSpacing(SP2)

        self._view_btn = QPushButton("View full report →")
        self._view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._view_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._view_btn.setStyleSheet(
            "QPushButton {"
            f"  padding: 6px 14px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFFFFF;"
            "  border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
            "QPushButton:pressed { background: #B45638; }"
        )
        self._view_btn.clicked.connect(self._on_view_clicked)
        _safe_call(
            self._view_btn.setAccessibleName,
            "View full session report",
        )

        self._close_btn = QPushButton("Close")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._close_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._close_btn.setStyleSheet(
            "QPushButton {"
            f"  padding: 6px 14px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {_LABEL_SECONDARY};"
            f"  border: 0.5px solid {_SEPARATOR};"
            "}"
            "QPushButton:hover { background: rgba(0, 0, 0, 0.04);"
            f" color: {_LABEL}; }}"
        )
        self._close_btn.clicked.connect(self._on_close_clicked)
        _safe_call(
            self._close_btn.setAccessibleName,
            "Close recap and finish stopping Cortex",
        )

        buttons_row.addStretch(1)
        buttons_row.addWidget(self._close_btn)
        buttons_row.addWidget(self._view_btn)

        col.addWidget(self._headline)
        col.addWidget(self._subtext)
        col.addLayout(self._stats_row, stretch=1)
        col.addStretch(1)
        col.addLayout(buttons_row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_report(self, report: dict) -> None:
        """Render ``report`` (raw ``SessionReport.model_dump`` payload)
        then slide the sheet up over the parent dashboard.

        Safe to call repeatedly — each call rebuilds the stats row and
        restarts the autohide timer. The ``dismissed`` signal will only
        fire once between :meth:`show_report` and the eventual close.
        """
        if not isinstance(report, dict):
            logger.debug("RecapSheet.show_report: payload was %s, skipping", type(report))
            # Defensive: a non-dict payload (e.g. mis-routed broadcast)
            # would otherwise leave the dashboard's stop flow waiting on
            # the 6 s recap watchdog. Emit ``dismissed`` immediately so
            # ``_finalize_stop`` runs on the same Qt tick.
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
        self._session_id = str(report.get("session_id") or "")

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
            f"Session ended  ·  {flow_min}m in flow  ({flow_pct:.0f}%)"
        )

        # Five stats.
        self._rebuild_stats(report)

        # Position and slide up.
        self._position_relative_to_parent()
        self.show()
        self.raise_()
        # Activate so the sheet owns keyboard focus — required for the
        # Esc / Enter handlers below to receive the keypress without
        # the user clicking the sheet first.
        try:
            self.activateWindow()
        except Exception:
            logger.debug("RecapSheet.activateWindow raised", exc_info=True)
        # Esc / Enter need focus on the sheet; default focus to Close so
        # the user can hit Enter to dismiss without rearming the stop
        # flow.
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

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
        # Phase 4.B fix (#9): clamp to a sane percent range before
        # rendering so a daemon glitch can't display "112%".
        flow_pct = max(0.0, min(100.0, flow_pct))
        avg_hr = report.get("avg_hr_bpm")
        breaks = int(report.get("breaks_taken") or 0)
        distraction_domains = report.get("top_distraction_domains") or []
        if not isinstance(distraction_domains, list):
            distraction_domains = []
        distractions = len(distraction_domains)
        # Phase 4.B fix (#8): "Spikes" is the accurate caption — this is
        # a count of state_transitions whose to_state is HYPER, i.e.
        # how many times the user spiked into the high-arousal state.
        # The previous "Helpful" label implied user-rated intervention
        # quality, which the data does not capture.
        transitions = report.get("state_transitions") or []
        if not isinstance(transitions, list):
            transitions = []
        spikes = sum(
            1
            for t in transitions
            if isinstance(t, dict) and str(t.get("to_state", "")).upper() == "HYPER"
        )

        stats: list[tuple[str, str]] = []
        stats.append(("Flow", f"{flow_pct:.0f}%"))
        # Phase 4.B fix (#7): the schema field is ``avg_hr_bpm`` — an
        # average over the session, not a peak. Surface that honestly.
        if isinstance(avg_hr, (int, float)) and avg_hr is not None:
            stats.append(("Avg HR", f"{int(round(avg_hr))} bpm"))
        stats.append(("Breaks", str(breaks)))
        stats.append(("Blocked", str(distractions)))
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
        # Stash the resting position for the animation. We use a Python
        # attribute because QPoint isn't a Qt property on QWidget.
        self._rest_pos = QPoint(x, rest_y)
        self._start_pos = QPoint(x, start_y)

    def _animate_in(self) -> None:
        if not hasattr(self, "_rest_pos"):
            return
        # Phase 4.B fix (#5): respect Accessibility → Reduce motion.
        # Snap directly to the resting position so VOR-sensitive users
        # don't get a slide animation. ``DURATION_NORMAL`` is replaced
        # by ``_REDUCED_MOTION_DURATION_MS`` (0) for the bypass path.
        if _reduced_motion_active():
            try:
                self.move(self._rest_pos)
            except Exception:
                logger.debug("reduced-motion snap (in) failed", exc_info=True)
            return
        try:
            anim = QPropertyAnimation(self, b"pos", self)
        except Exception:
            # Animation system unavailable (mock harness) — snap directly.
            try:
                self.move(self._rest_pos)
            except Exception:
                pass
            return
        anim.setDuration(DURATION_NORMAL)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self._start_pos)
        anim.setEndValue(self._rest_pos)
        anim.start()
        self._anim = anim

    def _animate_out(self) -> None:
        if not hasattr(self, "_rest_pos"):
            self.hide()
            self._emit_dismissed()
            return
        # Phase 4.B fix (#5): reduced-motion bypass — skip the tween
        # and hide immediately so the dashboard's stop flow can finish
        # without an animation budget.
        if _reduced_motion_active():
            self.hide()
            self._emit_dismissed()
            return
        try:
            anim = QPropertyAnimation(self, b"pos", self)
        except Exception:
            self.hide()
            self._emit_dismissed()
            return
        anim.setDuration(DURATION_NORMAL)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(self.pos())
        anim.setEndValue(self._start_pos)
        anim.finished.connect(self._finish_close)
        anim.start()
        self._anim = anim

    def _finish_close(self) -> None:
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
        # switch tabs and request detail while the sheet animates away.
        try:
            self.view_full_report.emit(self._session_id)
        except Exception:
            logger.debug("view_full_report.emit raised", exc_info=True)
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
            # If the View button currently holds focus, treat Enter as
            # "view"; otherwise treat as "close".
            focused = self.focusWidget()
            if focused is self._view_btn:
                self._on_view_clicked()
            else:
                self._on_close_clicked()
            event.accept()
            return
        try:
            super().keyPressEvent(event)
        except Exception:
            pass


__all__ = ["RecapSheet"]
