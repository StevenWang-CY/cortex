"""Desktop Shell — History Tab (P0 §3.1 + §3.2).

Third page of the dashboard's ``QStackedWidget``. Houses three sub-views:

* **Today**  ─ paginated session listing (``REQUEST_SESSION_LIST`` /
  ``SESSION_LIST``) plus an inline overlay detail panel
  (``REQUEST_SESSION_DETAIL`` / ``SESSION_DETAIL``).
* **Week**  ─ rolling 7-day rollup with golden-hour stripe and
  trend-direction pill (``REQUEST_TRENDS week`` /
  ``TRENDS_PAYLOAD``).
* **Month** ─ rolling 30-day rollup (``REQUEST_TRENDS month``).

The widget owns three Qt signals — ``history_requested``,
``detail_requested``, ``trends_requested`` — that the controller wires to
the daemon. The widget never reaches into the daemon itself; everything
goes through the request/response WS envelopes already specified in
:mod:`cortex.libs.schemas.session_history`.

All inbound payloads come through the three ``apply_*`` slots which the
``DashboardWindow`` delegates from its bridge subscriptions.

A render cache (mirrors the consumer dashboard's ``_set_text_if_changed``
/ ``_set_style_if_changed`` pattern) keeps repeated apply calls free of
unnecessary widget churn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal

try:
    from PySide6.QtCore import QRectF
except ImportError:  # pragma: no cover
    from PySide6.QtCore import QRect as QRectF

try:
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
except ImportError:  # pragma: no cover - test stubs
    from PySide6.QtGui import QColor, QFont, QPainter, QPen  # type: ignore

    class QPainterPath:  # type: ignore[override]
        def addRoundedRect(self, *_a: Any, **_kw: Any) -> None: ...

try:
    from PySide6.QtWidgets import (
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMenu,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover - test stubs
    from PySide6.QtWidgets import (  # type: ignore[attr-defined]
        QFrame,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    class QFileDialog(QWidget):  # type: ignore[override]
        @staticmethod
        def getSaveFileName(
            *_a: Any, **_kw: Any,
        ) -> tuple[str, str]:
            return ("", "")

    class QMenu(QWidget):  # type: ignore[override]
        def addAction(self, *_a: Any, **_kw: Any) -> object:
            class _A:
                def triggered(self) -> object:
                    class _S:
                        def connect(self, *_a: Any, **_kw: Any) -> None:
                            pass
                    return _S()
            return _A()

        def exec(self, *_a: Any, **_kw: Any) -> object:
            return None

    class QScrollArea(QWidget):  # type: ignore[override]
        def setWidgetResizable(self, *_a: Any, **_kw: Any) -> None: ...

        def setWidget(self, *_a: Any, **_kw: Any) -> None: ...

        def widget(self) -> Any:
            return None

    class QSizePolicy:  # type: ignore[override]
        class Policy:
            Expanding = 0
            Preferred = 0
            Minimum = 0

    class QStackedWidget(QWidget):  # type: ignore[override]
        def addWidget(self, *_a: Any, **_kw: Any) -> None: ...

        def setCurrentIndex(self, *_a: Any, **_kw: Any) -> None: ...

        def currentIndex(self) -> int:
            return 0

from cortex.apps.desktop_shell import mac_native
from cortex.apps.desktop_shell.tokens import (
    BRAND_ACCENT,
    BRAND_ACCENT_DIM,
    BRAND_ACCENT_HOVER,
    BRAND_DISPLAY_FONT,
    CX_TEXT_SECONDARY,
    CX_TEXT_TERTIARY,
    DASHBOARD_WIDTH,
    FS_CAPTION,
    FS_FOOTNOTE,
    FS_TITLE,
    FW_SEMIBOLD,
    RADIUS_BUTTON,
    RADIUS_CARD,
    RADIUS_PILL,
    SEMANTIC_LIGHT,
    SP1,
    SP2,
    SP3,
    SP4,
    STATE_COLORS,
    STATE_LABELS,
)

logger = logging.getLogger(__name__)


_CONTROL_BG = SEMANTIC_LIGHT["control_bg"]
_GROUPED_BG = SEMANTIC_LIGHT["grouped_bg"]
_WINDOW_BG = SEMANTIC_LIGHT["window_bg"]
_SEPARATOR = SEMANTIC_LIGHT["separator"]
_LABEL = SEMANTIC_LIGHT["label_primary"]
_LABEL_SECONDARY = CX_TEXT_SECONDARY
_LABEL_TERTIARY = CX_TEXT_TERTIARY
_SUCCESS = SEMANTIC_LIGHT["success"]
_WARNING = SEMANTIC_LIGHT["warning"]


def _safe(target: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return target(*args, **kwargs)
    except Exception:
        return None


def _hash_payload(value: Any) -> str:
    """Stable hash for render-cache keying. Falls back to repr if the
    payload contains non-JSON-able objects (datetimes are stringified by
    ``model_dump(mode='json')`` so this should rarely trigger)."""
    try:
        return hashlib.sha1(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:
        return hashlib.sha1(repr(value).encode("utf-8")).hexdigest()


def _format_duration(seconds: float) -> str:
    """Compact human duration: ``1h 24m`` / ``38m`` / ``42s``."""
    try:
        s = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        s = 0
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m, _ = divmod(rem, 60)
        return f"{h}h {m:02d}m" if m else f"{h}h"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def _format_date_time(iso: str) -> str:
    """ISO timestamp → ``Mon 5/22 · 3:14 PM`` (best-effort)."""
    if not iso:
        return ""
    try:
        # The wire format from ``model_dump(mode='json')`` is ISO 8601.
        from datetime import datetime

        # Tolerate Z suffix.
        normalised = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
        dt = datetime.fromisoformat(normalised)
        return dt.strftime("%a %-m/%-d · %-I:%M %p")
    except Exception:
        return iso[:16].replace("T", " ")


def _short_date(iso: str) -> str:
    """ISO date or datetime → ``5/22`` (used on bar-chart axes)."""
    if not iso:
        return ""
    try:
        from datetime import date, datetime

        if "T" in iso:
            normalised = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
            d = datetime.fromisoformat(normalised).date()
        else:
            d = date.fromisoformat(iso)
        return d.strftime("%-m/%-d")
    except Exception:
        return iso[5:10] if len(iso) >= 10 else iso


def _weekday(iso: str) -> str:
    """ISO date or datetime → ``Mon`` (3-letter abbreviation)."""
    if not iso:
        return ""
    try:
        from datetime import date, datetime

        if "T" in iso:
            normalised = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
            d = datetime.fromisoformat(normalised).date()
        else:
            d = date.fromisoformat(iso)
        return d.strftime("%a")
    except Exception:
        return iso[:3]


def _format_relative_age(iso: str | None) -> tuple[str, bool]:
    """Format ``iso`` as a "Updated <relative>" label string.

    Returns ``(text, stale)`` where ``stale`` is True when the age is
    >= 24 h (used to apply the BRAND_ACCENT staleness hint). Handles
    edge cases:

    * ``None`` / empty → ``("Not yet aggregated", False)``
    * Future timestamps (clock drift) → ``("Updated just now", False)``
    * Naive ISO strings (no tz) → treated as UTC.
    * Parse failure → ``("Updated recently", False)`` as a graceful fallback.
    """
    if not iso:
        return ("Not yet aggregated", False)
    try:
        from datetime import UTC, datetime

        norm = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        delta_s = (now - dt).total_seconds()
    except Exception:
        logger.debug("relative-age parse failed for %r", iso, exc_info=True)
        return ("Updated recently", False)
    # Clock drift / future timestamps: clamp to "just now" so the UI
    # doesn't render "Updated in 30s" which would confuse the user.
    if delta_s < 60:
        return ("Updated just now", False)
    if delta_s < 3600:
        minutes = int(delta_s // 60)
        return (f"Updated {minutes}m ago", False)
    if delta_s < 86400:
        hours = int(delta_s // 3600)
        return (f"Updated {hours}h ago", False)
    days = int(delta_s // 86400)
    return (f"Updated {days}d ago", True)


# Phase 4.B fix (#13): after this many ms the detail-panel loading state
# converts into a timeout error so the user isn't left staring at a
# spinner if the daemon never replies. 8 s gives slow disks / corrupt
# files enough time but doesn't make the user wait too long.
_DETAIL_TIMEOUT_MS = 8_000

# Phase 4.B fix (#19): the "Refreshing…" pill resets after this many ms
# even if no payload arrives — defensive bound so a missed broadcast
# doesn't permanently disable the Refresh button.
_REFRESH_RESET_MS = 10_000


# ---------------------------------------------------------------------------
# _RenderCache mixin — port of the consumer-tab pattern.
# ---------------------------------------------------------------------------


class _RenderCacheMixin:
    """``QObject``-friendly render-cache helpers.

    Mirrors :meth:`_ConsumerTab._set_text_if_changed` so the History tab
    stays as cheap to re-apply as the consumer dashboard.
    """

    def _cache(self) -> dict[int, dict[str, Any]]:
        cache = getattr(self, "_render_cache_dict", None)
        if cache is None:
            cache = {}
            self._render_cache_dict = cache  # type: ignore[attr-defined]
        return cache

    def _set_text_if_changed(self, widget: QLabel, text: str) -> bool:
        slot = self._cache().setdefault(id(widget), {})
        if slot.get("text") == text:
            return False
        slot["text"] = text
        try:
            widget.setText(text)
        except Exception:
            return False
        return True

    def _set_style_if_changed(self, widget: QWidget, qss: str) -> bool:
        slot = self._cache().setdefault(id(widget), {})
        if slot.get("style") == qss:
            return False
        slot["style"] = qss
        try:
            widget.setStyleSheet(qss)
        except Exception:
            return False
        return True


# ---------------------------------------------------------------------------
# Sub-segmented control (Today / Week / Month).
# ---------------------------------------------------------------------------


class _SubSegmented(QWidget):
    """Mini three-segment control. Visually lighter than the top-level
    one because it nests inside a tab content area."""

    selection_changed = Signal(int)

    def __init__(self, labels: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._buttons: list[QPushButton] = []

        track = QFrame()
        track.setObjectName("_subseg_track")
        track.setStyleSheet(
            f"#_subseg_track {{ background: {_GROUPED_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: 7px; }}"
        )
        track_layout = QHBoxLayout(track)
        track_layout.setContentsMargins(3, 3, 3, 3)
        track_layout.setSpacing(2)
        for i, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
            try:
                btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            except Exception:
                pass
            _safe(btn.setAccessibleName, f"{label} history view")
            btn.setStyleSheet(
                "QPushButton {"
                "  padding: 3px 12px;"
                "  border-radius: 5px;"
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
            btn.clicked.connect(lambda _checked=False, idx=i: self._on_clicked(idx))
            track_layout.addWidget(btn, stretch=1)
            self._buttons.append(btn)
        outer.addWidget(track, stretch=1)
        if self._buttons:
            self._buttons[0].setChecked(True)

    def _on_clicked(self, index: int) -> None:
        for i, b in enumerate(self._buttons):
            b.setChecked(i == index)
        self.selection_changed.emit(index)

    def set_selected(self, index: int) -> None:
        if 0 <= index < len(self._buttons):
            for i, b in enumerate(self._buttons):
                b.setChecked(i == index)


# ---------------------------------------------------------------------------
# Session row — one entry in the Today list.
# ---------------------------------------------------------------------------


class _SessionRow(_RenderCacheMixin, QFrame):
    """One clickable row in the session list. Shows date / duration /
    flow % / peak stress / top domain. Click anywhere on the row → emits
    :attr:`clicked`."""

    clicked = Signal(str)

    def __init__(self, summary: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("_SessionRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "#_SessionRow {"
            f"  background: {_CONTROL_BG};"
            f"  border: 0.5px solid {_SEPARATOR};"
            f"  border-radius: {RADIUS_CARD}px;"
            "}"
            "#_SessionRow:hover {"
            f"  background: {BRAND_ACCENT_DIM};"
            "}"
        )
        try:
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass

        self._session_id = str(summary.get("session_id") or "")
        _safe(
            self.setAccessibleName,
            f"Session {_format_date_time(str(summary.get('start_time') or ''))}",
        )
        _safe(
            self.setAccessibleDescription,
            "Press Enter or click to view this session report",
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SP4, SP3, SP4, SP3)
        layout.setSpacing(2)

        # Top line: date+time on the left, duration on the right.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(SP3)
        self._date_label = QLabel(_format_date_time(str(summary.get("start_time") or "")))
        self._date_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._date_label.setStyleSheet(
            f"color: {_LABEL}; background: transparent;"
        )
        self._duration_label = QLabel(_format_duration(summary.get("duration_seconds") or 0.0))
        self._duration_label.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        self._duration_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._duration_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        top.addWidget(self._date_label, stretch=1)
        top.addWidget(self._duration_label, stretch=0)

        # Bottom line: flow %, peak stress, top distraction.
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(SP3)

        flow_pct = float(summary.get("flow_percentage") or 0.0)
        if flow_pct >= 60.0:
            flow_color = BRAND_ACCENT
        elif flow_pct < 30.0:
            flow_color = _LABEL_TERTIARY
        else:
            flow_color = _LABEL_SECONDARY
        self._flow_label = QLabel(f"{flow_pct:.0f}% flow")
        self._flow_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._flow_label.setStyleSheet(
            f"color: {flow_color}; background: transparent;"
        )

        peak = float(summary.get("peak_stress_integral") or 0.0)
        self._peak_label = QLabel(f"peak {peak:.0f}")
        self._peak_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._peak_label.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )

        top_domain = str(summary.get("top_distraction_domain") or "").strip()
        domain_text = top_domain if top_domain else "no distractions"
        self._domain_label = QLabel(domain_text)
        self._domain_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._domain_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._domain_label.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )

        bottom.addWidget(self._flow_label, stretch=0)
        bottom.addWidget(self._peak_label, stretch=0)
        bottom.addWidget(self._domain_label, stretch=1)

        layout.addLayout(top)
        layout.addLayout(bottom)

    def mousePressEvent(self, event: Any) -> None:  # noqa: D401 - Qt
        if self._session_id:
            try:
                self.clicked.emit(self._session_id)
            except Exception:
                pass
        try:
            super().mousePressEvent(event)
        except Exception:
            pass

    def keyPressEvent(self, event: Any) -> None:  # noqa: D401 - Qt
        try:
            key = event.key()
        except Exception:
            key = None
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self._session_id:
                self.clicked.emit(self._session_id)
            event.accept()
            return
        try:
            super().keyPressEvent(event)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Today sub-panel — paginated session list + header + Load more.
# ---------------------------------------------------------------------------


class _TodayPanel(_RenderCacheMixin, QWidget):
    """The Today sub-page: header + scroll area of :class:`_SessionRow`
    rows + a Load more button at the bottom."""

    detail_requested = Signal(str)
    more_requested = Signal(object, int)  # since, limit
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")

        self._rows: list[_SessionRow] = []
        self._items: list[dict] = []
        self._items_hash = ""
        self._next_cursor: float | None = None
        self._total_known: int = 0
        # P2-5: track whether a list request is in-flight so we can
        # show a "Loading…" label while waiting for SESSION_LIST.
        self._list_loading: bool = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP4, SP3, SP4, SP3)
        outer.setSpacing(SP3)

        # Header row: title + count chip.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(SP3)
        self._header_title = QLabel("Sessions  ·  last 30 days")
        self._header_title.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._header_title.setStyleSheet(
            f"color: {_LABEL}; background: transparent;"
        )
        self._count_chip = QLabel("0")
        self._count_chip.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._count_chip.setStyleSheet(
            f"color: {_LABEL_SECONDARY};"
            f" background: {_GROUPED_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_PILL}px;"
            "  padding: 1px 9px;"
        )
        self._count_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self._header_title, stretch=1)
        header.addWidget(self._count_chip, stretch=0)
        outer.addLayout(header)

        # Scroll area with the rows.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 8px; background: transparent; }"
            f"QScrollBar::handle:vertical {{ background: {_LABEL_TERTIARY};"
            "  border-radius: 4px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(SP2)
        self._list_layout.addStretch(1)
        scroll.setWidget(self._list_host)
        outer.addWidget(scroll, stretch=1)

        # P2-5: list-loading label (shown while SESSION_LIST is in-flight).
        self._list_loading_label = QLabel("Loading…")
        self._list_loading_label.setObjectName("CortexListLoadingLabel")
        self._list_loading_label.setFont(
            mac_native.system_font(FS_FOOTNOTE, "regular")
        )
        self._list_loading_label.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        self._list_loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_loading_label.setVisible(False)
        outer.addWidget(self._list_loading_label)

        # Empty-state label (shown when there are no items).
        self._empty_label = QLabel(
            "No sessions yet — your history will appear here as you use Cortex."
        )
        self._empty_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._empty_label.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        outer.addWidget(self._empty_label)
        self._empty_label.setVisible(True)

        # Load-more button.
        self._more_btn = QPushButton("Load more")
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        try:
            self._more_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._more_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 5px 14px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {_LABEL_SECONDARY};"
            f"  border: 0.5px solid {_SEPARATOR};"
            "}"
            "QPushButton:hover { background: rgba(0,0,0,0.03);"
            f" color: {_LABEL}; }}"
            f"QPushButton:disabled {{ color: {_LABEL_TERTIARY}; }}"
        )
        self._more_btn.clicked.connect(self._on_more_clicked)
        self._more_btn.setVisible(False)
        outer.addWidget(self._more_btn, alignment=Qt.AlignmentFlag.AlignCenter)

    def _on_more_clicked(self) -> None:
        if self._next_cursor is None:
            return
        self._more_btn.setEnabled(False)
        self._more_btn.setText("Loading…")
        self.more_requested.emit(self._next_cursor, 30)

    def set_list_loading(self, loading: bool) -> None:
        """P2-5: toggle the list-loading state.

        When *loading* is ``True`` the "Loading…" label is shown and the
        empty-state label is hidden. When *loading* is ``False`` the
        labels are restored to their data-driven visibility (the next
        ``apply_payload`` call will reconcile correctly because it already
        checks ``self._items``).
        """
        self._list_loading = loading
        try:
            self._list_loading_label.setVisible(loading)
        except Exception:
            pass
        if loading:
            # Suppress the empty-state label while fetching so the layout
            # doesn't briefly show "No sessions yet" before data arrives.
            try:
                self._empty_label.setVisible(False)
            except Exception:
                pass

    def apply_payload(self, payload: dict) -> None:
        """Replace or append the rows based on the incoming
        ``SessionListResponse`` payload.

        Append heuristic: if the new payload's first item ``start_time``
        is older than the last row currently displayed, treat it as a
        paginated continuation. Otherwise replace.
        """
        if not isinstance(payload, dict):
            return
        # P2-5: clear the in-flight loading state now that the response landed.
        self.set_list_loading(False)
        new_items_raw = payload.get("items") or []
        if not isinstance(new_items_raw, list):
            new_items_raw = []
        new_items: list[dict] = [item for item in new_items_raw if isinstance(item, dict)]
        next_cursor_raw = payload.get("next_cursor")
        try:
            next_cursor: float | None = (
                float(next_cursor_raw) if next_cursor_raw is not None else None
            )
        except (TypeError, ValueError):
            next_cursor = None
        total_known = int(payload.get("total_known") or 0)

        # Decide replace-vs-append. Compare the first new item's
        # start_time against the last currently-rendered item's
        # start_time. If strictly older → append.
        #
        # Phase 4.B fix (#15): parse the ISO strings to ``datetime``
        # before comparing. String comparison was technically correct
        # for uniformly-formatted UTC timestamps but silently broke on
        # mixed-offset (e.g. ``+02:00`` vs ``Z``) or differing
        # fractional-second precision payloads. The parsed comparison
        # is timezone-aware and stable across format drift.
        append_mode = False
        if self._items and new_items:
            last_existing = self._items[-1].get("start_time") or ""
            first_new = new_items[0].get("start_time") or ""
            if isinstance(last_existing, str) and isinstance(first_new, str):
                last_dt = self._parse_iso(last_existing)
                first_dt = self._parse_iso(first_new)
                if last_dt is not None and first_dt is not None:
                    if first_dt < last_dt:
                        append_mode = True
                elif first_new and last_existing and first_new < last_existing:
                    # Fall back to string compare only if both parses
                    # failed (extremely unlikely with model_dump output)
                    # so we don't lose the append-mode heuristic entirely.
                    append_mode = True

        if append_mode:
            combined = self._items + new_items
        else:
            combined = list(new_items)
        new_hash = _hash_payload({
            "items": combined,
            "next_cursor": next_cursor,
            "total_known": total_known,
        })
        if new_hash == self._items_hash:
            # Nothing to do — same data already on screen.
            self._restore_more_button(next_cursor)
            return
        self._items_hash = new_hash
        self._items = combined
        self._next_cursor = next_cursor
        self._total_known = total_known

        self._rebuild_rows()
        self._restore_more_button(next_cursor)

        # Header chip = current visible count vs total known.
        if total_known:
            chip_text = f"{len(self._items)}/{total_known}"
        else:
            chip_text = str(len(self._items))
        self._set_text_if_changed(self._count_chip, chip_text)

        self._empty_label.setVisible(len(self._items) == 0)

    def _restore_more_button(self, next_cursor: float | None) -> None:
        self._more_btn.setEnabled(True)
        self._more_btn.setText("Load more")
        self._more_btn.setVisible(next_cursor is not None)

    def _rebuild_rows(self) -> None:
        # Remove all existing rows except the trailing stretch.
        for row in self._rows:
            try:
                self._list_layout.removeWidget(row)
                row.deleteLater()
            except Exception:
                pass
        self._rows = []
        # Insert in order; trailing stretch is index = current count.
        for i, summary in enumerate(self._items):
            row = _SessionRow(summary)
            row.clicked.connect(self.detail_requested.emit)
            self._list_layout.insertWidget(i, row)
            self._rows.append(row)

    @staticmethod
    def _parse_iso(value: str) -> Any:
        """Parse an ISO 8601 datetime to a tz-aware ``datetime`` object.

        Phase 4.B fix (#15): pulled into a helper so the pagination
        heuristic can do a real datetime compare instead of a string
        one. Returns None on any parse failure so the caller can fall
        back gracefully.
        """
        if not value:
            return None
        try:
            from datetime import UTC, datetime

            norm = value.replace("Z", "+00:00") if value.endswith("Z") else value
            dt = datetime.fromisoformat(norm)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Detail panel — shown as an overlay on the Today sub-page.
# ---------------------------------------------------------------------------


class _StateTimelineWidget(QWidget):
    """Horizontal coloured-rectangle band representing
    ``state_transitions`` weighted by dwell time."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[tuple[str, float]] = []  # (state, weight)
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_segments(self, segments: list[tuple[str, float]]) -> None:
        self._segments = list(segments)
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            # Background card.
            bg_path = QPainterPath()
            bg_path.addRoundedRect(QRectF(0, 0, w, h), 6.0, 6.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_GROUPED_BG))
            painter.drawPath(bg_path)
            if not self._segments:
                return
            total = sum(weight for _, weight in self._segments) or 1.0
            x = 0.0
            painter.setClipPath(bg_path)
            for state, weight in self._segments:
                seg_w = max(1.0, (weight / total) * w)
                color = STATE_COLORS.get(state, _LABEL_TERTIARY)
                painter.setBrush(QColor(color))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(QRectF(x, 0, seg_w, h))
                x += seg_w
        finally:
            painter.end()


class _HeatmapWidget(QWidget):
    """24-hour flow heatmap rendered as an 8×3 grid (8 cols, 3 rows;
    cells in reading order represent hours 0..23). Alpha intensity is
    proportional to flow seconds in that hour."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buckets: list[float] = [0.0] * 24
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_hourly_flow(self, buckets: list[float]) -> None:
        if len(buckets) != 24:
            # Defensive: pad/truncate so paintEvent always has 24 entries.
            buckets = (list(buckets) + [0.0] * 24)[:24]
        self._buckets = [max(0.0, float(b)) for b in buckets]
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            cols, rows = 8, 3
            pad = 2.0
            cell_w = (w - pad * (cols + 1)) / cols
            cell_h = (h - pad * (rows + 1)) / rows
            max_val = max(self._buckets) if self._buckets else 0.0
            for hour in range(24):
                row = hour // cols
                col = hour % cols
                x = pad + col * (cell_w + pad)
                y = pad + row * (cell_h + pad)
                val = self._buckets[hour]
                # Background tile.
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(_GROUPED_BG))
                painter.drawRoundedRect(QRectF(x, y, cell_w, cell_h), 3, 3)
                if max_val > 0.0 and val > 0.0:
                    intensity = val / max_val
                    color = QColor(BRAND_ACCENT)
                    color.setAlphaF(max(0.10, min(1.0, intensity)))
                    painter.setBrush(color)
                    painter.drawRoundedRect(QRectF(x, y, cell_w, cell_h), 3, 3)
        finally:
            painter.end()


class _ChipRow(QFrame):
    """Tiny pill row used for the ``comparison_to_7day`` deltas."""

    def __init__(self, label: str, value: str, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            f"background: {_GROUPED_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_PILL}px;"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SP3, 2, SP3, 2)
        layout.setSpacing(SP2)
        cap = QLabel(label)
        cap.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        cap.setStyleSheet(f"color: {_LABEL_SECONDARY}; background: transparent;")
        val = QLabel(value)
        val.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        val.setStyleSheet(f"color: {accent}; background: transparent;")
        layout.addWidget(cap)
        layout.addWidget(val)


class _DetailPanel(_RenderCacheMixin, QWidget):
    """Overlay panel — sits above the Today list when a row is opened.

    Renders the full :class:`SessionReport` payload (state timeline,
    hourly heatmap, top 3 distractions, intervention proxy, comparison
    chips).
    """

    back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {_WINDOW_BG};")
        self.hide()
        self._current_payload_hash: str = ""
        # Phase 4.B fix (#13): tracks whether the panel is currently
        # awaiting a SESSION_DETAIL reply. The HistoryTab arms a
        # ``QTimer.singleShot`` against this flag to surface a timeout
        # error if the daemon never answers. Reset by every
        # ``apply_payload`` call.
        self._loading: bool = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP4, SP3, SP4, SP3)
        outer.setSpacing(SP3)

        # Top bar: back button.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self._back_btn = QPushButton("← Sessions")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.setFont(mac_native.system_font(FS_FOOTNOTE, "medium"))
        try:
            self._back_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        self._back_btn.setStyleSheet(
            "QPushButton {"
            "  padding: 4px 10px;"
            f"  border-radius: {RADIUS_BUTTON}px;"
            "  background: transparent;"
            f"  color: {_LABEL_SECONDARY};"
            "  border: none;"
            "}"
            f"QPushButton:hover {{ color: {_LABEL}; }}"
        )
        self._back_btn.clicked.connect(self.back_requested.emit)
        top.addWidget(self._back_btn)
        top.addStretch(1)
        outer.addLayout(top)

        # Headline date + duration.
        self._headline = QLabel("")
        self._headline.setStyleSheet(
            f"font-family: {BRAND_DISPLAY_FONT};"
            f" font-size: {FS_TITLE}pt;"
            "  font-style: italic;"
            f"  color: {_LABEL};"
            "  background: transparent;"
        )
        self._headline.setWordWrap(True)
        outer.addWidget(self._headline)

        self._subhead = QLabel("")
        self._subhead.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._subhead.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        outer.addWidget(self._subhead)

        # Phase 4.B fix (#13): explicit loading label distinct from the
        # error label. Uses LABEL_SECONDARY (not the danger red) so an
        # in-flight detail request reads as "in progress" rather than
        # "something went wrong". Hidden by default; toggled by
        # :meth:`set_loading`.
        self._loading_label = QLabel("Loading session…")
        self._loading_label.setFont(
            mac_native.system_font(FS_FOOTNOTE, "regular")
        )
        self._loading_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label.setVisible(False)
        _safe(
            self._loading_label.setAccessibleName,
            "Loading session details",
        )
        outer.addWidget(self._loading_label)

        # Error label (hidden by default).
        self._error_label = QLabel("")
        self._error_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._error_label.setStyleSheet(
            f"color: {SEMANTIC_LIGHT['danger']}; background: transparent;"
        )
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        outer.addWidget(self._error_label)

        # Scroll body so long reports remain in the dashboard height.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 8px; background: transparent; }"
            f"QScrollBar::handle:vertical {{ background: {_LABEL_TERTIARY};"
            "  border-radius: 4px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        # Phase 4.B fix (#13): keep references to the scroll + body so
        # :meth:`set_loading` can hide the content widgets while a
        # request is in flight.
        self._body_widget = body
        self._scroll_widget = scroll
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(SP4)

        # State timeline.
        self._timeline_label = QLabel("State timeline")
        self._timeline_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._timeline_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        self._timeline = _StateTimelineWidget()
        body_layout.addWidget(self._timeline_label)
        body_layout.addWidget(self._timeline)

        # Heatmap.
        self._heatmap_label = QLabel("Hourly flow")
        self._heatmap_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._heatmap_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        self._heatmap = _HeatmapWidget()
        body_layout.addWidget(self._heatmap_label)
        body_layout.addWidget(self._heatmap)

        # Top distractions.
        self._distractions_label = QLabel("Top distractions")
        self._distractions_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._distractions_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        self._distractions_host = QFrame()
        self._distractions_host.setStyleSheet("background: transparent;")
        self._distractions_layout = QVBoxLayout(self._distractions_host)
        self._distractions_layout.setContentsMargins(0, 0, 0, 0)
        self._distractions_layout.setSpacing(SP1)
        body_layout.addWidget(self._distractions_label)
        body_layout.addWidget(self._distractions_host)

        # Interventions.
        self._interventions_label = QLabel("Interventions")
        self._interventions_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._interventions_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        self._interventions_host = QFrame()
        self._interventions_host.setStyleSheet("background: transparent;")
        self._interventions_layout = QVBoxLayout(self._interventions_host)
        self._interventions_layout.setContentsMargins(0, 0, 0, 0)
        self._interventions_layout.setSpacing(SP1)
        body_layout.addWidget(self._interventions_label)
        body_layout.addWidget(self._interventions_host)

        # Comparison chips.
        self._comparison_label = QLabel("Compared to your 7-day rolling average")
        self._comparison_label.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._comparison_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        self._comparison_host = QFrame()
        self._comparison_host.setStyleSheet("background: transparent;")
        self._comparison_layout = QHBoxLayout(self._comparison_host)
        self._comparison_layout.setContentsMargins(0, 0, 0, 0)
        self._comparison_layout.setSpacing(SP2)
        body_layout.addWidget(self._comparison_label)
        body_layout.addWidget(self._comparison_host)

        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

    def set_loading(self, loading: bool) -> None:
        """Toggle the explicit "Loading session…" state.

        Phase 4.B fix (#13): when a row is clicked the panel needs a
        clearly-different visual state from "we tried and failed". The
        previous implementation reused the danger-red error label
        which made every click look like a failure for the duration of
        the request. This method:

        * ``loading=True`` — hides error + content, shows the
          secondary-coloured "Loading session…" label, blanks the
          headline / subhead so stale data doesn't bleed through.
        * ``loading=False`` — hides the loading label; content
          visibility is restored by :meth:`apply_payload` when the
          real payload arrives.
        """
        self._loading = bool(loading)
        try:
            self._loading_label.setVisible(self._loading)
            if self._loading:
                self._error_label.setVisible(False)
                if self._scroll_widget is not None:
                    self._scroll_widget.setVisible(False)
                # Blank stale headlines so the user sees a clean
                # loading state rather than the previous session's
                # title.
                self._set_text_if_changed(self._headline, "")
                self._set_text_if_changed(self._subhead, "")
            else:
                if self._scroll_widget is not None:
                    self._scroll_widget.setVisible(True)
        except Exception:
            logger.debug("set_loading toggle failed", exc_info=True)

    def apply_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        # Phase 4.B fix (#13): any apply_payload exits the loading
        # state — the data has arrived (or the error has been
        # explicitly delivered). Toggle BEFORE the hash short-circuit
        # so a repeat payload still clears the spinner.
        if self._loading:
            self.set_loading(False)
        new_hash = _hash_payload(payload)
        if new_hash == self._current_payload_hash:
            return
        self._current_payload_hash = new_hash

        error = payload.get("error")
        report = payload.get("report")
        if report is None or not isinstance(report, dict):
            msg = "Could not load this session."
            if error == "not_found":
                msg = "We couldn't find that session on disk."
            elif error == "unreadable":
                msg = "That session's file is unreadable — it may be corrupt."
            elif isinstance(error, str) and error:
                msg = error
            self._set_text_if_changed(self._headline, "Session unavailable")
            self._set_text_if_changed(self._subhead, "")
            self._error_label.setText(msg)
            self._error_label.setVisible(True)
            self._render_segments([])
            self._heatmap.set_hourly_flow([0.0] * 24)
            self._populate_distractions([])
            self._populate_interventions([])
            self._populate_comparison(None)
            return
        self._error_label.setVisible(False)

        # Headline.
        start = str(report.get("start_time") or "")
        duration = _format_duration(report.get("duration_seconds") or 0.0)
        self._set_text_if_changed(
            self._headline, _format_date_time(start) or "Session",
        )
        flow_pct = float(report.get("flow_percentage") or 0.0)
        # Phase 4.B fix (#9): clamp percent ranges defensively so a
        # stale or mis-computed payload can't render "112% in flow".
        flow_pct = max(0.0, min(100.0, flow_pct))
        self._set_text_if_changed(
            self._subhead,
            f"{duration}  ·  {flow_pct:.0f}% in flow",
        )

        # Timeline segments derived from state_transitions + end_time.
        self._render_segments_from_transitions(report)

        # Hourly heatmap derived from state_transitions.
        self._render_heatmap_from_transitions(report)

        # Distractions.
        domains = report.get("top_distraction_domains") or []
        if not isinstance(domains, list):
            domains = []
        self._populate_distractions([str(d) for d in domains[:3]])

        # Interventions: state_transitions with to_state=="HYPER".
        transitions = report.get("state_transitions") or []
        if not isinstance(transitions, list):
            transitions = []
        intervention_rows = [
            t
            for t in transitions
            if isinstance(t, dict) and str(t.get("to_state", "")).upper() == "HYPER"
        ]
        self._populate_interventions(intervention_rows)

        # Comparison.
        self._populate_comparison(report.get("comparison_to_7day"))

    # ---- rendering helpers ------------------------------------------------

    def _render_segments(self, segments: list[tuple[str, float]]) -> None:
        self._timeline.set_segments(segments)

    def _render_segments_from_transitions(self, report: dict) -> None:
        transitions = report.get("state_transitions") or []
        if not isinstance(transitions, list):
            transitions = []
        end_iso = str(report.get("end_time") or "")
        segments: list[tuple[str, float]] = []
        try:
            from datetime import datetime

            def _parse(value: str) -> Any:
                if not value:
                    return None
                norm = value.replace("Z", "+00:00") if value.endswith("Z") else value
                return datetime.fromisoformat(norm)

            end_dt = _parse(end_iso)
            for i, t in enumerate(transitions):
                if not isinstance(t, dict):
                    continue
                state = str(t.get("to_state", "FLOW")).upper()
                ts_dt = _parse(str(t.get("timestamp") or ""))
                if ts_dt is None:
                    continue
                next_dt = None
                for nxt in transitions[i + 1 :]:
                    if isinstance(nxt, dict):
                        next_dt = _parse(str(nxt.get("timestamp") or ""))
                        if next_dt is not None:
                            break
                if next_dt is None:
                    next_dt = end_dt
                if next_dt is None:
                    weight = 1.0
                else:
                    weight = max(0.0, (next_dt - ts_dt).total_seconds())
                if weight <= 0.0:
                    weight = 1.0
                segments.append((state, weight))
        except Exception:
            logger.debug("state-transition parse failed", exc_info=True)
            segments = []
        if not segments:
            # Fall back to a single FLOW segment so the band is at least
            # visible at all.
            segments = [("FLOW", 1.0)]
        self._render_segments(segments)

    def _render_heatmap_from_transitions(self, report: dict) -> None:
        transitions = report.get("state_transitions") or []
        if not isinstance(transitions, list):
            transitions = []
        end_iso = str(report.get("end_time") or "")
        buckets = [0.0] * 24
        try:
            from datetime import datetime, timedelta

            def _parse(value: str) -> Any:
                if not value:
                    return None
                norm = value.replace("Z", "+00:00") if value.endswith("Z") else value
                return datetime.fromisoformat(norm)

            end_dt = _parse(end_iso)
            for i, t in enumerate(transitions):
                if not isinstance(t, dict):
                    continue
                state = str(t.get("to_state", "")).upper()
                if state != "FLOW":
                    continue
                ts_dt = _parse(str(t.get("timestamp") or ""))
                if ts_dt is None:
                    continue
                next_dt = None
                for nxt in transitions[i + 1 :]:
                    if isinstance(nxt, dict):
                        next_dt = _parse(str(nxt.get("timestamp") or ""))
                        if next_dt is not None:
                            break
                if next_dt is None:
                    next_dt = end_dt
                if next_dt is None or next_dt <= ts_dt:
                    continue
                # Walk the FLOW interval one minute at a time so cross-hour
                # spans bucket correctly.
                cursor = ts_dt
                while cursor < next_dt:
                    step_end = min(
                        next_dt,
                        cursor.replace(minute=0, second=0, microsecond=0)
                        + timedelta(hours=1),
                    )
                    seconds = (step_end - cursor).total_seconds()
                    hour = cursor.hour
                    if 0 <= hour < 24:
                        buckets[hour] += max(0.0, seconds)
                    cursor = step_end
        except Exception:
            logger.debug("hourly heatmap parse failed", exc_info=True)
            buckets = [0.0] * 24
        if all(b == 0.0 for b in buckets):
            # Fallback: derive from top_activities via dwell_seconds.
            top_activities = report.get("top_activities") or []
            if isinstance(top_activities, list) and top_activities:
                # Spread the activity dwell evenly across an arbitrary
                # block; the heatmap is now indicative, not exact.
                total = sum(
                    float(a.get("dwell_seconds") or 0.0)
                    for a in top_activities
                    if isinstance(a, dict)
                )
                if total > 0.0:
                    buckets = [total / 24.0] * 24
        self._heatmap.set_hourly_flow(buckets)

    def _populate_distractions(self, domains: list[str]) -> None:
        # Tear down and rebuild — these are at most 3 rows; rebuilding
        # avoids cache key churn.
        for i in reversed(range(self._distractions_layout.count())):
            item = self._distractions_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                self._distractions_layout.removeWidget(w)
                w.deleteLater()
        if not domains:
            # P2-8: per-session detail empty state — sessions exist but this
            # one had no distraction domains detected.
            placeholder = QLabel(
                "No interventions in this session — focus held steady."
            )
            placeholder.setObjectName("CortexDetailDistractionsEmpty")
            placeholder.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            placeholder.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            self._distractions_layout.addWidget(placeholder)
            return
        for d in domains:
            row = QLabel(f"·  {d}")
            row.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            row.setStyleSheet(
                f"color: {_LABEL_SECONDARY}; background: transparent;"
            )
            self._distractions_layout.addWidget(row)

    def _populate_interventions(self, rows: list[dict]) -> None:
        for i in reversed(range(self._interventions_layout.count())):
            item = self._interventions_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                self._interventions_layout.removeWidget(w)
                w.deleteLater()
        if not rows:
            placeholder = QLabel("No HYPER spikes recorded.")
            placeholder.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            placeholder.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            self._interventions_layout.addWidget(placeholder)
            return
        for r in rows[:6]:
            ts = str(r.get("timestamp") or "")
            from_state = str(r.get("from_state") or "?")
            to_state = str(r.get("to_state") or "?")
            label = QLabel(
                f"·  {_format_date_time(ts)}  —  "
                f"{STATE_LABELS.get(from_state, from_state)} → "
                f"{STATE_LABELS.get(to_state, to_state)}"
            )
            label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            label.setStyleSheet(
                f"color: {_LABEL_SECONDARY}; background: transparent;"
            )
            self._interventions_layout.addWidget(label)

    def _populate_comparison(self, comparison: Any) -> None:
        for i in reversed(range(self._comparison_layout.count())):
            item = self._comparison_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                self._comparison_layout.removeWidget(w)
                w.deleteLater()
        if not isinstance(comparison, dict):
            placeholder = QLabel("Insufficient history yet — try a few more sessions.")
            placeholder.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            placeholder.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            self._comparison_layout.addWidget(placeholder)
            self._comparison_layout.addStretch(1)
            return
        focus = float(comparison.get("focus_delta") or 0.0)
        stress = float(comparison.get("stress_delta") or 0.0)
        focus_color = _SUCCESS if focus >= 0 else SEMANTIC_LIGHT["danger"]
        stress_color = _SUCCESS if stress <= 0 else SEMANTIC_LIGHT["danger"]
        self._comparison_layout.addWidget(
            _ChipRow("Focus", f"{focus:+.1f}pp", focus_color)
        )
        self._comparison_layout.addWidget(
            _ChipRow("Stress", f"{stress:+.0f}", stress_color)
        )
        self._comparison_layout.addStretch(1)


# ---------------------------------------------------------------------------
# Trends panel — Week / Month sub-pages.
# ---------------------------------------------------------------------------


class _TrendBarsWidget(QWidget):
    """Vertical bars for ``DailyBaseline.total_flow_minutes``.

    Top-25-percentile days highlighted with the brand accent; bottom
    quartile dimmed. The x-axis labels are appended below as a separate
    row so layout stays simple.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._values: list[float] = []
        self._labels: list[str] = []
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_data(self, values: list[float], labels: list[str]) -> None:
        self._values = [max(0.0, float(v)) for v in values]
        self._labels = [str(s) for s in labels]
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            if not self._values:
                painter.setPen(QColor(_LABEL_TERTIARY))
                painter.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
                painter.drawText(
                    self.rect(),
                    Qt.AlignmentFlag.AlignCenter,
                    "No data in this window yet.",
                )
                return
            n = len(self._values)
            label_h = 14
            chart_h = h - label_h - 6
            pad_x = 6.0
            avail_w = w - pad_x * 2
            gap = max(2.0, avail_w / (n * 8))
            bar_w = (avail_w - gap * (n - 1)) / n
            max_v = max(self._values) or 1.0
            sorted_vals = sorted(self._values)
            # Phase 4.B fix (#14): correct quartile threshold.
            # ``int(0.75 * (n - 1))`` underestimated the cutoff for
            # small n (e.g. n=7 → index 4 = 5th value, which is the
            # 71st percentile, not 75th). Use ``ceil(0.75 * n) - 1``
            # so the threshold lands on the smallest sample whose
            # rank-from-top is in the top quartile. The ``n >= 4``
            # guard avoids highlighting on tiny windows where the
            # concept of a quartile doesn't apply.
            if n >= 4:
                top_q_idx = max(0, min(n - 1, math.ceil(0.75 * n) - 1))
                bot_q_idx = max(0, min(n - 1, math.ceil(0.25 * n) - 1))
                top_q = sorted_vals[top_q_idx]
                bot_q = sorted_vals[bot_q_idx]
            else:
                # Too few samples for a meaningful quartile split —
                # render everything as the neutral secondary tint so
                # we don't visually privilege a tiny sample.
                top_q = float("inf")
                bot_q = float("-inf")
            for i, value in enumerate(self._values):
                x = pad_x + i * (bar_w + gap)
                height = (value / max_v) * chart_h if max_v > 0 else 0.0
                y = chart_h - height + 3
                if value >= top_q and value > 0.0:
                    color = QColor(BRAND_ACCENT)
                elif value <= bot_q:
                    color = QColor(_LABEL_TERTIARY)
                else:
                    color = QColor(_LABEL_SECONDARY)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(x, y, bar_w, max(2.0, height)), 3, 3)
                # Label.
                if i < len(self._labels):
                    painter.setPen(QColor(_LABEL_TERTIARY))
                    font = mac_native.system_font(FS_CAPTION, "regular")
                    if isinstance(font, QFont):
                        painter.setFont(font)
                    painter.drawText(
                        QRectF(x, h - label_h, bar_w, label_h),
                        Qt.AlignmentFlag.AlignCenter,
                        self._labels[i],
                    )
        finally:
            painter.end()


class _GoldenHourStripe(QWidget):
    """24-hour-wide horizontal stripe. Hours with low overload
    (``overload_rate`` below median) are tinted with the brand accent;
    the strongest contiguous low-overload run gets an outlined emphasis.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hourly: list[tuple[int, float, int]] = []  # (hour, rate, sample_count)
        self._strongest_range: tuple[int, int] | None = None
        self.setMinimumHeight(36)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_hourly_patterns(self, patterns: list[dict]) -> None:
        rows: list[tuple[int, float, int]] = []
        for p in patterns:
            if not isinstance(p, dict):
                continue
            try:
                hour = int(p.get("hour"))
                rate = float(p.get("overload_rate") or 0.0)
                samples = int(p.get("sample_count") or 0)
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23:
                rows.append((hour, rate, samples))
        # Sort by hour ascending.
        rows.sort(key=lambda r: r[0])
        self._hourly = rows
        # Compute strongest contiguous low-overload run.
        if rows:
            rates = [r[1] for r in rows]
            sorted_rates = sorted(rates)
            median = sorted_rates[len(sorted_rates) // 2]
            best_run: tuple[int, int] | None = None
            best_len = 0
            cur_start = None
            cur_len = 0
            for hour, rate, _ in rows:
                if rate <= median:
                    if cur_start is None:
                        cur_start = hour
                        cur_len = 1
                    else:
                        cur_len += 1
                    if cur_len > best_len:
                        best_len = cur_len
                        best_run = (cur_start, hour)
                else:
                    cur_start = None
                    cur_len = 0
            self._strongest_range = best_run
        else:
            self._strongest_range = None
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            track_h = h - 12
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_GROUPED_BG))
            painter.drawRoundedRect(QRectF(0, 0, w, track_h), 4, 4)
            if not self._hourly:
                return
            # Compute medianised low/high.
            rates = [r[1] for r in self._hourly]
            sorted_rates = sorted(rates)
            median = sorted_rates[len(sorted_rates) // 2]
            cell_w = w / 24.0
            for hour, rate, samples in self._hourly:
                if rate > median or samples == 0:
                    continue
                # Lower rate → more saturated terracotta.
                intensity = 1.0 - (rate / (median or 1.0))
                color = QColor(BRAND_ACCENT)
                color.setAlphaF(max(0.18, min(0.85, intensity)))
                x = hour * cell_w
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(x + 1, 1, cell_w - 2, track_h - 2), 3, 3)
            if self._strongest_range is not None:
                start_h, end_h = self._strongest_range
                x = start_h * cell_w
                width = (end_h - start_h + 1) * cell_w
                pen = QPen(QColor(BRAND_ACCENT_HOVER), 1.2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(x + 1, 1, width - 2, track_h - 2), 4, 4)
            # Hour labels (every 4 hours).
            painter.setPen(QColor(_LABEL_TERTIARY))
            font = mac_native.system_font(FS_CAPTION, "regular")
            if isinstance(font, QFont):
                painter.setFont(font)
            for hour in (0, 6, 12, 18):
                x = hour * cell_w
                painter.drawText(
                    QRectF(x, track_h, cell_w * 4, 12),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    f"{hour:02d}",
                )
        finally:
            painter.end()


class _TrendsPanel(_RenderCacheMixin, QWidget):
    """One of the Week / Month sub-pages. Renders bars + golden-hour
    stripe + trend pill + top distractions.

    Phase 4.B fix (#19): the header row also surfaces an "Updated
    <relative>" caption and a small "Refresh" button so the user can
    force a re-aggregation when they suspect the trends are stale
    (the daemon aggregates on its own ~hourly cadence). Pressing
    Refresh emits ``refresh_requested(window)`` which the HistoryTab
    forwards as ``trends_requested(window, refresh=True)``.
    """

    refresh_requested = Signal(str)
    """Emitted with the window literal (``"week"`` / ``"month"``) when
    the user clicks Refresh. Triggers a ``trends_requested`` with
    ``refresh=True``."""

    def __init__(self, window: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._window_days = 7 if window == "week" else 30
        self._payload_hash = ""
        # Phase 4.B fix (#19): tracks whether the user has pressed
        # Refresh and the response hasn't arrived yet. While True the
        # button shows "Refreshing…" and is disabled; reset on the next
        # apply_payload OR the 10 s safety timer.
        self._refresh_pending: bool = False
        self._refresh_reset_timer: QTimer | None = None
        self.setStyleSheet("background: transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP4, SP3, SP4, SP3)
        outer.setSpacing(SP3)

        # Header row.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(SP3)
        header_title = "This week's flow" if window == "week" else "This month's flow"
        self._header_label = QLabel(header_title)
        self._header_label.setFont(mac_native.system_font(FS_FOOTNOTE, "semibold"))
        self._header_label.setStyleSheet(
            f"color: {_LABEL}; background: transparent;"
        )
        # Phase 4.B fix (#19): "Updated <relative>" caption. Lives on
        # the right side of the header row, immediately left of the
        # trend pill, so the staleness signal is visible without
        # extra scrolling. Default text is rebuilt by apply_payload.
        self._updated_label = QLabel("Not yet aggregated")
        self._updated_label.setFont(mac_native.system_font(FS_CAPTION, "regular"))
        self._updated_label.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        _safe(
            self._updated_label.setAccessibleName,
            f"{window.capitalize()} trends last aggregated time",
        )
        # Refresh button — small ghost style with the terracotta accent
        # so it reads as a tertiary action (not as scary as the Stop
        # button but still visibly tinted toward the brand). 6 px
        # radius + (SP_2, SP_3) padding per the spec.
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        try:
            self._refresh_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        except Exception:
            pass
        _safe(
            self._refresh_btn.setAccessibleName,
            f"Refresh {window} trends",
        )
        _safe(
            self._refresh_btn.setAccessibleDescription,
            f"Force re-aggregation of the {window} rollup right now.",
        )
        # BRAND_ACCENT at ~40% alpha for the border — keeps the warm
        # cue without competing with the trend pill or the bars below.
        self._refresh_btn.setStyleSheet(self._refresh_btn_qss(enabled=True))
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        self._trend_pill = QLabel("Trend: stable")
        self._trend_pill.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._trend_pill.setStyleSheet(
            f"color: {_LABEL_SECONDARY};"
            f" background: {_GROUPED_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_PILL}px;"
            "  padding: 1px 9px;"
        )
        self._trend_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self._header_label, stretch=1)
        header.addWidget(self._updated_label, stretch=0)
        header.addWidget(self._refresh_btn, stretch=0)
        header.addWidget(self._trend_pill, stretch=0)
        outer.addLayout(header)

        # Empty-state. Phase 4.B fix (#21): parametrise the minimum
        # sample threshold so the Month panel doesn't claim trends
        # populate after only 3 sessions when the slope detector
        # actually needs more samples on a 30-day window.
        min_sessions = max(3, self._window_days // 4)
        self._empty_label = QLabel(
            f"Run a few sessions first; trends populate after {min_sessions} sessions."
        )
        self._empty_label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
        self._empty_label.setStyleSheet(
            f"color: {_LABEL_TERTIARY}; background: transparent;"
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        outer.addWidget(self._empty_label)
        self._empty_label.setVisible(True)

        # Bars.
        self._bars = _TrendBarsWidget()
        outer.addWidget(self._bars)

        # Golden-hour stripe.
        self._golden_caption = QLabel("Golden hours (lower-overload windows)")
        self._golden_caption.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._golden_caption.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        outer.addWidget(self._golden_caption)
        self._golden = _GoldenHourStripe()
        outer.addWidget(self._golden)

        # Top distractions.
        self._distractions_caption = QLabel("Top distractions in this window")
        self._distractions_caption.setFont(mac_native.system_font(FS_CAPTION, "semibold"))
        self._distractions_caption.setStyleSheet(
            f"color: {_LABEL_SECONDARY}; background: transparent;"
        )
        outer.addWidget(self._distractions_caption)
        self._distractions_host = QFrame()
        self._distractions_host.setStyleSheet("background: transparent;")
        self._distractions_layout = QVBoxLayout(self._distractions_host)
        self._distractions_layout.setContentsMargins(0, 0, 0, 0)
        self._distractions_layout.setSpacing(SP1)
        outer.addWidget(self._distractions_host)

        outer.addStretch(1)

    def _refresh_btn_qss(self, *, enabled: bool) -> str:
        """Stylesheet for the small Refresh button. Ghost / tertiary
        style with a BRAND_ACCENT-tinted border at ~40 % alpha — keeps
        the warmth without competing visually with the trend pill."""
        # ``rgba`` strings are inlined because Qt's stylesheet engine
        # doesn't honour CSS variables. Disabled state drops contrast
        # so the user can't fight the spinner.
        text_color = _LABEL if enabled else _LABEL_TERTIARY
        border_color = "rgba(212, 90, 56, 0.40)" if enabled else "rgba(212, 90, 56, 0.20)"
        return (
            "QPushButton {"
            f"  padding: {SP2}px {SP3}px;"
            "  border-radius: 6px;"
            "  background: transparent;"
            f"  color: {text_color};"
            f"  border: 0.5px solid {border_color};"
            "}"
            f"QPushButton:hover {{ color: {BRAND_ACCENT_HOVER};"
            f" border-color: {BRAND_ACCENT}; }}"
            "QPushButton:disabled {"
            f"  color: {_LABEL_TERTIARY};"
            f"  border-color: rgba(212, 90, 56, 0.20);"
            "}"
        )

    def _on_refresh_clicked(self) -> None:
        """User clicked Refresh. Flips the button into the pending
        state, arms the 10 s reset safety timer, and emits
        ``refresh_requested`` so the HistoryTab forwards a
        ``trends_requested(window, refresh=True)`` to the controller.
        """
        if self._refresh_pending:
            # Coalesce double-clicks while a refresh is in flight.
            return
        self._refresh_pending = True
        self._set_text_if_changed(self._refresh_btn, "Refreshing…")
        try:
            self._refresh_btn.setEnabled(False)
        except Exception:
            logger.debug("refresh_btn.setEnabled failed", exc_info=True)
        self._set_style_if_changed(self._refresh_btn, self._refresh_btn_qss(enabled=False))
        # Arm the reset safety timer so a missed broadcast can't keep
        # the button permanently disabled.
        if self._refresh_reset_timer is None:
            self._refresh_reset_timer = QTimer(self)
            self._refresh_reset_timer.setSingleShot(True)
            self._refresh_reset_timer.setInterval(_REFRESH_RESET_MS)
            self._refresh_reset_timer.timeout.connect(self._reset_refresh_state)
        try:
            self._refresh_reset_timer.start()
        except Exception:
            logger.debug("refresh reset timer start failed", exc_info=True)
        try:
            self.refresh_requested.emit(self._window)
        except Exception:
            logger.debug("refresh_requested emit failed", exc_info=True)

    def _reset_refresh_state(self) -> None:
        """Drop the pending flag, re-enable the button, restore the
        "Refresh" label. Idempotent — safe to call from both the
        apply_payload path and the safety timer."""
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        if self._refresh_reset_timer is not None:
            try:
                self._refresh_reset_timer.stop()
            except Exception:
                pass
        self._set_text_if_changed(self._refresh_btn, "Refresh")
        self._set_style_if_changed(self._refresh_btn, self._refresh_btn_qss(enabled=True))
        try:
            self._refresh_btn.setEnabled(True)
        except Exception:
            logger.debug("refresh_btn.setEnabled(True) failed", exc_info=True)

    def _update_updated_label(self, payload: dict) -> None:
        """Refresh the "Updated <relative>" caption from the payload's
        ``last_aggregated`` (top-level) or ``chronotype.last_updated``.

        Phase 4.B fix (#19). Uses the render cache so the 2 Hz state
        broadcast / repeated apply calls don't churn the style sheet.
        Stale (>= 24 h) timestamps get a BRAND_ACCENT colour hint so
        the user sees that something has gone wrong with aggregation.
        """
        chronotype = payload.get("chronotype") if isinstance(payload, dict) else None
        if not isinstance(chronotype, dict):
            chronotype = {}
        iso = payload.get("last_aggregated") if isinstance(payload, dict) else None
        if not iso:
            iso = chronotype.get("last_updated")
        text, stale = _format_relative_age(iso if isinstance(iso, str) else None)
        self._set_text_if_changed(self._updated_label, text)
        colour = BRAND_ACCENT if stale else _LABEL_SECONDARY
        self._set_style_if_changed(
            self._updated_label,
            f"color: {colour}; background: transparent;",
        )

    def apply_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        # Only react if the payload's ``window`` matches ours.
        win = str(payload.get("window") or "")
        if win and win != self._window:
            return
        # Phase 4.B fix (#19): the staleness label and the refresh-pending
        # state must always be updated, even when the payload hash matches
        # the cached one (e.g. the user pressed Refresh and the daemon
        # returned the same payload — we still need to clear the spinner).
        self._update_updated_label(payload)
        if self._refresh_pending:
            self._reset_refresh_state()
        new_hash = _hash_payload(payload)
        if new_hash == self._payload_hash:
            return
        self._payload_hash = new_hash

        daily = payload.get("daily") or []
        if not isinstance(daily, list):
            daily = []
        chronotype = payload.get("chronotype") or {}
        if not isinstance(chronotype, dict):
            chronotype = {}
        baselines = chronotype.get("baselines") if isinstance(chronotype, dict) else None
        if not isinstance(baselines, list):
            baselines = []

        if not daily and not baselines:
            self._empty_label.setVisible(True)
            self._bars.set_data([], [])
            self._golden.set_hourly_patterns([])
            self._populate_distractions([])
            self._update_trend_pill("stable")
            return
        self._empty_label.setVisible(False)

        # Bars: total_flow_minutes per day, in chronological order.
        values = [float(d.get("total_flow_minutes") or 0.0) for d in daily if isinstance(d, dict)]
        if self._window == "week":
            labels = [_weekday(str(d.get("record_date") or "")) for d in daily if isinstance(d, dict)]
        else:
            labels = [_short_date(str(d.get("record_date") or "")) for d in daily if isinstance(d, dict)]
        self._bars.set_data(values, labels)

        # Golden-hour stripe.
        hourly = chronotype.get("hourly_patterns") if isinstance(chronotype, dict) else []
        if not isinstance(hourly, list):
            hourly = []
        self._golden.set_hourly_patterns(hourly)

        # Trend pill.
        direction = str(chronotype.get("trend_direction") or "stable")
        self._update_trend_pill(direction)

        # Top distractions: pick task_patterns sorted by overload_rate desc.
        task_patterns = chronotype.get("task_patterns") if isinstance(chronotype, dict) else []
        if not isinstance(task_patterns, list):
            task_patterns = []
        rows = sorted(
            (p for p in task_patterns if isinstance(p, dict)),
            key=lambda p: float(p.get("overload_rate") or 0.0),
            reverse=True,
        )[:5]
        self._populate_distractions(rows)

    def _update_trend_pill(self, direction: str) -> None:
        if direction == "improving":
            color = _SUCCESS
            text = "Trend: improving"
        elif direction == "declining":
            color = SEMANTIC_LIGHT["danger"]
            text = "Trend: declining"
        else:
            color = _LABEL_SECONDARY
            text = "Trend: stable"
        self._set_text_if_changed(self._trend_pill, text)
        self._set_style_if_changed(
            self._trend_pill,
            f"color: {color};"
            f" background: {_GROUPED_BG};"
            f" border: 0.5px solid {_SEPARATOR};"
            f" border-radius: {RADIUS_PILL}px;"
            "  padding: 1px 9px;",
        )

    def _populate_distractions(self, rows: list[dict]) -> None:
        for i in reversed(range(self._distractions_layout.count())):
            item = self._distractions_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if w is not None:
                self._distractions_layout.removeWidget(w)
                w.deleteLater()
        if not rows:
            placeholder = QLabel("Not enough samples yet.")
            placeholder.setFont(mac_native.system_font(FS_CAPTION, "regular"))
            placeholder.setStyleSheet(
                f"color: {_LABEL_TERTIARY}; background: transparent;"
            )
            self._distractions_layout.addWidget(placeholder)
            return
        for r in rows:
            key = str(r.get("pattern_key") or "?")
            rate = float(r.get("overload_rate") or 0.0)
            label = QLabel(f"·  {key}  —  {rate * 100:.0f}% overload")
            label.setFont(mac_native.system_font(FS_FOOTNOTE, "regular"))
            label.setStyleSheet(
                f"color: {_LABEL_SECONDARY}; background: transparent;"
            )
            self._distractions_layout.addWidget(label)


# ---------------------------------------------------------------------------
# HistoryTab — the public widget plugged into DashboardWindow's QStackedWidget.
# ---------------------------------------------------------------------------


class HistoryTab(QWidget):
    """Top-level History tab. Owns the sub-segmented control, the three
    sub-pages (Today / Week / Month), and the overlay detail panel.
    """

    history_requested = Signal(object, int)  # since, limit
    detail_requested = Signal(str)
    trends_requested = Signal(str, bool)  # window, refresh

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CortexHistoryTab")
        self.setStyleSheet(f"background: {_WINDOW_BG};")
        self.setMaximumWidth(DASHBOARD_WIDTH + 60)

        # State: which sub-panels have already been auto-requested so we
        # don't refetch on every tab switch.
        #
        # Phase 4.B fix (#12): dropped the ``_first_show_done`` boolean.
        # The earlier flag duplicated the ``_requested`` set's role and
        # actively blocked the post-recap refresh path because
        # ``force_refresh`` only cleared ``_requested`` — the boolean
        # short-circuited ``showEvent`` from re-firing requests. The set
        # alone is sufficient: an empty set means "haven't fetched yet
        # for any sub-panel" and is what both first-show and
        # force_refresh check.
        self._requested: set[str] = set()
        # Phase 4.B fix (#13): per-session detail-request timeout timer.
        # Reset on every row click + every apply_session_detail arrival.
        self._detail_timeout_timer: QTimer | None = None
        self._pending_detail_id: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP4, SP3, SP4, SP3)
        outer.setSpacing(SP3)

        # P0 §3.23: Export current session button row. Sits above the
        # sub-segmented control so it is reachable from any tab. Only
        # enabled while a detail panel has a session loaded.
        export_row = QHBoxLayout()
        export_row.addStretch(1)
        self._export_btn = QPushButton("Export…")
        try:
            self._export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        except Exception:
            pass
        self._export_btn.setFont(mac_native.system_font(FS_CAPTION, "medium"))
        self._export_btn.setEnabled(False)
        try:
            self._export_btn.setToolTip(
                "Export the most recent session as CSV or JSON."
            )
        except Exception:
            pass
        self._export_btn.clicked.connect(self._on_export_clicked)
        export_row.addWidget(self._export_btn)
        outer.addLayout(export_row)

        self._sub_seg = _SubSegmented(["Today", "Week", "Month"])
        outer.addWidget(self._sub_seg)

        self._stack = QStackedWidget()
        self._today_panel = _TodayPanel()
        self._week_panel = _TrendsPanel(window="week")
        self._month_panel = _TrendsPanel(window="month")
        self._stack.addWidget(self._today_panel)
        self._stack.addWidget(self._week_panel)
        self._stack.addWidget(self._month_panel)
        outer.addWidget(self._stack, stretch=1)

        self._detail_panel = _DetailPanel(self)
        self._detail_panel.back_requested.connect(self._hide_detail_panel)

        self._sub_seg.selection_changed.connect(self._on_sub_segment_changed)
        self._today_panel.detail_requested.connect(self._on_row_clicked)
        self._today_panel.more_requested.connect(self.history_requested.emit)
        # Phase 4.B fix (#19): plumb the Trends panels' Refresh button
        # through to the outer ``trends_requested`` signal with
        # ``refresh=True`` so the daemon forces a re-aggregation.
        self._week_panel.refresh_requested.connect(self._on_refresh_requested)
        self._month_panel.refresh_requested.connect(self._on_refresh_requested)

    # ------------------------------------------------------------------
    # Visibility + first-show fetch.
    # ------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        try:
            super().showEvent(event)
        except Exception:
            pass
        # Phase 4.B fix (#12): no more ``_first_show_done`` flag. The
        # ``_requested`` set is the single source of truth; an empty
        # set on first show triggers the initial fetch, and
        # ``force_refresh`` empties the set to re-arm subsequent shows
        # after a daemon restart.
        self._maybe_request_for_index(self._stack.currentIndex())

    def _on_sub_segment_changed(self, index: int) -> None:
        try:
            self._stack.setCurrentIndex(index)
        except Exception:
            pass
        # Hide any open detail overlay when switching sub-pages.
        if index != 0 and self._detail_panel.isVisible():
            self._hide_detail_panel()
        self._maybe_request_for_index(index)

    def _maybe_request_for_index(self, index: int) -> None:
        if index == 0:
            if "today" not in self._requested:
                self._requested.add("today")
                # P2-5: show "Loading…" label while waiting for SESSION_LIST.
                try:
                    self._today_panel.set_list_loading(True)
                except Exception:
                    pass
                try:
                    self.history_requested.emit(None, 30)
                except Exception:
                    logger.debug("history_requested emit failed", exc_info=True)
                    # Clear loading if emit itself failed.
                    try:
                        self._today_panel.set_list_loading(False)
                    except Exception:
                        pass
        elif index == 1:
            if "week" not in self._requested:
                self._requested.add("week")
                try:
                    self.trends_requested.emit("week", False)
                except Exception:
                    logger.debug("trends_requested(week) emit failed", exc_info=True)
        elif index == 2:
            if "month" not in self._requested:
                self._requested.add("month")
                try:
                    self.trends_requested.emit("month", False)
                except Exception:
                    logger.debug("trends_requested(month) emit failed", exc_info=True)

    def force_refresh(self) -> None:
        """Drop the auto-request memo so the next show / segment switch
        re-fetches. Useful for tests and for re-pulling after a daemon
        restart."""
        self._requested.clear()
        self._maybe_request_for_index(self._stack.currentIndex())

    # ------------------------------------------------------------------
    # Detail panel routing.
    # ------------------------------------------------------------------

    def _on_row_clicked(self, session_id: str) -> None:
        """Phase 4.B fix (#13): show the detail panel in an explicit
        loading state and arm an 8 s timeout so a never-arriving
        SESSION_DETAIL doesn't leave the user staring at a spinner.
        """
        self._pending_detail_id = session_id
        self._show_detail_panel_loading()
        # Arm the timeout timer. ``QTimer.singleShot`` is fine but we
        # also need a way to cancel on apply, so use a held QTimer.
        if self._detail_timeout_timer is None:
            self._detail_timeout_timer = QTimer(self)
            self._detail_timeout_timer.setSingleShot(True)
            self._detail_timeout_timer.setInterval(_DETAIL_TIMEOUT_MS)
            self._detail_timeout_timer.timeout.connect(self._on_detail_timeout)
        try:
            self._detail_timeout_timer.start()
        except Exception:
            logger.debug("detail timeout timer start failed", exc_info=True)
        try:
            self.detail_requested.emit(session_id)
        except Exception:
            logger.debug("detail_requested emit failed", exc_info=True)

    def open_detail(self, session_id: str) -> None:
        """Public entry point — used by the RecapSheet's
        ``view_full_report`` route. Switches to the Today sub-page so
        the overlay appears in the right spot, then asks the daemon for
        the report.
        """
        # Force-switch to Today so the overlay anchors correctly.
        self._sub_seg.set_selected(0)
        try:
            self._stack.setCurrentIndex(0)
        except Exception:
            pass
        self._on_row_clicked(session_id)

    def _show_detail_panel_loading(self) -> None:
        """Phase 4.B fix (#13): use the panel's explicit
        :meth:`_DetailPanel.set_loading` API so the loading state has
        a distinct visual treatment (secondary colour, not danger red)
        from the post-fetch error path.
        """
        try:
            self._detail_panel.set_loading(True)
        except Exception:
            logger.debug("set_loading on detail panel failed", exc_info=True)
        self._position_detail_panel()
        self._detail_panel.show()
        self._detail_panel.raise_()

    def _on_detail_timeout(self) -> None:
        """Detail-request timeout fired. If the panel is still in the
        loading state, surface a clear "Request timed out" error so the
        user can hit back / try again. If the response arrived in the
        meantime (race), do nothing — apply_session_detail already
        cleared the loading flag."""
        if not getattr(self._detail_panel, "_loading", False):
            return
        logger.warning(
            "Detail request timed out for session_id=%s after %d ms",
            self._pending_detail_id, _DETAIL_TIMEOUT_MS,
        )
        try:
            self._detail_panel.apply_payload({
                "report": None,
                "error": "Request timed out",
            })
        except Exception:
            logger.debug(
                "apply_payload(error) on detail timeout failed", exc_info=True
            )

    def _hide_detail_panel(self) -> None:
        # Cancel any pending timeout so a stale fire doesn't surface a
        # timeout error after the user has navigated back to the list.
        if self._detail_timeout_timer is not None:
            try:
                self._detail_timeout_timer.stop()
            except Exception:
                pass
        self._pending_detail_id = None
        try:
            self._detail_panel.hide()
        except Exception:
            pass

    def _on_refresh_requested(self, window: str) -> None:
        """Phase 4.B fix (#19): forward the user's Refresh click to the
        outer ``trends_requested`` signal with ``refresh=True``."""
        if window not in ("week", "month"):
            logger.debug("ignoring refresh for unknown window %r", window)
            return
        try:
            self.trends_requested.emit(window, True)
        except Exception:
            logger.debug("trends_requested(refresh) emit failed", exc_info=True)

    def _position_detail_panel(self) -> None:
        try:
            self._detail_panel.setGeometry(self.rect())
        except Exception:
            pass

    def resizeEvent(self, event: Any) -> None:  # noqa: D401 - Qt override
        try:
            super().resizeEvent(event)
        except Exception:
            pass
        if self._detail_panel.isVisible():
            self._position_detail_panel()

    # ------------------------------------------------------------------
    # Apply incoming WS payloads (called from the controller wiring).
    # ------------------------------------------------------------------

    def apply_session_list(self, payload: dict) -> None:
        self._today_panel.apply_payload(payload)

    def apply_session_detail(self, payload: dict) -> None:
        # Phase 4.B fix (#13): cancel the detail-request timeout now
        # that the response (or an error envelope) has arrived. The
        # panel's apply_payload clears the loading flag itself.
        if self._detail_timeout_timer is not None:
            try:
                self._detail_timeout_timer.stop()
            except Exception:
                pass
        self._pending_detail_id = None
        self._detail_panel.apply_payload(payload)
        # P0 §3.23: stash the latest report for export. The report
        # lives under ``report`` per the SessionReport WS contract.
        try:
            if isinstance(payload, dict) and payload.get("report"):
                self._latest_export_report: dict = dict(payload.get("report") or {})
                self._export_btn.setEnabled(True)
        except Exception:
            logger.debug("export stash failed", exc_info=True)
        # Make sure the panel is visible after the data lands (it may
        # have been pre-shown by ``_show_detail_panel_loading``).
        self._position_detail_panel()
        try:
            self._detail_panel.show()
            self._detail_panel.raise_()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # P0 §3.23 — Session export (CSV / JSON)
    # ------------------------------------------------------------------

    def _on_export_clicked(self) -> None:
        """Open a small menu offering CSV or JSON export of the latest
        loaded session report. The choice opens a QFileDialog at the
        user's home directory.
        """
        report = getattr(self, "_latest_export_report", None) or None
        if not isinstance(report, dict) or not report:
            return
        try:
            menu = QMenu(self)
            csv_action = menu.addAction("Export as CSV…")
            json_action = menu.addAction("Export as JSON…")
            try:
                csv_action.triggered.connect(
                    lambda: self._do_export("csv", report),
                )
                json_action.triggered.connect(
                    lambda: self._do_export("json", report),
                )
            except Exception:
                pass
            anchor = self._export_btn.mapToGlobal(
                self._export_btn.rect().bottomLeft()
            )
            menu.exec(anchor)
        except Exception:
            logger.debug("export menu failed", exc_info=True)

    def _do_export(self, fmt: str, report: dict) -> None:
        from pathlib import Path
        session_id = str(report.get("session_id") or "session")
        suggested = f"cortex_{session_id}.{fmt}"
        try:
            path_str, _filter = QFileDialog.getSaveFileName(
                self,
                f"Export session as {fmt.upper()}",
                suggested,
                f"{fmt.upper()} files (*.{fmt})",
            )
        except Exception:
            logger.debug("file dialog open failed", exc_info=True)
            return
        if not path_str:
            return
        target = Path(path_str)
        try:
            if fmt == "json":
                import json as _json
                target.write_text(_json.dumps(report, indent=2, default=str))
            else:
                target.write_text(self._report_to_csv(report))
        except Exception:
            logger.warning("session export to %s failed", target, exc_info=True)

    @staticmethod
    def _report_to_csv(report: dict) -> str:
        """Flatten a SessionReport-like dict into a CSV string.

        We emit one row per state transition + one row per intervention.
        The columns are intentionally minimal so the output opens
        cleanly in Excel / Numbers without ragged-row warnings.
        """
        import csv as _csv
        import io as _io

        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(
            ["kind", "timestamp", "field_a", "field_b", "field_c"]
        )
        for trans in report.get("state_transitions", []) or []:
            if not isinstance(trans, dict):
                continue
            writer.writerow([
                "state_transition",
                str(trans.get("timestamp") or ""),
                str(trans.get("from_state") or ""),
                str(trans.get("to_state") or ""),
                str(trans.get("confidence") or ""),
            ])
        for itv in report.get("interventions", []) or []:
            if not isinstance(itv, dict):
                continue
            writer.writerow([
                "intervention",
                str(itv.get("timestamp") or itv.get("issued_at") or ""),
                str(itv.get("intervention_id") or ""),
                str(itv.get("headline") or ""),
                str(itv.get("user_action") or ""),
            ])
        return buf.getvalue()

    def apply_trends(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        # Phase 4.B fix (#18): the schema is ``Literal["week", "month"]``
        # — there is no "quarter" window any more. Drop the previous
        # fallthrough that routed quarter → month_panel (which would
        # have silently mis-rendered a payload labelled as quarter on
        # the month sub-page). Unknown windows are now logged and
        # ignored so the bug surfaces visibly instead of corrupting
        # the month panel.
        win = str(payload.get("window") or "week")
        if win == "week":
            self._week_panel.apply_payload(payload)
        elif win == "month":
            self._month_panel.apply_payload(payload)
        else:
            logger.debug(
                "apply_trends: ignoring payload with unknown window=%r", win,
            )


__all__ = ["HistoryTab"]
