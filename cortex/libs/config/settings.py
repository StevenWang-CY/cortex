"""
Cortex Configuration Settings

Global configuration using Pydantic BaseSettings with YAML defaults.
Environment variables override YAML values.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def _is_bundled() -> bool:
    """True when running inside a PyInstaller ``.app`` bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _bundled_env_files() -> tuple[str, ...]:
    """Resolve .env search paths for bundled vs. dev mode."""
    if _is_bundled():
        app_support = Path.home() / "Library" / "Application Support" / "Cortex"
        meipass = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        return (
            str(app_support / ".env"),  # User overrides (highest priority)
            str(meipass / ".env"),       # Bundled defaults
        )
    return (".env", ".env.local")


def _bundled_storage_path() -> str:
    """Default storage path: App Support in bundled mode, ./storage in dev."""
    if _is_bundled():
        return str(Path.home() / "Library" / "Application Support" / "Cortex" / "Data")
    return "./storage"

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


LogicalModelId = Literal[
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]
"""Cortex-canonical logical model IDs.

Resolved to provider-specific identifiers (Bedrock inference profiles,
Vertex revisions, or direct Anthropic API names) by
``cortex.libs.llm.anthropic_client.resolve_anthropic_model_id``.
"""


class BedrockConfig(BaseModel):
    """AWS Bedrock transport configuration."""

    aws_region: str = "us-east-2"
    keychain_service: str = "cortex.bedrock"
    keychain_account: str = "bearer_token"


class LLMConfig(BaseModel):
    """LLM engine configuration — Anthropic SDK over Bedrock (primary).

    Direct ``AsyncAnthropic`` and ``AsyncAnthropicVertex`` remain as escape
    hatches behind the same interface for capacity / residency failover.
    All Azure/Qwen/Ollama branches were removed in v0.2.0.

    Backwards compatibility: ``extra="ignore"`` drops legacy env vars
    (``CORTEX_LLM__MODE``, ``CORTEX_LLM__AZURE__*``, ``CORTEX_LLM__REMOTE__*``,
    ``CORTEX_LLM__LOCAL__*``, ``CORTEX_LLM__MODEL_NAME``) without raising.
    Users still on a 0.1.x ``.env`` can rerun ``cortex-dev``/the desktop
    BYOK step to migrate cleanly.
    """

    model_config = ConfigDict(protected_namespaces=(), extra="ignore")

    provider: Literal["bedrock", "vertex", "direct"] = "bedrock"
    bedrock: BedrockConfig = Field(default_factory=BedrockConfig)
    use_keychain: bool = True

    # Three logical tiers — actual model selection per template lives in
    # ``cortex/services/llm_engine/anthropic_planner.py:_TEMPLATE_TIER``.
    model_default: LogicalModelId = "claude-sonnet-4-6"
    model_fast: LogicalModelId = "claude-haiku-4-5"
    model_deep: LogicalModelId = "claude-opus-4-7"

    max_tokens: int = 1024
    temperature: float = 0.3
    # Bedrock cold starts can take 5-10s; Opus calls can exceed 20s.
    timeout_seconds: float = 30.0
    cache_ttl_seconds: int = 300
    # Per-template overrides keyed by template_name (e.g. {"debug_error_summary": "deep"}).
    template_tier_overrides: dict[str, Literal["fast", "default", "deep"]] = Field(
        default_factory=dict,
    )
    # If Bedrock fails after retries, the only remaining option is the
    # deterministic rule-based plan. Direct Anthropic API access is reserved
    # for environments where ``ANTHROPIC_API_KEY`` is explicitly provisioned.
    fallback_mode: Literal["direct_anthropic", "rule_based"] = "rule_based"
    # Bound in-flight requests (Bedrock account-level concurrency limits apply).
    max_concurrent_requests: int = 3

    @field_validator("fallback_mode", mode="before")
    @classmethod
    def _coerce_legacy_fallback(cls, v: object) -> object:
        # Legacy values from 0.1.x .env files (local_ollama / remote / azure)
        # are silently mapped to ``rule_based`` so the daemon doesn't crash
        # on first launch after upgrade.
        if isinstance(v, str) and v not in {"direct_anthropic", "rule_based"}:
            return "rule_based"
        return v

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_legacy_provider(cls, v: object) -> object:
        if isinstance(v, str) and v not in {"bedrock", "vertex", "direct"}:
            # Legacy values: azure, local, remote, openai_compat → bedrock
            return "bedrock"
        return v
    # Circuit-breaker: open after this many consecutive failures, in this window.
    circuit_failure_threshold: int = 5
    circuit_window_seconds: float = 60.0
    circuit_open_seconds: float = 30.0


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
    hyper_dwell_seconds: int = 30
    hypo_dwell_seconds: int = 60
    flow_dwell_seconds: int = 120
    ema_alpha: float = 0.3
    ml_enabled: bool = False
    ml_min_labeled_episodes: int = 30
    ml_alpha_max: float = 0.7
    ml_alpha_full_at_episodes: int = 150
    weights: StateWeights = Field(default_factory=StateWeights)


class InterventionConfig(BaseModel):
    """Intervention engine configuration."""

    overlay_threshold: float = 0.70
    simplified_threshold: float = 0.85
    guided_threshold: float = 0.95
    complexity_threshold: float = 0.6
    cooldown_seconds: int = 60
    # NOTE: `hyper_dwell_seconds` lives on StateConfig (default 30) — the
    # duplicate field that lived here was removed in v0.2.0 (C.5). The
    # trigger policy reads StateConfig.hyper_dwell_seconds directly.
    quiet_mode_minutes: int = 30
    max_dismissals: int = 3
    dismissal_window_minutes: int = 5
    timeout_minutes: int = 5
    dismissal_threshold_bump: float = 0.05
    dismissal_decay_hours: int = 1
    receptivity_enforced: bool = True
    receptivity_typing_burst_seconds: float = 10.0
    receptivity_block_fullscreen: bool = True
    receptivity_block_if_mic_active: bool = True
    receptivity_work_hours_start: int = 7
    receptivity_work_hours_end: int = 22
    adaptive_threshold_enabled: bool = True
    adaptive_threshold_min: float = 0.75
    adaptive_threshold_max: float = 0.95
    dismissal_model_enabled: bool = True
    dismissal_model_threshold: float = 0.6


class HandoverConfig(BaseModel):
    """Handover / shutdown detector configuration."""

    late_hour: int = 23
    posture_slump_threshold: float = 0.6
    hrv_drop_threshold: float = 0.7
    error_rate_threshold: int = 3


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
    backend: Literal["pos", "chrom", "green", "tscan"] = "pos"
    model_path: str = "cortex/models/tscan.onnx"
    bandpass_low: float = 0.7
    bandpass_high: float = 3.5
    bandpass_order: int = 4
    welch_resolution: float = 0.1
    nsqi_threshold: float = 0.293
    min_cardiac_snr_db: float = 2.0
    min_resp_snr_db: float = 1.5
    max_face_loss_ratio: float = 0.20
    max_head_jitter_deg: float = 7.5
    hrv_min_window_seconds: int = 60
    hrv_min_valid_ibi: int = 30


class BlinkSignalConfig(BaseModel):
    """Blink detection configuration."""

    ear_threshold: float = 0.21
    ear_recovery: float = 0.25
    min_frames: int = 3
    perclos_threshold: float = 0.2
    personalize_ear_percentile: float = 0.15


class PostureSignalConfig(BaseModel):
    """Posture detection configuration."""

    shoulder_drop_threshold: float = 0.15
    forward_lean_threshold: float = 20.0


class SignalConfig(BaseModel):
    """Signal processing configuration."""

    rppg: RPPGSignalConfig = Field(default_factory=RPPGSignalConfig)
    blink: BlinkSignalConfig = Field(default_factory=BlinkSignalConfig)
    posture: PostureSignalConfig = Field(default_factory=PostureSignalConfig)


class AMIPConfig(BaseModel):
    """Adaptive microrandomized intervention policy configuration."""

    enabled: bool = True
    tau0: float = 1.0
    tau_min: float = 0.1
    epsilon_explore: float = 0.05
    epsilon_explore_after_500: float = 0.01
    safety_floor_stress_ratio: float = 1.0
    reward_window_seconds: int = 300


class CausalReportConfig(BaseModel):
    """Nightly causal reporting configuration."""

    enabled: bool = True
    bootstrap_samples: int = 300
    nightly_hour_local: int = 2


class EvalConfig(BaseModel):
    """Evaluation and policy-learning configuration."""

    policy: Literal["amip", "greedy", "uniform"] = "amip"
    amip: AMIPConfig = Field(default_factory=AMIPConfig)
    causal_report: CausalReportConfig = Field(default_factory=CausalReportConfig)


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

    path: str = Field(default_factory=_bundled_storage_path)
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
        env_file=_bundled_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Sub-configurations
    llm: LLMConfig = Field(default_factory=LLMConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    intervention: InterventionConfig = Field(default_factory=InterventionConfig)
    handover: HandoverConfig = Field(default_factory=HandoverConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    landmarks: LandmarksConfig = Field(default_factory=LandmarksConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    eval: EvalConfig = Field(default_factory=EvalConfig)

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
    # In bundled mode, check _MEIPASS first
    if _is_bundled():
        bundled = Path(sys._MEIPASS) / "cortex" / "libs" / "config" / "defaults.yaml"  # type: ignore[attr-defined]
        if bundled.exists():
            with open(bundled) as f:
                return yaml.safe_load(f) or {}
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
    then environment variables override any values. When ``use_keychain`` is
    enabled the AWS Bedrock bearer token is sourced from the macOS Keychain
    (service ``cortex.bedrock`` / account ``bearer_token``) at startup and
    exported into ``AWS_BEARER_TOKEN_BEDROCK`` for the Anthropic SDK to pick
    up. The token itself is never persisted into ``CortexConfig`` or any
    file on disk.

    Returns:
        CortexConfig: The global configuration instance.
    """
    config = CortexConfig()

    if _is_bundled():
        storage_path = Path(config.storage.path).expanduser()
        if not storage_path.is_absolute():
            config.storage.path = _bundled_storage_path()
        Path(config.storage.path).mkdir(parents=True, exist_ok=True)

    # BYOK: surface the Bedrock bearer token via env so the Anthropic SDK
    # (AsyncAnthropicBedrock) and any boto3 fallbacks can find it. We never
    # store the token on the config object — keychain is the source of truth.
    if (
        config.llm.provider == "bedrock"
        and config.llm.use_keychain
        and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    ):
        try:
            import keyring
            token = keyring.get_password(
                config.llm.bedrock.keychain_service,
                config.llm.bedrock.keychain_account,
            )
            if token:
                os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token
        except Exception:
            pass  # keyring not available — daemon will degrade to fallback

    # Mirror provider + region into the env the SDK reads, so subprocess
    # workers and the planner module see the same configuration.
    os.environ.setdefault("ANTHROPIC_PROVIDER", config.llm.provider)
    if config.llm.provider == "bedrock":
        os.environ.setdefault("AWS_REGION", config.llm.bedrock.aws_region)

    return config


def reset_config() -> None:
    """Reset the cached configuration (useful for testing)."""
    get_config.cache_clear()
