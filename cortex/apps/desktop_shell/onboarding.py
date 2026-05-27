"""Desktop shell onboarding — 4-step first-run wizard (macOS-native refactor).

Visual layer adopts:

* Native popover vibrancy material under the window (via mac_native)
* Horizontal progress strip showing all 4 steps at once
* Sentence-case section headings, SF system fonts
* Terracotta number badges + Cormorant Garamond brand wordmark preserved
* Native ``AVCaptureDevice.requestAccessForMediaType_`` for camera grant
  (already in cortex/libs/utils/platform.py) and the standard
  ``AXIsProcessTrustedWithOptions`` for accessibility

Public API (Signals + ``onboarding_marker_path``) preserved byte-identical.

F49: in addition to the legacy ``.onboarding_complete`` marker in the
storage path, per-step completion is persisted to
``<config_dir>/onboarding_state.json`` via ``atomic_write_json``. The
state survives back-then-forward navigation (the user can re-open the
wizard, edit a step, and re-finish without resetting prior progress)
and is the authoritative resume signal on next app launch.
"""

from __future__ import annotations

import collections
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PySide6.QtCore import QRectF  # noqa: F401 — re-exported for older Qt fallback
except ImportError:  # pragma: no cover - older Qt fallback
    pass  # type: ignore[assignment]

from PySide6.QtCore import Qt, QTimer, Signal

try:
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
except ImportError:  # pragma: no cover - test stubs
    from PySide6.QtGui import QColor, QPainter, QPen  # type: ignore[assignment]

    QPainterPath = None  # type: ignore[assignment,misc]
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.a11y import (
    chain_tab_order,
    set_accessible_description,
    set_accessible_name,
)
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
    BRAND_ACCENT_DIM,
    BRAND_ACCENT_HOVER,
    BRAND_DISPLAY_FONT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    FS_BODY,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_REGULAR,
    RADIUS_BUTTON,
    RADIUS_CARD,
    SEMANTIC_LIGHT,
    SP2,
    SP3,
    SP4,
    SP5,
    SP8,
)
from cortex.libs.config.settings import get_config
from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.libs.utils.platform import get_config_dir

logger = logging.getLogger(__name__)


def _is_embedded_daemon_runtime() -> bool:
    """True when Cortex is running with an in-process embedded daemon.

    The frozen .app bundle always uses the in-process daemon (no external
    ``python -m cortex.scripts.run_dev`` invocation is needed); the dev
    harness flips on ``--in-process``. End-user copy in the onboarding
    wizard should never instruct a Terminal command in either of those
    runtimes — the daemon is already in the process.
    """
    try:
        import sys as _sys
        if getattr(_sys, "frozen", False):
            return True
        if "--in-process" in _sys.argv:
            return True
    except Exception:
        pass
    return False


# Phase J-1: copy keyed by step id. Surfaced from the "Why we need this"
# expand-on-click affordance on each card so a first-run user can see the
# rationale without leaving the wizard. The copy is short and scoped to
# the single permission/setup ask of that card — it is not a substitute
# for documentation, it is a trust-building inline reminder.
_WHY_COPY: dict[str, str] = {
    "camera": (
        "Cortex reads tiny facial cues (blink rate, breathing rhythm) "
        "to detect overwhelm. The video stream never leaves your Mac."
    ),
    "accessibility": (
        "Cortex listens for keyboard idle time and window switches to "
        "gauge focus. macOS requires the permission so we can see "
        "system-wide events, not just our own window."
    ),
    "llm_backend": (
        "Your Bedrock token stays in the macOS Keychain. Cortex reads "
        "it once at launch and never persists it elsewhere."
    ),
    "calibration": (
        "Cortex compares each frame against YOUR resting baseline. "
        "Without calibration we fall back to population averages and "
        "miss the personal subtleties."
    ),
    "extensions": (
        "The browser + VS Code extensions are how Cortex sees what "
        "you're working on and offers one-click interventions."
    ),
    "macos_notifications": (
        "When you're focused in another app, Cortex sends a quick "
        "macOS notification so you don't miss an important "
        "intervention. You can disable this in Settings → "
        "Notifications anytime."
    ),
}


def _detect_continuity_camera() -> bool:
    """Return True when AVFoundation is currently advertising at least
    one iPhone / iPad / Continuity Camera device.

    The existing webcam.py logic already skips Continuity Camera devices
    silently, but the user has no visibility into that — first-runners
    plugged in to an iPhone for a meeting wonder whether Cortex is
    about to grab the wrong feed. Surfacing the skip here closes the
    feedback gap.

    Defensive: failures (no AVFoundation, non-mac, enumeration crash)
    return False so the callout simply doesn't appear.
    """
    try:
        from cortex.services.capture_service.webcam import (
            _CONTINUITY_CAMERA_KEYWORDS,
            _list_macos_video_device_names,
        )

        names = _list_macos_video_device_names() or []
        for name in names:
            normalized = (name or "").lower()
            if any(kw in normalized for kw in _CONTINUITY_CAMERA_KEYWORDS):
                return True
    except Exception:
        logger.debug("Continuity Camera detection failed", exc_info=True)
    return False

# F49 / P0 §3.4: canonical step identifiers used by ``OnboardingState``.
# Order matches the cards rendered in ``OnboardingWindow``. Step 4
# ("calibration") was added in P0 §3.4 — it sits between LLM backend
# and Extensions so the new user finishes the wizard with personal
# baselines on disk rather than relying on population averages.
ONBOARDING_STEPS: tuple[str, ...] = (
    "camera",
    "accessibility",
    "llm_backend",
    "calibration",
    "extensions",
    # P0 §3.12 — final lightweight step. macOS prompts for notification
    # permission on first ``UNUserNotificationCenter.requestAuthorization``
    # call, so we surface the rationale + a "Try it" button here so the
    # user grants consciously rather than dismissing a surprise prompt
    # at intervention time. Skippable; OS notifications also remain
    # toggleable from Settings → Notifications.
    "macos_notifications",
)


def onboarding_state_path() -> Path:
    """Resolve the on-disk path for the onboarding completion marker
    introduced in F49. Lives under :func:`get_config_dir` (not the
    storage data dir) because it is a system-state record, not a user
    data artifact."""
    return get_config_dir() / "onboarding_state.json"


@dataclass
class OnboardingState:
    """Per-step completion state persisted to
    ``<config_dir>/onboarding_state.json`` (F49).

    ``completed_steps`` is a set of step identifiers from
    :data:`ONBOARDING_STEPS`. The wizard marks each step complete after
    the user successfully advances past it (either by granting a
    permission, saving a token, or clicking the explicit Get Started
    button at the end). A subsequent re-entry that toggles a step back
    to incomplete and forward to complete preserves the rest of the
    state — no whole-flow reset.
    """

    completed_steps: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path | None = None) -> OnboardingState:
        target = path or onboarding_state_path()
        if not target.exists():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            steps = payload.get("completed_steps", [])
            if not isinstance(steps, list):
                steps = []
            return cls(completed_steps={str(s) for s in steps})
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Onboarding state file exists but is unreadable; "
                "treating as fresh: %s", target, exc_info=True,
            )
            return cls()

    def save(self, path: Path | None = None) -> None:
        target = path or onboarding_state_path()
        atomic_write_json(
            target,
            {
                "completed_steps": sorted(self.completed_steps),
                "version": 1,
            },
        )

    def mark_complete(self, step: str, *, path: Path | None = None) -> None:
        """Mark a single step complete and persist via atomic_write_json."""
        if step not in ONBOARDING_STEPS:
            raise ValueError(f"unknown onboarding step: {step}")
        self.completed_steps.add(step)
        self.save(path=path)

    def mark_incomplete(self, step: str, *, path: Path | None = None) -> None:
        """Reset a step (user went 'back' to edit it) and persist."""
        if step not in ONBOARDING_STEPS:
            raise ValueError(f"unknown onboarding step: {step}")
        self.completed_steps.discard(step)
        self.save(path=path)

    @property
    def is_complete(self) -> bool:
        """True only when every step in :data:`ONBOARDING_STEPS` is done."""
        return set(ONBOARDING_STEPS).issubset(self.completed_steps)

_WINDOW_BG = SEMANTIC_LIGHT["window_bg"]
_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_GROUPED_BG = SEMANTIC_LIGHT["grouped_bg"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
# WCAG-AA-passing label tints from the token registry — was carrying a
# private sub-AA copy that drifted from the dashboard's audit-F55 fix.
_LABEL_SECONDARY = CX_TEXT_SECONDARY
_LABEL_TERTIARY = CX_TEXT_TERTIARY
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_SUCCESS = SEMANTIC_LIGHT["success"]
_SUCCESS_DIM = "rgba(48, 178, 87, 0.10)"


# ---------------------------------------------------------------------------
# Permission checks (unchanged — keep the AVFoundation + AX paths)
# ---------------------------------------------------------------------------

def check_camera_permission() -> bool:
    try:
        from cortex.libs.utils import check_camera_permission as _check
        return _check()
    except Exception:
        return False


def check_accessibility_permission() -> bool:
    try:
        from cortex.libs.utils import check_accessibility_permission as _check
        return _check()
    except Exception:
        return False


def request_camera_permission() -> None:
    """Trigger the native AVFoundation camera permission dialog."""
    try:
        from cortex.libs.utils.platform import (
            request_camera_permission as _request_camera_permission,
        )

        _request_camera_permission()
        return
    except Exception:
        pass
    try:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
        ])
    except Exception:
        pass


def request_accessibility_permission() -> None:
    try:
        import ApplicationServices  # type: ignore[import-not-found]
        options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        ApplicationServices.AXIsProcessTrustedWithOptions(options)
    except Exception:
        try:
            subprocess.Popen([
                "open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# P0 §3.4 — Calibration card subwidgets
# ---------------------------------------------------------------------------


class _StatusPill(QLabel):
    """Tiny capsule label that flips between a good (terracotta-dim
    success tint) and a poor (neutral) appearance based on a boolean.
    Used by the calibration card to surface lighting / motion / face
    quality at a glance."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(label, parent)
        self._label = label
        self.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(20)
        self.set_ok(False)

    def set_ok(self, ok: bool) -> None:
        if ok:
            self.setText(f"✓ {self._label}")
            self.setStyleSheet(
                f"color: {_SUCCESS}; background: {_SUCCESS_DIM};"
                f" border: none; border-radius: {RADIUS_BUTTON}px;"
                "  padding: 2px 10px;"
            )
        else:
            self.setText(f"○ {self._label}")
            self.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: rgba(0,0,0,0.04);"
                f" border: none; border-radius: {RADIUS_BUTTON}px;"
                "  padding: 2px 10px;"
            )


class _ECGTrace(QWidget):
    """Tiny ECG-style live trace of recent HR samples.

    Renders a horizontal hairline baseline (no data yet) or a smoothed
    polyline through the most recent ~60 samples scaled to the widget
    height. Designed to feel calm — a thin terracotta line on the
    grouped background — rather than a debug oscilloscope.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: collections.deque[float] = collections.deque(maxlen=60)
        self.setMinimumHeight(80)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"background: {_GROUPED_BG}; border-radius: {RADIUS_CARD}px;"
        )

    def push_sample(self, value: float) -> None:
        if value <= 0 or value != value:  # NaN guard
            return
        self._samples.append(value)
        try:
            self.update()
        except Exception:
            pass

    def paintEvent(self, _event: object) -> None:  # noqa: D401 - Qt override
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        except Exception:
            return

        rect = self.rect()
        w = rect.width()
        h = rect.height()
        pad = SP3

        if not self._samples or len(self._samples) < 2:
            # Flat baseline
            pen = QPen(QColor(_LABEL_TERTIARY))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            mid = h // 2
            painter.drawLine(pad, mid, w - pad, mid)
            painter.end()
            return

        samples = list(self._samples)
        smin = min(samples)
        smax = max(samples)
        span = max(1.0, smax - smin)
        n = len(samples)
        step = (w - 2 * pad) / max(1, n - 1)

        pen = QPen(QColor(BRAND_ACCENT))
        pen.setWidthF(1.6)
        painter.setPen(pen)
        if QPainterPath is None:
            # Fallback for old Qt — draw a series of line segments.
            for i in range(n - 1):
                x1 = pad + i * step
                y1 = h - pad - ((samples[i] - smin) / span) * (h - 2 * pad)
                x2 = pad + (i + 1) * step
                y2 = h - pad - ((samples[i + 1] - smin) / span) * (h - 2 * pad)
                painter.drawLine(int(x1), int(y1), int(x2), int(y2))
        else:
            path = QPainterPath()
            for i, v in enumerate(samples):
                x = pad + i * step
                y = h - pad - ((v - smin) / span) * (h - 2 * pad)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.drawPath(path)
        painter.end()


# ---------------------------------------------------------------------------
# Native progress strip — dots connected by hairlines
# ---------------------------------------------------------------------------

class _ProgressStrip(QWidget):
    """Horizontal step indicator: 4 numbered dots, the current one
    rendered as the terracotta brand accent."""

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = count
        self._current = 0
        self._dots: list[QLabel] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # SP2 = 8 — matches the 4pt grid for inter-dot spacing.
        layout.setSpacing(SP2)
        for i in range(count):
            dot = QLabel(str(i + 1))
            dot.setFixedSize(22, 22)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
            self._dots.append(dot)
            layout.addWidget(dot)
            if i < count - 1:
                bar = QFrame()
                bar.setFixedHeight(1)
                bar.setMinimumWidth(20)
                bar.setStyleSheet(f"background: {_SEPARATOR};")
                layout.addWidget(bar, stretch=1)
        self._restyle()

    def set_current(self, index: int) -> None:
        self._current = max(0, min(index, self._count - 1))
        self._restyle()

    def _restyle(self) -> None:
        for i, dot in enumerate(self._dots):
            if i == self._current:
                dot.setStyleSheet(
                    f"background: {BRAND_ACCENT};"
                    f" color: #FFF; border-radius: 11px;"
                )
            elif i < self._current:
                dot.setStyleSheet(
                    f"background: {BRAND_ACCENT_DIM};"
                    f" color: {BRAND_ACCENT}; border-radius: 11px;"
                )
            else:
                dot.setStyleSheet(
                    f"background: {_GROUPED_BG};"
                    f" color: {_LABEL_TERTIARY}; border-radius: 11px;"
                )


# ---------------------------------------------------------------------------
# OnboardingWindow
# ---------------------------------------------------------------------------

class OnboardingWindow(QWidget):
    """Four-step first-run setup. Public Signals unchanged."""

    completed = Signal()
    open_settings_requested = Signal()
    run_calibration_requested = Signal()
    extensions_requested = Signal()
    # Audit-2 fix: emit when the user saves a BYOK token so the running
    # daemon's planner can hot-reload its SDK client without forcing the
    # user to restart Cortex. Without this, the first session after
    # onboarding silently uses the rule-based fallback.
    byok_token_saved = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cortex Setup")
        # Roomy default. The previous 560×620 was shorter than the
        # combined card heights, so Qt compressed widgets into each
        # other (e.g. card 3's region picker + token row overlapped the
        # description and hint paragraphs).
        self.setMinimumSize(600, 720)
        self.resize(640, 820)
        self.setStyleSheet(f"background: {_WINDOW_BG}; color: {_LABEL};")
        # F49: durable per-step completion record. Loaded from disk so
        # a re-entry into the wizard does not lose prior progress.
        self._onboarding_state = OnboardingState.load()
        self._build_ui()

        # Permissions are granted in System Settings out-of-process — there's
        # no callback path back into the app. Poll every 1.5s while the
        # wizard is visible so the "Not granted" pills flip to "Granted"
        # without a relaunch. Timer is paused on hide via showEvent below.
        self._permission_timer = QTimer(self)
        self._permission_timer.setInterval(1500)
        self._permission_timer.timeout.connect(self._refresh_permission_states)
        self._permission_timer.start()

    def _refresh_permission_states(self) -> None:
        try:
            cam = check_camera_permission()
            getattr(self._camera_step, "_cortex_set_state", lambda _b: None)(cam)
        except Exception:
            pass
        try:
            acc = check_accessibility_permission()
            getattr(self._accessibility_step, "_cortex_set_state", lambda _b: None)(acc)
        except Exception:
            pass

    # -- Native chrome ---------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        super().showEvent(event)
        # First-show centering — prevents stale Qt geometry from a previous
        # multi-monitor session from stranding the window off-screen.
        if not getattr(self, "_positioned_once", False):
            try:
                screen = self.screen()
                if screen is not None:
                    geo = screen.availableGeometry()
                    self.move(
                        geo.x() + (geo.width() - self.width()) // 2,
                        geo.y() + max(40, (geo.height() - self.height()) // 4),
                    )
            except Exception:
                pass
            self._positioned_once = True
        try:
            mac_native.apply_unified_titlebar(self)
            mac_native.apply_vibrancy(self, material="popover")
        except Exception:
            pass
        # Resume permission polling whenever the wizard becomes visible.
        try:
            if not self._permission_timer.isActive():
                self._permission_timer.start()
            self._refresh_permission_states()
        except Exception:
            pass

    def hideEvent(self, event: object) -> None:  # noqa: D401 - Qt override
        try:
            self._permission_timer.stop()
        except Exception:
            pass
        super().hideEvent(event)

    def _build_ui(self) -> None:
        # Two-tier layout:
        #   outer: window itself (no margin) → host QScrollArea
        #   inner: scrollable content widget with the actual layout
        # This way the user can shrink the window without overlapping
        # any card, and tall content scrolls naturally.
        try:
            from PySide6.QtWidgets import QScrollArea
        except ImportError:  # pragma: no cover
            QScrollArea = None  # type: ignore[assignment]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        content = QWidget()
        content.setObjectName("CortexOnboardingContent")
        content.setStyleSheet(
            f"#CortexOnboardingContent {{ background: {_WINDOW_BG}; }}"
        )
        layout = QVBoxLayout(content)
        layout.setContentsMargins(SP8, SP8, SP8, SP8)
        layout.setSpacing(SP5)

        if QScrollArea is not None:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet(
                "QScrollArea { border: none; background: transparent; }"
                "QScrollBar:vertical { background: transparent; width: 8px; }"
                "QScrollBar::handle:vertical {"
                "  background: rgba(0,0,0,0.18); border-radius: 4px;"
                "  min-height: 24px;"
                "}"
                "QScrollBar::handle:vertical:hover { background: rgba(0,0,0,0.32); }"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
                "  height: 0;"
                "}"
            )
            scroll.setWidget(content)
            outer.addWidget(scroll)
        else:  # pragma: no cover - test stub path
            outer.addWidget(content)

        # ── Brand wordmark + welcome header ───────────────────────────
        brand = QLabel("Cortex")
        brand.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT}, ui-serif, Georgia, serif;"
            f"font-style: italic; font-size: {FS_BODY}px;"
            f"font-weight: {FW_REGULAR};"
            f"color: {BRAND_ACCENT}; background: transparent;"
        )
        layout.addWidget(brand)
        layout.addSpacing(SP2)

        title = QLabel("Welcome to Cortex")
        title.setFont(mac_native.system_font(FS_TITLE, "bold"))
        title.setStyleSheet(f"color: {_LABEL}; background: transparent;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Grant permissions, choose your LLM backend, and connect your "
            "browser and editor. This only takes a minute."
        )
        subtitle.setWordWrap(True)
        subtitle.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        subtitle.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        layout.addWidget(subtitle)
        layout.addSpacing(SP3)

        # ── Progress strip ────────────────────────────────────────────
        # P0 §3.4: 5 dots — extended to 6 in P0 §3.12 to cover the
        # macOS notifications step.
        self._progress = _ProgressStrip(6)
        layout.addWidget(self._progress)
        layout.addSpacing(SP3)

        # ── Step 1: Camera ────────────────────────────────────────────
        self._camera_step = self._make_step(
            "Camera access",
            "Required for biometric sensing via webcam.",
            check_camera_permission(),
            "Grant Access",
            request_camera_permission,
            "1",
            step_id="camera",
        )
        layout.addWidget(self._camera_step)

        # ── Step 2: Accessibility ─────────────────────────────────────
        self._accessibility_step = self._make_step(
            "Accessibility",
            "Required for keyboard and mouse tracking.",
            check_accessibility_permission(),
            "Grant Access",
            request_accessibility_permission,
            "2",
            step_id="accessibility",
        )
        layout.addWidget(self._accessibility_step)

        # ── Step 3: LLM backend ───────────────────────────────────────
        self._llm_step = self._make_llm_step()
        layout.addWidget(self._llm_step)

        # ── Step 4: Calibration ───────────────────────────────────────
        # P0 §3.4 — new wizard step. Emits ``run_calibration_requested``
        # which the controller routes to ``CalibrationRunner.start(...)``.
        self._calibration_step = self._make_calibration_step()
        layout.addWidget(self._calibration_step)

        # ── Step 5: Connect Extensions ────────────────────────────────
        ext_frame = self._make_section("5", "Connect extensions", step_id="extensions")
        self._extensions_step = ext_frame
        ext_layout = ext_frame.layout()
        hint = QLabel(
            "Install the browser and editor extensions to give Cortex "
            "context about your tabs and code. You can also do this "
            "later from the menu bar → Connect Extensions."
        )
        hint.setWordWrap(True)
        hint.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        hint.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none;"
        )
        ext_layout.addWidget(hint)

        connect_btn = QPushButton("Open Connections")
        connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        connect_btn.setMinimumHeight(34)
        connect_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        connect_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 6px 16px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        connect_btn.clicked.connect(self.extensions_requested.emit)
        set_accessible_name(connect_btn, "Open Connections panel")
        set_accessible_description(
            connect_btn,
            "Install the Cortex browser and editor extensions.",
        )
        self._connect_btn_ref = connect_btn
        ext_layout.addWidget(connect_btn)
        layout.addWidget(ext_frame)

        # ── Step 6: macOS Notifications (P0 §3.12) ───────────────────
        notif_frame = self._make_section(
            "6", "macOS notifications",
            step_id="macos_notifications",
        )
        self._notifications_step = notif_frame
        notif_layout = notif_frame.layout()
        notif_hint = QLabel(
            "Allow Cortex to send macOS notifications so you see "
            "important interventions even when you're in another app "
            "or full-screen mode. You can change this in System "
            "Settings anytime."
        )
        notif_hint.setWordWrap(True)
        notif_hint.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        notif_hint.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none;"
        )
        notif_layout.addWidget(notif_hint)

        notif_btn = QPushButton("Enable notifications")
        notif_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        notif_btn.setMinimumHeight(34)
        notif_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        notif_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 6px 16px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        notif_btn.clicked.connect(self._on_request_notifications)
        set_accessible_name(notif_btn, "Enable macOS notifications")
        set_accessible_description(
            notif_btn,
            "Triggers the macOS notification permission prompt.",
        )
        self._notif_btn_ref = notif_btn
        notif_layout.addWidget(notif_btn)
        layout.addWidget(notif_frame)

        layout.addStretch()

        # ── Finish bar ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        finish_btn = QPushButton("Get Started")
        finish_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        finish_btn.setMinimumHeight(38)
        finish_btn.setMinimumWidth(140)
        finish_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        finish_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 8px 24px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {_LABEL};"
            "  color: #FFF; border: none;"
            "}"
            "QPushButton:hover { background: #333; }"
        )
        finish_btn.clicked.connect(self._on_finish)
        set_accessible_name(finish_btn, "Finish onboarding")
        set_accessible_description(
            finish_btn,
            "Mark every onboarding step complete and start Cortex.",
        )
        btn_row.addWidget(finish_btn)
        layout.addLayout(btn_row)

        # audit-w2 (F55 carry-over): chain tab order across the wizard.
        # Step 1 + 2 Grant buttons live behind a closure (``_cortex_set_state``);
        # we walk the BYOK card → Connect Extensions → Get Started
        # explicitly so the keyboard user lands on the primary actions
        # without bouncing through every non-interactive label.
        chain_targets = [
            w
            for w in (
                getattr(self, "_region_combo", None),
                getattr(self, "_key_input", None),
                getattr(self, "_save_key_btn", None),
                getattr(self, "_begin_calibration_btn", None),
                connect_btn,
                finish_btn,
            )
            if w is not None
        ]
        chain_tab_order(*chain_targets)

    # ------------------------------------------------------------------
    # F49: completion-marker hooks
    # ------------------------------------------------------------------

    def _on_request_notifications(self) -> None:
        """P0 §3.12: trigger the macOS notification permission prompt.

        Sends a single welcoming notification through
        ``send_intervention_notification``; the helper internally calls
        ``requestAuthorization`` if the user hasn't granted yet, and
        the welcome notification surfaces once permission is granted.
        On non-mac / missing PyObjC the call is a no-op — we still
        mark the step complete because the underlying capability isn't
        available anyway.
        """
        try:
            from cortex.libs.utils.macos_notifications import (
                send_intervention_notification,
            )
            send_intervention_notification(
                title="Cortex notifications enabled",
                body="You'll see interventions here when the dashboard isn't active.",
                intervention_id="cortex_welcome",
            )
        except Exception:
            logger.debug(
                "send_intervention_notification welcome failed", exc_info=True,
            )
        self.mark_step_complete("macos_notifications")
        # Visually mark the section as complete.
        if hasattr(self, "_notif_btn_ref"):
            try:
                self._notif_btn_ref.setText("Notifications enabled ✓")
                self._notif_btn_ref.setEnabled(False)
            except Exception:
                pass

    def _on_finish(self) -> None:
        """Click handler for the Get Started button. Persists every step
        as complete BEFORE re-emitting ``completed`` so a crash between
        the click and the daemon-launch path does not lose progress."""
        for step in ONBOARDING_STEPS:
            try:
                self._onboarding_state.mark_complete(step)
            except OSError:
                # Atomic write failed (disk full, read-only filesystem).
                # Log but don't block the user from finishing onboarding.
                logger.warning(
                    "Failed to persist onboarding step %s", step,
                    exc_info=True,
                )
        self.completed.emit()

    def mark_step_complete(self, step: str) -> None:
        """Public hook for individual step affordances (camera grant,
        accessibility grant, BYOK save, Connections panel close) to mark
        that step complete the moment the user finishes it — independent
        of whether they click Get Started. F49."""
        try:
            self._onboarding_state.mark_complete(step)
        except (OSError, ValueError):
            logger.warning(
                "Failed to mark onboarding step %s complete", step,
                exc_info=True,
            )

    def mark_step_incomplete(self, step: str) -> None:
        """Inverse of :meth:`mark_step_complete` — used when the user
        navigates 'back' to re-edit a step. F49."""
        try:
            self._onboarding_state.mark_incomplete(step)
        except (OSError, ValueError):
            logger.warning(
                "Failed to mark onboarding step %s incomplete", step,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Section helpers
    # ------------------------------------------------------------------

    def _make_section(
        self,
        number: str,
        title: str,
        *,
        step_id: str | None = None,
    ) -> QFrame:
        frame = QFrame()
        # Scope to objectName so the QFrame stylesheet (background +
        # 0.5px hairline + 8px radius) doesn't cascade onto every
        # QLabel/QPushButton descendant (those classes inherit QFrame
        # in Qt and would otherwise pick up the white background +
        # border, scrambling text rendering).
        frame.setObjectName("CortexOnbStep")
        frame.setStyleSheet(
            "QFrame#CortexOnbStep {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(SP4, SP4, SP4, SP4)
        layout.setSpacing(SP3)

        header = QHBoxLayout()
        header.setSpacing(SP3)

        num_label = QLabel(number)
        num_label.setFixedSize(22, 22)
        num_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        num_label.setStyleSheet(
            f"color: {BRAND_ACCENT}; background: {BRAND_ACCENT_DIM};"
            f" border: none; border-radius: 11px;"
        )
        header.addWidget(num_label)

        heading = QLabel(title)
        heading.setFont(mac_native.system_font(FS_BODY, "semibold"))
        heading.setStyleSheet(
            f"color: {_LABEL}; border: none; background: transparent;"
        )
        header.addWidget(heading)
        header.addStretch()

        # Phase J-1: "Why we need this" expand-on-click chevron. The
        # chevron sits on the right of the header so it doesn't compete
        # with the primary action button below; clicking it toggles a
        # collapsible paragraph with the rationale.
        if step_id and step_id in _WHY_COPY:
            why_btn = QPushButton("Why?  ›")
            why_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            why_btn.setCheckable(True)
            why_btn.setFont(mac_native.system_font(FS_CAPTION, "medium"))
            why_btn.setStyleSheet(
                "QPushButton {"
                "  padding: 2px 8px;"
                f"  border-radius: {RADIUS_BUTTON}px;"
                "  background: transparent;"
                f"  color: {BRAND_ACCENT};"
                "  border: none;"
                "}"
                f"QPushButton:hover {{ color: {BRAND_ACCENT_HOVER}; }}"
            )
            set_accessible_name(why_btn, f"Why Cortex needs {title}")
            set_accessible_description(
                why_btn,
                "Expand a short explanation of why Cortex requests this.",
            )
            header.addWidget(why_btn)
            layout.addLayout(header)

            why_body = QLabel(_WHY_COPY[step_id])
            why_body.setWordWrap(True)
            why_body.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            why_body.setStyleSheet(
                f"color: {_LABEL_SECONDARY}; border: none; background: transparent;"
            )
            why_body.setVisible(False)
            set_accessible_name(why_body, f"{title} rationale")
            layout.addWidget(why_body)

            def _toggle_why(checked: bool, body: QLabel = why_body, btn: QPushButton = why_btn) -> None:
                body.setVisible(checked)
                btn.setText("Why?  ⌄" if checked else "Why?  ›")

            why_btn.toggled.connect(_toggle_why)
            # Stash refs on the frame so tests (and future surfaces) can
            # introspect the expander without re-walking the layout.
            frame._cortex_why_btn = why_btn  # type: ignore[attr-defined]
            frame._cortex_why_body = why_body  # type: ignore[attr-defined]
        else:
            layout.addLayout(header)

        return frame

    def _make_step(
        self,
        title: str,
        description: str,
        granted: bool,
        btn_text: str,
        action: object,
        number: str,
        *,
        step_id: str | None = None,
    ) -> QFrame:
        """Build a permission step.

        The status pill + Grant button are kept as attributes on the frame
        so the polling timer (``_refresh_permission_states``) can flip them
        when the user grants the underlying OS permission without forcing
        the user to relaunch the wizard. This addresses the bug where
        granting Accessibility in System Settings didn't update the
        onboarding "Not granted" pill.
        """
        frame = self._make_section(number, title, step_id=step_id)
        layout = frame.layout()

        desc = QLabel(description)
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none;"
        )
        layout.addWidget(desc)

        # Phase J-1: surface Continuity Camera skip rationale on the
        # Camera step. The webcam.py logic already deprioritises
        # iPhone/iPad cameras silently; this inline callout tells the
        # user explicitly so they aren't surprised when a paired iPhone
        # doesn't drive the biometrics.
        if step_id == "camera" and _detect_continuity_camera():
            callout = QLabel(
                "We will skip your iPhone camera and use the MacBook camera."
            )
            callout.setObjectName("CortexContinuityCallout")
            callout.setWordWrap(True)
            callout.setFont(mac_native.system_font(FS_CAPTION, "medium"))
            callout.setStyleSheet(
                "QLabel#CortexContinuityCallout {"
                f"  color: {BRAND_ACCENT};"
                f"  background: {BRAND_ACCENT_DIM};"
                f"  border-radius: {RADIUS_BUTTON}px;"
                "  padding: 6px 10px;"
                "}"
            )
            set_accessible_name(callout, "Continuity Camera skip notice")
            layout.addWidget(callout)
            frame._cortex_continuity_callout = callout  # type: ignore[attr-defined]

        row = QHBoxLayout()

        status = QLabel("")
        status.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        row.addWidget(status)
        row.addStretch()

        btn = QPushButton(btn_text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(28)
        btn.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        btn.setStyleSheet(
            "QPushButton {"
            "  padding: 4px 12px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        if callable(action):
            btn.clicked.connect(action)
        set_accessible_name(btn, f"{title} — {btn_text}")
        set_accessible_name(status, f"{title} status")
        row.addWidget(btn)

        layout.addLayout(row)

        def _set_state(is_granted: bool) -> None:
            if is_granted:
                status.setText("Granted")
                status.setStyleSheet(
                    f"color: {_SUCCESS}; background: {_SUCCESS_DIM};"
                    f" border: none; border-radius: {RADIUS_BUTTON}px;"
                    "  padding: 3px 8px;"
                )
                btn.setVisible(False)
            else:
                status.setText("Not granted")
                status.setStyleSheet(
                    f"color: {_LABEL_TERTIARY}; background: rgba(0,0,0,0.04);"
                    f" border: none; border-radius: {RADIUS_BUTTON}px;"
                    "  padding: 3px 8px;"
                )
                btn.setVisible(True)

        _set_state(bool(granted))
        # Stash the refresh closure on the frame so the polling timer can
        # call it without re-resolving widgets by index.
        frame._cortex_set_state = _set_state  # type: ignore[attr-defined]
        return frame

    # ------------------------------------------------------------------
    # P0 §3.4 — Calibration step
    # ------------------------------------------------------------------

    def _make_calibration_step(self) -> QFrame:
        """Build the calibration card (step 4 of 5).

        The card hosts:

        * ECG-style live trace (`_ECGTrace`) at the top.
        * Three good/poor status pills (lighting, motion, face).
        * Live HR / HRV / SQI numerics.
        * A primary Begin button that emits ``run_calibration_requested``.
        * A horizontal progress bar that fills over the 120 s.
        * A subtle "Skip — use generic baselines" link.

        On successful completion the controller calls
        :meth:`apply_calibration_progress` with status ``completed``;
        we then mark the step complete and swap the Begin button for a
        success label.
        """
        frame = self._make_section("4", "Calibration", step_id="calibration")
        layout = frame.layout()

        desc = QLabel(
            "Sit calmly for 2 minutes while Cortex learns your resting baseline."
        )
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(f"color: {_LABEL_SECONDARY}; border: none;")
        layout.addWidget(desc)

        # ECG trace — gives the user something to look at and signals
        # liveness once HR samples arrive.
        self._ecg_trace = _ECGTrace()
        self._ecg_trace.setFixedHeight(120)
        self._ecg_trace.setMinimumWidth(420)
        layout.addWidget(self._ecg_trace)

        # Three status pills (lighting / motion / face).
        pill_row = QHBoxLayout()
        pill_row.setSpacing(SP2)
        self._cal_lighting_pill = _StatusPill("Lighting")
        self._cal_motion_pill = _StatusPill("Motion")
        self._cal_face_pill = _StatusPill("Face")
        pill_row.addWidget(self._cal_lighting_pill)
        pill_row.addWidget(self._cal_motion_pill)
        pill_row.addWidget(self._cal_face_pill)
        pill_row.addStretch()
        layout.addLayout(pill_row)

        # Live numerics line.
        self._cal_numerics = QLabel("HR: — bpm  ·  HRV: — ms  ·  SQI: —")
        self._cal_numerics.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._cal_numerics.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; border: none; background: transparent;"
        )
        layout.addWidget(self._cal_numerics)

        # Progress bar.
        self._cal_progress_bar = QProgressBar()
        self._cal_progress_bar.setRange(0, 100)
        self._cal_progress_bar.setValue(0)
        self._cal_progress_bar.setTextVisible(False)
        self._cal_progress_bar.setFixedHeight(6)
        self._cal_progress_bar.setStyleSheet(
            "QProgressBar {"
            f"  background: {_GROUPED_BG};"
            "  border: none;"
            "  border-radius: 3px;"
            "}"
            "QProgressBar::chunk {"
            f"  background: {BRAND_ACCENT};"
            "  border-radius: 3px;"
            "}"
        )
        layout.addWidget(self._cal_progress_bar)

        # Action row — Begin button + Skip link.
        action_row = QHBoxLayout()
        begin_btn = QPushButton("Begin")
        begin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        begin_btn.setMinimumHeight(32)
        begin_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        begin_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 6px 20px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
            "QPushButton:disabled { background: rgba(0,0,0,0.10); color: rgba(0,0,0,0.30); }"
        )
        begin_btn.clicked.connect(self._on_begin_calibration)
        set_accessible_name(begin_btn, "Begin calibration")
        set_accessible_description(
            begin_btn,
            "Start the 2-minute calibration capture. Cortex will read your resting baseline.",
        )
        self._begin_calibration_btn = begin_btn
        action_row.addWidget(begin_btn)

        skip_btn = QPushButton("Skip — use generic baselines")
        skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        skip_btn.setFlat(True)
        skip_btn.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        skip_btn.setStyleSheet(
            "QPushButton {"
            "  background: transparent; border: none;"
            f"  color: {_LABEL_TERTIARY};"
            "  text-decoration: underline;"
            "  padding: 4px 8px;"
            "}"
            f"QPushButton:hover {{ color: {_LABEL_SECONDARY}; }}"
        )
        skip_btn.clicked.connect(self._on_skip_calibration)
        set_accessible_name(skip_btn, "Skip calibration")
        set_accessible_description(
            skip_btn,
            "Skip calibration and use generic baselines. You can recalibrate later from Settings.",
        )
        self._skip_calibration_btn = skip_btn
        action_row.addStretch()
        action_row.addWidget(skip_btn)
        layout.addLayout(action_row)

        # Success label — hidden until completion.
        self._cal_success_label = QLabel("✓ Recalibrated · baselines saved")
        self._cal_success_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._cal_success_label.setStyleSheet(
            f"color: {_SUCCESS}; background: {_SUCCESS_DIM};"
            f" border: none; border-radius: {RADIUS_BUTTON}px;"
            "  padding: 4px 10px;"
        )
        self._cal_success_label.setVisible(False)
        layout.addWidget(self._cal_success_label)

        return frame

    def _on_begin_calibration(self) -> None:
        """Click handler for the Begin button. Disables itself and emits
        the dormant ``run_calibration_requested`` signal so the
        controller can spin up a ``CalibrationRunner``."""
        try:
            self._begin_calibration_btn.setEnabled(False)
            self._cal_progress_bar.setValue(1)
            self._cal_numerics.setText("HR: — bpm  ·  HRV: — ms  ·  SQI: starting…")
        except AttributeError:
            pass
        self.run_calibration_requested.emit()

    def _on_skip_calibration(self) -> None:
        """User opted out of calibration. We don't write a baseline, but
        we do mark the step complete so the wizard can finish — the
        dashboard will surface a yellow 'Calibrate now' prompt later."""
        self.mark_step_complete("calibration")
        try:
            self._begin_calibration_btn.setVisible(False)
            self._skip_calibration_btn.setVisible(False)
            self._cal_progress_bar.setVisible(False)
            self._cal_success_label.setText("Skipped — using generic baselines")
            self._cal_success_label.setVisible(True)
        except AttributeError:
            pass

    def apply_calibration_progress(
        self,
        *,
        elapsed_seconds: float,
        total_seconds: float,
        current_hr: float | None,
        current_hrv: float | None,
        current_sqi: float | None,
        lighting_ok: bool,
        motion_ok: bool,
        face_ok: bool,
        pct_complete: float,
        status: str,
    ) -> None:
        """Slot wired by the controller to the calibration progress
        callback. Updates the live trace, pills, numerics, and bar.

        Marshalled onto the Qt main thread by the controller before
        this is invoked.
        """
        try:
            self._cal_progress_bar.setValue(int(round(pct_complete)))
        except AttributeError:
            pass
        try:
            hr_text = f"{current_hr:.0f}" if current_hr is not None else "—"
            hrv_text = f"{current_hrv:.0f}" if current_hrv is not None else "—"
            sqi_text = f"{current_sqi:.2f}" if current_sqi is not None else "—"
            self._cal_numerics.setText(
                f"HR: {hr_text} bpm  ·  HRV: {hrv_text} ms  ·  SQI: {sqi_text}"
            )
        except AttributeError:
            pass
        try:
            self._cal_lighting_pill.set_ok(lighting_ok)
            self._cal_motion_pill.set_ok(motion_ok)
            self._cal_face_pill.set_ok(face_ok)
        except AttributeError:
            pass
        try:
            if current_hr is not None:
                self._ecg_trace.push_sample(float(current_hr))
        except AttributeError:
            pass

        if status == "completed":
            self.mark_step_complete("calibration")
            try:
                self._cal_progress_bar.setValue(100)
                self._begin_calibration_btn.setVisible(False)
                self._skip_calibration_btn.setVisible(False)
                self._cal_success_label.setVisible(True)
            except AttributeError:
                pass
        elif status in ("aborted", "failed"):
            try:
                self._begin_calibration_btn.setEnabled(True)
            except AttributeError:
                pass

    def _make_llm_step(self) -> QFrame:
        frame = self._make_section("3", "AWS Bedrock bearer token", step_id="llm_backend")
        layout = frame.layout()

        desc = QLabel(
            "Cortex calls Anthropic Claude via AWS Bedrock. Paste your "
            "long-lived bearer token below — it's stored only in the macOS "
            "Keychain and never written to disk."
        )
        desc.setWordWrap(True)
        desc.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        desc.setStyleSheet(f"color: {_LABEL_SECONDARY}; border: none;")
        layout.addWidget(desc)

        config = get_config()

        region_combo = QComboBox()
        region_combo.addItems([
            "us-east-2", "us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2",
        ])
        region_combo.setCurrentText(config.llm.bedrock.aws_region)
        region_combo.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        # Explicit min-height so the combo doesn't get squashed when Qt
        # tries to fit a too-tall card into a too-short window.
        region_combo.setMinimumHeight(30)
        region_combo.setStyleSheet(
            "QComboBox {"
            f"  color: {_LABEL};"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 12px;"
            "}"
        )
        self._region_combo = region_combo
        layout.addWidget(region_combo)

        key_row = QHBoxLayout()
        key_row.setSpacing(SP2)
        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("AWS Bedrock bearer token")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._key_input.setMinimumHeight(32)
        self._key_input.setStyleSheet(
            "QLineEdit {"
            f"  color: {_LABEL};"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  padding: 6px 12px;"
            "}"
            f"QLineEdit:focus {{ border: 1.5px solid {BRAND_ACCENT}; }}"
        )
        key_row.addWidget(self._key_input)

        save_key_btn = QPushButton("Save")
        save_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_key_btn.setMinimumHeight(32)
        save_key_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        save_key_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 6px 16px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            f"  background: {BRAND_ACCENT};"
            "  color: #FFF; border: none;"
            "}"
            f"QPushButton:hover {{ background: {BRAND_ACCENT_HOVER}; }}"
        )
        save_key_btn.clicked.connect(self._save_api_key)
        set_accessible_name(save_key_btn, "Save Bedrock bearer token")
        set_accessible_description(
            save_key_btn,
            "Store the AWS Bedrock bearer token in the macOS Keychain.",
        )
        # Stash so the wizard's tab-order chain can pin focus.
        self._save_key_btn = save_key_btn
        set_accessible_name(self._key_input, "Bedrock bearer token")
        set_accessible_name(region_combo, "Bedrock AWS region")
        key_row.addWidget(save_key_btn)

        self._key_widget = QWidget()
        self._key_widget.setLayout(key_row)
        layout.addWidget(self._key_widget)

        has_key = False
        try:
            # Phase-4a Debt-1: use ``get_password_safe`` so a wedged
            # Keychain unlock sheet cannot pin the onboarding wizard.
            from cortex.libs.utils.secrets import get_password_safe
            existing = get_password_safe(
                config.llm.bedrock.keychain_service,
                config.llm.bedrock.keychain_account,
            )
            has_key = bool(existing)
        except Exception:
            pass

        if has_key:
            saved_label = QLabel("Bedrock bearer token found in Keychain")
            saved_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            saved_label.setStyleSheet(
                f"color: {_SUCCESS}; border: none;"
            )
            layout.addWidget(saved_label)

        hint = QLabel(
            "Cortex calls Claude via AWS Bedrock inference profiles  ·  "
            "Stored in macOS Keychain (service: cortex.bedrock)  ·  "
            "Without a token, the daemon falls back to rule-based plans."
        )
        hint.setWordWrap(True)
        hint.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        hint.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; border: none;"
        )
        layout.addWidget(hint)

        return frame

    def _save_api_key(self) -> None:
        key = self._key_input.text().strip()
        if not key:
            QMessageBox.warning(self, "Error", "Please paste a Bedrock bearer token.")
            return
        # Bedrock bearer tokens are JWT-shaped and run 100+ chars; anything under
        # 20 is almost certainly a paste error (e.g. truncated copy, AWS account
        # ID, profile name). Catch this before we write garbage to the Keychain.
        if len(key) < 20:
            QMessageBox.warning(
                self,
                "Token looks too short",
                "Token looks too short — Bedrock tokens are typically 100+ chars.",
            )
            return
        try:
            import keyring
            config = get_config()
            keyring.set_password(
                config.llm.bedrock.keychain_service,
                config.llm.bedrock.keychain_account,
                key,
            )
            try:
                config.llm.bedrock.aws_region = self._region_combo.currentText()
            except AttributeError:
                pass
            # Audit-2 fix: signal the running daemon so its planner
            # hot-reloads the new token. The user no longer has to
            # restart Cortex for the first session to use BYOK.
            try:
                self.byok_token_saved.emit()
            except Exception:
                logger.debug("byok_token_saved emit failed", exc_info=True)
            QMessageBox.information(
                self,
                "Saved",
                "Bedrock bearer token saved to macOS Keychain. "
                "Cortex will use it for the next intervention.",
            )
            self._key_input.clear()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to save token:\n{e}")


def onboarding_marker_path() -> Path:
    return Path(get_config().storage.path) / ".onboarding_complete"
