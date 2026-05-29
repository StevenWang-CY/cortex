"""
Cortex Configuration Settings

Global configuration using Pydantic BaseSettings with YAML defaults.
Environment variables override YAML values.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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

from cortex.libs.config.ports import (
    HTTP_API_PORT,
    LAUNCHER_AGENT_PORT,
    WEBSOCKET_PORT,
)


class StorageConfigError(RuntimeError):
    """Raised when the configured storage path cannot be created.

    Carries the offending path and the original OS errno so callers can
    surface a clean user-facing message ("Cortex can't write to ...; pick
    a different location") without swallowing the underlying ``OSError``.
    """

    def __init__(self, path: str, original: OSError) -> None:
        self.path = path
        self.original = original
        self.errno = getattr(original, "errno", None)
        super().__init__(
            f"Cannot create storage directory {path!r}: {original} (errno={self.errno})"
        )


# I2: Bedrock bearer token cache. Populated lazily by
# ``get_bedrock_token()`` from the macOS Keychain (BYOK). The token is
# NEVER written into ``os.environ`` — child processes inherit env, so a
# debugger / crash-dump attached to any descendant could read it. The
# Anthropic SDK reads ``AWS_BEARER_TOKEN_BEDROCK`` at construction time;
# call ``bedrock_token_env_scope()`` around the SDK constructor instead.
_bedrock_token_cache: str | None = None


def get_bedrock_token(config: CortexConfig | None = None) -> str | None:
    """Return the Bedrock bearer token, cached after first successful read.

    Resolution order:
        1. If ``AWS_BEARER_TOKEN_BEDROCK`` is already set in the env
           (user-supplied), return that value unchanged.
        2. Otherwise consult the macOS Keychain via ``get_password_safe``
           with a 5 s ceiling so a wedged TCC prompt cannot pin the
           daemon. The retrieved value is cached for the process lifetime.

    Returns ``None`` when no token is available (the planner will then
    fall back to the rule-based intervention path).
    """
    global _bedrock_token_cache
    env_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if env_token:
        return env_token
    if _bedrock_token_cache is not None:
        return _bedrock_token_cache
    cfg = config or get_config()
    if cfg.llm.provider != "bedrock" or not cfg.llm.use_keychain:
        return None
    try:
        from cortex.libs.utils.secrets import get_password_safe
        token = get_password_safe(
            cfg.llm.bedrock.keychain_service,
            cfg.llm.bedrock.keychain_account,
        )
    except (ImportError, OSError, RuntimeError):
        return None
    if token:
        _bedrock_token_cache = token
    return token


def clear_bedrock_token_cache() -> None:
    """Reset the in-memory Bedrock token cache.

    Must be called whenever the keychain entry is rewritten (e.g. after a
    BYOK onboarding step overwrites the ``cortex.bedrock`` / ``bearer_token``
    entry). Without calling this the old token lingers for the lifetime of the
    process and the freshly-written credential is never used.

    Also useful in tests: call between subtests to force a fresh keychain read.
    """
    global _bedrock_token_cache
    _bedrock_token_cache = None


# Backward-compatible private alias so any call sites that pre-date the
# public rename continue to work without a hard error.
_clear_bedrock_token_cache = clear_bedrock_token_cache


@contextmanager
def bedrock_token_env_scope(
    config: CortexConfig | None = None,
) -> Iterator[str | None]:
    """Context-manager: temporarily expose the Bedrock token via env.

    The Anthropic Bedrock SDK reads ``AWS_BEARER_TOKEN_BEDROCK`` at
    construction time only. This context manager scopes that env mutation
    so the variable is removed (or restored to its prior value) on exit —
    no child process spawned after the ``with`` block can inherit the
    token.

    Yields the resolved token (or ``None`` if no token is available so
    callers can short-circuit to the fallback path).
    """
    token = get_bedrock_token(config)
    if not token:
        yield None
        return
    prior = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    try:
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = token
        yield token
    finally:
        if prior is None:
            os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
        else:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = prior

# Bound at module-import time so Pydantic field defaults read from the
# same constants without re-importing on every model construction.
_PORTS: dict[str, int] = {
    "HTTP_API_PORT": HTTP_API_PORT,
    "LAUNCHER_AGENT_PORT": LAUNCHER_AGENT_PORT,
    "WEBSOCKET_PORT": WEBSOCKET_PORT,
}


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
    """Redis connection configuration.

    C7 (audit): ``enabled`` defaults to ``False``. The shipped desktop
    .app has no Redis service to manage, so the DMG-default deployment
    runs on the persistent :class:`InMemoryStore` fallback selected by
    :func:`cortex.libs.store.make_default_store`. Operators who run a
    Redis instance flip this to ``True`` via
    ``CORTEX_REDIS__ENABLED=true`` (or ``defaults.yaml``).
    """

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    enabled: bool = False
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

    # F20: per-day USD spend rails. ``cost_warn_usd`` emits a single
    # ``llm.budget.warn`` log line per local day; ``daily_cost_budget_usd``
    # is the hard kill — the planner serves the deterministic fallback
    # plan and stamps ``metadata["budget_killed"] = True`` for the rest
    # of the day. Defaults are deliberately tight (≈$20/day) because an
    # oscillating state machine can issue 60+ planner calls per hour.
    cost_warn_usd: float = 5.0
    daily_cost_budget_usd: float = 20.0


class CaptureConfig(BaseModel):
    """Webcam capture configuration."""

    device_id: int | None = None
    fps: int = 30
    width: int = 640
    height: int = 480
    min_brightness: int = 50
    max_jitter_px: float = 5.0
    face_lost_tolerance_frames: int = 5
    # audit Phase-I: how often to run MediaPipe FaceLandmarker relative
    # to the capture cadence. ``1`` = every frame, ``2`` = every other
    # frame (15 Hz at 30 Hz capture). C5 (audit): default reverted to
    # ``1`` (every frame) — SENSING's apnea / blink-suppression timing
    # needs the full-rate mesh so the per-frame timestamp threading is
    # exact; the half-rate path introduced a sub-Nyquist gap on the
    # respiration estimator that the apnea sustain timer depends on.
    face_mesh_subsample_n: int = 1


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
    # B.4 fix: these are *separate* semantic quantities from the trigger
    # cooldown. ``cooldown_seconds`` (60s) is the minimum spacing between
    # successive trigger evaluations; the dismiss-cooldowns below are the
    # grace windows during which a freshly-dismissed intervention or URL
    # must not re-fire. Conflating them (W-16's original implementation
    # broadcast ``cooldown_seconds * 1000`` for both) produced a 30×
    # shrink relative to the browser-extension defaults (30 min / 10 min).
    intervention_dismiss_cooldown_ms: int = 30 * 60 * 1000
    url_dismiss_cooldown_ms: int = 10 * 60 * 1000
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
    # F48: breathing pacer cadence (inhale, hold, exhale) in seconds.
    # Default 4-7-8 (Dr. Andrew Weil's relaxation pattern); the overlay's
    # ``BreathingPacer`` reads this so users with different rhythm
    # preferences can override without patching the source.
    breathing_pattern: tuple[int, int, int] = (4, 7, 8)

    # F25 (audit): hysteresis against cooldown/dwell oscillation. The
    # ``cooldown_seconds`` + ``hyper_dwell_seconds`` pair admits a 90 s
    # oscillation pattern (HYPER 30 s → trigger → FLOW 25 s → HYPER 30 s
    # → trigger again) that fires on every cycle. Two additional gates
    # bound this independently of cost (F20 bounds cost; F25 bounds
    # user-visible spam):
    #
    # 1. ``max_interventions_per_hour`` — sliding-window cap on triggers
    #    in the trailing 60 minutes. Default 6/hr (one every ten
    #    minutes); a session sustaining six interventions an hour is
    #    almost certainly oscillating, not in genuine sustained
    #    overwhelm.
    # 2. ``oscillation_window_seconds`` + ``oscillation_max_flips`` +
    #    ``oscillation_dwell_multiplier`` — when the state has entered
    #    HYPER more than ``oscillation_max_flips`` times within
    #    ``oscillation_window_seconds``, multiply the required dwell
    #    time so genuine sustained overwhelm still passes but jittery
    #    flickers don't.
    max_interventions_per_hour: int = 6
    oscillation_window_seconds: float = 600.0
    oscillation_max_flips: int = 6
    oscillation_dwell_multiplier: float = 2.0

    # P0 §3.5: HYPO / RECOVERY intervention catalog. Opt-in for the
    # first release — the new arms (re-engagement nudge, recovery
    # reinforcement) are only evaluated when this flag is True.
    # AMIP starts cold for these arms, so leaving the default at False
    # avoids any early all-in on a not-yet-trained reward signal.
    enable_hypo_recovery_interventions: bool = False

    # P0 §3.7: biology-driven break feature flag. When True, a
    # ``StressIntegralTracker.should_break`` False→True transition
    # emits ``BREAK_RECOMMENDATION`` and the planner promotes
    # ``take_biology_break`` to the primary action. Disabled in
    # contexts where the biology-break overlay would interfere
    # (e.g. CI smoke tests). Maps to env var
    # ``CORTEX_INTERVENTION__ENABLE_BIOLOGY_BREAK``.
    enable_biology_break: bool = True

    # P0 §3.7 risk mitigation (spec line 643): the spec calls for
    # audio to default off when ``mic_active`` was detected in the
    # last 5 minutes (user is on a call). The runtime daemon honours
    # this window by tracking the most recent mic_active timestamp;
    # the controller flips ``audio_cue=False`` for the duration.
    biology_break_audio_mute_after_mic_seconds: float = 300.0

    # P0 §3.10: auto-armed distraction blocking on HYPER. Defaults OFF —
    # the principle of least surprise wins for any autonomous action
    # that could surface a full-screen interstitial. Users opt in from
    # Settings → Focus protection. Maps to env var
    # ``CORTEX_INTERVENTION__ENABLE_AUTO_DISTRACTION_BLOCK``.
    enable_auto_distraction_block: bool = False

    # P0 §3.10: confidence + dwell gates on the auto-arm path. The
    # spec calls for ``confidence > 0.85 AND dwell > 30s`` so the
    # F25 sliding-window oscillation pattern is bounded. Both knobs
    # are exposed so a forensic user can dial in conservative gates
    # without patching code.
    auto_distraction_block_confidence: float = 0.85
    auto_distraction_block_dwell_seconds: float = 30.0
    # Auto-armed sessions exit after sustained non-HYPER (FLOW or
    # RECOVERY) for this many seconds (5 min = 300 s). The browser
    # extension's manual focus session has no auto-exit; the daemon
    # owns this gate so the user is never silently kept in focus
    # mode after they've genuinely recovered.
    auto_distraction_block_exit_seconds: float = 300.0
    # Default session duration the daemon proposes when arming. 20 min
    # matches the typical Pomodoro upper bound + the spec example.
    auto_distraction_block_session_minutes: int = 20
    # Default preset for the merged blocklist. Browser extension owns
    # the per-preset domain map. ``custom`` reads ``custom_domains``.
    auto_distraction_block_preset: Literal[
        "developer", "student", "writer", "custom",
    ] = "developer"
    # User-editable extra domains layered on top of the preset (or the
    # exclusive set when ``preset == "custom"``).
    auto_distraction_block_custom_domains: list[str] = Field(
        default_factory=list,
    )

    # P0 §3.12: OS-level notification routing. When True and the
    # desktop dashboard is NOT the foreground window at the moment an
    # ``INTERVENTION_TRIGGER`` is broadcast, the daemon also dispatches
    # a UNUserNotification (macOS), Chrome action badge bump, and VS
    # Code status-bar pulse so the user actually sees the cue from
    # other Spaces / fullscreen apps. Maps to env var
    # ``CORTEX_INTERVENTION__ENABLE_OS_NOTIFICATIONS``.
    enable_os_notifications: bool = True


class HandoverConfig(BaseModel):
    """Handover / shutdown detector configuration."""

    late_hour: int = 23
    posture_slump_threshold: float = 0.6
    hrv_drop_threshold: float = 0.7
    error_rate_threshold: int = 3


class APIConfig(BaseModel):
    """API gateway configuration.

    Port defaults sourced from :mod:`cortex.libs.config.ports` so a
    future port migration only edits one file.

    Phase-4b TASK L: ``cors_allow_origins`` lifts the hardcoded
    localhost allowlist out of ``app.py`` into config so deployments
    that proxy through a different origin (e.g. a Tauri shell at a
    custom scheme) can extend it via env var without patching code.
    """

    host: str = "127.0.0.1"
    port: int = Field(default_factory=lambda: _PORTS["HTTP_API_PORT"])
    ws_port: int = Field(default_factory=lambda: _PORTS["WEBSOCKET_PORT"])
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost",
            "http://127.0.0.1",
        ],
        description=(
            "Static CORS allowlist for the HTTP API. The dynamic "
            "extension/webview regex is still applied in app.py via "
            "``allow_origin_regex``; this list is the simple-match "
            "fallback for browser tabs that aren't extensions."
        ),
    )


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
    # F36: hard ceiling on the cumulative size of ``storage/sessions/*.json``.
    # When writing a new session report would push the total over budget,
    # oldest sessions (lowest mtime) are evicted first until the total
    # drops back under the cap. Default 500 MB roughly corresponds to
    # 6 months of typical use (1-3 MB per session × ~3 sessions/day).
    # Set to 0 to evict every existing session before each write — used in
    # tests as the lowest-bound smoke test of the eviction path.
    max_total_size_mb: int = 500


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


class LauncherConfig(BaseModel):
    """Project-launcher configuration.

    Cortex's ProjectLauncher runs ``terminal_commands`` lifted from
    user-importable YAML files. The allowlist in
    :mod:`cortex.libs.utils.shell_allowlist` covers the common editor /
    terminal launchers. Power users with bespoke tooling extend the
    allowlist via ``user_command_allowlist`` rather than disabling the
    check entirely (audit F12).
    """

    user_command_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "Extra binary basenames the project launcher will accept in "
            "addition to the built-in editor/terminal allowlist."
        ),
    )


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
    launcher: LauncherConfig = Field(default_factory=LauncherConfig)

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


def load_yaml_defaults() -> dict[str, Any]:
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


# Phase-4a Debt-1: env toggles that gate user-visible features. The
# values are read by Pydantic from the environment / .env file; if
# neither source carries them the field defaults apply. That is
# semantically correct but operationally silent — a power user who
# meant to flip ``ENABLE_AUTO_DISTRACTION_BLOCK=true`` and mistyped the
# key would never know. We surface a one-line WARN at config load to
# bound mis-configuration surprise.
_REQUIRED_FEATURE_TOGGLES: tuple[str, ...] = (
    "CORTEX_INTERVENTION__ENABLE_BIOLOGY_BREAK",
    "CORTEX_INTERVENTION__ENABLE_AUTO_DISTRACTION_BLOCK",
    "CORTEX_INTERVENTION__ENABLE_OS_NOTIFICATIONS",
)


def _check_required_feature_toggles() -> None:
    """Warn (once per process) when documented feature toggles are
    absent from both the environment and the .env files.

    The defaults remain authoritative; this only surfaces mis-typed
    or forgotten overrides so operations is never silent.

    I5: ``CORTEX_SUPPRESS_FEATURE_TOGGLE_WARNINGS`` is honoured ONLY when
    ``CORTEX_ENV=test``. Production deployments that inadvertently set
    the suppression flag still emit warnings — we'd rather log-flood than
    silently ship with a wedged feature flag. Tests that need to suppress
    must explicitly set ``CORTEX_ENV=test`` as well.
    """
    if (
        os.environ.get("CORTEX_SUPPRESS_FEATURE_TOGGLE_WARNINGS") == "1"
        and os.environ.get("CORTEX_ENV") == "test"
    ):
        return
    log = __import__("logging").getLogger(__name__)
    env_files = _bundled_env_files()
    env_contents = ""
    for env_path in env_files:
        try:
            with open(env_path) as fp:
                env_contents += fp.read() + "\n"
        except OSError:
            continue
    for key in _REQUIRED_FEATURE_TOGGLES:
        if key in os.environ:
            continue
        if env_contents and key in env_contents:
            continue
        log.warning(
            "Documented feature toggle %s is not set in environment or "
            ".env; falling back to compiled default. Set it explicitly "
            "to silence this warning.",
            key,
        )


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
    _check_required_feature_toggles()

    if _is_bundled():
        storage_path = Path(config.storage.path).expanduser()
        if not storage_path.is_absolute():
            config.storage.path = _bundled_storage_path()
        # I10: surface mkdir failures as a structured ``StorageConfigError``
        # rather than letting a raw ``PermissionError`` escape from
        # ``get_config()``. The caller (desktop shell / native host) maps
        # this to a user-visible "pick a different storage location"
        # toast; the unstructured exception would have surfaced as a
        # cryptic stack trace at first launch.
        try:
            Path(config.storage.path).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log = __import__("logging").getLogger(__name__)
            log.error(
                "storage_mkdir_failed path=%s errno=%s msg=%s",
                config.storage.path,
                getattr(exc, "errno", None),
                exc,
            )
            raise StorageConfigError(config.storage.path, exc) from exc

    # I2: BYOK Bedrock token is NO LONGER written into ``os.environ``
    # at config-load time. The Anthropic SDK reads
    # ``AWS_BEARER_TOKEN_BEDROCK`` only at construction time; callers that
    # need the token use ``bedrock_token_env_scope()`` (a context manager
    # that scopes the env mutation to the SDK constructor call) or
    # ``get_bedrock_token()`` for direct access. This eliminates the leak
    # of the long-lived bearer into every subprocess the daemon spawns.

    # Mirror provider + region into the env the SDK reads, so subprocess
    # workers and the planner module see the same configuration.
    os.environ.setdefault("ANTHROPIC_PROVIDER", config.llm.provider)
    if config.llm.provider == "bedrock":
        os.environ.setdefault("AWS_REGION", config.llm.bedrock.aws_region)

    return config


def reset_config() -> None:
    """Reset the cached configuration (useful for testing)."""
    get_config.cache_clear()
