"""Crash-visible bootstrap for the packaged Cortex desktop application.

This module intentionally imports only the standard library at module load.
PyInstaller uses it as the executable entry point, allowing Cortex to record
and surface failures that happen while importing the heavier Qt/runtime graph.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
import traceback
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from uuid import uuid4

_LOG_DIRECTORY = Path.home() / "Library" / "Logs" / "Cortex"
_STARTUP_LOG = _LOG_DIRECTORY / "startup.log"
_LAST_ERROR = _LOG_DIRECTORY / "last-startup-error.txt"
_HANDLER_MARKER = "_cortex_startup_file_handler"


def startup_log_path() -> Path:
    """Return the stable, user-readable desktop startup log path."""

    return _STARTUP_LOG


def _secure_log_directory() -> None:
    _LOG_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        _LOG_DIRECTORY.chmod(0o700)
    except OSError:
        # A pre-existing directory may live on a filesystem without POSIX
        # modes. Logging remains more valuable than aborting startup.
        pass


def install_startup_logging() -> None:
    """Install one bounded file sink before importing application modules."""

    _secure_log_directory()
    root = logging.getLogger()
    if not any(
        bool(getattr(handler, _HANDLER_MARKER, False))
        for handler in root.handlers
    ):
        handler = RotatingFileHandler(
            _STARTUP_LOG,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        root.addHandler(handler)
        try:
            _STARTUP_LOG.chmod(0o600)
        except OSError:
            pass
    root.setLevel(logging.INFO)


def _write_last_error(
    incident_id: str,
    exception: BaseException,
    traceback_value: TracebackType | None,
) -> None:
    """Atomically retain the latest complete startup traceback."""

    rendered = "".join(
        traceback.format_exception(type(exception), exception, traceback_value)
    )
    payload = (
        "Cortex could not finish starting.\n"
        f"Diagnostic reference: {incident_id}\n"
        f"Version: {_version_for_diagnostics()}\n"
        f"Architecture: {platform.machine()}\n"
        f"Frozen bundle: {bool(getattr(sys, 'frozen', False))}\n\n"
        f"{rendered}"
    )
    temporary = _LAST_ERROR.with_suffix(".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(_LAST_ERROR)
    try:
        _LAST_ERROR.chmod(0o600)
    except OSError:
        pass


def _version_for_diagnostics() -> str:
    try:
        from cortex import __version__

        return __version__
    except Exception:
        return "unknown"


def _show_fatal_dialog(incident_id: str) -> None:
    """Show a bounded, actionable Qt alert when a GUI startup fails."""

    if os.environ.get("CORTEX_HEADLESS_STARTUP") == "1":
        return
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        owns_app = app is None
        if app is None:
            app = QApplication([sys.argv[0]])
        message = (
            "Cortex could not finish starting. No sensing or workspace "
            "changes were started.\n\n"
            f"Diagnostic reference: {incident_id}\n"
            f"Details were saved to:\n{_LAST_ERROR}\n\n"
            "If Cortex is still on the disk image, drag it to Applications "
            "before trying again."
        )
        QMessageBox.critical(None, "Cortex couldn’t start", message)
        if owns_app:
            app.quit()
    except Exception:
        # The durable file remains authoritative if Qt itself cannot load.
        logging.getLogger(__name__).exception("startup.fatal_dialog_failed")


def report_startup_failure(
    exception: BaseException,
    traceback_value: TracebackType | None,
) -> str:
    """Persist and display one startup failure; return its reference id."""

    incident_id = f"startup-{uuid4().hex[:12]}"
    logger = logging.getLogger(__name__)
    logger.critical(
        "startup.failed incident_id=%s exception_type=%s",
        incident_id,
        type(exception).__name__,
        exc_info=(type(exception), exception, traceback_value),
    )
    try:
        _write_last_error(incident_id, exception, traceback_value)
    except OSError:
        logger.exception("startup.last_error_write_failed incident_id=%s", incident_id)
    _show_fatal_dialog(incident_id)
    return incident_id


def _install_unhandled_hooks() -> None:
    previous_sys_hook = sys.excepthook

    def _sys_hook(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback_value: TracebackType | None,
    ) -> None:
        report_startup_failure(exception, traceback_value)
        previous_sys_hook(exception_type, exception, traceback_value)

    sys.excepthook = _sys_hook

    previous_thread_hook = threading.excepthook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger(__name__).critical(
            "background_thread.failed thread=%s exception_type=%s",
            args.thread.name if args.thread is not None else "unknown",
            args.exc_type.__name__,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        previous_thread_hook(args)

    threading.excepthook = _thread_hook


def run_guarded(entrypoint: Callable[[], object]) -> int:
    """Run the desktop entry point with durable top-level failure handling."""

    install_startup_logging()
    _install_unhandled_hooks()
    logger = logging.getLogger(__name__)
    logger.info(
        "startup.begin version=%s frozen=%s architecture=%s executable=%s",
        _version_for_diagnostics(),
        bool(getattr(sys, "frozen", False)),
        platform.machine(),
        sys.executable,
    )
    try:
        result = entrypoint()
    except KeyboardInterrupt:
        logger.info("startup.interrupted")
        return 130
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        logger.error("startup.system_exit_non_numeric value=%r", code)
        return 1
    except BaseException as exc:
        report_startup_failure(exc, exc.__traceback__)
        return 1
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    logger.error("startup.entrypoint_invalid_return type=%s", type(result).__name__)
    return 1


def main() -> int:
    """Import the full application only after the crash sink is active."""

    def _application_entrypoint() -> object:
        from cortex.apps.desktop_shell.main import main as desktop_main

        return desktop_main()

    return run_guarded(_application_entrypoint)


if __name__ == "__main__":
    sys.exit(main())
