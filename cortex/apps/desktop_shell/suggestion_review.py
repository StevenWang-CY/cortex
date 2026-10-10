"""One lifetime for the dashboard review entry and its exact suggestion card.

Unsolicited suggestions never activate a window. A user's explicit Review
gesture may activate the existing card; it does not authorize an action.
"""

from __future__ import annotations

import copy
import math
import time
from collections import deque
from typing import Any

from PySide6.QtCore import QObject, QTimer


class SuggestionReview(QObject):
    """Shared presenter for in-process and WebSocket desktop hosts."""

    def __init__(self, dashboard: Any, overlay: Any) -> None:
        super().__init__(dashboard)
        self._dashboard = dashboard
        self._overlay = overlay
        self._payload: dict[str, Any] | None = None
        self._deadline = 0.0
        self._shown = False
        self._retired: deque[str] = deque(maxlen=128)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.clear)
        dashboard.review_suggestion_requested.connect(self.review)
        dashboard.daemon_stop_requested.connect(self.clear)
        overlay.dismissed.connect(self.clear)
        overlay.action_invoked.connect(lambda intervention_id, _action: self.clear(intervention_id))

    def present(self, payload: dict[str, Any], *, show_immediately: bool) -> None:
        intervention_id = str(payload.get("intervention_id") or "")
        if not intervention_id or intervention_id in self._retired:
            return
        now = time.monotonic()
        deadline = now + 300.0
        manifest = payload.get("action_manifest")
        if isinstance(manifest, dict):
            raw_expiry = manifest.get("expires_at_unix_ms")
            if isinstance(raw_expiry, (int, float)) and not isinstance(raw_expiry, bool):
                if not math.isfinite(raw_expiry):
                    self.clear()
                    return
                deadline = min(deadline, now + raw_expiry / 1000.0 - time.time())
        if self._payload and self._payload.get("intervention_id") == intervention_id:
            # Micro-step updates and reopening the card cannot renew authority.
            deadline = min(deadline, self._deadline)
        else:
            self.clear()
        if deadline <= now:
            self.clear()
            return
        self._payload = copy.deepcopy(payload)
        self._deadline = deadline
        self._dashboard.set_pending_suggestion(self._payload)
        self._timer.start(max(1, int((deadline - now) * 1000)))
        if show_immediately:
            self._overlay.show_intervention(copy.deepcopy(self._payload))
            self._record_shown()

    def review(self) -> None:
        if self._payload is None or time.monotonic() >= self._deadline:
            self.clear()
            return
        self._overlay.show_intervention(copy.deepcopy(self._payload))
        self._overlay.focus_for_review()
        self._record_shown()

    def _record_shown(self) -> None:
        # Micro-step updates and reopening a card are the same nudge. A
        # background-only or screen-share-suppressed proposal was not shown.
        if self._shown or not self._overlay.isVisible():
            return
        self._shown = True
        self._dashboard.record_intervention_seen()

    def clear(self, intervention_id: str = "") -> None:
        if intervention_id and (
            self._payload is None
            or self._payload.get("intervention_id") != intervention_id
        ):
            return
        self._timer.stop()
        if self._payload is not None:
            self._retired.append(str(self._payload["intervention_id"]))
        self._payload = None
        self._deadline = 0.0
        self._shown = False
        self._dashboard.set_pending_suggestion(None)
        retire = getattr(self._overlay, "retire_suggestion", self._overlay.suppress)
        retire()

    def transaction_updated(self, payload: dict[str, Any]) -> None:
        if payload.get("intervention_id") and str(payload.get("state") or "") in {
            "applied", "failed", "restored", "restore_failed", "expired", "cancelled",
        }:
            self.clear(str(payload.get("intervention_id") or ""))

    def quiet_updated(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") not in (None, "off"):
            self.clear()
