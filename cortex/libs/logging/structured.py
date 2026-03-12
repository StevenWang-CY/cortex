"""
Cortex Structured Logging

JSON-based structured logging using structlog for consistent event tracking.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from enum import Enum
from typing import Any

import structlog
from pydantic import BaseModel


class EventType(str, Enum):
    """Cortex log event types."""

    # State events
    STATE_TRANSITION = "state_transition"
    STATE_UPDATE = "state_update"

    # Feature events
    FEATURE_VECTOR = "feature_vector"
    SIGNAL_QUALITY = "signal_quality"

    # Intervention events
    INTERVENTION_TRIGGERED = "intervention_triggered"
    INTERVENTION_APPLIED = "intervention_applied"
    INTERVENTION_RESTORED = "intervention_restored"
    INTERVENTION_DISMISSED = "intervention_dismissed"
    INTERVENTION_OUTCOME = "intervention_outcome"

    # LLM events
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    LLM_ERROR = "llm_error"
    LLM_FALLBACK = "llm_fallback"

    # Capture events
    CAPTURE_STARTED = "capture_started"
    CAPTURE_STOPPED = "capture_stopped"
    FACE_LOST = "face_lost"
    FACE_REACQUIRED = "face_reacquired"

    # System events
    SERVICE_STARTED = "service_started"
    SERVICE_STOPPED = "service_stopped"
    ERROR = "error"
    WARNING = "warning"

    # WebSocket events
    WS_CLIENT_CONNECTED = "ws_client_connected"
    WS_CLIENT_DISCONNECTED = "ws_client_disconnected"
    WS_MESSAGE_SENT = "ws_message_sent"
    WS_MESSAGE_RECEIVED = "ws_message_received"


class StateTransitionEvent(BaseModel):
    """State transition event data."""

    event_type: str = EventType.STATE_TRANSITION.value
    timestamp: float
    from_state: str
    to_state: str
    confidence: float
    reasons: list[str]
    dwell_seconds: float


class InterventionTriggeredEvent(BaseModel):
    """Intervention trigger event data."""

    event_type: str = EventType.INTERVENTION_TRIGGERED.value
    timestamp: float
    intervention_id: str
    level: str
    trigger_reasons: list[str]
    state_confidence: float
    workspace_complexity: float


class FeatureVectorEvent(BaseModel):
    """Feature vector logging event."""

    event_type: str = EventType.FEATURE_VECTOR.value
    timestamp: float
    hr: float | None
    hrv_rmssd: float | None
    blink_rate: float | None
    slump_score: float | None
    mouse_velocity_mean: float
    window_switch_rate: float


class ErrorEvent(BaseModel):
    """Error event data."""

    event_type: str = EventType.ERROR.value
    timestamp: float
    service: str
    message: str
    traceback: str | None = None
    context: dict[str, Any] | None = None


def add_timestamp(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Add ISO timestamp to log events."""
    event_dict["timestamp"] = datetime.utcnow().isoformat() + "Z"
    return event_dict


def add_service_context(
    logger: structlog.types.WrappedLogger,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Add service context to log events."""
    if "service" not in event_dict:
        event_dict["service"] = "cortex"
    return event_dict


def configure_logging(
    level: str = "INFO",
    json_format: bool = True,
    include_timestamp: bool = True,
) -> None:
    """
    Configure structlog for Cortex.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        json_format: Whether to output JSON (True) or console format (False)
        include_timestamp: Whether to include timestamps
    """
    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper()),
    )

    # Build processor chain
    processors: list[structlog.types.Processor] = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if include_timestamp:
        processors.append(add_timestamp)

    processors.append(add_service_context)

    if json_format:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Get a structured logger instance.

    Args:
        name: Optional logger name (defaults to "cortex")

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name or "cortex")


# Convenience logging functions
_default_logger: structlog.stdlib.BoundLogger | None = None


def _get_default_logger() -> structlog.stdlib.BoundLogger:
    """Get or create the default logger."""
    global _default_logger
    if _default_logger is None:
        _default_logger = get_logger()
    return _default_logger


def log_state_transition(
    from_state: str,
    to_state: str,
    confidence: float,
    reasons: list[str],
    dwell_seconds: float,
) -> None:
    """Log a state transition event."""
    logger = _get_default_logger()
    logger.info(
        EventType.STATE_TRANSITION.value,
        from_state=from_state,
        to_state=to_state,
        confidence=confidence,
        reasons=reasons,
        dwell_seconds=dwell_seconds,
    )


def log_intervention_triggered(
    intervention_id: str,
    level: str,
    trigger_reasons: list[str],
    state_confidence: float,
    workspace_complexity: float,
) -> None:
    """Log an intervention trigger event."""
    logger = _get_default_logger()
    logger.info(
        EventType.INTERVENTION_TRIGGERED.value,
        intervention_id=intervention_id,
        level=level,
        trigger_reasons=trigger_reasons,
        state_confidence=state_confidence,
        workspace_complexity=workspace_complexity,
    )


def log_intervention_outcome(
    intervention_id: str,
    user_action: str,
    duration_seconds: float,
    recovery_detected: bool,
) -> None:
    """Log an intervention outcome event."""
    logger = _get_default_logger()
    logger.info(
        EventType.INTERVENTION_OUTCOME.value,
        intervention_id=intervention_id,
        user_action=user_action,
        duration_seconds=duration_seconds,
        recovery_detected=recovery_detected,
    )


def log_llm_request(
    prompt_hash: str,
    token_count: int,
    model: str,
) -> None:
    """Log an LLM request event."""
    logger = _get_default_logger()
    logger.info(
        EventType.LLM_REQUEST.value,
        prompt_hash=prompt_hash,
        token_count=token_count,
        model=model,
    )


def log_llm_response(
    prompt_hash: str,
    response_hash: str,
    latency_ms: float,
    token_count: int,
    success: bool,
) -> None:
    """Log an LLM response event."""
    logger = _get_default_logger()
    logger.info(
        EventType.LLM_RESPONSE.value,
        prompt_hash=prompt_hash,
        response_hash=response_hash,
        latency_ms=latency_ms,
        token_count=token_count,
        success=success,
    )


def log_error(
    service: str,
    message: str,
    traceback: str | None = None,
    **context: Any,
) -> None:
    """Log an error event."""
    logger = _get_default_logger()
    logger.error(
        EventType.ERROR.value,
        service=service,
        error_message=message,
        traceback=traceback,
        **context,
    )


def log_warning(
    service: str,
    message: str,
    **context: Any,
) -> None:
    """Log a warning event."""
    logger = _get_default_logger()
    logger.warning(
        EventType.WARNING.value,
        service=service,
        warning_message=message,
        **context,
    )


def log_service_started(service: str, **config: Any) -> None:
    """Log a service started event."""
    logger = _get_default_logger()
    logger.info(
        EventType.SERVICE_STARTED.value,
        service=service,
        **config,
    )


def log_service_stopped(service: str, reason: str | None = None) -> None:
    """Log a service stopped event."""
    logger = _get_default_logger()
    logger.info(
        EventType.SERVICE_STOPPED.value,
        service=service,
        reason=reason,
    )
