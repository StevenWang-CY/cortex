"""Configuration management."""

from __future__ import annotations

from importlib import import_module
from typing import Any

# Ports are imported by short-lived native messaging processes. Keep the
# package initializer lazy so reading those constants does not initialize the
# complete pydantic-settings graph.
_EXPORT_MODULES: dict[str, str] = {
    "APIConfig": "cortex.libs.config.settings",
    "CaptureConfig": "cortex.libs.config.settings",
    "CortexConfig": "cortex.libs.config.settings",
    "DebugConfig": "cortex.libs.config.settings",
    "InterventionConfig": "cortex.libs.config.settings",
    "LandmarksConfig": "cortex.libs.config.settings",
    "LLMConfig": "cortex.libs.config.settings",
    "LLMPrivacyConfig": "cortex.libs.config.settings",
    "LoggingConfig": "cortex.libs.config.settings",
    "RedisConfig": "cortex.libs.config.settings",
    "SignalConfig": "cortex.libs.config.settings",
    "StateConfig": "cortex.libs.config.settings",
    "StorageConfig": "cortex.libs.config.settings",
    "TelemetryConfig": "cortex.libs.config.settings",
    "get_config": "cortex.libs.config.settings",
    "reset_config": "cortex.libs.config.settings",
}

__all__ = [
    "CortexConfig",
    "LLMConfig",
    "LLMPrivacyConfig",
    "CaptureConfig",
    "StateConfig",
    "InterventionConfig",
    "APIConfig",
    "TelemetryConfig",
    "SignalConfig",
    "LandmarksConfig",
    "StorageConfig",
    "DebugConfig",
    "LoggingConfig",
    "RedisConfig",
    "get_config",
    "reset_config",
]


def __getattr__(name: str) -> Any:
    """Load a public configuration type only when first requested."""

    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy public attributes to IDEs and introspection."""

    return sorted({*globals(), *__all__})
