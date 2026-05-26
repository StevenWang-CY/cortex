"""Desktop Shell — Intervention Overlay (HUD-vibrancy refactor).

A frameless, always-on-top panel that renders LLM-generated intervention
content. On macOS the window is backed by ``NSVisualEffectMaterialHUDWindow``
(the same dark vibrancy used by Spotlight / Notification Center), so the
overlay feels like a native HUD instead of a custom-drawn dark rectangle.

The terracotta brand accent + breathing-pacer animation + causal_explanation
row are all preserved from the previous implementation. ``BreathingPacer``'s
geometry math is unchanged.

Public API: ``dismissed(str)`` Signal, ``show_intervention(payload)`` method.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
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
    QToolButton = QPushButton  # type: ignore[assignment,misc]

# Phase J-4: subtle scale-in (headline) + fade-in (causal row) micro-
# interactions. QPropertyAnimation / QEasingCurve / QGraphicsOpacityEffect
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
    QEasingCurve = None  # type: ignore[assignment]
    QPropertyAnimation = None  # type: ignore[assignment]
    QGraphicsOpacityEffect = None  # type: ignore[assignment]
    _ANIMATION_AVAILABLE = False

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BRAND_DISPLAY_FONT,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_REGULAR,
    HUD_ACCENT,
    RADIUS_BUTTON,
    RADIUS_WINDOW,
    SP1,
    SP2,
    SP3,
    SP4,
    SP6,
    SP8,
    TEXT_HUD_PRIMARY,
    TEXT_HUD_SECONDARY,
    TEXT_HUD_TERTIARY,
)

# Phase J-4: tween constants. Chosen to be "perceptible but never
# distracting" — the headline scale-in is fast enough to feel
# responsive (under 300 ms is below the typical user attention
# threshold) and the causal fade lags by exactly the headline duration
# so the two animations read as one continuous motion rather than two
# competing tweens. The Reduce Motion path forces both to 0 ms.
HEADLINE_SCALE_DURATION_MS: int = 250
CAUSAL_FADE_DURATION_MS: int = 180

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
# The vibrancy view below the window provides the actual dark blur; these
# QColors are how the overlay's content layers itself on top of that
# material. The token values are the spec; do not introduce hex literals
# in this module — extend ``tokens.py`` instead.
_ACCENT = QColor(*HUD_ACCENT)
_TEXT_PRIMARY = QColor(*TEXT_HUD_PRIMARY)
_TEXT_SECONDARY = QColor(*TEXT_HUD_SECONDARY)
_TEXT_TERTIARY = QColor(*TEXT_HUD_TERTIARY)


class BreathingPacer(QWidget):
    """Breathing pacer animation widget. F48: cadence is read from
    :class:`cortex.libs.config.settings.InterventionConfig.breathing_pattern`
    at construction time and falls back to the 4-7-8 default if no config
    is supplied (test stubs, ad-hoc previews).

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
        self._elapsed_ms = 0
        # F48: resolve the breathing pattern. Explicit ``pattern`` arg
        # wins (used by tests + future user-supplied profiles); otherwise
        # we read ``InterventionConfig.breathing_pattern`` from the
        # global config. If neither is available, fall back to 4-7-8.
        if pattern is None:
            try:
                from cortex.libs.config.settings import get_config

                pattern = tuple(  # type: ignore[assignment]
                    get_config().intervention.breathing_pattern
                )
            except Exception:
                pattern = _DEFAULT_BREATHING_PATTERN
        self._inhale, self._hold, self._exhale = pattern
        self._cycle = self._inhale + self._hold + self._exhale
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(160, 160)

    def start(self) -> None:
        self._active = True
        self._elapsed_ms = 0
        self._timer.start()

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        self.update()

    @property
    def is_active(self) -> bool:
        return self._active

    def _tick(self) -> None:
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

        phase, remaining, scale = self._get_phase()

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
            f"{remaining:.0f}s",
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
    """Frameless always-on-top intervention overlay backed by HUD vibrancy."""

    dismissed = Signal(str)
    # G4 (audit-prod): emitted when the user clicks a suggested-action
    # button. Payload is ``(intervention_id, action_dict)`` matching the
    # ``SuggestedAction`` schema; the controller / main routes it to
    # either a native handler (clipboard, timer) or the WS
    # ``ACTION_EXECUTE`` channel for browser-bound actions.
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._intervention_id = ""
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

        # Frameless + always-on-top. Translucent background lets the
        # NSVisualEffectView under the window show through.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setMinimumSize(440, 520)

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
        self._causal_opacity_effect: object | None = None
        # Test affordance: when True, ``_play_show_animations`` records
        # the durations it would use without actually starting the
        # timers. Useful in offscreen tests where the real Qt event loop
        # is not free to tick at 16ms intervals.
        self._record_animations: bool = False
        self._last_animation_log: dict[str, int] = {}

        self._build_ui()

    # -- Lifecycle hook: apply HUD vibrancy ------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="hudWindow")
        except Exception:
            pass

    def _build_ui(self) -> None:
        self._main_layout = QVBoxLayout(self)
        # 24px outer margin — matches SP6 (4pt grid).
        self._main_layout.setContentsMargins(SP6, SP6, SP6, SP6)

        # Card — translucent dark surface that layers on top of the HUD
        # vibrancy material below. The 6% white border picks out the card
        # edge against the blur.
        self._card = QFrame()
        self._card.setObjectName("CortexOverlayCard")
        self._card.setStyleSheet(
            "QFrame#CortexOverlayCard {"
            "  background-color: rgba(30, 30, 32, 0.55);"
            f"  border-radius: {RADIUS_WINDOW}px;"
            "  border: 0.5px solid rgba(255, 255, 255, 0.10);"
            "}"
        )
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(SP8, SP6, SP8, SP6)
        card_layout.setSpacing(SP4)

        # Headline — Cormorant display (brand-preserved).
        self._headline = QLabel("—")
        self._headline.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-size: {FS_TITLE}px;"
            f"font-weight: {FW_REGULAR};"
            "font-style: italic;"
            f"color: {_TEXT_PRIMARY.name()};"
            "background: transparent;"
        )
        self._headline.setWordWrap(True)
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(self._headline)

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
        # ``suggested_actions`` list. Each click emits ``action_invoked``;
        # the controller routes browser-bound actions through the WS
        # ACTION_EXECUTE channel and handles ``copy_to_clipboard`` /
        # ``start_timer`` natively in the desktop shell.
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

        # "Why this?" causal explanation — surfaces only when supplied.
        # F51: long explanations are truncated to a one-line preview with
        # a trailing ellipsis; a "Show more" QToolButton (checkable) toggles
        # to the full text. The full text is stashed on the label so the
        # toggle handler can swap without re-parsing the payload.
        self._causal_label = QLabel("")
        self._causal_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._causal_label.setStyleSheet(
            f"color: {_TEXT_TERTIARY.name()};"
            "background: transparent;"
            "font-style: italic;"
        )
        self._causal_label.setWordWrap(True)
        self._causal_label.hide()
        card_layout.addWidget(self._causal_label)

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

        # F51 (audit): expandable causal explanation. When the causal
        # text exceeds the visible area, ``_causal_label`` shows a
        # truncated preview with an ellipsis and the toggle below
        # reveals the full body on click.
        self._causal_full_text: str = ""
        self._causal_preview_text: str = ""
        self._causal_toggle = QToolButton()
        _safe_call(self._causal_toggle, "setCheckable", True)
        _safe_call(self._causal_toggle, "setText", "Show more")
        # F55: accessible name + description for VoiceOver / screen readers.
        _set_accessible_name(
            self._causal_toggle, "Show full causal explanation"
        )
        _safe_call(self._causal_toggle, "setCursor", Qt.CursorShape.PointingHandCursor)
        _safe_call(
            self._causal_toggle,
            "setStyleSheet",
            (
                "QToolButton {"
                f"  color: {_TEXT_SECONDARY.name()};"
                "  background: transparent;"
                "  border: none;"
                "  padding: 2px 0;"
                f"  font-size: {FS_CAPTION}px;"
                "}"
                "QToolButton:hover { color: white; }"
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

        # Breathing pacer.
        pacer_layout = QHBoxLayout()
        pacer_layout.addStretch()
        self._pacer = BreathingPacer()
        pacer_layout.addWidget(self._pacer)
        pacer_layout.addStretch()
        card_layout.addLayout(pacer_layout)

        # Dismiss button — HUD-style capsule.
        self._dismiss_btn = QPushButton("Dismiss (Esc)")
        self._dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # F55: accessible name for VoiceOver.
        _set_accessible_name(self._dismiss_btn, "Dismiss intervention")
        self._dismiss_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._dismiss_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 0.08);"
            "  color: rgba(255, 255, 255, 0.85);"
            "  border: 0.5px solid rgba(255, 255, 255, 0.14);"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 8px 22px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 0.16);"
            "  color: white;"
            "}"
        )
        self._dismiss_btn.clicked.connect(self._user_dismiss)
        card_layout.addWidget(
            self._dismiss_btn, alignment=Qt.AlignmentFlag.AlignCenter
        )

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

        # P0 §3.9: "Why?" chevron + drilldown panel. Renders a row per
        # causal signal with a tiny sparkline and a delta pill. Hidden
        # by default; the chevron toggles visibility.
        self._why_row = QHBoxLayout()
        self._why_row.setSpacing(SP2)
        self._why_toggle = QPushButton("Why?")
        _set_accessible_name(self._why_toggle, "Show structured causal rationale")
        self._why_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._why_toggle.setStyleSheet(
            "QPushButton {"
            f"  color: {_TEXT_SECONDARY.name()};"
            "  background: transparent;"
            "  border: none;"
            f"  font-size: {FS_CAPTION}px;"
            "  text-decoration: underline;"
            "  padding: 0;"
            "}"
            "QPushButton:hover { color: white; }"
        )
        self._why_toggle.clicked.connect(self._on_why_toggle_clicked)
        self._why_row.addWidget(self._why_toggle)
        self._why_row.addStretch()
        card_layout.addLayout(self._why_row)
        self._why_panel = QFrame()
        self._why_panel.setObjectName("CortexOverlayWhyPanel")
        self._why_panel.setStyleSheet(
            "QFrame#CortexOverlayWhyPanel {"
            "  background-color: rgba(255, 255, 255, 0.03);"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px;"
            "}"
        )
        self._why_panel_layout = QVBoxLayout(self._why_panel)
        self._why_panel_layout.setContentsMargins(8, 6, 8, 6)
        self._why_panel_layout.setSpacing(SP1)
        self._why_panel.hide()
        card_layout.addWidget(self._why_panel)
        self._causal_signals_cache: list[dict] = []
        self._why_open: bool = False
        # Hide the Why row entirely until the daemon supplies signals.
        self._why_toggle.hide()

        self._main_layout.addWidget(self._card)

        # F55: explicit tab-order chain. Without setTabOrder, Qt falls
        # back to widget-creation order which the cascading micro-step
        # rebuilds in show_intervention can scramble. Causal toggle (if
        # surfaced) comes between the steps and the dismiss button.
        _set_tab_order(self._causal_toggle, self._dismiss_btn)

    # ------------------------------------------------------------------
    # Public API (preserved byte-identical)
    # ------------------------------------------------------------------

    def show_intervention(self, payload: dict) -> None:
        self._intervention_id = payload.get("intervention_id", "")
        # Fresh intervention — clear dismissed flag so this one can dismiss.
        self._dismissed = False

        self._headline.setText(payload.get("headline", "Take a moment"))
        self._summary.setText(payload.get("situation_summary", ""))
        self._focus_label.setText(
            f"Focus: {payload.get('primary_focus', '')}"
        )

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
        # F51: only surface causal explanations with substantive content;
        # the prior 20-char filter is preserved for the show/hide gate.
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
            )
        except Exception:
            logger.debug("Action rendering failed", exc_info=True)

        ui_plan = payload.get("ui_plan", {})
        level = payload.get("level", "overlay_only")
        if level == "overlay_only" or ui_plan.get("show_overlay", True):
            self._pacer.start()
            self._pacer.show()
        else:
            self._pacer.stop()
            self._pacer.hide()

        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.resize(min(460, geo.width() - 40), min(620, geo.height() - 40))
            self.move(
                geo.center().x() - self.width() // 2,
                geo.center().y() - self.height() // 2,
            )

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

        self.show()
        self.raise_()
        self.activateWindow()

        # Phase J-4: subtle scale-in (headline) + fade-in (causal row)
        # micro-interactions. Skipped entirely under Reduce Motion or
        # when the Qt build lacks QPropertyAnimation. The animations
        # are visually subordinate to the breathing pacer (which keeps
        # its existing rhythm); the dismiss button and checkboxes are
        # NOT animated per the audit's "strictly purposeful" rule.
        self._play_show_animations()

        logger.info(f"Overlay shown for intervention {self._intervention_id}")

    # ------------------------------------------------------------------
    # Phase J-4: micro-interactions
    # ------------------------------------------------------------------

    def _play_show_animations(self) -> None:
        """Animate the headline (scale-in 250 ms) and the causal row
        (fade-in 180 ms, starts after the headline animation completes).

        Honours the macOS "Reduce Motion" accessibility preference: when
        enabled, both end states are applied directly and the animations
        skip entirely.

        Defensive: short-circuits when the Qt build lacks the animation
        classes (the lightweight test stubs) or when the headline /
        causal widgets are unavailable. The end state is always applied
        so the UI never gets stuck in a half-animated state.
        """
        # Always record the durations we *would* use so the unit test
        # can assert against the wired-up constants without spinning a
        # real event loop.
        reduced = self._reduce_motion_enabled()
        if reduced:
            headline_ms = 0
            causal_ms = 0
        else:
            headline_ms = HEADLINE_SCALE_DURATION_MS
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
            # Reduce-motion / mocked-out path: apply the end state directly.
            # The causal label is whatever ``_show_causal_explanation``
            # set; ensure its opacity effect (if any) is at full.
            self._reset_causal_opacity_to_full()
            return

        # Headline scale-in: animate ``geometry`` from a slightly
        # squashed rect to the natural rect. The squash is 90 % height
        # so the eye reads it as growing into place; lateral position
        # is preserved so the text doesn't appear to drift.
        try:
            target_rect = self._headline.geometry()
            squashed = QRect(
                target_rect.x(),
                target_rect.y() + target_rect.height() // 20,
                target_rect.width(),
                max(1, int(target_rect.height() * 0.9)),
            )
            self._headline.setGeometry(squashed)
            anim = QPropertyAnimation(self._headline, b"geometry")
            anim.setDuration(HEADLINE_SCALE_DURATION_MS)
            anim.setStartValue(squashed)
            anim.setEndValue(target_rect)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._headline_anim = anim
            anim.start()
        except Exception:
            logger.debug("Headline scale-in animation failed", exc_info=True)

        # Causal fade-in: opacity 0 → 1 over 180 ms, started after the
        # headline animation completes so the two reads as one
        # continuous motion. We arm a singleShot timer for the start
        # rather than chaining via ``finished`` because the headline
        # animation may be replaced (back-to-back interventions) and a
        # finished signal carries no context about which run it
        # belongs to.
        if not getattr(self, "_causal_label", None):
            return
        try:
            effect = self._causal_opacity_effect
            if effect is None:
                effect = QGraphicsOpacityEffect(self._causal_label)
                self._causal_label.setGraphicsEffect(effect)
                self._causal_opacity_effect = effect
            effect.setOpacity(0.0)
            fade = QPropertyAnimation(effect, b"opacity")
            fade.setDuration(CAUSAL_FADE_DURATION_MS)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setEasingCurve(QEasingCurve.Type.InOutSine)
            self._causal_fade_anim = fade
            QTimer.singleShot(HEADLINE_SCALE_DURATION_MS, fade.start)
        except Exception:
            logger.debug("Causal fade-in animation failed", exc_info=True)
            self._reset_causal_opacity_to_full()

    def _reduce_motion_enabled(self) -> bool:
        """Wrapper around :func:`mac_native.prefers_reduced_motion` so a
        test can monkeypatch the predicate without reaching into
        ``mac_native``."""
        try:
            return bool(mac_native.prefers_reduced_motion())
        except Exception:
            return False

    def _reset_causal_opacity_to_full(self) -> None:
        """Restore the causal row's opacity effect (if any) to full so
        a Reduce-Motion path doesn't leave the label hidden behind a
        stale 0-opacity effect from a prior intervention."""
        effect = self._causal_opacity_effect
        if effect is not None:
            try:
                effect.setOpacity(1.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # F51: causal-explanation truncation + Show more toggle
    # ------------------------------------------------------------------

    # Characters above which the explanation gets the truncate + toggle
    # treatment. ~180 chars is roughly one rendered line at the FS_CAPTION
    # size inside the 460-pt-wide HUD card — picked empirically rather
    # than measured because the actual visible area depends on font
    # metrics that change between dev mode and the bundled .app.
    _CAUSAL_TRUNCATE_THRESHOLD: int = 180

    def _show_causal_explanation(self, causal: str) -> None:
        """Set the causal explanation label. If the text exceeds the
        truncation threshold, show a preview with a trailing ellipsis
        plus a "Show more" toggle button. F51."""
        full_text = f"Why this? {causal}"
        self._causal_full_text = full_text
        if len(causal) > self._CAUSAL_TRUNCATE_THRESHOLD:
            preview = causal[: self._CAUSAL_TRUNCATE_THRESHOLD].rstrip()
            self._causal_preview_text = f"Why this? {preview}…"
            self._causal_toggle.setChecked(False)
            self._causal_toggle.setText("Show more")
            self._causal_toggle.show()
            self._causal_label.setText(self._causal_preview_text)
        else:
            self._causal_preview_text = full_text
            self._causal_toggle.hide()
            self._causal_label.setText(full_text)
        self._causal_label.show()

    def _hide_causal_explanation(self) -> None:
        """Reset the causal slot back to its empty / hidden state. F51."""
        self._causal_full_text = ""
        self._causal_preview_text = ""
        self._causal_label.setText("")
        self._causal_label.hide()
        self._causal_toggle.hide()
        self._causal_toggle.setChecked(False)

    def _on_causal_toggled(self, checked: bool) -> None:
        """Handler for the Show more / Show less QToolButton. F51."""
        if checked:
            self._causal_label.setText(self._causal_full_text)
            self._causal_toggle.setText("Show less")
        else:
            self._causal_label.setText(self._causal_preview_text)
            self._causal_toggle.setText("Show more")

    def keyPressEvent(self, event: object) -> None:
        if hasattr(event, "key") and event.key() == Qt.Key.Key_Escape:
            self._user_dismiss()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: object) -> None:
        # The NSVisualEffectView provides the actual blur. We paint only
        # a very faint translucent scrim on top so the card edge reads
        # cleanly against bright wallpapers; on Linux/Windows fallback
        # this is what gives the dim effect.
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

    # Action types that the desktop shell can execute natively without
    # routing through the browser extension. Everything else needs an
    # IDENTIFY-ed Chrome / Edge / VS Code client to receive the
    # ACTION_EXECUTE frame.
    _NATIVE_ACTION_TYPES = frozenset({"copy_to_clipboard", "start_timer"})
    _BROWSER_ACTION_TYPES = frozenset({
        "close_tab",
        "bookmark_and_close",
        "group_tabs",
        "open_url",
        "search_error",
        "highlight_tab",
        "save_session",
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
        # The desktop shell can drive any browser-side action if either
        # Chrome OR Edge is identified; VS Code is the executor for
        # editor-bound actions but none of the 7 browser-bound types
        # require it. The map of "which client_type executes which
        # action_type" lives implicitly here.
        has_browser_executor = bool(connected & {"chrome", "edge"})
        any_browser_bound = False

        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("action_type") or "")
            label = str(action.get("label") or action_type or "Action")
            reason = str(action.get("reason") or "")
            is_browser = action_type in self._BROWSER_ACTION_TYPES
            is_native = action_type in self._NATIVE_ACTION_TYPES

            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
            if reason:
                btn.setToolTip(reason)
            btn.setStyleSheet(
                "QPushButton {"
                "  background-color: rgba(255, 255, 255, 0.10);"
                "  color: rgba(255, 255, 255, 0.92);"
                "  border: 0.5px solid rgba(255, 255, 255, 0.18);"
                f"  border-radius: {RADIUS_BUTTON}px;"
                "  padding: 8px 16px;"
                "  text-align: left;"
                "}"
                "QPushButton:hover {"
                "  background-color: rgba(255, 255, 255, 0.18);"
                "  color: white;"
                "}"
                "QPushButton:disabled {"
                "  color: rgba(255, 255, 255, 0.42);"
                "  background-color: rgba(255, 255, 255, 0.04);"
                "}"
            )
            _set_accessible_name(btn, label)

            if is_browser and not has_browser_executor:
                btn.setEnabled(False)
                any_browser_bound = True
            elif not is_browser and not is_native:
                # Unknown action_type: disable rather than silently fail.
                btn.setEnabled(False)
                btn.setToolTip(f"Unsupported action type: {action_type}")

            # Capture-by-default to bind the action dict to this button.
            action_snapshot = dict(action)
            try:
                btn.clicked.connect(
                    lambda _checked=False, a=action_snapshot:
                        self._on_action_clicked(a)
                )
            except Exception:
                pass
            self._actions_container.addWidget(btn)
            self._action_buttons.append(btn)

        if any_browser_bound and not has_browser_executor:
            self._actions_caption.setText(
                "Open Cortex in Chrome or Edge to enable these actions."
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
        if not self._intervention_id:
            return
        try:
            self.action_invoked.emit(self._intervention_id, dict(action))
        except Exception:
            logger.debug("action_invoked emit failed", exc_info=True)
        # P0 §3.8: reveal the rating row immediately after engagement.
        self._reveal_feedback_row()

    # ─────────────────────────────────────────────────────────────────
    # P0 §3.8: rating + frustration-spiral helpers
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _feedback_btn_stylesheet() -> str:
        """Shared style for 👍 / 👎 buttons."""
        return (
            "QPushButton {"
            "  background-color: rgba(255, 255, 255, 0.06);"
            "  color: white;"
            "  border: 0.5px solid rgba(255, 255, 255, 0.14);"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 16px;"
            "  font-size: 16px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(255, 255, 255, 0.12);"
            "}"
            "QPushButton:checked, QPushButton:pressed {"
            f"  background-color: {_ACCENT.name()};"
            "}"
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

    def _on_why_toggle_clicked(self) -> None:
        self._why_open = not self._why_open
        if self._why_open:
            self._why_toggle.setText("Hide why")
            self._why_panel.show()
            if not self._causal_signals_cache and self._intervention_id:
                try:
                    self.why_requested.emit(self._intervention_id)
                except Exception:
                    logger.debug("why_requested.emit failed", exc_info=True)
        else:
            self._why_toggle.setText("Why?")
            self._why_panel.hide()

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
        if self._causal_signals_cache:
            self._why_toggle.show()
        else:
            self._why_toggle.hide()
            self._why_panel.hide()
            self._why_open = False
            self._why_toggle.setText("Why?")

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
        # F06: idempotent dismiss. First caller wins; subsequent calls no-op.
        # Always stop the timeout timer, even if already dismissed, so that
        # a stale timer cannot re-trigger on a hidden widget.
        self._timeout_timer.stop()
        # P0 §3.8 audit fix: tear down the feedback reveal timer + row so
        # a late-firing 30 s singleShot cannot surface the 👍/👎 row on a
        # dismissed intervention (which would emit USER_RATING against a
        # stale intervention_id).
        self._stop_feedback_reveal_timer()
        if self._dismissed:
            return
        self._dismissed = True
        self._pacer.stop()
        self.hide()
        dismissed_id = self._intervention_id
        self.dismissed.emit(dismissed_id)
        # Audit-prod fix (P2): clear ``_intervention_id`` so a stale
        # button click after dismiss (Qt repaint tail, animation queue)
        # cannot emit ``action_invoked`` with the dismissed id.
        self._intervention_id = ""
        logger.info(f"Intervention {dismissed_id} dismissed by user")

    def _auto_dismiss(self) -> None:
        # F06: idempotent dismiss. First caller wins; subsequent calls no-op.
        self._timeout_timer.stop()
        self._stop_feedback_reveal_timer()
        if self._dismissed:
            return
        self._dismissed = True
        self._pacer.stop()
        self.hide()
        dismissed_id = self._intervention_id
        self.dismissed.emit(dismissed_id)
        self._intervention_id = ""
        logger.info(f"Intervention {dismissed_id} auto-dismissed (timeout)")

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
        # F06: ensure the timeout timer never fires after the window closes.
        try:
            self._timeout_timer.stop()
        except RuntimeError:
            # Timer already torn down by Qt; safe to ignore.
            pass
        self._dismissed = True
        super().closeEvent(event)

    def deleteLater(self) -> None:  # noqa: D401 - Qt override
        # F06: defensive stop on deferred deletion so the timer cannot fire
        # against a Qt-collected widget. The flag is set before stop() to
        # short-circuit any callback that races in before the timer is gone.
        self._dismissed = True
        try:
            self._timeout_timer.stop()
        except RuntimeError:
            pass
        super().deleteLater()
