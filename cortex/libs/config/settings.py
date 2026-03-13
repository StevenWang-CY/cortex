"""
Cortex Configuration Settings

Global configuration using Pydantic BaseSettings with YAML defaults.
Environment variables override YAML values.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# =============================================================================
# Sub-configuration Models
# =============================================================================


class RedisConfig(BaseModel):
    """Redis connection configuration."""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    enabled: bool = True
    key_prefix: str = "cortex"


class LLMRemoteConfig(BaseModel):
    """Remote LLM server configuration (gwhiz1)."""

    host: str = "gwhiz1.cis.upenn.edu"
    port: int = 8800
    ssh_tunnel: bool = True
    ssh_user: str = "wangcy07"


class LLMLocalConfig(BaseModel):
    """Local LLM configuration (Ollama)."""

    host: str = "localhost"
    port: int = 11434
    model: str = "llama3.1:8b"


class LLMAzureConfig(BaseModel):
    """Azure OpenAI configuration."""

    endpoint: str = ""
    api_key: str = ""
    api_version: str = "2025-01-01-preview"
    deployment_name: str = ""
    reasoning_deployment_name: str = ""
    max_completion_tokens: int = 1024
    use_keychain: bool = True
    keychain_service: str = "cortex.azure_openai"
    keychain_account: str = "default"


class LLMConfig(BaseModel):
    """LLM engine configuration."""

    model_config = ConfigDict(protected_namespaces=())

    mode: Literal["remote", "local", "azure", "rule_based", "openai_compat"] = "azure"
    remote: LLMRemoteConfig = Field(default_factory=LLMRemoteConfig)
    local: LLMLocalConfig = Field(default_factory=LLMLocalConfig)
    azure: LLMAzureConfig = Field(default_factory=LLMAzureConfig)
    model_name: str = "qwen3-8b"
    max_tokens: int = 1024
    temperature: float = 0.3
    timeout_seconds: float = 10.0
    fallback_mode: Literal["local_ollama", "rule_based"] = "rule_based"


class CaptureConfig(BaseModel):
    """Webcam capture configuration."""

    device_id: int | None = None
    fps: int = 30
    width: int = 640
    height: int = 480
    min_brightness: int = 50
    max_jitter_px: float = 5.0
    face_lost_tolerance_frames: int = 5


class StateWeights(BaseModel):
    """Weights for overwhelm score components."""

    pulse_elevation: float = 0.20
    hrv_drop: float = 0.15
    blink_suppression: float = 0.12
    posture_collapse: float = 0.08
    mouse_thrashing: float = 0.15
    window_switching: float = 0.15
    workspace_complexity: float = 0.15


class StateConfig(BaseModel):
    """State engine configuration."""

    entry_threshold: float = 0.85
    exit_threshold: float = 0.70
    hyper_dwell_seconds: int = 8
    hypo_dwell_seconds: int = 15
    flow_dwell_seconds: int = 15
    ema_alpha: float = 0.3
    weights: StateWeights = Field(default_factory=StateWeights)


class InterventionConfig(BaseModel):
    """Intervention engine configuration."""

    overlay_threshold: float = 0.70
    simplified_threshold: float = 0.85
    guided_threshold: float = 0.95
    complexity_threshold: float = 0.6
    cooldown_seconds: int = 60
    quiet_mode_minutes: int = 30
    max_dismissals: int = 3
    dismissal_window_minutes: int = 5
    timeout_minutes: int = 5
    dismissal_threshold_bump: float = 0.05
    dismissal_decay_hours: int = 1


class APIConfig(BaseModel):
    """API gateway configuration."""

    host: str = "127.0.0.1"
    port: int = 9472
    ws_port: int = 9473


class TelemetryConfig(BaseModel):
    """Telemetry engine configuration."""

    mouse_sample_hz: int = 60
    downsample_hz: int = 10
    window_seconds: int = 15


class RPPGSignalConfig(BaseModel):
    """rPPG signal processing configuration."""

    window_seconds: int = 10
    stride_seconds: int = 1
    bandpass_low: float = 0.7
    bandpass_high: float = 3.5
    bandpass_order: int = 4
    welch_resolution: float = 0.1


class BlinkSignalConfig(BaseModel):
    """Blink detection configuration."""

    ear_threshold: float = 0.21
    ear_recovery: float = 0.25
    min_frames: int = 3


class PostureSignalConfig(BaseModel):
    """Posture detection configuration."""

    shoulder_drop_threshold: float = 0.15
    forward_lean_threshold: float = 20.0


class SignalConfig(BaseModel):
    """Signal processing configuration."""

    rppg: RPPGSignalConfig = Field(default_factory=RPPGSignalConfig)
    blink: BlinkSignalConfig = Field(default_factory=BlinkSignalConfig)
    posture: PostureSignalConfig = Field(default_factory=PostureSignalConfig)


class LandmarksConfig(BaseModel):
    """MediaPipe landmark indices for ROI extraction."""

    forehead: list[int] = Field(default=[10, 67, 69, 104, 108, 151, 299, 337, 338])
    left_cheek: list[int] = Field(default=[50, 101, 116, 117, 118, 119, 120, 121])
    right_cheek: list[int] = Field(default=[280, 330, 345, 346, 347, 348, 349, 350])
    left_eye: list[int] = Field(default=[33, 160, 158, 133, 153, 144])
    right_eye: list[int] = Field(default=[362, 385, 387, 263, 373, 380])
    shoulders: list[int] = Field(default=[11, 12])


class StorageConfig(BaseModel):
    """Storage configuration."""

    path: str = "./storage"
    session_retention_days: int = 7
    feature_retention_days: int = 7
    error_retention_days: int = 90


class DebugConfig(BaseModel):
    """Debug configuration."""

    enabled: bool = False
    capture: bool = False
    rppg: bool = False
    state: bool = False
    llm: bool = False


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    format: str = "json"
    include_timestamp: bool = True


# =============================================================================
# Main Configuration
# =============================================================================


class CortexConfig(BaseSettings):
    """
    Main Cortex configuration.

    Loads configuration from:
    1. defaults.yaml (lowest priority)
    2. Environment variables (highest priority)

    Environment variables use CORTEX_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="CORTEX_",
        env_nested_delimiter="__",
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Sub-configurations
    llm: LLMConfig = Field(default_factory=LLMConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    intervention: InterventionConfig = Field(default_factory=InterventionConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    landmarks: LandmarksConfig = Field(default_factory=LandmarksConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlDefaultsSource(settings_cls),
            file_secret_settings,
        )


class _YamlDefaultsSource(PydanticBaseSettingsSource):
    """Expose defaults.yaml as a BaseSettings source with lower precedence than env."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return load_yaml_defaults()


def load_yaml_defaults() -> dict:
    """Load default configuration from YAML file."""
    defaults_path = Path(__file__).parent / "defaults.yaml"
    if defaults_path.exists():
        with open(defaults_path) as f:
            return yaml.safe_load(f) or {}
    return {}


@lru_cache
def get_config() -> CortexConfig:
    """
    Get the global Cortex configuration instance.

    Configuration is loaded once and cached. YAML defaults are loaded first,
    then environment variables override any values.

    Returns:
        CortexConfig: The global configuration instance.
    """
    return CortexConfig()


def reset_config() -> None:
    """Reset the cached configuration (useful for testing)."""
    get_config.cache_clear()
