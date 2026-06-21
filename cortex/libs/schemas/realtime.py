"""Cortex realtime WebSocket payload envelopes (Phase-4a Debt-1 closure).

This module groups the payload schemas for the small family of
broadcast / request / reply messages that were previously emitted as
raw ``dict`` literals from ``runtime_daemon.py`` and
``websocket_server.py``. Promoting them to Pydantic models gives:

* a single source of truth for the wire shape (the codegen pipeline at
  ``cortex/scripts/generate_ts_schemas.py`` will emit matching TS types),
* compile-time validation at the daemon callsite (so a typo on
  ``"breathing_pattern"`` is a Pydantic error rather than a silent
  client-side missing-field), and
* round-trippable serialisation via ``model_dump(mode="json")`` —
  the only sanctioned way to lift these objects onto the WS wire.

Each model uses ``ConfigDict(extra="ignore")`` so a forward-compatible
field added by a future daemon is silently ignored by an older client
parser, matching the rest of the schemas in this package.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cortex.libs.schemas.intervention import CausalSignal, InterventionPlan
from cortex.libs.schemas.session_report import SessionReport
from cortex.libs.schemas.state import SignalQuality, StateScores

# ─── Shared Literal vocabularies ──────────────────────────────────────

# P0 §3.11: the full set of acceptable ``source`` strings for
# QUIET_MODE_TOGGLE / QUIET_MODE_STATE. Single source of truth — the
# dispatch arm in ``websocket_server.py`` validates against this same
# set (see _ALLOWED_SOURCES at line ~1067). When adding a new
# originator, extend this Literal AND the dispatcher's frozenset.
QuietModeSource = Literal[
    "dashboard",
    "overlay",
    "tray",
    "shortcut",
    "popup",
    "vscode",
    "os_notification",
    "settings_sync",
    "daemon",
    "daemon_decay",
]


# P0 §3.10: per-spec preset taxonomy. ``custom`` reads
# ``custom_domains``; the other presets read the per-preset domain map
# the browser extension owns.
DistractionBlockPreset = Literal["developer", "student", "writer", "custom"]


# ─── BreakRecommendation (BREAK_RECOMMENDATION payload) ───────────────


class BreakRecommendation(BaseModel):
    """P0 §3.7: BREAK_RECOMMENDATION wire payload.

    Emitted exactly once per ``StressIntegralTracker.should_break``
    False → True transition. The popup / desktop overlay surfaces a
    soft pill with a single CTA that fires ``take_biology_break`` via
    ``ACTION_EXECUTE``.

    The ``urgency`` literal mirrors what the daemon's
    ``_classify_break_urgency`` actually emits (``"low" | "medium" |
    "high"``); see ``cortex.services.runtime_daemon.CortexDaemon._classify_break_urgency``.
    """

    model_config = ConfigDict(extra="ignore")

    reason: str = Field(
        ...,
        max_length=120,
        description=(
            "Why this break was recommended — short, human-friendly "
            "string the overlay can read verbatim into the CTA pill."
        ),
    )
    urgency: Literal["low", "medium", "high"] = Field(
        "low",
        description=(
            "Daemon-classified urgency derived from "
            "``StressIntegralTracker.load_ratio``. The UI uses this to "
            "decide pill colour / tone, never to gate the action."
        ),
    )
    stress_load: float = Field(
        ...,
        ge=0.0,
        description="Current cumulative stress-integral load",
    )
    threshold: float = Field(
        ...,
        ge=0.0,
        description="Threshold the tracker crossed to fire this recommendation",
    )
    duration_seconds: int = Field(
        ...,
        ge=1,
        le=24 * 3600,
        description=(
            "Suggested length of the guided breathing session, in "
            "seconds. The overlay's ``BreathingPacer`` paces ``pattern`` "
            "across this duration."
        ),
    )
    breathing_pattern: Literal["4-7-8", "box", "coherent"] = Field(
        "4-7-8",
        description=(
            "Pacer cadence variant. Picked by the daemon based on the "
            "user's recent HRV (4-7-8 = relaxation / parasympathetic; "
            "box = balanced; coherent = 5.5 BPM resonance breathing)."
        ),
    )


# ─── QuietModeState (QUIET_MODE_STATE payload) ────────────────────────


class QuietModeState(BaseModel):
    """P0 §3.11: QUIET_MODE_STATE broadcast payload.

    Emitted whenever ``RuntimeDaemon.set_quiet_mode`` runs, regardless
    of whether the mode was armed from the dashboard, overlay, tray,
    keyboard shortcut, or the F26 frustration-spiral path. All
    surfaces (dashboard, overlay, tray, browser popup, VS Code status
    bar) must rerender from this single broadcast — never from
    independent local state.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["snooze_15", "quiet_session", "pause", "off"] = Field(
        ...,
        description=(
            "Mode flavour. ``snooze_15`` is overlay-only for ~15 min, "
            "``quiet_session`` is a long-form blocking window, "
            "``pause`` suspends sensing entirely, ``off`` clears any "
            "active mode."
        ),
    )
    duration_minutes: int | None = Field(
        None,
        ge=0,
        description=(
            "How long this mode runs from arming, in minutes. None when "
            "kind=='off' or when the daemon uses an implicit default. "
            "Semantically integral — the daemon already rounds via "
            "``int(round(...))`` before broadcasting, so the wire shape "
            "matches."
        ),
    )
    ends_at: float | None = Field(
        None,
        description=(
            "UNIX epoch seconds (wall-clock) when the mode lapses. None "
            "when kind=='off'. Clients compute a countdown from "
            "(ends_at - Date.now() / 1000)."
        ),
    )
    source: QuietModeSource = Field(
        "daemon",
        description=(
            "Originator of the toggle. Lets analytics distinguish "
            "user-initiated quiet-mode entries from daemon-decay "
            "auto-armed ones without parsing the rest of the payload."
        ),
    )


class QuietModeTogglePayload(BaseModel):
    """P0 §3.11: QUIET_MODE_TOGGLE inbound wire payload.

    Sent client → daemon to enter / leave a quiet or pause mode. The
    daemon validates ``source`` against the same vocabulary as
    :class:`QuietModeState`; an unknown source falls back to the
    presenting client's ``client_type``.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["snooze_15", "quiet_session", "pause", "off"] = Field(
        ...,
        description="Mode to enter (or 'off' to clear)",
    )
    duration_minutes: int | None = Field(
        None,
        ge=0,
        le=240,
        description=(
            "Requested duration. 0 / None reverts to the daemon's "
            "configured default for this ``kind``."
        ),
    )
    source: QuietModeSource | None = Field(
        None,
        description=(
            "Optional originator label; daemon falls back to the "
            "client's ``client_type`` when unset or unknown."
        ),
    )


# ─── WhyDetail (WHY_DETAIL payload) ───────────────────────────────────


class WhyDetail(BaseModel):
    """P0 §3.9: WHY_DETAIL reply payload (causal rationale).

    Carries the structured causal rationale (top 2-3 signals, primary
    first) plus the originating ``intervention_id`` so the requesting
    client can match the reply to its in-flight prompt.
    """

    model_config = ConfigDict(extra="ignore")

    intervention_id: str = Field(
        ...,
        description="Intervention this rationale belongs to",
    )
    causal_signals: list[CausalSignal] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "2-3 dominant signals behind the trigger, ranked by "
            "|z-score|. First entry is the primary driver; empty list "
            "means the daemon could not attribute the trigger."
        ),
    )
    error: str | None = Field(
        None,
        description=(
            "Set when the daemon cannot supply a rationale (e.g. the "
            "intervention has already been GC'd from the active cache). "
            "Known values: 'not_found' | 'internal' | "
            "'handler_not_registered' (no WHY callback wired) | None "
            "(success)."
        ),
    )


# ─── START_FOCUS_AUTO / STOP_FOCUS_AUTO payloads ──────────────────────


class StartFocusAutoPayload(BaseModel):
    """P0 §3.10: START_FOCUS_AUTO directive payload.

    Emitted to the browser extension when the user has opted in to
    ``CORTEX_INTERVENTION__ENABLE_AUTO_DISTRACTION_BLOCK`` AND the live
    state is HYPER with confidence above the gate. The extension reuses
    its existing focus-session start path; the daemon broadcasts a
    paired ``STOP_FOCUS_AUTO`` when the user exits HYPER.
    """

    model_config = ConfigDict(extra="ignore")

    duration_minutes: int = Field(
        ...,
        ge=1,
        le=240,
        description="Length of the auto-armed focus session, in minutes",
    )
    reason: str = Field(
        ...,
        max_length=120,
        description=(
            "Short reason string the extension surfaces in the start "
            "notification (e.g. 'sustained_hyper_confidence')."
        ),
    )
    preset: DistractionBlockPreset = Field(
        "developer",
        description=(
            "Domain preset to apply. ``custom`` exclusively uses "
            "``custom_domains``; other presets layer ``custom_domains`` "
            "on top of the preset's built-in list."
        ),
    )
    custom_domains: list[str] = Field(
        default_factory=list,
        max_length=256,
        description=(
            "User-editable extra domains to block. When "
            "``preset=='custom'`` this is the entire blocklist; "
            "otherwise it's an additive layer."
        ),
    )


class StopFocusAutoPayload(BaseModel):
    """P0 §3.10: STOP_FOCUS_AUTO directive payload.

    Sent after sustained non-HYPER state (FLOW ≥ 5 min) OR an explicit
    user disarm; the browser extension calls ``stopFocusSession`` only
    if the daemon was the one that armed the current session.
    """

    model_config = ConfigDict(extra="ignore")

    reason: str = Field(
        ...,
        max_length=120,
        description=(
            "Short reason string for analytics + extension logging "
            "(e.g. 'natural_recovery', 'user_disarm', 'shutdown')."
        ),
    )


# ─── SessionRecap (SESSION_RECAP payload, Phase-4a envelope) ──────────


class SessionRecap(BaseModel):
    """P0 §3.3: SESSION_RECAP envelope (Phase-4a).

    Carries the full ``SessionReport`` plus two metadata fields the
    legacy raw-dict broadcast lacked:

    * ``generated_at`` — wall-clock instant the recap was constructed
      (distinct from ``report.end_time`` which is the *session* end);
      the popup uses this to badge stale recaps.
    * ``persisted`` — whether the atomic write to disk succeeded.
      Defaults to True; the daemon sets it to False when the broadcast
      fires before the disk write completes (e.g. the disk-write
      coroutine raised). This lets the UI hint at "live-only, not on
      disk" recaps.

    The legacy wire shape ``model_dump(mode="json")`` of bare
    ``SessionReport`` remains compatible because every field in
    :class:`SessionReport` appears under ``report``.
    """

    model_config = ConfigDict(extra="ignore")

    report: SessionReport = Field(
        ..., description="The full on-disk session report"
    )
    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description=(
            "ISO-8601 instant the recap envelope was constructed "
            "(wall-clock). C4 (audit): typed as ``str`` so the daemon's "
            "``SessionRecap(generated_at=<iso8601 str>, ...)`` wrapper "
            "matches the schema exactly and the generated TypeScript "
            "type is a plain ``string``. Distinct from "
            "``report.end_time`` which is the session end."
        ),
    )
    persisted: bool = Field(
        True,
        description=(
            "True iff the atomic write to "
            "``storage/sessions/session_<id>.json`` succeeded. False "
            "when the broadcast races ahead of the disk write."
        ),
    )


# ─── CostResponse (COST_RESPONSE payload, §3.15) ──────────────────────


class CostResponse(BaseModel):
    """P0 §3.15: COST_RESPONSE wire payload (unified HTTP + WS envelope).

    Emitted as a reply to :attr:`MessageType.COST_REQUEST` (WebSocket path)
    AND as the response body of ``GET /api/cost`` (HTTP path). Both surfaces
    share this single canonical shape so consumers always see the same keys.

    Core fields (always present):
    - ``cost_today``: cumulative USD spent today (resets at local midnight).
    - ``budget_today``: daily budget cap (kill_usd); 0 means unlimited.
    - ``provider``: active provider key or ``None`` when no tracker is wired.
      Never the string ``"none"`` — always ``None`` (JSON ``null``).
    - ``budget_exhausted``: True when today's spend exceeded the budget cap.
    - ``timestamp``: UNIX epoch seconds (wall-clock UTC) when this snapshot
      was taken.

    Extended fields (optional — may be ``None`` when the WS path omits them
    or when no LLM client is registered):
    - ``prompt_tokens``: best-effort prompt-token counter for today.
    - ``completion_tokens``: best-effort completion-token counter for today.
    - ``model``: active model id (e.g. ``claude-sonnet-4-5``).
    """

    model_config = ConfigDict(extra="ignore")

    cost_today: float = Field(
        ..., ge=0.0,
        description="USD spent today by the LLM cost tracker.",
    )
    budget_today: float = Field(
        ..., ge=0.0,
        description="USD daily budget cap (kill_usd). 0 means unlimited.",
    )
    provider: str | None = Field(
        None,
        description=(
            "Active LLM provider key (e.g. ``bedrock``, ``vertex``, "
            "``anthropic_direct``, ``rule_based``). ``None`` (JSON null) "
            "when no tracker is active — never the string ``\"none\"``."
        ),
    )
    budget_exhausted: bool = Field(
        False,
        description="True when today's spend exceeded the budget cap.",
    )
    timestamp: float = Field(
        default_factory=lambda: time.time(),
        description=(
            "UNIX epoch seconds (wall-clock UTC) when this cost snapshot "
            "was taken. Comparable across producer and consumer."
        ),
    )
    prompt_tokens: int | None = Field(
        None, ge=0,
        description=(
            "Best-effort prompt-token counter for today. ``None`` when "
            "the active tracker does not expose token counters."
        ),
    )
    completion_tokens: int | None = Field(
        None, ge=0,
        description=(
            "Best-effort completion-token counter for today. ``None`` "
            "when the active tracker does not expose token counters."
        ),
    )
    model: str | None = Field(
        None,
        description=(
            "Active model id (e.g. ``claude-sonnet-4-5``). ``None`` "
            "when no LLM client is registered."
        ),
    )


# ─── TestProvider (TEST_PROVIDER / TEST_PROVIDER_RESULT, §3.19) ───────


class TestProviderRequest(BaseModel):
    """P0 §3.19: TEST_PROVIDER inbound wire payload."""

    model_config = ConfigDict(extra="ignore")

    provider: Literal["bedrock", "vertex", "anthropic_direct", "rule_based"] = Field(
        ...,
        description="Provider key the user selected in the settings dropdown.",
    )


class TestProviderResult(BaseModel):
    """P0 §3.19: TEST_PROVIDER_RESULT outbound wire payload."""

    model_config = ConfigDict(extra="ignore")

    provider: str = Field(
        ...,
        description="Echoes the provider that was tested.",
    )
    ok: bool = Field(
        ...,
        description="True on a successful probe round-trip within 5 s.",
    )
    latency_ms: float | None = Field(
        None,
        description="Wall-clock round-trip in milliseconds (None on failure).",
    )
    error: str | None = Field(
        None,
        description="Short error category when ``ok`` is False.",
    )


# ─── StateUpdatePayload (STATE_UPDATE payload) ────────────────────────


class CaptureStatus(BaseModel):
    """P0 §3 (audit Debt-1 closure): capture-channel status sub-payload.

    The dashboard's "Reading your pulse" / "Camera offline" ambient
    string is driven by this sub-shape. The producer in
    ``websocket_server._make_state_update`` stamps it on every STATE_UPDATE
    broadcast off the registry-cached ``latest_frame_meta``.
    """

    model_config = ConfigDict(extra="ignore")

    frames_flowing: bool = Field(
        False,
        description=(
            "True when a frame newer than 2 s ago was observed. False "
            "when the capture loop hasn't produced a frame yet (camera "
            "not open, permission denied, daemon mid-startup)."
        ),
    )
    face_detected: bool = Field(
        False,
        description=(
            "True when the most recent frame's MediaPipe FaceMesh "
            "detected at least one face."
        ),
    )
    stale: bool = Field(
        False,
        description=(
            "True when the daemon planted ``capture_stale`` because the "
            "pipeline failed to start or has gone offline. Cleared "
            "automatically when ``frames_flowing`` is True (transient "
            "init failure followed by a successful resume)."
        ),
    )
    sequence: int | None = Field(
        None,
        description=(
            "Latest capture-loop sequence number; surfaced for debug "
            "overlays that need to detect dropped frames. None when the "
            "producer does not stamp a sequence."
        ),
    )


class StoreHealth(BaseModel):
    """Persistence-layer health indicator surfaced on every STATE_UPDATE.

    The desktop dashboard uses ``degraded`` to render an in-memory
    "you'll lose state on restart" hint when Redis is unavailable; the
    DMG default deployment now uses :func:`make_default_store` so this
    flag is only True when both Redis is configured AND unreachable.
    """

    model_config = ConfigDict(extra="ignore")

    degraded: bool = Field(
        False,
        description=(
            "True when the daemon is running on the InMemoryStore "
            "fallback (intended Redis unreachable). The dashboard uses "
            "this to surface a soft 'no Redis' hint."
        ),
    )
    backend: str | None = Field(
        None,
        description=(
            "Backend identifier (``redis`` / ``in_memory``). Optional — "
            "present when the daemon plants it in the registry; None "
            "otherwise."
        ),
    )
    healthy: bool | None = Field(
        None,
        description=(
            "Optional explicit health flag from the store's "
            "``health_check`` probe. None when the daemon hasn't run a "
            "probe recently."
        ),
    )


class BiometricsSummary(BaseModel):
    """Per-tick biometrics summary attached to STATE_UPDATE payloads.

    All fields are ``float | None`` because the underlying signals are
    independently gated — heart rate may be available while respiration
    isn't, etc. The producer in
    ``runtime_daemon._process_capture_output`` builds this dict from
    the live ``FusedFeatureVector`` plus the stress-integral tracker.

    The wire-level shape is ``payload.biometrics`` and is omitted when
    the producer has no values to share (the ``_make_state_update``
    helper only sets the key when biometrics is truthy).
    """

    model_config = ConfigDict(extra="ignore")

    heart_rate: float | None = Field(
        None, description="rPPG heart-rate estimate in BPM"
    )
    hrv_rmssd: float | None = Field(
        None, description="HRV RMSSD in milliseconds"
    )
    hr_delta: float | None = Field(
        None,
        description=(
            "Heart-rate delta versus baseline; sign carries direction "
            "(positive = above baseline)."
        ),
    )
    blink_rate: float | None = Field(
        None, description="Blink rate in blinks/minute"
    )
    perclos: float | None = Field(
        None,
        description=(
            "PERCLOS (percent eye closure) over the recent window; not "
            "always populated."
        ),
    )
    forward_lean: float | None = Field(
        None,
        description=(
            "Forward-lean score rescaled to 0..1. Browser-side posture "
            "alert threshold (0.6) is compared against this rescaled "
            "value, not raw degrees."
        ),
    )
    forward_lean_angle: float | None = Field(
        None,
        description=(
            "Forward-lean angle in degrees (legacy / debug). Consumers "
            "preferring a score should read ``forward_lean`` instead."
        ),
    )
    respiration_rate: float | None = Field(
        None, description="Respiration rate in breaths/minute"
    )
    thrashing_score: float | None = Field(
        None,
        description=(
            "Kinematic thrashing score (input-device chaos indicator); "
            "0..1 with higher meaning more thrashing."
        ),
    )
    stress_integral: float | None = Field(
        None,
        ge=0.0,
        description=(
            "Cumulative stress-integral load tracked by "
            "``StressIntegralTracker``. Used by the break-readiness UI."
        ),
    )


class StateUpdatePayload(BaseModel):
    """P0 §3 (audit Debt-1): STATE_UPDATE wire payload — typed.

    Previously ``websocket_server._make_state_update`` built a free-form
    ``dict[str, Any]`` literal; promoting it to a Pydantic model gives
    the codegen pipeline a generated TypeScript type the browser
    extension can consume and prevents silent field drift between the
    daemon and the dashboard.

    Field set mirrors ``StateEstimate`` plus the envelope-level F18
    additions (``degraded`` / ``source``) and the capture / store /
    biometrics sub-shapes the producer stamps. ``extra="ignore"`` keeps
    forward-compatibility: a new field added by a future daemon is
    silently ignored by an older client parser.
    """

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    state: Literal["FLOW", "HYPO", "HYPER", "RECOVERY"] = Field(
        ...,
        description="Classified user state (mirrors ``StateEstimate.state``)",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in state classification"
    )
    scores: StateScores = Field(
        ...,
        description="Raw scores for each state",
    )
    signal_quality: SignalQuality = Field(
        ...,
        description="Signal quality per channel",
    )
    dwell_seconds: float = Field(
        0.0, ge=0.0, description="Seconds in current state"
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable reasons for current state",
    )
    stress_integral: float | None = Field(
        None,
        ge=0.0,
        description="Cumulative stress-integral load (ms*s)",
    )
    calibrated_probabilities: StateScores | None = Field(
        None,
        description="Calibrated class probabilities (optional ML/rule ensemble output)",
    )
    classifier_source: Literal["rule", "ml", "ensemble"] | None = Field(
        None,
        description="Classifier source used for this estimate",
    )
    classifier_alpha: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Ensemble weight on ML branch when used",
    )
    source: Literal["classifier", "fallback"] = Field(
        "classifier",
        description=(
            "Envelope-level source (mirrors ``StateInferResponse.source``). "
            "``fallback`` when no real classifier ran; the dashboard's "
            "'classifier unavailable' banner reads this."
        ),
    )
    degraded: bool = Field(
        False,
        description=(
            "True when no real classifier ran (``classifier_source is "
            "None``) — same condition the ``/state/infer`` fallback "
            "branch uses to flag synthetic confidence."
        ),
    )
    timestamp: float | str | None = Field(
        None,
        description=(
            "Wall-clock timestamp the estimate was produced. May be an "
            "ISO string (datetime path) or float (monotonic-style "
            "producer); consumers must accept both shapes for "
            "backwards-compatibility."
        ),
    )
    connected_clients: list[str] = Field(
        default_factory=list,
        description=(
            "Deduped list of currently-IDENTIFY-ed client types "
            "(``chrome``, ``edge``, ``vscode``). Used by the dashboard "
            "to light up connection dots without a separate event "
            "stream. ``desktop`` and ``unknown`` are intentionally "
            "EXCLUDED by the producer "
            "(WebSocketServer.connected_client_types) — the dashboard IS "
            "the desktop surface, so it never needs a self-connection dot."
        ),
    )
    # ``default_factory=CaptureStatus`` follows the established project
    # pattern (session_history.py:220 / leetcode.py:122). mypy-strict
    # without the pydantic plugin flags the class-as-factory pattern;
    # the project already accepts these two warnings in the listed
    # peers and the CI gate does not fail on them.
    capture: CaptureStatus = Field(
        default_factory=CaptureStatus,
        description="Capture-channel health sub-payload",
    )
    store: StoreHealth = Field(
        default_factory=StoreHealth,
        description="Persistence-layer health sub-payload",
    )
    biometrics: BiometricsSummary | None = Field(
        None,
        description=(
            "Per-tick biometrics summary. Omitted by the producer when "
            "no values are available (early startup, capture offline)."
        ),
    )
    sequence: int | None = Field(
        None,
        description=(
            "Monotonic sequence number stamped by the producer for "
            "consumer-side dedup. Currently the envelope-level "
            "``sequence`` field on ``WSMessage`` carries this; the "
            "field here is a forward-compatibility hook for callers "
            "that round-trip just the payload."
        ),
    )


# ─── InterventionTriggerPayload (INTERVENTION_TRIGGER payload) ────────


class InterventionTriggerPayload(InterventionPlan):
    """P0 §3 (audit Debt-1): INTERVENTION_TRIGGER wire payload.

    The producer stamps two envelope-level fields onto the dumped
    :class:`InterventionPlan` — ``desktop_not_focused`` and
    ``connected_clients`` — before broadcasting. To keep the wire shape
    backward-compatible with consumers that read
    ``payload.intervention_id`` directly (browser extension, popup,
    VS Code), we extend :class:`InterventionPlan` rather than wrapping
    it. The two stamp fields are optional with defaults so older code
    constructing a bare ``InterventionPlan`` is still type-valid.

    DESIGN NOTE: this choice preserves the flat wire shape at the cost
    of carrying two non-domain fields on the InterventionPlan extension.
    The alternative — a nested ``{plan: ..., desktop_not_focused: ...,
    connected_clients: ...}`` envelope — is more correct semantically
    but would break every consumer that reads
    ``payload.intervention_id``. The extension approach matches the
    pattern Pydantic uses for protocol evolution (additive fields with
    defaults).
    """

    model_config = ConfigDict(extra="ignore")

    desktop_not_focused: bool | None = Field(
        None,
        description=(
            "P0 §3.12: True when the daemon observed that the desktop "
            "shell isn't focused (user on a different Space / "
            "fullscreen app). Receivers surface OS-level notification "
            "cues. None means 'focus state unknown' (default — only "
            "stamped when explicitly observed unfocused)."
        ),
    )
    connected_clients: list[str] | None = Field(
        None,
        description=(
            "Snapshot of currently-IDENTIFY-ed client types at "
            "broadcast time, so WS-mode overlay action buttons gate on "
            "the same authoritative list ``STATE_UPDATE`` uses. None "
            "means the producer didn't stamp this field."
        ),
    )


# ─── Native-messaging-related (RaiseDashboard relay) ──────────────────
#
# ``RaiseDashboardMessage`` lives in ``native_messaging.py`` not here —
# it belongs to the native-host command vocabulary, not the WS
# broadcast vocabulary. See ``cortex.libs.schemas.native_messaging``.


__all__ = [
    "BiometricsSummary",
    "BreakRecommendation",
    "CaptureStatus",
    "CostResponse",
    "DistractionBlockPreset",
    "InterventionTriggerPayload",
    "QuietModeSource",
    "QuietModeState",
    "QuietModeTogglePayload",
    "SessionRecap",
    "StartFocusAutoPayload",
    "StateUpdatePayload",
    "StopFocusAutoPayload",
    "StoreHealth",
    "TestProviderRequest",
    "TestProviderResult",
    "WhyDetail",
]
