"""Regression tests for crash-visible packaged desktop startup."""

from __future__ import annotations

import logging
from pathlib import Path

from cortex.apps.desktop_shell import bootstrap


def test_startup_failure_is_persisted_without_a_gui(
    tmp_path: Path,
    monkeypatch,
) -> None:
    log_directory = tmp_path / "logs"
    startup_log = log_directory / "startup.log"
    last_error = log_directory / "last-startup-error.txt"
    monkeypatch.setattr(bootstrap, "_LOG_DIRECTORY", log_directory)
    monkeypatch.setattr(bootstrap, "_STARTUP_LOG", startup_log)
    monkeypatch.setattr(bootstrap, "_LAST_ERROR", last_error)
    monkeypatch.setenv("CORTEX_HEADLESS_STARTUP", "1")

    root = logging.getLogger()
    original_handlers = tuple(root.handlers)
    try:
        bootstrap.install_startup_logging()
        try:
            raise FileNotFoundError("frozen implementation input is unavailable")
        except FileNotFoundError as exc:
            incident_id = bootstrap.report_startup_failure(exc, exc.__traceback__)
        for handler in root.handlers:
            handler.flush()

        assert incident_id.startswith("startup-")
        assert incident_id in last_error.read_text(encoding="utf-8")
        assert "FileNotFoundError" in last_error.read_text(encoding="utf-8")
        assert incident_id in startup_log.read_text(encoding="utf-8")
    finally:
        for handler in tuple(root.handlers):
            if handler not in original_handlers:
                root.removeHandler(handler)
                handler.close()


def test_guarded_entrypoint_returns_failure_after_reporting(monkeypatch) -> None:
    reported: list[BaseException] = []
    monkeypatch.setattr(bootstrap, "install_startup_logging", lambda: None)
    monkeypatch.setattr(bootstrap, "_install_unhandled_hooks", lambda: None)
    monkeypatch.setattr(
        bootstrap,
        "report_startup_failure",
        lambda exception, _traceback: reported.append(exception) or "startup-test",
    )

    def _fail() -> None:
        raise RuntimeError("composition failed")

    assert bootstrap.run_guarded(_fail) == 1
    assert len(reported) == 1
    assert isinstance(reported[0], RuntimeError)
