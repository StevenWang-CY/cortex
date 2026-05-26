"""macOS user notifications via PyObjC ``UNUserNotificationCenter``.

P0 §3.12 — when the desktop dashboard is NOT the foreground window,
the daemon dispatches a system notification so the user actually sees
the intervention cue from another Space or fullscreen app.

Privacy invariant: the notification body MUST NOT include biometric
values (``hr``, ``hrv_rmssd``, ``blink_rate``, raw scores). The body
shows the LLM-generated ``headline`` / ``primary_focus`` only; both
are already F09-sanitised before they leave the planner. Notifications
respect macOS Focus Mode and the system-wide "Do Not Disturb" setting.

Threading invariant (Phase 3 P1-N2): every PyObjC call into AppKit
must run on a thread that owns an ``NSRunLoop``. Scheduling from an
``asyncio.to_thread`` worker is unsafe — the completion handlers
queue on the calling thread's run loop, and the worker thread has
none. We therefore dispatch every notification via
``dispatch_get_main_queue`` (PyObjC's
``Foundation.NSOperationQueue.mainQueue()``) so the Cocoa main run
loop owns the request lifetime.

Permission lifecycle (Phase 3 P2-N1): the OS may revoke notification
permission at any time. The previous implementation cached
``_authorization_requested`` and never re-checked — a user who later
disabled permissions in System Settings appeared to be receiving
notifications but the daemon silently dropped them. We now query
``getNotificationSettingsWithCompletionHandler_`` and cache the live
authorisation status with a short TTL so revocation is observable.

Action buttons (Phase 3 P0-N2): banners need a registered
``UNNotificationCategory`` with ``UNNotificationAction``s and a live
``UNUserNotificationCenterDelegate`` — without the category, the OS
renders a plain banner with no clickable choices. The
``cortex_intervention`` category is registered at module load and a
``CortexNotificationDelegate`` bridges click events into a callback
the daemon registers via :func:`set_user_action_handler`.

This module is import-safe on non-macOS / when PyObjC is missing.
Every public helper returns silently in that case so the caller
(``runtime_daemon._dispatch_os_notification``) can degrade cleanly.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from functools import lru_cache
from typing import Callable

logger = logging.getLogger(__name__)


def _is_macos() -> bool:
    return sys.platform == "darwin"


@lru_cache(maxsize=None)
def _log_unsupported_once(reason: str) -> None:
    """Log each distinct ``reason`` exactly once. ``maxsize=None`` (vs
    the previous ``maxsize=1`` which evicted between reasons) so two
    distinct unsupported paths each emit a single line."""
    logger.info("macOS notifications unsupported: %s", reason)


@lru_cache(maxsize=1)
def _load_user_notifications():  # type: ignore[no-untyped-def]
    """Lazy-import ``UserNotifications`` once. Returns ``None`` when the
    PyObjC bridge or the framework itself is unavailable.
    """
    if not _is_macos():
        _log_unsupported_once("not macOS")
        return None
    try:
        import UserNotifications  # type: ignore[import-not-found]
    except Exception:
        _log_unsupported_once("UserNotifications import failed")
        return None
    return UserNotifications


# Live authorisation state with a TTL so OS-level revocation is observable.
_AUTH_TTL_SECONDS: float = 30.0
_auth_state: dict[str, object] = {
    "granted": None,  # None=unknown, True/False once resolved
    "checked_at": 0.0,
    "requested": False,
}
_auth_lock = threading.Lock()

# Delegate / category registration state.
_delegate_instance = None  # type: ignore[assignment]
_user_action_handler: Callable[[str, str], None] | None = None
_CATEGORY_ID = "cortex_intervention"
_ACTION_OPEN = "cortex_open"
_ACTION_SNOOZE = "cortex_snooze"

# Shutdown guard (audit P1): once the daemon flips this Event, any
# in-flight Cocoa notification callback must short-circuit BEFORE it
# invokes the registered handler — otherwise the click reaches a
# half-torn-down daemon (cancelled tasks, closed asyncio loop) and can
# crash the process. Cocoa callbacks fire on the AppKit main thread
# while daemon shutdown runs on the asyncio thread; ``threading.Event``
# gives us a lock-free, thread-safe boolean visible from both.
_shutdown_event = threading.Event()


def set_user_action_handler(handler: Callable[[str, str], None] | None) -> None:
    """Register a callback for notification action clicks.

    The handler receives ``(intervention_id, action_id)`` where
    ``action_id`` is one of ``"open"`` / ``"snooze"`` / ``"default"``
    (tap-on-banner without a button). The daemon binds this on startup
    to fan into ``set_quiet_mode`` / dashboard activation.
    """
    global _user_action_handler
    _user_action_handler = handler


def mark_shutting_down() -> None:
    """Tell the notification delegate the daemon is tearing down.

    After this is called, in-flight notification action callbacks
    invoke their completion handler and return without dispatching to
    the registered ``_user_action_handler``. Safe to call from any
    thread — the underlying ``threading.Event`` is lock-free.

    Idempotent: callable multiple times during a shutdown sequence
    (e.g. cooperative stop, then forced stop).
    """
    _shutdown_event.set()


def is_shutting_down() -> bool:
    """Query the shutdown guard (test/debug helper)."""
    return _shutdown_event.is_set()


def reset_shutdown_state_for_tests() -> None:
    """Test-only: clear the shutdown latch between cases."""
    _shutdown_event.clear()


def _on_main_thread(callable_obj) -> None:  # type: ignore[no-untyped-def]
    """Run ``callable_obj`` on the AppKit main thread. PyObjC's
    ``NSOperationQueue.mainQueue().addOperationWithBlock_`` is the
    documented thread-marshal primitive that doesn't require a
    running CFRunLoop on the caller's side."""
    try:
        import Foundation  # type: ignore[import-not-found]
        Foundation.NSOperationQueue.mainQueue().addOperationWithBlock_(callable_obj)
    except Exception:
        # Best-effort fallback: just run in-place. Used by unit tests
        # where PyObjC isn't loaded.
        try:
            callable_obj()
        except Exception:
            logger.debug("inline main-thread fallback raised", exc_info=True)


def _build_delegate(un):  # type: ignore[no-untyped-def]
    """Construct (once) the UNUserNotificationCenterDelegate subclass."""
    global _delegate_instance
    if _delegate_instance is not None:
        return _delegate_instance
    try:
        import objc  # type: ignore[import-not-found]
        from Foundation import NSObject  # type: ignore[import-not-found]

        class _CortexNotificationDelegate(NSObject):  # type: ignore[misc]
            # Tap on banner OR action button.
            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(  # noqa: D401
                self, _center, response, completion_handler,
            ):
                # Audit P1 shutdown guard: if the daemon is tearing
                # down, complete the OS contract (call the completion
                # handler) but DO NOT dispatch into the daemon — its
                # asyncio tasks may already be cancelled and the
                # callback could crash on a half-torn-down state.
                if _shutdown_event.is_set():
                    try:
                        completion_handler()
                    except Exception:
                        pass
                    return
                try:
                    action_id_raw = str(response.actionIdentifier())
                    request = response.notification().request()
                    notif_id = str(request.identifier())
                except Exception:
                    action_id_raw = ""
                    notif_id = ""
                action_id = "default"
                if action_id_raw == _ACTION_OPEN:
                    action_id = "open"
                elif action_id_raw == _ACTION_SNOOZE:
                    action_id = "snooze"
                # ``UNNotificationDefaultActionIdentifier`` is the
                # opaque sentinel string for "user tapped the banner".
                intervention_id = ""
                prefix = "cortex_intervention_"
                if notif_id.startswith(prefix):
                    intervention_id = notif_id[len(prefix):]
                handler = _user_action_handler
                # Re-check the guard right before dispatch — shutdown
                # can flip between the top-of-callback check and here
                # (different threads).
                if handler is not None and not _shutdown_event.is_set():
                    try:
                        handler(intervention_id, action_id)
                    except Exception:
                        logger.debug(
                            "macOS notification handler raised",
                            exc_info=True,
                        )
                try:
                    completion_handler()
                except Exception:
                    pass

            # Foreground delivery — allow banner + sound when the app
            # is active so the user still sees the cue.
            def userNotificationCenter_willPresentNotification_withCompletionHandler_(  # noqa: D401
                self, _center, _notification, completion_handler,
            ):
                # Audit P1 shutdown guard: suppress the banner if we
                # are tearing down so the user isn't left with a click
                # target that routes into a dead daemon.
                if _shutdown_event.is_set():
                    try:
                        completion_handler(0)  # UNNotificationPresentationOptionNone
                    except Exception:
                        pass
                    return
                try:
                    # 0x07 = Banner + List + Sound (UNNotificationPresentationOption*).
                    completion_handler(7)
                except Exception:
                    pass

        _delegate_instance = _CortexNotificationDelegate.alloc().init()
        return _delegate_instance
    except Exception:
        logger.debug("UN delegate construction failed", exc_info=True)
        return None


def _register_category(un) -> None:  # type: ignore[no-untyped-def]
    """Register the ``cortex_intervention`` category with Open/Snooze
    action buttons. Safe to call repeatedly — UN deduplicates by id."""
    try:
        center = un.UNUserNotificationCenter.currentNotificationCenter()
        delegate = _build_delegate(un)
        if delegate is not None:
            center.setDelegate_(delegate)
        open_action = un.UNNotificationAction.actionWithIdentifier_title_options_(
            _ACTION_OPEN, "Open", 0,  # 0 = UNNotificationActionOptionNone
        )
        snooze_action = un.UNNotificationAction.actionWithIdentifier_title_options_(
            _ACTION_SNOOZE, "Snooze 15", 0,
        )
        category = un.UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
            _CATEGORY_ID, [open_action, snooze_action], [], 0,
        )
        center.setNotificationCategories_({category})
    except Exception:
        logger.debug("UN category registration failed", exc_info=True)


def _refresh_auth_state(un) -> None:  # type: ignore[no-untyped-def]
    """Query the OS for the live notification settings and update the
    cached state. The completion handler runs on a Cocoa worker; we
    use a threading.Event-like ``_auth_lock`` to serialise updates.
    """
    try:
        center = un.UNUserNotificationCenter.currentNotificationCenter()
        def _completion(settings):  # type: ignore[no-untyped-def]
            with _auth_lock:
                try:
                    status = int(settings.authorizationStatus())
                    # UNAuthorizationStatusAuthorized == 2
                    # UNAuthorizationStatusProvisional == 3
                    # UNAuthorizationStatusEphemeral == 4
                    _auth_state["granted"] = status in (2, 3, 4)
                except Exception:
                    _auth_state["granted"] = None
                _auth_state["checked_at"] = time.time()
        center.getNotificationSettingsWithCompletionHandler_(_completion)
    except Exception:
        logger.debug("UN settings probe raised", exc_info=True)


def _ensure_authorized(un) -> bool:  # type: ignore[no-untyped-def]
    """Best-effort authorisation flow. Returns True iff we believe
    notifications are permitted right now.

    Side effects:
      * On first call OR after the auth TTL expires, schedules a
        permission re-query.
      * On first call ever, also schedules an authorisation request
        — this triggers the macOS permission prompt if needed.
    Both are non-blocking; the first notification after a fresh grant
    may be silently dropped by the OS while the prompt is on-screen
    (acceptable: subsequent notifications will land).
    """
    with _auth_lock:
        granted = _auth_state.get("granted")
        checked_at = float(_auth_state.get("checked_at") or 0.0)
        already_requested = bool(_auth_state.get("requested"))
        ttl_ok = (time.time() - checked_at) < _AUTH_TTL_SECONDS
    if ttl_ok and granted is False:
        # Fresh deny — return immediately so we don't queue add-requests.
        return False
    # Schedule a fresh probe so the next call sees current settings.
    _on_main_thread(lambda: _refresh_auth_state(un))
    if not already_requested:
        with _auth_lock:
            _auth_state["requested"] = True
        # First-ever call: also fire the authorisation request.
        try:
            center = un.UNUserNotificationCenter.currentNotificationCenter()
            options = (
                un.UNAuthorizationOptionAlert
                | un.UNAuthorizationOptionSound
            )
            def _completion(granted_local, _error):  # type: ignore[no-untyped-def]
                with _auth_lock:
                    _auth_state["granted"] = bool(granted_local)
                    _auth_state["checked_at"] = time.time()
                if granted_local:
                    logger.info("macOS notification permission granted")
                else:
                    logger.info(
                        "macOS notification permission denied or pending",
                    )
            _on_main_thread(
                lambda: center.requestAuthorizationWithOptions_completionHandler_(
                    options, _completion,
                ),
            )
        except Exception:
            logger.debug("requestAuthorization raised", exc_info=True)
            return False
    # If we've never resolved a status, optimistically allow — the OS
    # will reject silently if denied and the next probe will reconcile.
    return granted is not False


def send_intervention_notification(
    *,
    title: str,
    body: str,
    intervention_id: str = "",
    sound: bool = True,
) -> bool:
    """Post a single intervention notification to the macOS Notification
    Center with Open/Snooze action buttons.

    Args:
        title: Notification title (the LLM-generated headline).
        body: One-line body. MUST NOT include biometric numerics.
        intervention_id: id used to thread the notification (so a
            second pulse for the same intervention replaces the first).
            The id is also surfaced in the action callback so the
            handler can correlate clicks back to interventions.
        sound: When True, the OS plays its default notification sound
            (subject to the user's Focus Mode and system-wide DND).

    Returns:
        True when the notification was scheduled. False on any failure
        path (non-mac, missing PyObjC, permission denied, schedule
        error). Callers should not treat the return value as a
        delivery guarantee — the OS may still suppress the alert via
        Focus Mode / DND.
    """
    if not _is_macos():
        _log_unsupported_once("not macOS")
        return False
    un = _load_user_notifications()
    if un is None:
        return False
    if not _ensure_authorized(un):
        return False
    title = (title or "").strip() or "Cortex"
    body = (body or "").strip() or "Cortex has a suggestion"
    title = title[:120]
    body = body[:240]
    request_id = (
        f"cortex_intervention_{intervention_id}"
        if intervention_id
        else "cortex_intervention"
    )

    def _post() -> None:
        try:
            content = un.UNMutableNotificationContent.alloc().init()
            content.setTitle_(title)
            content.setBody_(body)
            content.setCategoryIdentifier_(_CATEGORY_ID)
            if sound:
                content.setSound_(un.UNNotificationSound.defaultSound())
            request = un.UNNotificationRequest.requestWithIdentifier_content_trigger_(
                request_id, content, None,
            )
            center = un.UNUserNotificationCenter.currentNotificationCenter()
            def _add_completion(error):  # type: ignore[no-untyped-def]
                if error is not None:
                    logger.warning(
                        "UN add error for %s: %s", request_id, error,
                    )
                else:
                    logger.info("UN scheduled %s", request_id)
            center.addNotificationRequest_withCompletionHandler_(
                request, _add_completion,
            )
        except Exception:
            logger.debug("UN post raised", exc_info=True)

    # Lazy registration: ensure the category + delegate are in place
    # before posting. Both operations need the Cocoa main thread.
    _on_main_thread(lambda: _register_category(un))
    _on_main_thread(_post)
    return True


def send_notification(
    *,
    title: str,
    body: str,
    sound: bool = True,
) -> bool:
    """Generic notification dispatcher — kept for forward compatibility.
    The intervention path uses :func:`send_intervention_notification`
    so the request id namespace stays consistent across replaces.
    """
    return send_intervention_notification(
        title=title, body=body, intervention_id="", sound=sound,
    )


def reset_auth_state_for_tests() -> None:
    """Test-only: clear the cached authorisation latch."""
    with _auth_lock:
        _auth_state["granted"] = None
        _auth_state["checked_at"] = 0.0
        _auth_state["requested"] = False
