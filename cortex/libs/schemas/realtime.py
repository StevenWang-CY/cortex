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

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cortex.libs.schemas.intervention import CausalSignal
from cortex.libs.schemas.session_report import SessionReport

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
    duration_minutes: float | None = Field(
        None,
        ge=0.0,
        description=(
            "How long this mode runs from arming, in minutes. None when "
            "kind=='off' or when the daemon uses an implicit default."
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
            "Known values: 'not_found' | 'internal' | None (success)."
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
    generated_at: datetime = Field(
        default_factory=datetime.now,
        description=(
            "When the recap envelope was constructed (wall-clock). "
            "Distinct from ``report.end_time`` which is the session "
            "end."
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
    """P0 §3.15: COST_RESPONSE wire payload.

    Emitted as a reply to :attr:`MessageType.COST_REQUEST` and as an
    unsolicited push on every plan-finalised event so the desktop's
    cost meter updates without polling lag.

    Field naming note: the desktop shell historically read ``budget_usd``
    from the legacy stub payload; the new wire shape sends ``budget_today``
    (matching the §3.15 spec). The shell's ``apply_cost_response`` accepts
    both keys (Phase 4 follow-up).
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
            "Active LLM provider name (e.g. ``bedrock``, ``vertex``, "
            "``direct``, ``rule_based``)."
        ),
    )
    budget_exhausted: bool = Field(
        False,
        description="True when today's spend exceeded the budget cap.",
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


__all__ = [
    "BreakRecommendation",
    "CostResponse",
    "DistractionBlockPreset",
    "QuietModeSource",
    "QuietModeState",
    "QuietModeTogglePayload",
    "SessionRecap",
    "StartFocusAutoPayload",
    "StopFocusAutoPayload",
    "TestProviderRequest",
    "TestProviderResult",
    "WhyDetail",
]
