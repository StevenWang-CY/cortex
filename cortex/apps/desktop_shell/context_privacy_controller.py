"""Non-blocking controller for the authenticated local privacy API.

Both desktop launch modes use this controller.  Raw workspace context never
enters the Qt process: the view sends only source-selection booleans, receives
the broker's already-redacted preview, and later submits the opaque one-time
handle.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot

from cortex.libs.auth import load_or_create_token
from cortex.libs.config.settings import get_config

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 256_000


class ContextPrivacyController(QObject):
    """Small view-controller adapter; all loopback I/O stays off the UI thread."""

    status_received = Signal(dict)
    preview_received = Signal(dict)
    confirmation_received = Signal(dict)
    cancellation_received = Signal(dict)
    request_failed = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        config = get_config()
        self._base_url = f"http://{config.api.host}:{config.api.port}"
        self._busy: set[str] = set()
        self._busy_lock = threading.Lock()

    @Slot()
    def refresh_status(self) -> None:
        self._start("status", "GET", "/privacy/context/status", None, timeout=5.0)

    @Slot(dict)
    def preview_current(self, payload: dict[str, Any]) -> None:
        body: dict[str, Any] = {
            "selection": payload.get("selection")
            if isinstance(payload.get("selection"), dict)
            else {},
            "extra_context": str(payload.get("extra_context") or "")[:2_000],
        }
        self._start(
            "preview",
            "POST",
            "/privacy/context/preview/current",
            body,
            timeout=8.0,
        )

    @Slot(str, str)
    def confirm_once(self, preview_id: str, confirmation_phrase: str) -> None:
        self._start(
            "confirm",
            "POST",
            "/privacy/context/confirm",
            {
                "preview_id": str(preview_id)[:160],
                "confirmation_phrase": str(confirmation_phrase),
            },
            timeout=45.0,
        )

    @Slot(str)
    def cancel_preview(self, preview_id: str) -> None:
        handle = str(preview_id)[:160]
        if not handle:
            return
        self._start(
            "cancel",
            "DELETE",
            f"/privacy/context/preview/{urllib.parse.quote(handle, safe='')}",
            None,
            timeout=5.0,
        )

    def _start(
        self,
        operation: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout: float,
    ) -> None:
        with self._busy_lock:
            if operation in self._busy:
                return
            self._busy.add(operation)

        worker = threading.Thread(
            target=self._request_worker,
            args=(operation, method, path, payload, timeout),
            name=f"cortex-privacy-{operation}",
            daemon=True,
        )
        worker.start()

    def _request_worker(
        self,
        operation: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> None:
        try:
            token = load_or_create_token()
            body = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                if payload is not None
                else None
            )
            request = urllib.request.Request(
                self._base_url + path,
                data=body,
                method=method,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Cortex-Request-ID": str(uuid4()),
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("local privacy response exceeded its size bound")
            decoded = json.loads(raw.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("local privacy response had an invalid shape")
        except urllib.error.HTTPError as exc:
            message = self._http_error_message(exc)
            self.request_failed.emit({"operation": operation, "message": message})
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Local privacy %s failed: %s", operation, type(exc).__name__)
            self.request_failed.emit(
                {
                    "operation": operation,
                    "message": self._friendly_error(operation, exc),
                }
            )
        except Exception as exc:
            logger.exception("Unexpected local privacy controller failure")
            self.request_failed.emit(
                {
                    "operation": operation,
                    "message": self._friendly_error(operation, exc),
                }
            )
        else:
            if operation == "status":
                self.status_received.emit(decoded)
            elif operation == "preview":
                self.preview_received.emit(decoded)
            elif operation == "confirm":
                self.confirmation_received.emit(decoded)
            else:
                self.cancellation_received.emit(decoded)
        finally:
            with self._busy_lock:
                self._busy.discard(operation)

    @staticmethod
    def _http_error_message(exc: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            raw = exc.read(4_097)
            if len(raw) <= 4_096:
                body = json.loads(raw.decode("utf-8"))
                if isinstance(body, dict):
                    detail = str(body.get("detail") or "")[:500]
        except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
            detail = ""
        if detail:
            return detail
        if exc.code == 401:
            return "Cortex could not authenticate the local privacy request. Rotate the local token or restart the app."
        if exc.code == 409:
            return "The external planner is off or a live workspace snapshot is not ready yet."
        return f"The local privacy service returned HTTP {exc.code}."

    @staticmethod
    def _friendly_error(operation: str, exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return (
                "The provider did not answer before the request deadline."
                if operation == "confirm"
                else "The local privacy service did not answer in time."
            )
        if isinstance(exc, urllib.error.URLError):
            return "The Cortex daemon is not reachable yet. Start it and try again."
        if isinstance(exc, ValueError):
            return "Cortex rejected an invalid or oversized privacy response."
        return "The local privacy request could not be completed."
