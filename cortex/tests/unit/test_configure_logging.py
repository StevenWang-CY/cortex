"""C6 (audit): ``configure_logging`` is fully built and exported, but the
entrypoints historically called ``logging.basicConfig`` directly so the
structlog processor chain was never installed. These tests pin the
behaviour the entrypoints (run_dev, runtime_daemon, desktop_shell) now
rely on:

1. After ``configure_logging()`` structlog reports itself as configured.
2. The function is idempotent w.r.t. level — re-invoking with a new level
   updates the root logger level (``basicConfig`` alone would not).
3. ``configure_logging`` is exported from the package.
"""

from __future__ import annotations

import logging

import structlog

from cortex.libs.logging import configure_logging as exported_configure_logging
from cortex.libs.logging.structured import configure_logging, get_logger


def test_configure_logging_marks_structlog_configured() -> None:
    # structlog tracks whether ``configure`` has been called via this
    # private flag; ``configure_logging`` must flip it to True.
    structlog.reset_defaults()
    assert structlog.is_configured() is False
    configure_logging(level="INFO", json_format=True)
    assert structlog.is_configured() is True


def test_configure_logging_installs_processor_chain() -> None:
    """After configuration the live structlog config carries our custom
    processor chain (``add_service_context`` / ``add_timestamp``), proving
    the chain — not just basicConfig — is installed. Also confirms the
    stdlib BoundLogger wrapper_class is wired (get_logger returns a lazy
    proxy that materialises into a BoundLogger on first use)."""
    configure_logging(level="INFO", json_format=True)
    # get_logger returns a logger usable for emitting events.
    logger = get_logger("cortex.test")
    logger.info("smoke")  # must not raise — proves the chain is callable
    cfg = structlog.get_config()
    assert cfg["wrapper_class"] is structlog.stdlib.BoundLogger
    proc_names = {getattr(p, "__name__", type(p).__name__) for p in cfg["processors"]}
    assert "add_service_context" in proc_names
    assert "add_timestamp" in proc_names


def test_configure_logging_is_idempotent_on_level() -> None:
    configure_logging(level="INFO", json_format=True)
    assert logging.getLogger().level == logging.INFO
    # Re-invoke with a different level — must take effect even though the
    # root logger already has handlers (basicConfig alone is a no-op here).
    configure_logging(level="DEBUG", json_format=True)
    assert logging.getLogger().level == logging.DEBUG
    # restore a sane default for sibling tests
    configure_logging(level="INFO", json_format=True)


def test_configure_logging_is_exported_from_package() -> None:
    assert exported_configure_logging is configure_logging
