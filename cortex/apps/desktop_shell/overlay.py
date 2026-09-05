"""Desktop Shell — Intervention Overlay (native HUD styling).

A frameless, always-on-top *notification-style* card that renders
LLM-generated intervention content. It anchors to the top-right of the
screen under the cursor, never activates (``WA_ShowWithoutActivating``) so a
nudge cannot steal keystrokes from the app the user is typing in, and every
keyboard shortcut is Command-modified. On macOS the window retains Qt's
content view and receives the dark HUD appearance from
:mod:`cortex.apps.desktop_shell.mac_native`; its HUD tokens provide the
visual material. True ``NSVisualEffectView`` replacement is intentionally
unavailable because it can orphan the packaged Qt window.

Every hide path — user dismiss, timeout, pause/suppress, close — runs
through :meth:`OverlayWindow._stop_presentation_timers` so no 30 Hz pacer,
five-minute timeout, or movement countdown keeps ticking on a hidden window.

Public API: ``dismissed(str)`` Signal, ``show_intervention(payload)`` and
``suppress()`` methods.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QRect, Qt, QTimer, Signal

if TYPE_CHECKING:
    from PySide6.QtWidgets import QGraphicsOpacityEffect
from PySide6.QtGui import QColor, QFont, QPainter, QPen

# FE-3 multi-monitor placement: ``QGuiApplication.screenAt`` /
# ``QCursor.pos`` let us land the overlay on the display under the
# cursor instead of trusting ``self.screen()`` on a still-hidden window.
# The legacy desktop_shell test stub does not ship these symbols, so the
# import is guarded; ``_target_screen`` falls back to ``self.screen()``
# when either is unavailable.
try:
    from PySide6.QtGui import QCursor, QGuiApplication
except ImportError:  # pragma: no cover - lightweight stubs
    QCursor = None
    QGuiApplication = None
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtWidgets import QToolButton
except ImportError:  # pragma: no cover - lightweight stub fallback
    # The legacy desktop_shell tests stub PySide6 with a minimal class
    # surface; QToolButton isn't in those stubs. QPushButton supports
    # the same setCheckable/setText/setChecked API we use for the F51
    # "Show more" toggle, so it stands in faithfully.
    QToolButton = QPushButton

# Phase J-4: subtle concurrent opacity feedback for newly presented text.
# QPropertyAnimation / QEasingCurve / QGraphicsOpacityEffect
# are real-PySide6 only — the lightweight stubs don't expose them. The
# import-time guard keeps this file importable from the legacy mock
# harness; the runtime guard inside ``_play_show_animations`` short-
# circuits when any piece is missing or when the user has Reduce Motion
# enabled.
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
from cortex.apps.desktop_shell.palette_runtime import active_state_color
from cortex.apps.desktop_shell.tokens import (
    BREATHING_PACER_SIZE,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_SEMIBOLD,
    HUD_ACCENT,
    POPUP_WIDTH,
    RADIUS_BUTTON,
    RADIUS_WINDOW,
    SP1,
    SP2,
    SP3,
    SP4,
    STATE_LABELS,
    TEXT_HUD_PRIMARY,
    TEXT_HUD_SECONDARY,
    TEXT_HUD_TERTIARY,
)
from cortex.libs.schemas.intervention_transaction import (
    ActionManifest,
    manifest_suggestion_matches,
)

# Support text must be readable on the first frame.  Both labels therefore
# begin at 72% opacity and settle together; no geometry or delayed phase is
# involved.  The Reduce Motion path forces both to their final state.
HEADLINE_FADE_DURATION_MS: int = 160
CAUSAL_FADE_DURATION_MS: int = 160

logger = logging.getLogger(__name__)


def _safe_call(widget: object, attr: str, *args: object) -> None:
    """Call ``widget.<attr>(*args)`` only if the attribute exists.
    Used to keep desktop_shell code defensive against the lightweight
    PySide6 test stubs in :mod:`cortex.tests.unit.test_desktop_shell`
    which intentionally omit many QWidget methods."""
    fn = getattr(widget, attr, None)
    if callable(fn):
        try:
            fn(*args)
        except Exception:
            pass


def _set_accessible_name(widget: object, name: str) -> None:
    """Wrapper for ``setAccessibleName`` that no-ops cleanly when the
    target widget is a lightweight test stub without that method (F55)."""
    _safe_call(widget, "setAccessibleName", name)


def _set_accessible_description(widget: object, description: str) -> None:
    """Defensive companion to :func:`_set_accessible_name`."""
    _safe_call(widget, "setAccessibleDescription", description)


def _set_tab_order(first: object, second: object) -> None:
    """Wrapper for ``QWidget.setTabOrder`` — see :func:`_set_accessible_name`."""
    fn = getattr(QWidget, "setTabOrder", None)
    if callable(fn):
        try:
            fn(first, second)
        except Exception:
            pass


# 4-7-8 breathing pattern: inhale 4s, hold 7s, exhale 8s = 19s total cycle.
# These remain as module-level fallbacks so callers without a config
# (test stubs, ad-hoc previews) keep their prior behaviour. F48 moves the
# spec to ``InterventionConfig.breathing_pattern`` and BreathingPacer
# reads from there at construction time.
_DEFAULT_BREATHING_PATTERN: tuple[int, int, int] = (4, 7, 8)
_INHALE_SECONDS, _HOLD_SECONDS, _EXHALE_SECONDS = _DEFAULT_BREATHING_PATTERN
_CYCLE_SECONDS = _INHALE_SECONDS + _HOLD_SECONDS + _EXHALE_SECONDS

# HUD palette — resolved from :mod:`cortex.apps.desktop_shell.tokens` (F47).
# These QColors provide the complete safe material; there is no native blur
# view below Qt's content. The token values are the spec; do not introduce hex
# literals in this module — extend ``tokens.py`` instead.
_ACCENT = QColor(*HUD_ACCENT)
_TEXT_PRIMARY = QColor(*TEXT_HUD_PRIMARY)
_TEXT_SECONDARY = QColor(*TEXT_HUD_SECONDARY)
_TEXT_TERTIARY = QColor(*TEXT_HUD_TERTIARY)


class BreathingPacer(QWidget):
    """Breathing pacer animation widget. F48: cadence is read from
    :class:`cortex.libs.config.settings.InterventionConfig.breathing_pattern`
    at construction time and falls back to the 4-7-8 default if no config
    is supplied (test stubs, ad-hoc previews).

    Under Reduce Motion the pacer remains useful as a static breathing prompt:
    no timer runs, the disc stays at a fixed scale, and the decrementing
    countdown is replaced with ``at your pace``.

    Geometry unchanged from prior revision; only label fonts swap to the
    SF system stack."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        pattern: tuple[int, int, int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._active = False
        self._reduced_motion = False
        self._elapsed_ms = 0
        # F48: resolve the breathing pattern. Explicit ``pattern`` arg
        # wins (used by tests + future user-supplied profiles); otherwise
        # we read ``InterventionConfig.breathing_pattern`` from the
        # global config. If neither is available, fall back to 4-7-8.
        if pattern is None:
            try:
                from cortex.libs.config.settings import get_config

                pattern = tuple(
                    get_config().intervention.breathing_pattern
                )
            except Exception:
                pattern = _DEFAULT_BREATHING_PATTERN
        self._inhale, self._hold, self._exhale = pattern
        self._cycle = self._inhale + self._hold + self._exhale
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(BREATHING_PACER_SIZE, BREATHING_PACER_SIZE)

    def start(self, *, reduced_motion: bool = False) -> None:
        self._active = True
        self._reduced_motion = bool(reduced_motion)
        self._elapsed_ms = 0
        if self._reduced_motion:
            self._timer.stop()
            _set_accessible_name(
                self,
                "Breathing guide, breathe at your pace",
            )
        else:
            self._timer.start()
            _set_accessible_name(self, "Breathing guide")
        self.update()

    def stop(self) -> None:
        self._active = False
        self._reduced_motion = False
        self._timer.stop()
        self.update()

    @property
    def is_active(self) -> bool:
        return self._active

    def _tick(self) -> None:
        if self._reduced_motion:
            return
        self._elapsed_ms += 33
        self.update()

    def _get_phase(self) -> tuple[str, float, float]:
        cycle_pos = (self._elapsed_ms / 1000.0) % self._cycle
        if cycle_pos < self._inhale:
            progress = cycle_pos / self._inhale
            remaining = self._inhale - cycle_pos
            scale = 0.3 + 0.7 * progress
            return "Inhale", remaining, scale
        cycle_pos -= self._inhale
        if cycle_pos < self._hold:
            remaining = self._hold - cycle_pos
            return "Hold", remaining, 1.0
        cycle_pos -= self._hold
        progress = cycle_pos / self._exhale
        remaining = self._exhale - cycle_pos
        scale = 1.0 - 0.7 * progress
        return "Exhale", remaining, scale

    def _display_state(self) -> tuple[str, str, float]:
        """Return the two labels and scale for the current rendered frame."""
        if self._reduced_motion:
            return "Breathe", "at your pace", 0.46
        phase, remaining, scale = self._get_phase()
        return phase, f"{remaining:.0f}s", scale

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        cx, cy = w // 2, h // 2

        if not self._active:
            painter.setPen(_TEXT_SECONDARY)
            f = mac_native.system_font(FS_FOOTNOTE, "regular")
            if isinstance(f, QFont):
                painter.setFont(f)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Pacer")
            painter.end()
            return

        phase, secondary_label, scale = self._display_state()

        max_radius = min(w, h) // 2 - 10
        radius = int(max_radius * scale)
        for i in range(3):
            r = radius - i * 3
            if r < 5:
                break
            alpha = 100 - i * 25
            color = QColor(_ACCENT.red(), _ACCENT.green(), _ACCENT.blue(), alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        painter.setPen(_TEXT_PRIMARY)
        f = mac_native.system_font(FS_BODY, "semibold")
        if isinstance(f, QFont):
            painter.setFont(f)
        painter.drawText(
            QRect(0, cy - 20, w, 40),
            Qt.AlignmentFlag.AlignCenter,
            phase,
        )

        f = mac_native.system_font(FS_CAPTION, "regular")
        if isinstance(f, QFont):
            painter.setFont(f)
        painter.setPen(_TEXT_SECONDARY)
        painter.drawText(
            QRect(0, cy + 15, w, 30),
            Qt.AlignmentFlag.AlignCenter,
            secondary_label,
        )

        painter.end()


class _SparklineWidget(QWidget):
    """P0 §3.9: 60×24 px sparkline for one causal signal's 60-sample buffer."""

    def __init__(
        self,
        samples: list[float],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._samples = [float(v) for v in samples if isinstance(v, (int, float))]
        self.setFixedSize(60, 24)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, _event: object) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), QColor(255, 255, 255, 8))
            samples = self._samples
            if len(samples) < 2:
                return
            lo = min(samples)
            hi = max(samples)
            if hi <= lo:
                lo = lo - 1.0
                hi = lo + 2.0
            w = self.width() - 2
            h = self.height() - 4
            step = w / (len(samples) - 1)
            pen = QPen(QColor(217, 119, 87, 220))
            pen.setWidth(1)
            painter.setPen(pen)
            prev_x = 1.0
            prev_y = self.height() - 2 - (samples[0] - lo) / (hi - lo) * h
            for i in range(1, len(samples)):
                x = 1.0 + i * step
                y = self.height() - 2 - (samples[i] - lo) / (hi - lo) * h
                painter.drawLine(int(prev_x), int(prev_y), int(x), int(y))
                prev_x, prev_y = x, y
        finally:
            painter.end()


class OverlayWindow(QWidget):
    """Frameless always-on-top intervention overlay with native HUD styling."""

    dismissed = Signal(str)
    # G4 (audit-prod): emitted when the user clicks a suggested-action
    # button. Payload is ``(intervention_id, action_dict)`` matching the
    # ``SuggestedAction`` schema; the controller submits it to the daemon's
    # manifest-bound authorization path.
    action_invoked = Signal(str, dict)
    # P0 §3.6: emitted when the user toggles a micro-step checkbox.
    # Payload is ``(intervention_id, step_index, new_status)`` where
    # ``new_status`` ∈ {"pending", "done"}. The desktop controller
    # forwards this to ``RuntimeDaemon.toggle_micro_step`` which
    # mutates the active plan and rebroadcasts ``INTERVENTION_TRIGGER``
    # so peer surfaces (extension popup, VS Code panel) render the
    # strikethrough.
    micro_step_toggled = Signal(str, int, str)
    # P0 §3.8: emitted when the user clicks 👍 / 👎 on the active
    # intervention. Payload: ``(intervention_id, rating, text_feedback)``;
    # ``rating`` ∈ {"thumbs_up", "thumbs_down"}. ``text_feedback`` is
    # the empty string when no text was provided.
    rating_invoked = Signal(str, str, str)
    # P0 §3.9: emitted when the user requests the structured causal
    # rationale (clicks the "Why?" chevron). Payload is the active
    # intervention id; the controller fans out to a WHY_DETAIL_REQUEST.
    why_requested = Signal(str)
    # P0 §3.11: emitted when the user clicks the overlay's quiet/pause
    # footer buttons. Payload: ``(kind, duration_minutes_or_zero)``
    # where ``kind`` ∈ {"snooze_15", "quiet_session"} (Pause is
    # handled by the dashboard / tray surfaces, not the overlay
    # footer). The controller forwards to the daemon's
    # ``set_quiet_mode`` so QUIET_MODE_STATE re-broadcasts.
    quiet_requested = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._intervention_id = ""
        self._execution_mode = "suggest_only"
        # P0 §3.6: per-intervention checkbox cache. Keyed by
        # ``intervention_id``; value is the list of ``QCheckBox``
        # widgets currently in the steps_container. When a re-render
        # arrives for the SAME intervention_id, we sync ``status``
        # onto the existing checkboxes instead of destroying them, so
        # the user's tick survives F16 atomic-swap re-emissions.
        # When a fresh intervention_id arrives, the prior list is
        # torn down (new plan = fresh steps).
        self._step_intervention_id: str = ""
        self._step_status_cache: list[str] = []
        # Idempotency guard: once dismissed (auto or user), subsequent dismiss
        # calls are no-ops. First emitter wins. See F06.
        self._dismissed: bool = False
        # P0 §3.8 audit fix: per-intervention rating-row gate. ``True``
        # for guided_mode + simplified_workspace; ``False`` for
        # overlay_only minimal-tone interventions. Set in
        # ``show_intervention`` and consulted by ``_reveal_feedback_row``.
        self._wants_rating: bool = False

        # Frameless + always-on-top. The translucent Qt surface lets the
        # NSWindow's safe system background tint remain continuous beneath
        # the HUD scrim; no native content-view replacement is involved.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # A nudge is a notification, not a modal: it must never take
        # keyboard focus away from whatever the user is typing into.
        # Escape still works once the user has clicked into the card.
        _safe_call(self, "setAttribute", Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        _safe_call(self, "setFocusPolicy", Qt.FocusPolicy.StrongFocus)
        _set_accessible_name(self, "Cortex suggestion")
        _set_accessible_description(
            self,
            "A non-blocking suggestion from Cortex. It does not take focus; "
            "press Escape after clicking into it, or use the × button, to dismiss.",
        )

        # Notification width: the popup token (380 px). Height follows the
        # content so a one-line nudge stays small.
        self.setFixedWidth(POPUP_WIDTH)

        # Auto-timeout — 5 min per spec.
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(5 * 60 * 1000)
        self._timeout_timer.timeout.connect(self._auto_dismiss)

        # Phase J-4: animation slots. Created on demand inside
        # ``_play_show_animations`` so a Qt build without the animation
        # module never pays the import cost. Stashed on the instance so
        # back-to-back interventions reuse the same animation objects.
        self._headline_anim: object | None = None
        self._causal_fade_anim: object | None = None
        self._headline_opacity_effect: QGraphicsOpacityEffect | None = None
        self._causal_opacity_effect: QGraphicsOpacityEffect | None = None
        # Test affordance: when True, ``_play_show_animations`` records
        # the durations it would use without actually starting the
        # timers. Useful in offscreen tests where the real Qt event loop
        # is not free to tick at 16ms intervals.
        self._record_animations: bool = False
        self._last_animation_log: dict[str, int] = {}
        # Resolved once per presentation so the text cue and pacer cannot
        # disagree if the system preference changes between two probes.
        self._current_reduce_motion: bool = False

        self._build_ui()

    # -- Lifecycle hook: apply HUD vibrancy ------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, appearance="dark")
        except Exception:
            pass

    def hideEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        # Every hide path (dismiss, timeout, suppress, close) lands here, so
        # this is the single place that guarantees no presentation timer
        # keeps running against an invisible window.
        self._stop_presentation_timers()
        super().hideEvent(event)

    def suppress(self) -> None:
        """Hide without recording a user decision (pause / restore).

        Stops the pacer, the five-minute timeout, the movement countdown,
        the rating-reveal timer, and any in-flight text animation. Use this
        instead of ``hide()`` from host code so a hidden overlay never
        keeps ticking.
        """
        self._stop_presentation_timers()
        self.hide()

    def _stop_presentation_timers(self) -> None:
        for timer_name in ("_timeout_timer", "_feedback_reveal_timer"):
            timer = getattr(self, timer_name, None)
            stop = getattr(timer, "stop", None)
            if callable(stop):
                try:
                    stop()
                except RuntimeError:
                    pass
        pacer = getattr(self, "_pacer", None)
        if pacer is not None:
            try:
                pacer.stop()
            except Exception:
                logger.debug("pacer stop failed", exc_info=True)
        movement = getattr(self, "_inline_movement_timer", None)
        if movement is not None:
            try:
                movement.stop()
            except Exception:
                logger.debug("movement timer stop failed", exc_info=True)
        for animation in (
            getattr(self, "_headline_anim", None),
            getattr(self, "_causal_fade_anim", None),
        ):
            stop = getattr(animation, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    logger.debug("text animation stop failed", exc_info=True)
        try:
            self._reset_text_opacity_to_full()
        except Exception:
            pass

    @property
    def workspace_actions_enabled(self) -> bool:
        """Whether the current proposal permits action-button dispatch."""
        return self._execution_mode in ("authorized", "research_autonomous")

    def _build_ui(self) -> None:
        self._main_layout = QVBoxLayout(self)
        # Slim scrim margin: the card is the notification; the window only
        # carries a faint shadow around it.
        self._main_layout.setContentsMargins(SP2, SP2, SP2, SP2)

        # Card — translucent dark surface that layers on top of the HUD
        # material below. The 10% white border picks out the card edge.
        self._card = QFrame()
        self._card.setObjectName("CortexOverlayCard")
        self._card.setStyleSheet(
            "QFrame#CortexOverlayCard {"
            "  background-color: rgba(30, 30, 32, 0.92);"
            f"  border-radius: {RADIUS_WINDOW}px;"
            "  border: 1px solid rgba(255, 255, 255, 0.10);"
            "}"
        )
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(SP4, SP4, SP4, SP4)
        card_layout.setSpacing(SP3)

        # Header row: headline (system face — the display serif is reserved
        # for the wordmark and hero numerals) + × dismiss at top-right.
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(SP2)
        self._headline = QLabel("—")
        self._headline.setFont(mac_native.system_font(FS_TITLE, "semibold"))
        self._headline.setStyleSheet(
            f"font-size: {FS_TITLE}px;"
            f"font-weight: {FW_SEMIBOLD};"
            f"color: {_TEXT_PRIMARY.name()};"
            "background: transparent;"
        )
        self._headline.setWordWrap(True)
        self._headline.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        header_row.addWidget(self._headline, 1)

        self._dismiss_btn = QPushButton("×")
        self._dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _safe_call(self._dismiss_btn, "setFixedSize", 24, 24)
        _set_accessible_name(self._dismiss_btn, "Dismiss suggestion")
        _set_accessible_description(
            self._dismiss_btn,
            "Close this suggestion. Keyboard shortcut: Escape when the card has focus.",
        )
        _safe_call(self._dismiss_btn, "setToolTip", "Dismiss (Escape)")
        self._dismiss_btn.setFont(mac_native.system_font(FS_BODY, "regular"))
        self._dismiss_btn.setStyleSheet(self._icon_btn_stylesheet())
        self._dismiss_btn.clicked.connect(self._user_dismiss)
        header_row.addWidget(
            self._dismiss_btn, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )
        card_layout.addLayout(header_row)

        # P0 §3.5: cognitive-state pill — small label below the headline
        # that picks up the FLOW/HYPER/HYPO/RECOVERY palette + label.
        # Hidden by default; ``show_intervention`` sets visibility based on
        # ``payload["state"]`` / ``payload["cognitive_state"]``.
        state_pill_row = QHBoxLayout()
        state_pill_row.setContentsMargins(0, 0, 0, 0)
        state_pill_row.setSpacing(SP2)
        state_pill_row.addStretch(1)
        self._state_pill = QLabel("")
        self._state_pill.setFont(
            mac_native.system_font(FS_CAPTION, "semibold")
        )
        self._state_pill.setObjectName("CortexOverlayStatePill")
        self._state_pill.hide()
        self._state_pill.setStyleSheet(
            "QLabel#CortexOverlayStatePill {"
            "  padding: 2px 10px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  color: {_TEXT_PRIMARY.name()};"
            "  background: rgba(255,255,255,0.08);"
            "  border: 1px solid rgba(255,255,255,0.18);"
            "}"
        )
        state_pill_row.addWidget(self._state_pill)
        state_pill_row.addStretch(1)
        card_layout.addLayout(state_pill_row)

        # F27 (audit): fallback / offline-mode hint. Shown only when the
        # plan was produced by the rule-based fallback path (LLM circuit
        # open, retries exhausted, or daily budget killed). Placed
        # directly below the headline so the user sees the degradation
        # before they read the rest. Distinct widget below the headline
        # is intentional — coordinates with the F29 truncation affordance
        # which lands next to the causal explanation.
        self._fallback_hint = QLabel("")
        self._fallback_hint.setFont(
            mac_native.system_font(FS_CAPTION, "medium")
        )
        self._fallback_hint.setStyleSheet(
            "color: rgba(255, 224, 178, 0.85);"  # warm amber for "degraded"
            "background: transparent;"
        )
        self._fallback_hint.setObjectName("CortexOverlayFallbackHint")
        self._fallback_hint.setWordWrap(True)
        self._fallback_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fallback_hint.hide()
        card_layout.addWidget(self._fallback_hint)

        # Situation summary — SF system, FN-size, secondary alpha.
        self._summary = QLabel("")
        self._summary.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._summary.setStyleSheet(
            f"color: {_TEXT_SECONDARY.name()};"
            "background: transparent; line-height: 1.5;"
        )
        self._summary.setWordWrap(True)
        card_layout.addWidget(self._summary)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(
            "background-color: rgba(255, 255, 255, 0.10); max-height: 1px;"
        )
        card_layout.addWidget(divider)

        # Primary focus.
        self._focus_label = QLabel("Focus:")
        self._focus_label.setFont(mac_native.system_font(FS_BODY, "semibold"))
        self._focus_label.setStyleSheet(
            f"color: {_ACCENT.name()}; background: transparent;"
        )
        self._focus_label.setWordWrap(True)
        card_layout.addWidget(self._focus_label)

        # Micro-steps checklist.
        self._steps_container = QVBoxLayout()
        self._steps_container.setSpacing(SP3)
        self._step_widgets: list[QCheckBox] = []
        card_layout.addLayout(self._steps_container)

        # G4 (audit-prod): suggested-action buttons. Rendered in
        # ``show_intervention`` whenever the plan carries a non-empty
        # ``suggested_actions`` list. Each click emits ``action_invoked`` and
        # only a manifest-listed, reversible remote executor may act on it.
        self._actions_container = QVBoxLayout()
        self._actions_container.setSpacing(SP2)
        self._action_buttons: list[QPushButton] = []
        # Sentinel caption shown when the plan contains browser-bound
        # actions but no Chrome/Edge client is currently identified.
        self._actions_caption = QLabel("")
        self._actions_caption.setFont(
            mac_native.system_font(FS_CAPTION, "regular")
        )
        self._actions_caption.setStyleSheet(
            f"color: {_TEXT_TERTIARY.name()};"
            "background: transparent;"
            "font-style: italic;"
        )
        self._actions_caption.setWordWrap(True)
        self._actions_caption.hide()
        card_layout.addLayout(self._actions_container)
        card_layout.addWidget(self._actions_caption)

        # P0 §3.5: container for inline-revealed widgets (micro-commit
        # text input, movement-break countdown card). Hidden until an
        # action with ``action_type in _INLINE_WIDGET_ACTIONS`` is
        # clicked; teardown happens on dismiss or fresh intervention.
        self._inline_container = QVBoxLayout()
        self._inline_container.setSpacing(SP3)
        self._inline_widgets: list[QWidget] = []
        self._inline_movement_timer: QTimer | None = None
        self._inline_movement_remaining: int = 0
        card_layout.addLayout(self._inline_container)

        # Single "Why this?" affordance. The toggle below reveals one panel
        # holding the plain-language causal explanation AND the structured
        # signal rows (P0 §3.9), so the card never shows two competing
        # "why" controls. Hidden until the plan supplies either.
        self._causal_label = QLabel("")
        self._causal_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._causal_label.setStyleSheet(
            f"color: {_TEXT_SECONDARY.name()};"
            "background: transparent;"
        )
        self._causal_label.setWordWrap(True)
        self._causal_label.hide()

        # F29 (audit): "Show more context" affordance. Surfaces only when
        # the daemon stamped ``context_truncated_sections`` onto the
        # plan's metadata, i.e. when the prompt assembler had to trim
        # one or more sections to fit the token budget.
        self._context_truncation_label = QLabel("")
        self._context_truncation_label.setObjectName(
            "CortexContextTruncationAffordance"
        )
        self._context_truncation_label.setFont(
            mac_native.system_font(FS_CAPTION, "medium")
        )
        self._context_truncation_label.setStyleSheet(
            "QLabel#CortexContextTruncationAffordance {"
            "  color: rgba(217, 119, 87, 0.95);"  # terracotta accent
            "  background: transparent;"
            "  text-decoration: underline;"
            "}"
        )
        self._context_truncation_label.setWordWrap(True)
        self._context_truncation_label.hide()
        card_layout.addWidget(self._context_truncation_label)

        # The one "Why this?" toggle. Checked → the panel below shows the
        # full explanation + signal rows; unchecked → collapsed.
        self._causal_full_text: str = ""
        self._causal_toggle = QToolButton()
        _safe_call(self._causal_toggle, "setCheckable", True)
        _safe_call(self._causal_toggle, "setText", "Why this?")
        _set_accessible_name(self._causal_toggle, "Why Cortex suggested this")
        _set_accessible_description(
            self._causal_toggle,
            "Shows the explanation and the signals behind this suggestion.",
        )
        _safe_call(self._causal_toggle, "setCursor", Qt.CursorShape.PointingHandCursor)
        _safe_call(self._causal_toggle, "setFocusPolicy", Qt.FocusPolicy.StrongFocus)
        _safe_call(
            self._causal_toggle,
            "setStyleSheet",
            (
                "QToolButton, QPushButton {"
                f"  color: {_TEXT_SECONDARY.name()};"
                "  background: transparent;"
                "  border: 2px solid transparent;"
                f"  border-radius: {RADIUS_BUTTON}px;"
                "  padding: 2px 6px;"
                f"  font-size: {FS_CAPTION}px;"
                "  text-decoration: underline;"
                "}"
                "QToolButton:hover, QPushButton:hover { color: white; }"
                "QToolButton:pressed, QPushButton:pressed {"
                "  background: rgba(255, 255, 255, 0.10); }"
                "QToolButton:focus, QPushButton:focus {"
                "  border-color: rgba(255, 255, 255, 0.45); }"
            ),
        )
        # The toggled signal exists on real QToolButton / QPushButton;
        # the MockQPushButton stub does not expose it. Hook only when
        # available.
        toggled_sig = getattr(self._causal_toggle, "toggled", None)
        if toggled_sig is not None and hasattr(toggled_sig, "connect"):
            try:
                toggled_sig.connect(self._on_causal_toggled)
            except Exception:
                pass
        _safe_call(self._causal_toggle, "hide")
        card_layout.addWidget(self._causal_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        # Compatibility alias: older call sites (and the controller's
        # WHY_DETAIL plumbing) refer to ``_why_toggle``; it is the same
        # control now.
        self._why_toggle = self._causal_toggle

        # Breathing pacer — shown only for breathing-type plans.
        pacer_layout = QHBoxLayout()
        pacer_layout.addStretch()
        self._pacer = BreathingPacer()
        pacer_layout.addWidget(self._pacer)
        pacer_layout.addStretch()
        card_layout.addLayout(pacer_layout)

        # P0 §3.11: footer row — Snooze 15 min / Quiet for this session.
        # Dismiss lives in the header (×). The dashboard owns the Pause
        # affordance (it must release the camera and orchestrate
        # cross-surface state); the overlay keeps the moment-of-
        # intervention escape valves.
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(SP3)

        # Overlay-only 15 min suppression. The daemon keeps sensing but no
        # new overlays fire during the window. Shortcuts are Command-
        # modified so a stray keystroke can never snooze or quiet Cortex.
        self._snooze_btn = QPushButton("Snooze 15 min")
        self._snooze_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _set_accessible_name(self._snooze_btn, "Snooze suggestions for 15 minutes")
        _set_accessible_description(
            self._snooze_btn,
            "Hide suggestions for 15 minutes. Shortcut: Command-Shift-S "
            "while this card has focus.",
        )
        _safe_call(self._snooze_btn, "setToolTip", "Snooze 15 minutes (⌘⇧S)")
        self._snooze_btn.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._snooze_btn.setStyleSheet(self._footer_btn_stylesheet())
        self._snooze_btn.clicked.connect(self._on_snooze_clicked)
        try:
            self._snooze_btn.setShortcut("Ctrl+Shift+S")
        except Exception:
            pass
        footer_row.addWidget(self._snooze_btn, 1)

        # Overlay-only suppression until the user clears it (or the
        # session ends).
        self._quiet_session_btn = QPushButton("Quiet for this session")
        self._quiet_session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _set_accessible_name(
            self._quiet_session_btn,
            "Quiet suggestions for the rest of this session",
        )
        _set_accessible_description(
            self._quiet_session_btn,
            "Hide suggestions for the rest of this session. Shortcut: "
            "Command-Shift-M while this card has focus.",
        )
        _safe_call(self._quiet_session_btn, "setToolTip", "Quiet for this session (⌘⇧M)")
        self._quiet_session_btn.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._quiet_session_btn.setStyleSheet(self._footer_btn_stylesheet())
        self._quiet_session_btn.clicked.connect(self._on_quiet_session_clicked)
        try:
            self._quiet_session_btn.setShortcut("Ctrl+Shift+M")
        except Exception:
            pass
        footer_row.addWidget(self._quiet_session_btn, 1)
        card_layout.addLayout(footer_row)

        # P0 §3.8: feedback row — 👍 / 👎 buttons rendered after any
        # action click OR 30 s after the overlay shows, whichever comes
        # first. ``_feedback_row`` is hidden by default; the timer
        # below reveals it.
        self._feedback_row = QFrame()
        self._feedback_row.setObjectName("CortexOverlayFeedbackRow")
        self._feedback_row.setStyleSheet(
            "QFrame#CortexOverlayFeedbackRow { background: transparent; }"
        )
        feedback_layout = QHBoxLayout(self._feedback_row)
        feedback_layout.setContentsMargins(0, 0, 0, 0)
        feedback_layout.setSpacing(SP3)
        feedback_layout.addStretch()
        self._thumbs_up_btn = QPushButton("👍")
        _set_accessible_name(self._thumbs_up_btn, "Mark helpful")
        self._thumbs_up_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumbs_up_btn.setStyleSheet(self._feedback_btn_stylesheet())
        self._thumbs_up_btn.clicked.connect(self._on_thumbs_up)
        feedback_layout.addWidget(self._thumbs_up_btn)
        self._thumbs_down_btn = QPushButton("👎")
        _set_accessible_name(self._thumbs_down_btn, "Mark unhelpful")
        self._thumbs_down_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumbs_down_btn.setStyleSheet(self._feedback_btn_stylesheet())
        self._thumbs_down_btn.clicked.connect(self._on_thumbs_down)
        feedback_layout.addWidget(self._thumbs_down_btn)
        feedback_layout.addStretch()
        self._feedback_row.hide()
        card_layout.addWidget(self._feedback_row)

        # P0 §3.8: optional one-line text input shown after 👎. Enter
        # commits, Esc skips. Hidden by default; revealed in
        # ``_on_thumbs_down``.
        self._feedback_text = QLineEdit()
        self._feedback_text.setObjectName("CortexOverlayFeedbackText")
        self._feedback_text.setPlaceholderText(
            "What would have helped? (Enter to send, Esc to skip)"
        )
        self._feedback_text.setStyleSheet(
            "QLineEdit#CortexOverlayFeedbackText {"
            "  background: rgba(255, 255, 255, 0.05);"
            "  color: rgba(232, 222, 207, 0.92);"
            f"  border: 1px solid {_ACCENT.name()}55;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 10px;"
            f"  font-size: {FS_CAPTION}px;"
            "}"
        )
        self._feedback_text.returnPressed.connect(self._commit_feedback_text)
        self._feedback_text.hide()
        card_layout.addWidget(self._feedback_text)

        # P0 §3.8: schedule a one-shot reveal of the feedback row 30 s
        # after the overlay shows. The action_invoked handler also
        # reveals the row immediately so the user gets the affordance
        # right after engagement.
        self._feedback_reveal_timer = QTimer(self)
        self._feedback_reveal_timer.setSingleShot(True)
        self._feedback_reveal_timer.setInterval(30 * 1000)
        self._feedback_reveal_timer.timeout.connect(self._reveal_feedback_row)

        # P0 §3.9: the "Why this?" panel — plain-language explanation on
        # top, then one row per causal signal with a tiny sparkline and a
        # delta pill. Toggled by ``_causal_toggle`` above.
        self._why_panel = QFrame()
        self._why_panel.setObjectName("CortexOverlayWhyPanel")
        self._why_panel.setStyleSheet(
            "QFrame#CortexOverlayWhyPanel {"
            "  background-color: rgba(255, 255, 255, 0.04);"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "}"
        )
        why_outer = QVBoxLayout(self._why_panel)
        why_outer.setContentsMargins(SP2, SP2, SP2, SP2)
        why_outer.setSpacing(SP2)
        why_outer.addWidget(self._causal_label)
        self._why_signals_host = QWidget()
        self._why_signals_host.setStyleSheet("background: transparent;")
        self._why_panel_layout = QVBoxLayout(self._why_signals_host)
        self._why_panel_layout.setContentsMargins(0, 0, 0, 0)
        self._why_panel_layout.setSpacing(SP1)
        why_outer.addWidget(self._why_signals_host)
        self._why_panel.hide()
        card_layout.addWidget(self._why_panel)
        self._causal_signals_cache: list[dict] = []
        self._why_open: bool = False

        self._main_layout.addWidget(self._card)

        # F55: explicit tab-order chain. Without setTabOrder, Qt falls
        # back to widget-creation order which the cascading micro-step
        # rebuilds in show_intervention can scramble. Causal toggle (if
        # surfaced) comes between the steps and the footer pills.
        _set_tab_order(self._causal_toggle, self._snooze_btn)
        _set_tab_order(self._snooze_btn, self._quiet_session_btn)
        _set_tab_order(self._quiet_session_btn, self._dismiss_btn)

    # ------------------------------------------------------------------
    # Public API (preserved byte-identical)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # P0 §3.5: cognitive-state visual distinction
    # ------------------------------------------------------------------

    # Friendly label for the small text next to the state pill. Falls back
    # to the title-cased upper-case state name if the lookup misses.
    _STATE_FRIENDLY_LABEL: dict[str, str] = {
        "HYPO": "Idle",
        "RECOVERY": "Recovering",
        "HYPER": "Elevated",
        "FLOW": "Flow",
    }
    # Tiny glyph next to each state. Picked from the unicode set so we
    # don't ship a vector asset just for four pills.
    _STATE_GLYPH: dict[str, str] = {
        "HYPO": "·",
        "RECOVERY": "↻",
        "HYPER": "▲",
        "FLOW": "◇",
    }

    def _apply_state_visual(self, payload: dict) -> None:
        """Read the cognitive state off ``payload`` and update the pill.

        The state is the canonical upper-case key (FLOW / HYPER / HYPO /
        RECOVERY); we accept either ``payload["state"]`` (matches the
        STATE_UPDATE wire format) or ``payload["cognitive_state"]`` (an
        older field name some plans still carry). Missing / unknown
        values collapse the pill so we never lie about the state.
        """
        raw = payload.get("state") or payload.get("cognitive_state") or ""
        kind = str(raw).upper().strip()
        if kind not in STATE_LABELS or kind == "UNKNOWN":
            try:
                self._state_pill.hide()
            except Exception:
                pass
            return
        color = active_state_color(kind)
        # Prefer the canonical label from tokens, fall back to our local
        # friendly label set so the audit's "Idle/Recovering/Elevated/Flow"
        # copy ships even if the token map drops a key.
        label = STATE_LABELS.get(kind) or self._STATE_FRIENDLY_LABEL.get(kind, kind.title())
        glyph = self._STATE_GLYPH.get(kind, "•")
        try:
            self._state_pill.setText(f"{glyph}  {label}")
            self._state_pill.setStyleSheet(
                "QLabel#CortexOverlayStatePill {"
                "  padding: 2px 10px;"
                f"  border-radius: {RADIUS_BUTTON}px;"
                f"  color: {color};"
                "  background: rgba(255,255,255,0.10);"
                f"  border: 1px solid {color};"
                "}"
            )
            self._state_pill.show()
        except Exception:
            logger.debug("state pill update failed", exc_info=True)

    def show_intervention(self, payload: dict) -> None:
        self._intervention_id = payload.get("intervention_id", "")
        raw_execution_mode = payload.get("execution_mode", "suggest_only")
        self._execution_mode = (
            str(raw_execution_mode)
            if raw_execution_mode in ("authorized", "research_autonomous")
            else "suggest_only"
        )
        # Fresh intervention — clear dismissed flag so this one can dismiss.
        self._dismissed = False
        self._current_reduce_motion = self._reduce_motion_enabled()

        self._headline.setText(payload.get("headline", "Take a moment"))
        self._summary.setText(payload.get("situation_summary", ""))
        self._focus_label.setText(
            f"Focus: {payload.get('primary_focus', '')}"
        )

        # P0 §3.5: visualise cognitive state via a small pill + tinted
        # border on the headline. The palette is keyed by the upper-case
        # state name (FLOW/HYPER/HYPO/RECOVERY) via the active (possibly
        # colour-blind) palette; an unknown state collapses the pill.
        self._apply_state_visual(payload)

        # P0 §3.5: collapse any inline widget left over from a prior
        # intervention so the new card starts clean.
        self._clear_inline_widgets()

        # F27 (audit): show the offline-mode hint when the daemon
        # stamped ``metadata["source"] = "fallback"``. Hide otherwise so
        # successful LLM plans look identical to before.
        metadata = payload.get("metadata") or {}
        if (
            isinstance(metadata, dict)
            and metadata.get("source") == "fallback"
        ):
            reason = str(metadata.get("fallback_reason") or "")
            if reason == "budget_killed":
                hint = (
                    "Cortex offline mode — daily AI budget reached; "
                    "using rule-based suggestions."
                )
            elif reason == "circuit_open":
                hint = (
                    "Cortex offline mode — Claude unreachable; "
                    "using rule-based suggestions."
                )
            else:
                hint = "Cortex offline mode — using rule-based suggestions."
            self._fallback_hint.setText(hint)
            self._fallback_hint.show()
        else:
            self._fallback_hint.clear()
            self._fallback_hint.hide()

        # P0 §3.6: only tear down the existing checkboxes when this is
        # a fundamentally new intervention. F16 atomic-swap re-emissions
        # (same intervention_id, possibly mutated step list) are handled
        # by ``_render_micro_steps`` below, which reuses existing widgets
        # so the user's checked state survives.
        if self._step_intervention_id != self._intervention_id:
            for cb in self._step_widgets:
                self._steps_container.removeWidget(cb)
                cb.deleteLater()
            self._step_widgets.clear()
            self._step_status_cache = []
            self._step_intervention_id = self._intervention_id

        causal = str(payload.get("causal_explanation") or "").strip()
        # Only surface causal explanations with substantive content; the
        # 20-char filter is preserved for the show/hide gate.
        if causal and len(causal) > 20:
            self._show_causal_explanation(causal)
        else:
            self._hide_causal_explanation()

        # F29 (audit): surface a "Show more context" affordance only when
        # the daemon trimmed sections to fit the token budget. The
        # affordance copy names the dominant section so the user knows
        # which slice of context they could expand. ``metadata`` is
        # free-form on the wire; we guard against non-list values.
        meta = payload.get("metadata") or {}
        truncated_sections = meta.get("context_truncated_sections") if isinstance(meta, dict) else None
        if isinstance(truncated_sections, list) and truncated_sections:
            primary = str(truncated_sections[0]).replace("_", " ")
            self._context_truncation_label.setText(
                f"Cortex saw only the first portion of your {primary}. "
                "Show more context →"
            )
            self._context_truncation_label.show()
        else:
            self._context_truncation_label.setText("")
            self._context_truncation_label.hide()

        # P0 §3.6: render micro-steps with state preservation across
        # F16 atomic-swap re-emissions. ``_render_micro_steps`` consumes
        # both legacy ``list[str]`` and the new ``list[dict]`` shape
        # (``{text, status, started_at, completed_at}``) and wires each
        # checkbox's ``toggled`` signal to the daemon.
        self._render_micro_steps(payload.get("micro_steps", []) or [])

        # G4 (audit-prod): render the suggested_actions as clickable
        # buttons. ``_render_actions`` clears the prior list, builds new
        # QPushButtons, and wires each click to emit ``action_invoked``.
        try:
            self._render_actions(
                payload.get("suggested_actions") or [],
                connected_clients=payload.get("connected_clients") or [],
                execution_mode=self._execution_mode,
                action_manifest=payload.get("action_manifest"),
            )
        except Exception:
            logger.debug("Action rendering failed", exc_info=True)

        # The pacer is a breathing guide, so it appears only when the plan
        # is actually about breathing. Any other nudge stays a compact
        # text card.
        show_pacer = self._plan_is_breathing(payload)

        self._timeout_timer.start()

        # P0 §3.8: reset and re-schedule the feedback reveal timer.
        # Rendering hidden by default; reveals after 30 s or an action click.
        # P0 §3.8 audit fix (spec line 710): only schedule the reveal
        # on guided_mode + simplified_workspace overlays. Minimal-tone
        # overlay_only interventions stay ambient and never solicit
        # ratings. The instance flag is read again from
        # ``_reveal_feedback_row`` so a tail action click can't surface
        # the row either.
        level = str(payload.get("level") or "")
        self._wants_rating = level in ("guided_mode", "simplified_workspace")
        try:
            self._feedback_reveal_timer.stop()
            self._feedback_row.hide()
            self._feedback_text.hide()
            self._feedback_text.clear()
            if self._wants_rating:
                self._feedback_reveal_timer.start()
        except Exception:
            logger.debug("feedback reset failed", exc_info=True)

        # P0 §3.9: ingest structured causal signals (idempotent; empty
        # list collapses the Why? affordance entirely).
        try:
            signals = payload.get("causal_signals") or []
            self.apply_causal_signals(signals if isinstance(signals, list) else [])
        except Exception:
            logger.debug("apply_causal_signals failed", exc_info=True)

        # P2-FEAT-SCREENSHARE (cortex.md:927): suppress the always-on-top
        # intervention overlay while the screen is being shared/recorded
        # so private nudges aren't broadcast to a meeting. Conservative —
        # only suppress when detection is BOTH positive AND available;
        # ``screen_share_active`` returns False on any uncertainty. The
        # card content was still prepared above, so the next, non-shared
        # intervention shows instantly.
        if self._screen_share_active():
            # A privacy transition can arrive while an earlier intervention is
            # visible. Hide the complete window and stop every presentation
            # timer; merely skipping the new ``show`` would leave old private
            # content on the captured display.
            self._stop_feedback_reveal_timer()
            self._pacer.hide()
            self.suppress()
            logger.info(
                "Overlay suppressed for intervention %s — screen sharing active",
                self._intervention_id,
            )
            return

        if show_pacer:
            self._pacer.start(reduced_motion=self._current_reduce_motion)
            self._pacer.show()
        else:
            self._pacer.stop()
            self._pacer.hide()

        # Anchor as a notification: top-right of the display under the
        # cursor (FE-3 multi-monitor), sized to the content. Never
        # ``activateWindow()`` — the card must not steal focus from the
        # app the user is working in.
        self._place_as_notification()
        self.show()
        self.raise_()

        # Phase J-4: concurrent opacity feedback for newly presented text.
        # Skipped under Reduce Motion or when Qt lacks QPropertyAnimation.
        # The breathing pacer uses the same preference to select its static
        # guidance state; controls do not move.
        self._play_show_animations()

        logger.info(f"Overlay shown for intervention {self._intervention_id}")

    @staticmethod
    def _plan_is_breathing(payload: dict) -> bool:
        """True when the plan is a breathing exercise (pacer is useful).

        Honest signals: a ``take_biology_break`` suggested action, an
        explicit ``breathing_pattern`` on the payload, or copy in the
        headline / focus / micro-steps that talks about breathing.
        """
        actions = payload.get("suggested_actions") or []
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict) and (
                    str(action.get("action_type") or "") == "take_biology_break"
                ):
                    return True
        if payload.get("breathing_pattern"):
            return True
        texts: list[str] = [
            str(payload.get("headline") or ""),
            str(payload.get("primary_focus") or ""),
        ]
        steps = payload.get("micro_steps") or []
        if isinstance(steps, list):
            for step in steps:
                texts.append(
                    str(step.get("text") or "") if isinstance(step, dict) else str(step)
                )
        return any("breath" in text.lower() for text in texts)

    def _place_as_notification(self) -> None:
        """Size to content and anchor to the top-right of the cursor's screen."""
        try:
            self.adjustSize()
        except Exception:
            pass
        screen = self._target_screen()
        if screen is None:
            return
        try:
            geo = screen.availableGeometry()
            width = min(POPUP_WIDTH, max(200, geo.width() - 2 * SP4))
            if width != self.width():
                self.setFixedWidth(width)
                self.adjustSize()
            height = min(self.height(), max(120, geo.height() - 2 * SP4))
            if height != self.height():
                self.resize(width, height)
            self.move(geo.right() - self.width() - SP4 + 1, geo.top() + SP4)
        except Exception:
            logger.debug("overlay placement failed", exc_info=True)

    def _target_screen(self) -> Any:
        """FE-3 (P2-FE-MULTIMON): the screen the overlay should land on.

        Prefers the display under the cursor (``QGuiApplication.screenAt(
        QCursor.pos())``) so a multi-monitor user sees the nudge where
        they are looking, not on whatever display the still-hidden window
        happens to report. Falls back to ``self.screen()`` and then the
        primary screen. Returns ``None`` only when none of those resolve
        (e.g. headless) — callers already guard against ``None``.
        """
        screen = None
        if QGuiApplication is not None and QCursor is not None:
            try:
                screen = QGuiApplication.screenAt(QCursor.pos())
            except Exception:
                logger.debug("screenAt(cursor) failed", exc_info=True)
                screen = None
        if screen is None:
            try:
                screen = self.screen()
            except Exception:
                screen = None
        if screen is None and QGuiApplication is not None:
            try:
                screen = QGuiApplication.primaryScreen()
            except Exception:
                logger.debug("primaryScreen() failed", exc_info=True)
                screen = None
        return screen

    def _screen_share_active(self) -> bool:
        """P2-FEAT-SCREENSHARE: True when a display is being captured /
        recorded / mirrored. Thin wrapper around
        :func:`mac_native.screen_share_active` so tests can monkeypatch
        the detector on the instance and so any failure degrades to
        "not sharing" (show the overlay) rather than hiding it."""
        try:
            return bool(mac_native.screen_share_active())
        except Exception:
            logger.debug("screen_share_active probe failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Phase J-4: micro-interactions
    # ------------------------------------------------------------------

    def _play_show_animations(self) -> None:
        """Settle readable support text together with a 160 ms opacity cue.

        Honours the macOS "Reduce Motion" accessibility preference: when
        enabled, both labels render at full opacity immediately.  Geometry is
        never animated: the card is actionable on its first frame and rapid
        replacement cannot reflow or squash text.

        Defensive: short-circuits when the Qt build lacks the animation
        classes (the lightweight test stubs) or when the headline /
        causal widgets are unavailable. The end state is always applied
        so the UI never gets stuck in a half-animated state.
        """
        # Always record the durations we *would* use so the unit test
        # can assert against the wired-up constants without spinning a
        # real event loop.
        reduced = self._current_reduce_motion
        if reduced:
            headline_ms = 0
            causal_ms = 0
        else:
            headline_ms = HEADLINE_FADE_DURATION_MS
            causal_ms = CAUSAL_FADE_DURATION_MS
        self._last_animation_log = {
            "headline_ms": headline_ms,
            "causal_ms": causal_ms,
            "reduce_motion": int(reduced),
        }

        if self._record_animations:
            # Test mode: capture the contract and return without
            # touching the real animation classes.
            return

        if reduced or not _ANIMATION_AVAILABLE:
            self._reset_text_opacity_to_full()
            return

        # Stop a prior run before retargeting. QPropertyAnimation otherwise
        # keeps the old opacity writer alive during a back-to-back update.
        for prior in (self._headline_anim, self._causal_fade_anim):
            stop = getattr(prior, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    logger.debug("Prior text animation stop failed", exc_info=True)

        try:
            headline_effect = self._headline_opacity_effect
            if headline_effect is None:
                headline_effect = QGraphicsOpacityEffect(self._headline)
                self._headline.setGraphicsEffect(headline_effect)
                self._headline_opacity_effect = headline_effect
            headline_effect.setOpacity(0.72)
            headline_fade = QPropertyAnimation(headline_effect, b"opacity")
            headline_fade.setDuration(HEADLINE_FADE_DURATION_MS)
            headline_fade.setStartValue(0.72)
            headline_fade.setEndValue(1.0)
            headline_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._headline_anim = headline_fade
            headline_fade.start()

            if getattr(self, "_causal_label", None):
                causal_effect = self._causal_opacity_effect
                if causal_effect is None:
                    causal_effect = QGraphicsOpacityEffect(self._causal_label)
                    self._causal_label.setGraphicsEffect(causal_effect)
                    self._causal_opacity_effect = causal_effect
                causal_effect.setOpacity(0.72)
                causal_fade = QPropertyAnimation(causal_effect, b"opacity")
                causal_fade.setDuration(CAUSAL_FADE_DURATION_MS)
                causal_fade.setStartValue(0.72)
                causal_fade.setEndValue(1.0)
                causal_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
                self._causal_fade_anim = causal_fade
                causal_fade.start()
        except Exception:
            logger.debug("Text opacity animation failed", exc_info=True)
            self._reset_text_opacity_to_full()

    def _reduce_motion_enabled(self) -> bool:
        """Wrapper around :func:`mac_native.prefers_reduced_motion` so a
        test can monkeypatch the predicate without reaching into
        ``mac_native``."""
        try:
            return bool(mac_native.prefers_reduced_motion())
        except Exception:
            return False

    def _reset_text_opacity_to_full(self) -> None:
        """Restore both text effects after reduce-motion or animation failure."""

        for effect in (
            self._headline_opacity_effect,
            self._causal_opacity_effect,
        ):
            if effect is None:
                continue
            try:
                effect.setOpacity(1.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # "Why this?" — one toggle, one panel
    # ------------------------------------------------------------------

    def _show_causal_explanation(self, causal: str) -> None:
        """Stash the explanation in the Why panel and expose the toggle.

        The full text is always available behind the single toggle — the
        card never shows a truncated preview competing with the panel.
        """
        self._causal_full_text = causal.strip()
        self._causal_label.setText(self._causal_full_text)
        self._causal_label.show()
        self._sync_why_affordance()

    def _hide_causal_explanation(self) -> None:
        """Reset the causal slot back to its empty / hidden state."""
        self._causal_full_text = ""
        self._causal_label.setText("")
        self._causal_label.hide()
        self._sync_why_affordance()

    def _sync_why_affordance(self) -> None:
        """Show the toggle only when there is something to reveal; collapse
        the panel whenever the toggle is unchecked or nothing remains."""
        has_content = bool(self._causal_full_text) or bool(self._causal_signals_cache)
        try:
            if has_content:
                self._causal_toggle.show()
            else:
                self._causal_toggle.setChecked(False)
                self._causal_toggle.hide()
                self._why_open = False
                self._why_panel.hide()
        except Exception:
            logger.debug("why affordance sync failed", exc_info=True)

    def _on_causal_toggled(self, checked: bool) -> None:
        """Handler for the single "Why this?" toggle."""
        self._why_open = bool(checked)
        if checked:
            self._causal_toggle.setText("Hide why")
            self._why_panel.show()
            if not self._causal_signals_cache and self._intervention_id:
                try:
                    self.why_requested.emit(self._intervention_id)
                except Exception:
                    logger.debug("why_requested.emit failed", exc_info=True)
        else:
            self._causal_toggle.setText("Why this?")
            self._why_panel.hide()

    def keyPressEvent(self, event: object) -> None:
        # Escape dismisses whenever the card (or a child) holds focus. The
        # card never activates on show, so this is opt-in: the user has
        # clicked into it first.
        if hasattr(event, "key") and event.key() == Qt.Key.Key_Escape:
            self._user_dismiss()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: object) -> None:
        # On macOS the unchanged Qt content view sits over the NSWindow's
        # system background tint. Paint only a faint HUD scrim so the card
        # edge reads cleanly. On Linux/Windows this fill supplies the complete
        # dim material.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not mac_native.is_macos():
            painter.fillRect(self.rect(), QColor(10, 12, 20, 200))
        else:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 30))
        painter.end()

    # ------------------------------------------------------------------
    # G4 (audit-prod): suggested-action rendering + dispatch
    # ------------------------------------------------------------------

    # The desktop is a proposal/authorization surface, never a capability
    # executor. These catalogs describe the remote owner used only for
    # availability copy; the immutable manifest remains authoritative.
    _NATIVE_ACTION_TYPES: frozenset[str] = frozenset()
    _BROWSER_ACTION_TYPES = frozenset({
        "close_tab",
        "bookmark_and_close",
        "group_tabs",
        "open_url",
        "search_error",
        "highlight_tab",
    })
    _EDITOR_ACTION_TYPES = frozenset({"resume_last_active_file"})

    # Action types that render an inline widget below the button row
    # rather than executing on click (the click reveals the widget; a
    # secondary Confirm/Done emits USER_ACTION via ``action_invoked``).
    _INLINE_WIDGET_ACTIONS = frozenset({
        "prompt_micro_commit",
        "suggest_movement_break",
    })

    # ------------------------------------------------------------------
    # P0 §3.6: micro-step rendering with state preservation
    # ------------------------------------------------------------------

    def _step_text_of(self, step: object) -> str:
        """Coerce a wire-format micro-step entry into its display text.

        The payload may carry either:
          * legacy ``list[str]`` — each entry is the text directly, or
          * ``list[dict]`` — each entry is
            ``{"text": str, "status": "pending"|"done"|"skipped", …}``.
        """
        if isinstance(step, dict):
            return str(step.get("text") or "")
        return str(step)

    def _step_status_of(self, step: object) -> str:
        """Coerce a wire-format micro-step entry into its status string.

        Legacy ``list[str]`` payloads always render as ``"pending"``.
        Dict payloads carry the user-driven status verbatim.
        """
        if isinstance(step, dict):
            status = str(step.get("status") or "pending")
            return status if status in ("pending", "done", "skipped") else "pending"
        return "pending"

    def _apply_step_visual_state(self, cb: QCheckBox, status: str) -> None:
        """Style a step checkbox according to its current ``status``.

        ``"done"`` adds a strikethrough on the label text and dims the
        colour to the secondary token so the eye reads it as completed
        without losing the legibility needed to confirm what was done.
        ``"pending"`` restores the primary colour and removes the
        strikethrough. ``"skipped"`` mirrors ``"done"`` visually (the
        checkbox is unchecked but the label is dimmed + struck through)
        — we don't currently surface the skip path in the desktop UI
        but the styling exists for symmetry with the wire schema.
        """
        if status == "done":
            cb.setChecked(True)
            cb.setStyleSheet(
                "QCheckBox {"
                f"  color: {_TEXT_SECONDARY.name()};"
                "  spacing: 10px;"
                "  background: transparent;"
                "  text-decoration: line-through;"
                "}"
                "QCheckBox::indicator { width: 16px; height: 16px; }"
            )
        elif status == "skipped":
            cb.setChecked(False)
            cb.setStyleSheet(
                "QCheckBox {"
                f"  color: {_TEXT_TERTIARY.name()};"
                "  spacing: 10px;"
                "  background: transparent;"
                "  text-decoration: line-through;"
                "}"
                "QCheckBox::indicator { width: 16px; height: 16px; }"
            )
        else:
            cb.setChecked(False)
            cb.setStyleSheet(
                "QCheckBox {"
                f"  color: {_TEXT_PRIMARY.name()};"
                "  spacing: 10px;"
                "  background: transparent;"
                "}"
                "QCheckBox::indicator { width: 16px; height: 16px; }"
            )

    def _render_micro_steps(self, steps: list[object]) -> None:
        """Render the micro-step checklist, preserving widget identity
        across F16 atomic-swap re-emissions of the same intervention.

        If the number of steps matches the prior render AND we're still
        on the same ``intervention_id``, the existing ``QCheckBox``
        widgets are updated in place (text + visual state + cached
        status). Otherwise the prior widgets are torn down (handled
        upstream in ``show_intervention``) and a fresh list is built.
        """
        same_intervention = self._step_intervention_id == self._intervention_id
        reuse = (
            same_intervention
            and len(self._step_widgets) == len(steps)
            and len(steps) > 0
        )
        if reuse:
            new_cache: list[str] = []
            for idx, step in enumerate(steps):
                text = self._step_text_of(step)
                status = self._step_status_of(step)
                # Server is authoritative: if the daemon's status differs
                # from the optimistic local cache (e.g. another surface
                # toggled the step), sync the widget. Block the toggled
                # signal during the setChecked call so we don't bounce
                # the change back to the daemon as a fake user click.
                cb = self._step_widgets[idx]
                cb.blockSignals(True)
                try:
                    cb.setText(text)
                    self._apply_step_visual_state(cb, status)
                finally:
                    cb.blockSignals(False)
                new_cache.append(status)
            self._step_status_cache = new_cache
            return

        # Fresh build path: clear any survivors (defensive — upstream
        # has usually already done this) and rebuild.
        for cb in self._step_widgets:
            self._steps_container.removeWidget(cb)
            cb.deleteLater()
        self._step_widgets.clear()
        self._step_status_cache = []
        self._step_intervention_id = self._intervention_id

        for idx, step in enumerate(steps):
            text = self._step_text_of(step)
            status = self._step_status_of(step)
            cb = QCheckBox(text)
            cb.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            self._apply_step_visual_state(cb, status)
            # Bind ``idx`` at definition time so back-to-back renders
            # don't capture the final loop variable. ``toggled``
            # carries the bool checked state directly.
            try:
                cb.toggled.connect(
                    lambda checked, i=idx: self._on_step_toggled(i, checked)
                )
            except Exception:
                logger.debug(
                    "micro-step toggled.connect failed", exc_info=True
                )
            self._steps_container.addWidget(cb)
            self._step_widgets.append(cb)
            self._step_status_cache.append(status)

    def _on_step_toggled(self, step_index: int, checked: bool) -> None:
        """Handler for a micro-step checkbox toggle. Emits
        ``micro_step_toggled(intervention_id, step_index, new_status)``
        with the new status string so the controller can forward to
        the daemon.

        Optimistically updates the local visual state so the user
        sees immediate feedback; the daemon will re-broadcast
        ``INTERVENTION_TRIGGER`` shortly after with the authoritative
        status, which is reconciled on the next render.
        """
        if not self._intervention_id:
            return
        new_status = "done" if checked else "pending"
        # Sync local cache + visuals immediately.
        if 0 <= step_index < len(self._step_widgets):
            cb = self._step_widgets[step_index]
            cb.blockSignals(True)
            try:
                self._apply_step_visual_state(cb, new_status)
            finally:
                cb.blockSignals(False)
        if 0 <= step_index < len(self._step_status_cache):
            self._step_status_cache[step_index] = new_status
        try:
            self.micro_step_toggled.emit(
                self._intervention_id, int(step_index), new_status
            )
        except Exception:
            logger.debug(
                "micro_step_toggled.emit failed", exc_info=True
            )

    def _render_actions(
        self,
        actions: list[dict],
        connected_clients: list[str] | None = None,
        execution_mode: str = "suggest_only",
        action_manifest: object | None = None,
    ) -> None:
        """Re-render the suggested_action button list.

        Idempotent: every call clears the prior buttons and creates fresh
        ones bound to the current payload's ``action_id`` / ``action_type``.
        Browser-bound actions are disabled (with a caption) when no
        chrome/edge/vscode client is currently identified.
        """
        # Tear down previous buttons.
        for btn in self._action_buttons:
            try:
                self._actions_container.removeWidget(btn)
                btn.deleteLater()
            except Exception:
                pass
        self._action_buttons.clear()

        if not actions:
            self._actions_caption.hide()
            return

        connected = {str(c).lower() for c in (connected_clients or [])}
        has_browser_executor = bool(connected & {"chrome", "edge"})
        has_editor_executor = "vscode" in connected
        executable_by_id: dict[str, str] = {}
        manifest: ActionManifest | None = None
        try:
            manifest = ActionManifest.model_validate(action_manifest)
            executable_by_id = {
                manifest_action.action_id: str(manifest_action.executor)
                for manifest_action in manifest.body.actions
            }
        except Exception:
            # Missing/corrupt authority disables every button. Presentation
            # remains readable and the daemon can issue a fresh proposal.
            executable_by_id = {}
        unavailable_reasons: set[str] = set()
        suggestion_only = execution_mode not in (
            "authorized", "research_autonomous",
        )

        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("action_type") or "")
            action_id = str(action.get("action_id") or "")
            label = str(action.get("label") or action_type or "Action")
            reason = str(action.get("reason") or "")
            executor = executable_by_id.get(action_id)
            presentation_matches = bool(
                manifest is not None
                and manifest_suggestion_matches(manifest, action)
            )

            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
            if reason:
                btn.setToolTip(reason)
            btn.setStyleSheet(self._action_btn_stylesheet())
            try:
                btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            except Exception:
                pass
            _set_accessible_name(btn, label)

            if suggestion_only:
                btn.setEnabled(False)
                btn.setToolTip(
                    "Suggestion only — switch to an authorized mode to apply actions."
                )
            elif executor is None or not presentation_matches:
                btn.setEnabled(False)
                btn.setToolTip(
                    "This suggestion is unavailable because its displayed "
                    "details do not match Cortex's exact reversible manifest."
                )
                unavailable_reasons.add("unsupported")
            elif executor == "browser" and not has_browser_executor:
                btn.setEnabled(False)
                btn.setToolTip("Connect Chrome or Edge to apply this action.")
                unavailable_reasons.add("browser")
            elif executor == "editor" and not has_editor_executor:
                btn.setEnabled(False)
                btn.setToolTip("Connect VS Code to apply this action.")
                unavailable_reasons.add("editor")

            # Capture-by-default to bind the action dict to this button.
            action_snapshot = dict(action)
            try:
                if action_type in self._INLINE_WIDGET_ACTIONS:
                    btn.clicked.connect(
                        lambda _checked=False, a=action_snapshot:
                            self._reveal_inline_widget(a)
                    )
                else:
                    btn.clicked.connect(
                        lambda _checked=False, a=action_snapshot:
                            self._on_action_clicked(a)
                    )
            except Exception:
                pass
            self._actions_container.addWidget(btn)
            self._action_buttons.append(btn)

        if suggestion_only:
            self._actions_caption.setText(
                "Suggestions only — Cortex will not change your workspace."
            )
            self._actions_caption.show()
        elif unavailable_reasons == {"browser"}:
            self._actions_caption.setText(
                "Open Cortex in Chrome or Edge to enable these actions."
            )
            self._actions_caption.show()
        elif unavailable_reasons == {"editor"}:
            self._actions_caption.setText(
                "Connect the Cortex VS Code extension to enable this action."
            )
            self._actions_caption.show()
        elif unavailable_reasons:
            self._actions_caption.setText(
                "Some suggestions are unavailable until their reversible "
                "executor is connected and verified."
            )
            self._actions_caption.show()
        else:
            self._actions_caption.clear()
            self._actions_caption.hide()

    def _on_action_clicked(self, action: dict) -> None:
        """Emit ``action_invoked`` carrying the current intervention_id +
        the action dict. The host (controller / main) decides how to
        dispatch.
        """
        if (
            not self._intervention_id
            or self._execution_mode == "suggest_only"
        ):
            return
        try:
            self.action_invoked.emit(self._intervention_id, dict(action))
        except Exception:
            logger.debug("action_invoked emit failed", exc_info=True)
        # P0 §3.8: reveal the rating row immediately after engagement.
        self._reveal_feedback_row()

    # ─────────────────────────────────────────────────────────────────
    # P0 §3.5: inline-revealed widgets (micro-commit / movement-break)
    # ─────────────────────────────────────────────────────────────────

    def _clear_inline_widgets(self) -> None:
        """Tear down any previously rendered inline widgets + timers."""
        if self._inline_movement_timer is not None:
            try:
                self._inline_movement_timer.stop()
            except Exception:
                logger.debug("inline movement timer stop failed", exc_info=True)
            self._inline_movement_timer = None
        self._inline_movement_remaining = 0
        for w in self._inline_widgets:
            try:
                self._inline_container.removeWidget(w)
                w.deleteLater()
            except Exception:
                pass
        self._inline_widgets.clear()

    def _reveal_inline_widget(self, action: dict) -> None:
        """Render a native widget below the action buttons.

        Two action types are supported:

        * ``prompt_micro_commit`` — single-line text input + Confirm
          button. Confirming emits ``action_invoked`` with the original
          ``action_type`` plus ``text`` carrying the user's typed
          commitment (≤ 200 chars).
        * ``suggest_movement_break`` — a 60-second countdown card with a
          "Done" button. The countdown automatically completes after
          60 s; either path emits ``action_invoked`` with the
          ``action_type`` so the daemon records the engagement.
        """
        if not self._intervention_id:
            return
        action_type = str(action.get("action_type") or "")
        self._clear_inline_widgets()
        if action_type == "prompt_micro_commit":
            self._build_micro_commit_widget(action)
        elif action_type == "suggest_movement_break":
            self._build_movement_break_widget(action)
        # P0 §3.8: reveal the rating row immediately after engagement.
        self._reveal_feedback_row()

    def _build_micro_commit_widget(self, action: dict) -> None:
        host = QFrame()
        host.setStyleSheet("QFrame { background: transparent; }")
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SP2)
        prompt_text = str(action.get("prompt") or "What's the smallest commit you can ship next?")
        prompt = QLabel(prompt_text)
        prompt.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        prompt.setStyleSheet(
            f"color: {_TEXT_SECONDARY.name()}; background: transparent;"
        )
        prompt.setWordWrap(True)
        host_layout_outer = QVBoxLayout()
        host_layout_outer.setContentsMargins(0, 0, 0, 0)
        host_layout_outer.setSpacing(SP2)
        host_layout_outer.addWidget(prompt)
        edit = QLineEdit()
        edit.setMaxLength(200)
        edit.setPlaceholderText("Type a one-line commitment…")
        edit.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        edit.setStyleSheet(
            "QLineEdit {"
            "  background: rgba(255,255,255,0.06);"
            "  color: rgba(255,255,255,0.92);"
            "  border: 0.5px solid rgba(255,255,255,0.18);"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 10px;"
            "}"
        )
        confirm = QPushButton("Confirm")
        confirm.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        confirm.setStyleSheet(self._accent_btn_stylesheet())

        def _on_confirm() -> None:
            text = edit.text().strip()[:200]
            payload = dict(action)
            payload["text"] = text
            try:
                self.action_invoked.emit(self._intervention_id, payload)
            except Exception:
                logger.debug("micro_commit emit failed", exc_info=True)
            self._clear_inline_widgets()

        try:
            confirm.clicked.connect(lambda _checked=False: _on_confirm())
            edit.returnPressed.connect(_on_confirm)
        except Exception:
            logger.debug("micro_commit signal connect failed", exc_info=True)
        row.addWidget(edit, stretch=1)
        row.addWidget(confirm)
        host_layout_outer.addLayout(row)
        # Mount the outer layout into a container widget so we can free
        # it cleanly on next intervention.
        outer = QWidget()
        outer.setLayout(host_layout_outer)
        self._inline_container.addWidget(outer)
        self._inline_widgets.append(outer)

    def _build_movement_break_widget(self, action: dict) -> None:
        # Soft 60s countdown card with "Done" early exit.
        duration_seconds = int(action.get("duration_seconds") or 60)
        self._inline_movement_remaining = max(15, min(300, duration_seconds))
        card = QFrame()
        card.setStyleSheet(
            "QFrame {"
            "  background: rgba(255,255,255,0.06);"
            "  border: 0.5px solid rgba(255,255,255,0.14);"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "}"
        )
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(SP3, SP3, SP3, SP3)
        vbox.setSpacing(SP2)
        title = QLabel(str(action.get("label") or "Stand up · stretch · breathe"))
        title.setFont(mac_native.system_font(FS_BODY, "semibold"))
        title.setStyleSheet(
            f"color: {_TEXT_PRIMARY.name()}; background: transparent;"
        )
        vbox.addWidget(title)
        countdown = QLabel(f"{self._inline_movement_remaining}s")
        countdown.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        countdown.setStyleSheet(
            f"color: {_TEXT_SECONDARY.name()}; background: transparent;"
        )
        vbox.addWidget(countdown)
        done_btn = QPushButton("Done")
        done_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        done_btn.setStyleSheet(self._accent_btn_stylesheet())
        vbox.addWidget(done_btn)

        def _on_done(_checked: bool = False) -> None:
            payload = dict(action)
            payload["elapsed_seconds"] = duration_seconds - self._inline_movement_remaining
            try:
                self.action_invoked.emit(self._intervention_id, payload)
            except Exception:
                logger.debug("movement_break emit failed", exc_info=True)
            self._clear_inline_widgets()

        def _tick() -> None:
            self._inline_movement_remaining = max(0, self._inline_movement_remaining - 1)
            try:
                countdown.setText(f"{self._inline_movement_remaining}s")
            except Exception:
                return
            if self._inline_movement_remaining <= 0:
                _on_done(False)

        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(_tick)
        timer.start()
        self._inline_movement_timer = timer
        try:
            done_btn.clicked.connect(_on_done)
        except Exception:
            logger.debug("movement_break done connect failed", exc_info=True)
        self._inline_container.addWidget(card)
        self._inline_widgets.append(card)

    # ─────────────────────────────────────────────────────────────────
    # P0 §3.8: rating + frustration-spiral helpers
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _footer_btn_stylesheet() -> str:
        """Shared style for the footer pills (Snooze 15 min, Quiet for this
        session). HUD-flavoured capsule with hover, pressed, and a visible
        focus ring; the 2px transparent border keeps the ring from
        shifting layout."""
        return (
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 0.08);"
            "  color: rgba(255, 255, 255, 0.88);"
            "  border: 2px solid transparent;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 8px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 0.16);"
            "  color: white;"
            "}"
            "QPushButton:pressed {"
            "  background-color: rgba(255, 255, 255, 0.26);"
            "  color: white;"
            "}"
            "QPushButton:focus { border-color: rgba(255, 255, 255, 0.55); }"
        )

    @staticmethod
    def _icon_btn_stylesheet() -> str:
        """Style for the × dismiss glyph button in the header."""
        return (
            "QPushButton {"
            "  background-color: transparent;"
            "  color: rgba(255, 255, 255, 0.70);"
            "  border: 2px solid transparent;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 0;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 0.12); color: white;"
            "}"
            "QPushButton:pressed { background-color: rgba(255, 255, 255, 0.24); }"
            "QPushButton:focus { border-color: rgba(255, 255, 255, 0.55); }"
        )

    @staticmethod
    def _action_btn_stylesheet() -> str:
        """Suggested-action buttons: full-width, left-aligned, with the
        same hover / pressed / focus / disabled vocabulary as the pills."""
        return (
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 0.10);"
            "  color: rgba(255, 255, 255, 0.92);"
            "  border: 2px solid transparent;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 12px;"
            "  text-align: left;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 0.18);"
            "  color: white;"
            "}"
            "QPushButton:pressed { background-color: rgba(255, 255, 255, 0.28); }"
            "QPushButton:focus { border-color: rgba(255, 255, 255, 0.55); }"
            "QPushButton:disabled {"
            "  color: rgba(255, 255, 255, 0.42);"
            "  background-color: rgba(255, 255, 255, 0.04);"
            "}"
        )

    @staticmethod
    def _accent_btn_stylesheet() -> str:
        """Filled terracotta confirm buttons inside inline widgets. Dark
        ink on the accent fill clears AA; pressed darkens to the pressed
        token and flips to white ink (see tokens.BTN_ACCENT_QSS)."""
        return (
            "QPushButton {"
            f"  background-color: {_ACCENT.name()};"
            "  color: #1A1A1A;"
            "  border: 2px solid transparent;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 4px 12px;"
            "}"
            "QPushButton:hover { background-color: #C46547; color: #111111; }"
            "QPushButton:pressed { background-color: #B45638; color: #FFFFFF; }"
            "QPushButton:focus { border-color: rgba(255, 255, 255, 0.85); }"
        )

    def _on_snooze_clicked(self) -> None:
        """P0 §3.11: emit a 15-min snooze + dismiss the overlay."""
        try:
            self.quiet_requested.emit("snooze_15", 15)
        except Exception:
            logger.debug("quiet_requested(snooze_15) emit failed", exc_info=True)
        self._user_dismiss()

    def _on_quiet_session_clicked(self) -> None:
        """P0 §3.11: emit a quiet-for-session toggle + dismiss the overlay."""
        try:
            # ``0`` here means "use the daemon's default
            # quiet_mode_minutes". The daemon clamps to [1, 240].
            self.quiet_requested.emit("quiet_session", 0)
        except Exception:
            logger.debug(
                "quiet_requested(quiet_session) emit failed", exc_info=True,
            )
        self._user_dismiss()

    @staticmethod
    def _feedback_btn_stylesheet() -> str:
        """Shared style for 👍 / 👎 buttons."""
        return (
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 0.06);"
            "  color: white;"
            "  border: 2px solid transparent;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 4px 12px;"
            f"  font-size: {FS_BODY}px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 0.12);"
            "}"
            "QPushButton:checked, QPushButton:pressed {"
            f"  background-color: {_ACCENT.name()};"
            "}"
            "QPushButton:focus { border-color: rgba(255, 255, 255, 0.55); }"
        )

    def _reveal_feedback_row(self) -> None:
        """Show the 👍/👎 row once.

        P0 §3.8 audit fix (spec line 710): respect ``_wants_rating``
        so a tail action click on a minimal-tone (overlay_only)
        intervention does not surface the rating row. ``_wants_rating``
        is set in ``show_intervention`` from the plan's ``level``.
        """
        try:
            self._feedback_reveal_timer.stop()
        except Exception:
            pass
        if not getattr(self, "_wants_rating", False):
            return
        if not self._feedback_row.isVisible():
            self._feedback_row.show()

    def _on_thumbs_up(self) -> None:
        if not self._intervention_id:
            return
        try:
            self.rating_invoked.emit(
                self._intervention_id, "thumbs_up", "",
            )
        except Exception:
            logger.debug("rating_invoked(thumbs_up) failed", exc_info=True)
        # Once rated, collapse the overlay — the rating completes the
        # interaction (consent: implicit "I'm done").
        QTimer.singleShot(180, self._auto_dismiss)

    def _on_thumbs_down(self) -> None:
        if not self._intervention_id:
            return
        # Reveal the optional one-line text input. The user can press
        # Enter to send, or skip with Esc / by clicking dismiss.
        self._feedback_text.show()
        self._feedback_text.setFocus(Qt.FocusReason.OtherFocusReason)
        try:
            self.rating_invoked.emit(
                self._intervention_id, "thumbs_down", "",
            )
        except Exception:
            logger.debug("rating_invoked(thumbs_down) failed", exc_info=True)

    def _commit_feedback_text(self) -> None:
        """Enter key on the one-line text input — emit + collapse."""
        text = self._feedback_text.text().strip()[:200]
        if not self._intervention_id:
            return
        if text:
            try:
                # Re-emit thumbs_down with the text payload so the
                # daemon's helpfulness tracker stashes the comment on
                # the same record.
                self.rating_invoked.emit(
                    self._intervention_id, "thumbs_down", text,
                )
            except Exception:
                logger.debug(
                    "rating_invoked(thumbs_down,text) failed",
                    exc_info=True,
                )
        self._feedback_text.clear()
        self._feedback_text.hide()
        QTimer.singleShot(120, self._auto_dismiss)

    # ─────────────────────────────────────────────────────────────────
    # P0 §3.9: structured "Why?" drilldown
    # ─────────────────────────────────────────────────────────────────

    def apply_causal_signals(self, signals: list[dict]) -> None:
        """Public slot: ingest structured signals and rebuild the panel.

        Called from ``show_intervention`` (initial trigger payload) and
        from the controller's ``WHY_DETAIL`` listener. Idempotent.
        """
        if not isinstance(signals, list):
            signals = []
        self._causal_signals_cache = list(signals)
        # Tear down prior rows.
        while self._why_panel_layout.count():
            item = self._why_panel_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        for sig in self._causal_signals_cache:
            row = self._render_why_row(sig)
            if row is not None:
                self._why_panel_layout.addWidget(row)
        try:
            self._why_signals_host.setVisible(bool(self._causal_signals_cache))
        except Exception:
            pass
        self._sync_why_affordance()

    def _render_why_row(self, sig: dict) -> QFrame | None:
        try:
            name = str(sig.get("name") or "")
            unit = str(sig.get("unit") or "")
            current = float(sig.get("current_value") or 0.0)
            baseline = sig.get("baseline_value")
            delta_pct = sig.get("delta_pct")
            severity = sig.get("severity") or "secondary"
            samples = sig.get("samples_60s") or []
            if not isinstance(samples, list):
                samples = []
        except Exception:
            logger.debug("malformed causal signal", exc_info=True)
            return None
        row = QFrame()
        row.setStyleSheet("QFrame { background: transparent; }")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(SP3)
        weight = "semibold" if severity == "primary" else "regular"
        name_label = QLabel(name)
        name_label.setFont(mac_native.system_font(FS_CAPTION, weight))
        name_label.setStyleSheet(
            f"color: {_TEXT_PRIMARY.name()}; background: transparent;"
        )
        name_label.setMinimumWidth(96)
        layout.addWidget(name_label)
        value_text = f"{current:.1f}{unit}"
        if isinstance(baseline, (int, float)):
            value_text += f"  (baseline {float(baseline):.1f}{unit})"
        value_label = QLabel(value_text)
        value_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        value_label.setStyleSheet(
            f"color: {_TEXT_SECONDARY.name()}; background: transparent;"
        )
        layout.addWidget(value_label, stretch=1)
        spark = _SparklineWidget(samples)
        layout.addWidget(spark)
        if isinstance(delta_pct, (int, float)):
            arrow = "↓" if delta_pct < 0 else "↑"
            pill = QLabel(f"{arrow}{abs(float(delta_pct)):.0f}%")
            pill.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
            pill_color = "#E47A6E" if delta_pct < 0 else _ACCENT.name()
            pill.setStyleSheet(
                f"color: {pill_color}; background: transparent;"
            )
            layout.addWidget(pill)
        return row

    def _user_dismiss(self) -> None:
        self._dismiss(reason="user")

    def _auto_dismiss(self) -> None:
        self._dismiss(reason="timeout")

    def _dismiss(self, *, reason: str) -> None:
        """The one dismiss path (user, timeout, rating completion).

        F06: idempotent — first caller wins; subsequent calls no-op. Every
        presentation timer is stopped unconditionally, even when already
        dismissed, so a stale timer cannot re-trigger on a hidden widget.
        P0 §3.8: the rating-reveal timer + row are torn down too so a late
        30 s single-shot cannot surface 👍/👎 against a stale id.
        """
        self._stop_presentation_timers()
        self._stop_feedback_reveal_timer()
        if self._dismissed:
            return
        self._dismissed = True
        try:
            self._clear_inline_widgets()
        except Exception:
            logger.debug("clear inline widgets failed", exc_info=True)
        self.hide()
        dismissed_id = self._intervention_id
        self.dismissed.emit(dismissed_id)
        # Audit-prod fix (P2): clear ``_intervention_id`` so a stale
        # button click after dismiss (Qt repaint tail, animation queue)
        # cannot emit ``action_invoked`` with the dismissed id.
        self._intervention_id = ""
        logger.info("Intervention %s dismissed (%s)", dismissed_id, reason)

    def _stop_feedback_reveal_timer(self) -> None:
        """P0 §3.8 audit fix: tear down the rating row reveal timer.

        Called by every dismiss path so a Qt single-shot timer queued
        before ``show_intervention`` cannot surface the 👍/👎 row on an
        already-collapsed overlay. Also collapses the optional text
        input so the next intervention starts from a clean slate.
        """
        try:
            self._feedback_reveal_timer.stop()
        except Exception:
            logger.debug("feedback_reveal_timer.stop failed", exc_info=True)
        try:
            self._feedback_row.hide()
            self._feedback_text.hide()
            self._feedback_text.clear()
        except Exception:
            logger.debug("feedback row teardown failed", exc_info=True)

    def closeEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        # F06: ensure no presentation timer fires after the window closes.
        self._stop_presentation_timers()
        self._dismissed = True
        super().closeEvent(event)

    def deleteLater(self) -> None:  # noqa: D401 - Qt override
        # F06: defensive stop on deferred deletion so the timer cannot fire
        # against a Qt-collected widget. The flag is set before stop() to
        # short-circuit any callback that races in before the timer is gone.
        self._dismissed = True
        self._stop_presentation_timers()
        super().deleteLater()
