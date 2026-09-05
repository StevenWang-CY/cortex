"""Privacy boundary for every external workspace-context request.

The broker is deliberately local and deterministic.  It classifies every
``TaskContext`` leaf, applies per-source selection, removes path/URL detail,
normalises hostile Unicode, redacts common and high-entropy secrets, and keeps
only a bounded prepared payload in memory.  An external planner can consume a
prepared payload exactly once after the caller repeats the explicit
confirmation phrase.  Preview creation itself never invokes the model SDK.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import secrets
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Final, Protocol
from urllib.parse import urlsplit, urlunsplit

from cortex.application.clock import SYSTEM_CLOCK, BoundedDeadline, Clock
from cortex.libs.config.settings import LLMConfig
from cortex.libs.llm.anthropic_client import resolve_anthropic_model_id
from cortex.libs.schemas.context import (
    BrowserContext,
    Diagnostic,
    EditorContext,
    TabInfo,
    TaskContext,
    TerminalContext,
)
from cortex.libs.schemas.intervention import (
    InterventionPlan,
    SimplificationConstraints,
    UIPlan,
)
from cortex.libs.schemas.privacy import (
    CONTEXT_DISCLOSURE_REVISION,
    CONTEXT_SEND_CONFIRMATION,
    ContextClassification,
    ContextFieldDisclosure,
    ContextOrigin,
    ContextPreviewRequest,
    ContextPreviewResponse,
    ContextPrivacyStatusResponse,
    ContextSourceSelection,
    ProviderRetentionDisclosure,
)
from cortex.libs.schemas.state import (
    EstimateStatus,
    SignalQuality,
    StateEstimate,
    StateScores,
    SupportState,
    UserState,
)
from cortex.services.llm_engine.prompts import (
    PROMPT_TEMPLATES,
    build_user_prompt,
    select_prompt_template,
)

logger = logging.getLogger(__name__)

# Reasons the external transport is unavailable, as surfaced by
# ``PrivacyAwarePlanner.transport_state`` / preview errors / fallback plans.
TRANSPORT_DISABLED: Final[str] = "external_context_disabled"
TRANSPORT_CREDENTIALS_MISSING: Final[str] = "credentials_missing"
TRANSPORT_READY: Final[str] = "ready"


class ExternalContextDisabledError(RuntimeError):
    """Raised when configuration has not enabled the external boundary.

    The message starts with ``external_context_disabled`` (disclosure not
    acknowledged / mode not external) or ``credentials_missing`` (external
    mode is configured but no provider credential could be loaded — the
    BYOK step fixes this without a restart) so callers can tell the two
    apart.
    """


class PreviewAuthorizationError(RuntimeError):
    """Raised when a preview is absent, expired, changed, or replayed."""


@dataclass(frozen=True, slots=True)
class ContextFieldPolicy:
    classification: ContextClassification
    origin: ContextOrigin
    selection_key: str | None


# Complete leaf catalog for TaskContext plus the only additional untrusted
# prompt input. ``None`` means the field is collected locally but deliberately
# never enters the current external prompt.  The test suite introspects the
# Pydantic model graph and rejects uncatalogued leaves.
CONTEXT_FIELD_CATALOG: Final[dict[str, ContextFieldPolicy]] = {
    "mode": ContextFieldPolicy("operational_aggregate", "daemon", "workspace_aggregates"),
    "active_app": ContextFieldPolicy("operational_aggregate", "daemon", "workspace_aggregates"),
    "current_goal_hint": ContextFieldPolicy("user_goal", "user", "user_goal"),
    "complexity_score": ContextFieldPolicy(
        "operational_aggregate", "daemon", "workspace_aggregates"
    ),
    "editor_context.file_path": ContextFieldPolicy(
        "workspace_metadata", "editor", "editor_metadata"
    ),
    "editor_context.visible_range": ContextFieldPolicy("workspace_metadata", "editor", None),
    "editor_context.symbol_at_cursor": ContextFieldPolicy(
        "workspace_metadata", "editor", "editor_metadata"
    ),
    "editor_context.diagnostics[].severity": ContextFieldPolicy(
        "workspace_metadata", "editor", "editor_metadata"
    ),
    "editor_context.diagnostics[].message": ContextFieldPolicy(
        "workspace_content", "editor", "editor_content"
    ),
    "editor_context.diagnostics[].line": ContextFieldPolicy(
        "workspace_metadata", "editor", "editor_metadata"
    ),
    "editor_context.diagnostics[].column": ContextFieldPolicy("workspace_metadata", "editor", None),
    "editor_context.diagnostics[].source": ContextFieldPolicy("workspace_metadata", "editor", None),
    "editor_context.diagnostics[].code": ContextFieldPolicy("workspace_metadata", "editor", None),
    "editor_context.recent_edits[]": ContextFieldPolicy("workspace_content", "editor", None),
    "editor_context.visible_code": ContextFieldPolicy(
        "workspace_content", "editor", "editor_content"
    ),
    "terminal_context.last_n_lines[]": ContextFieldPolicy("workspace_content", "terminal", None),
    "terminal_context.detected_errors[]": ContextFieldPolicy(
        "workspace_content", "terminal", "terminal_content"
    ),
    "terminal_context.repeated_commands[]": ContextFieldPolicy(
        "workspace_content", "terminal", None
    ),
    "terminal_context.running_command": ContextFieldPolicy("workspace_content", "terminal", None),
    "browser_context.active_tab_title": ContextFieldPolicy(
        "workspace_metadata", "browser", "browser_metadata"
    ),
    "browser_context.active_tab_url": ContextFieldPolicy("workspace_metadata", "browser", None),
    "browser_context.active_tab_content_excerpt": ContextFieldPolicy(
        "workspace_content", "browser", "browser_content"
    ),
    "browser_context.all_tabs[].tab_id": ContextFieldPolicy("workspace_metadata", "browser", None),
    "browser_context.all_tabs[].title": ContextFieldPolicy(
        "workspace_metadata", "browser", "browser_metadata"
    ),
    "browser_context.all_tabs[].url": ContextFieldPolicy(
        "workspace_metadata", "browser", "browser_metadata"
    ),
    "browser_context.all_tabs[].tab_type": ContextFieldPolicy(
        "workspace_metadata", "browser", "browser_metadata"
    ),
    "browser_context.all_tabs[].is_active": ContextFieldPolicy(
        "workspace_metadata", "browser", "browser_metadata"
    ),
    "browser_context.all_tabs[].topic_hint": ContextFieldPolicy(
        "workspace_metadata", "browser", "browser_metadata"
    ),
    "browser_context.all_tabs[].last_activated_ago_seconds": ContextFieldPolicy(
        "workspace_metadata", "browser", "browser_metadata"
    ),
    "browser_context.tab_type_classification": ContextFieldPolicy(
        "operational_aggregate", "browser", "browser_metadata"
    ),
    "browser_context.focus_goal": ContextFieldPolicy("user_goal", "user", "user_goal"),
    "learned_relevance": ContextFieldPolicy(
        "behavioral_preference", "daemon", "learned_preferences"
    ),
    "state.state": ContextFieldPolicy("support_estimate", "daemon", "support_estimate"),
    "state.confidence": ContextFieldPolicy("support_estimate", "daemon", "support_estimate"),
    "state.dwell_seconds": ContextFieldPolicy("support_estimate", "daemon", "support_estimate"),
    "extra_context": ContextFieldPolicy("workspace_content", "user", "extra_context"),
}


_BIDI_AND_ZERO_WIDTH = re.compile("[\u061c\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
# Any absolute POSIX path with at least two segments (``/Applications/...``,
# ``/etc/...``, ``/usr/local/...``, ``/srv``, ``/mnt``, ``/root``,
# ``/workspace``, ...), not just the home-style roots. The look-behind keeps
# URL paths intact: in ``https://host/path`` the slash is preceded by a
# word character, and the ``//`` after the scheme by ``:`` / ``/``.
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![\w:/.])/(?:[^\s:'\"<>|/]+/)+[^\s:'\"<>|/]+"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\|\\\\)[^\s:'\"<>|]+")
_URI_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s:]+:[^/@\s]+)@",
    re.IGNORECASE,
)
_GENERIC_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|client[_-]?secret|password|passwd|secret|token)"
    r"(\s*[=:]\s*|\s+)([\"']?)([^\s\"',;]{8,})([\"']?)"
)
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
    ),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b"),
    ),
    ("SLACK_TOKEN", re.compile(r"\bxox(?:a|b|p|r|s)-[A-Za-z0-9-]{10,}\b")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("API_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
)
_TOKEN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{24,}(?![A-Za-z0-9])")


@dataclass(frozen=True, slots=True)
class RedactedText:
    value: str
    redactions: int


@dataclass(frozen=True, slots=True)
class SanitizedContextBundle:
    context: TaskContext
    state: StateEstimate
    extra_context: str
    disclosures: tuple[ContextFieldDisclosure, ...]
    redaction_count: int


@dataclass(slots=True)
class _PreparedPreview:
    deadline: BoundedDeadline
    request_digest: str
    context: TaskContext
    state: StateEstimate
    constraints: SimplificationConstraints | None
    template_name: str
    extra_context: str
    disclosures: tuple[ContextFieldDisclosure, ...]
    selection: ContextSourceSelection


class _ExternalPlanner(Protocol):
    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
        disclosure_manifest: tuple[ContextFieldDisclosure, ...] | None = None,
    ) -> InterventionPlan: ...

    async def health_check(self) -> bool: ...


def _normalise_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = _BIDI_AND_ZERO_WIDTH.sub("", text)
    chars: list[str] = []
    for char in text:
        if char in {"\n", "\t"}:
            chars.append(char)
            continue
        if unicodedata.category(char).startswith("C"):
            chars.append(" ")
        else:
            chars.append(char)
    return "".join(chars)


def _basename(raw: str) -> str:
    candidate = raw.replace("\\", "/").rstrip("/")
    return candidate.rsplit("/", 1)[-1] if candidate else ""


def _minimise_embedded_paths(text: str) -> str:
    def posix_replace(match: re.Match[str]) -> str:
        return f"…/{_basename(match.group(0))}"

    def windows_replace(match: re.Match[str]) -> str:
        try:
            name = PureWindowsPath(match.group(0)).name
        except (TypeError, ValueError):
            name = _basename(match.group(0))
        return f"…\\{name}"

    text = _POSIX_ABSOLUTE_PATH.sub(posix_replace, text)
    return _WINDOWS_ABSOLUTE_PATH.sub(windows_replace, text)


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def redact_text(value: str, *, max_chars: int) -> RedactedText:
    """Normalise, path-minimise, redact secrets, then apply the hard cap."""

    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    # Nothing beyond the output boundary can leave the device. Scan a fixed
    # overlap so a credential or private-key block beginning just before that
    # boundary is still replaced, without letting a multi-megabyte editor/page
    # payload consume unbounded CPU or transient memory.
    scan_limit = max_chars + 4_096
    text = _minimise_embedded_paths(_normalise_text(value[:scan_limit]))
    redactions = 0
    text, uri_count = _URI_CREDENTIALS.subn(r"\g<scheme>[REDACTED:URI_CREDENTIALS]@", text)
    redactions += uri_count
    for label, pattern in _SECRET_PATTERNS:
        text, count = pattern.subn(f"[REDACTED:{label}]", text)
        redactions += count

    def generic_replace(match: re.Match[str]) -> str:
        nonlocal redactions
        if "[REDACTED:" in match.group(0):
            return match.group(0)
        redactions += 1
        return f"{match.group(1)}{match.group(2)}[REDACTED:SECRET]"

    text = _GENERIC_SECRET.sub(generic_replace, text)

    def entropy_replace(match: re.Match[str]) -> str:
        nonlocal redactions
        token = match.group(0)
        classes = sum(
            (
                any(ch.islower() for ch in token),
                any(ch.isupper() for ch in token),
                any(ch.isdigit() for ch in token),
                any(not ch.isalnum() for ch in token),
            )
        )
        if classes >= 3 and _entropy(token) >= 4.2:
            redactions += 1
            return "[REDACTED:HIGH_ENTROPY]"
        return token

    text = _TOKEN_CANDIDATE.sub(entropy_replace, text)
    return RedactedText(text[:max_chars], redactions)


def minimise_file_path(value: str) -> RedactedText:
    """Return only the final path component, with secret filtering."""

    return redact_text(_basename(value), max_chars=160)


def minimise_url(value: str) -> RedactedText:
    """Return an HTTP(S) origin only; userinfo, path, query, fragment vanish."""

    normalised = _normalise_text(value).strip()
    try:
        parts = urlsplit(normalised)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return RedactedText("[URL OMITTED]", 0)
        hostname = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
        default_port = (parts.scheme.lower() == "http" and port == 80) or (
            parts.scheme.lower() == "https" and port == 443
        )
        netloc = hostname if port is None or default_port else f"{hostname}:{port}"
        origin = urlunsplit((parts.scheme.lower(), netloc, "", "", ""))
        return redact_text(origin, max_chars=300)
    except (UnicodeError, ValueError):
        return RedactedText("[URL OMITTED]", 0)


def _preview_value(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, (str, int, float, bool)):
        return str(value)[:512]
    if isinstance(value, (list, tuple, dict)):
        return f"{len(value)} item(s)"
    return type(value).__name__


def _request_digest(
    *,
    context: TaskContext,
    state: StateEstimate,
    constraints: SimplificationConstraints | None,
    template_name: str,
    extra_context: str,
    selection: ContextSourceSelection,
) -> str:
    payload = {
        "context": context.model_dump(mode="json", exclude_none=False),
        # Only state values interpolated into the outbound prompt are bound.
        # ``estimate_id`` has a UUID default and two separately parsed but
        # byte-identical HTTP bodies would otherwise hash differently.
        "state": {
            "state": str(state.state),
            "confidence": state.confidence,
            "dwell_seconds": state.dwell_seconds,
        },
        "constraints": (
            constraints.model_dump(mode="json", exclude_none=False)
            if constraints is not None
            else None
        ),
        "template_name": template_name,
        "extra_context": extra_context,
        "selection": selection.model_dump(mode="json"),
        "disclosure_revision": CONTEXT_DISCLOSURE_REVISION,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _projected_state(state: StateEstimate) -> StateEstimate:
    """Forward only the three values the prompt interpolates.

    ``build_user_prompt`` reads ``state`` / ``confidence`` /
    ``dwell_seconds`` and nothing else, so the outbound estimate is rebuilt
    from those three with neutral defaults everywhere else — component
    scores, reasons, signal quality, timestamps, and the estimate id never
    reach the transport or the plan cache (audit D14).
    """

    return StateEstimate(
        state=state.state,
        confidence=state.confidence,
        dwell_seconds=state.dwell_seconds,
        scores=StateScores(),
        signal_quality=SignalQuality(),
        timestamp=0.0,
    )


def _neutral_state(state: StateEstimate) -> StateEstimate:
    """Keep wire shape valid while withholding all support-estimate values."""

    del state
    return StateEstimate(
        state=UserState.UNKNOWN,
        support_state=SupportState.UNKNOWN,
        status=EstimateStatus.INSUFFICIENT_EVIDENCE,
        confidence=0.0,
        dwell_seconds=0.0,
        scores=StateScores(),
        signal_quality=SignalQuality(),
        timestamp=0.0,
    )


class ContextBroker:
    """Pure transformation from raw local context to bounded outbound data."""

    def sanitize(
        self,
        context: TaskContext,
        state: StateEstimate,
        selection: ContextSourceSelection,
        *,
        extra_context: str = "",
    ) -> SanitizedContextBundle:
        values: dict[str, Any] = {}
        redactions: dict[str, int] = Counter()

        def record(path: str, value: Any, count: int = 0) -> Any:
            values[path] = value
            redactions[path] += count
            return value

        if selection.workspace_aggregates:
            mode = record("mode", context.mode)
            active_app = record("active_app", context.active_app)
            complexity = record("complexity_score", round(context.complexity_score, 2))
        else:
            mode, active_app, complexity = "mixed", "other", 0.0

        goal: str | None = None
        if selection.user_goal and context.current_goal_hint:
            clean = redact_text(context.current_goal_hint, max_chars=300)
            goal = record("current_goal_hint", clean.value, clean.redactions)

        editor: EditorContext | None = None
        if context.editor_context and (selection.editor_metadata or selection.editor_content):
            source = context.editor_context
            file_path = ""
            symbol: str | None = None
            if selection.editor_metadata:
                cleaned_path = minimise_file_path(source.file_path)
                file_path = record(
                    "editor_context.file_path", cleaned_path.value, cleaned_path.redactions
                )
                if source.symbol_at_cursor:
                    cleaned_symbol = redact_text(source.symbol_at_cursor, max_chars=160)
                    symbol = record(
                        "editor_context.symbol_at_cursor",
                        cleaned_symbol.value,
                        cleaned_symbol.redactions,
                    )

            diagnostics: list[Diagnostic] = []
            if selection.editor_content:
                for item in source.diagnostics:
                    if item.severity != "error" or len(diagnostics) >= 3:
                        continue
                    cleaned_message = redact_text(item.message, max_chars=300)
                    record(
                        "editor_context.diagnostics[].message",
                        cleaned_message.value,
                        cleaned_message.redactions,
                    )
                    diagnostics.append(
                        Diagnostic(
                            severity="error",
                            message=cleaned_message.value,
                            line=item.line if selection.editor_metadata else 1,
                            column=0,
                        )
                    )
                cleaned_code = redact_text(source.visible_code, max_chars=3_000)
                visible_code = record(
                    "editor_context.visible_code", cleaned_code.value, cleaned_code.redactions
                )
            else:
                visible_code = ""
            editor = EditorContext(
                file_path=file_path,
                visible_range=(1, 1),
                symbol_at_cursor=symbol,
                diagnostics=diagnostics,
                recent_edits=[],
                visible_code=visible_code,
            )

        terminal: TerminalContext | None = None
        if context.terminal_context and selection.terminal_content:
            errors: list[str] = []
            for terminal_error in context.terminal_context.detected_errors[:3]:
                cleaned = redact_text(terminal_error, max_chars=300)
                errors.append(cleaned.value)
                record("terminal_context.detected_errors[]", cleaned.value, cleaned.redactions)
            terminal = TerminalContext(detected_errors=errors)

        browser: BrowserContext | None = None
        if context.browser_context and (
            selection.browser_metadata or selection.browser_content or selection.user_goal
        ):
            source_browser = context.browser_context
            active_title = ""
            tabs: list[TabInfo] = []
            focus_goal: str | None = None
            if selection.browser_metadata:
                title = redact_text(source_browser.active_tab_title, max_chars=160)
                active_title = record(
                    "browser_context.active_tab_title", title.value, title.redactions
                )
                for tab in source_browser.all_tabs[:200]:
                    tab_title = redact_text(tab.title, max_chars=160)
                    tab_url = minimise_url(tab.url)
                    topic = redact_text(tab.topic_hint, max_chars=120)
                    record(
                        "browser_context.all_tabs[].title", tab_title.value, tab_title.redactions
                    )
                    record("browser_context.all_tabs[].url", tab_url.value, tab_url.redactions)
                    record("browser_context.all_tabs[].topic_hint", topic.value, topic.redactions)
                    tabs.append(
                        TabInfo(
                            tab_id=-1,
                            title=tab_title.value,
                            url=tab_url.value,
                            tab_type=tab.tab_type,
                            is_active=tab.is_active,
                            topic_hint=topic.value,
                            last_activated_ago_seconds=(
                                max(0, min(tab.last_activated_ago_seconds, 86_400))
                                if tab.last_activated_ago_seconds is not None
                                else None
                            ),
                        )
                    )
                record("browser_context.all_tabs[].tab_type", "bounded enum")
                record("browser_context.all_tabs[].is_active", "boolean")
                record("browser_context.all_tabs[].last_activated_ago_seconds", "bounded duration")
                record(
                    "browser_context.tab_type_classification",
                    source_browser.tab_type_classification,
                )
            if selection.user_goal and source_browser.focus_goal:
                cleaned_goal = redact_text(source_browser.focus_goal, max_chars=300)
                focus_goal = record(
                    "browser_context.focus_goal", cleaned_goal.value, cleaned_goal.redactions
                )
            if selection.browser_content:
                cleaned_excerpt = redact_text(
                    source_browser.active_tab_content_excerpt, max_chars=2_000
                )
                excerpt = record(
                    "browser_context.active_tab_content_excerpt",
                    cleaned_excerpt.value,
                    cleaned_excerpt.redactions,
                )
            else:
                excerpt = ""
            browser = BrowserContext(
                active_tab_title=active_title,
                active_tab_url="",
                active_tab_content_excerpt=excerpt,
                all_tabs=tabs,
                tab_type_classification=dict(Counter(tab.tab_type for tab in tabs)),
                focus_goal=focus_goal,
            )

        learned: dict[str, float] = {}
        if selection.learned_preferences:
            for raw_domain, score in list(context.learned_relevance.items())[:20]:
                domain = minimise_url(
                    raw_domain if "://" in raw_domain else f"https://{raw_domain}"
                )
                host = urlsplit(domain.value).hostname or "[domain omitted]"
                learned[host] = max(0.0, min(float(score), 1.0))
                record("learned_relevance", host, domain.redactions)

        if selection.support_estimate:
            outbound_state = _projected_state(state)
            record("state.state", str(state.state))
            record("state.confidence", round(state.confidence, 3))
            record("state.dwell_seconds", round(state.dwell_seconds, 1))
        else:
            outbound_state = _neutral_state(state)

        clean_extra = ""
        if selection.extra_context and extra_context:
            cleaned_extra = redact_text(extra_context, max_chars=2_000)
            clean_extra = record("extra_context", cleaned_extra.value, cleaned_extra.redactions)

        outbound = TaskContext(
            mode=mode,
            active_app=active_app,
            current_goal_hint=goal,
            complexity_score=complexity,
            editor_context=editor,
            terminal_context=terminal,
            browser_context=browser,
            learned_relevance=learned,
        )

        disclosures: list[ContextFieldDisclosure] = []
        for path, policy in CONTEXT_FIELD_CATALOG.items():
            selected = policy.selection_key is not None and bool(
                getattr(selection, policy.selection_key)
            )
            was_included = path in values
            count = redactions.get(path, 0)
            disposition = (
                "redacted"
                if was_included and count > 0
                else "included"
                if was_included
                else "omitted"
            )
            reason = values.get(path)
            if not was_included:
                reason = (
                    "not used by external planner"
                    if policy.selection_key is None
                    else "source unavailable"
                    if selected
                    else "not selected"
                )
            disclosures.append(
                ContextFieldDisclosure(
                    field_path=path,
                    classification=policy.classification,
                    origin=policy.origin,
                    disposition=disposition,
                    redaction_count=count,
                    value_preview=_preview_value(reason),
                )
            )

        return SanitizedContextBundle(
            context=outbound,
            state=outbound_state,
            extra_context=clean_extra,
            disclosures=tuple(disclosures),
            redaction_count=sum(redactions.values()),
        )


def provider_retention_disclosure(config: LLMConfig) -> ProviderRetentionDisclosure:
    """Return conservative provider copy; never infer account-level ZDR."""

    mode = config.privacy.provider_retention_mode
    if config.provider == "bedrock":
        destination = f"AWS Bedrock ({config.bedrock.aws_region})"
        url = "https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html"
        summary = (
            "Bedrock retention depends on the effective account/project mode and "
            "the selected model. AWS documents that default mode may retain data "
            "for abuse detection and that store=false alone is not a zero-data-"
            "retention guarantee. Cortex does not read or verify the effective mode."
        )
    elif config.provider == "vertex":
        destination = "Google Vertex AI"
        url = (
            "https://docs.cloud.google.com/vertex-ai/generative-ai/docs/"
            "vertex-ai-zero-data-retention"
        )
        summary = (
            "Vertex AI retention depends on model, feature, project configuration, "
            "terms, and abuse-monitoring eligibility. Some features retain prompts "
            "or responses and in-memory caching can have a separate TTL. Cortex "
            "does not verify the active project policy or claim zero retention."
        )
    else:
        destination = "Anthropic API"
        url = "https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data"
        summary = (
            "Anthropic documents a standard API deletion window of 30 days, with "
            "exceptions for contractual settings, usage-policy enforcement, law, "
            "and designated covered models. Cortex does not verify that your "
            "organization has a zero-data-retention agreement."
        )
    if mode == "zero_data_retention_contract":
        summary += (
            " Configuration says a zero-retention contract exists, but you must "
            "verify its scope in the provider account before sending."
        )
    return ProviderRetentionDisclosure(
        provider=config.provider,
        destination=destination,
        configured_mode=mode,
        summary=summary,
        documentation_url=url,
        verified_on="2026-08-25",
        account_contract_must_be_verified=True,
        zero_retention_asserted_by_cortex=False,
    )


def build_no_content_plan(*, reason: str = "no_content") -> InterventionPlan:
    """Deterministic plan that never reads or echoes workspace context."""

    return InterventionPlan(
        level="overlay_only",
        situation_summary="Workspace details stayed on this device.",
        headline="Choose one small next step",
        primary_focus="Continue the task you already chose",
        micro_steps=["Write down the next concrete action, then do only that"],
        hide_targets=[],
        ui_plan=UIPlan(
            dim_background=False,
            show_overlay=True,
            fold_unrelated_code=False,
            intervention_type="overlay_only",
        ),
        tone="supportive",
        suggested_actions=[],
        metadata={
            "source": "fallback",
            "fallback_reason": reason,
            "planner_mode": "no_content",
            "network_used": False,
        },
    )


class NoContentPlanner:
    """Network-free planner with no dependency on collected content."""

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
        disclosure_manifest: tuple[ContextFieldDisclosure, ...] | None = None,
        privacy_preview_id: str | None = None,
        privacy_confirmation: str | None = None,
    ) -> InterventionPlan:
        del context, state, constraints, template_name, extra_context
        del disclosure_manifest, privacy_preview_id, privacy_confirmation
        return build_no_content_plan()

    async def health_check(self) -> bool:
        return True


class PrivacyAwarePlanner:
    """One-time preview gate around the raw external transport primitive.

    ``inner`` may be ``None`` when external mode is configured but the
    provider credential was not available at daemon start (first-run
    BYOK). ``transport_factory`` then lets :meth:`reload_credentials`
    construct the transport lazily once the token has been saved, without
    a restart; the composition root (``create_llm_client``) supplies it.
    """

    def __init__(
        self,
        config: LLMConfig,
        inner: _ExternalPlanner | None,
        *,
        clock: Clock | None = None,
        broker: ContextBroker | None = None,
        transport_factory: Callable[[], _ExternalPlanner] | None = None,
    ) -> None:
        self._config = config
        self._inner = inner
        self._transport_factory = transport_factory
        self._clock = clock or SYSTEM_CLOCK
        self._broker = broker or ContextBroker()
        self._pending: OrderedDict[str, _PreparedPreview] = OrderedDict()
        self._lock = asyncio.Lock()

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def _cost_tracker(self) -> Any | None:
        return getattr(self._inner, "_cost_tracker", None)

    @property
    def transport_state(self) -> str:
        """``ready`` / ``credentials_missing`` / ``external_context_disabled``."""

        if not self._config.privacy.external_transport_enabled:
            return TRANSPORT_DISABLED
        if self._inner is None:
            return TRANSPORT_CREDENTIALS_MISSING
        return TRANSPORT_READY

    @property
    def worst_case_seconds(self) -> float:
        """Upper bound on one confirmed external request (delegates inward)."""

        inner_bound = getattr(self._inner, "worst_case_seconds", None)
        if isinstance(inner_bound, (int, float)) and not isinstance(inner_bound, bool):
            return float(inner_bound)
        return self._config.planner_worst_case_seconds

    def _require_transport(self) -> _ExternalPlanner:
        """Return the transport or raise with a reason callers can distinguish."""

        state = self.transport_state
        if state == TRANSPORT_DISABLED or self._inner is None:
            if state == TRANSPORT_CREDENTIALS_MISSING:
                raise ExternalContextDisabledError(
                    f"{TRANSPORT_CREDENTIALS_MISSING}: external planning is configured "
                    "but no provider credential is available; complete the BYOK step "
                    "(the planner reloads without a restart)"
                )
            raise ExternalContextDisabledError(
                f"{TRANSPORT_DISABLED}: external planning is disabled until the "
                "current context disclosure is acknowledged"
            )
        return self._inner

    @property
    def pending_preview_count(self) -> int:
        self._prune_expired()
        return len(self._pending)

    def _prune_expired(self) -> None:
        expired = [
            preview_id
            for preview_id, prepared in self._pending.items()
            if prepared.deadline.expired(self._clock)
        ]
        for preview_id in expired:
            self._pending.pop(preview_id, None)

    def _model_for_template(self, template_name: str) -> str:
        resolver = getattr(self._inner, "model_for_template", None)
        if callable(resolver):
            value = resolver(template_name)
            if isinstance(value, str) and value:
                return value
        return resolve_anthropic_model_id(
            self._config.model_default,
            provider=self._config.provider,
        )

    def privacy_status(self) -> ContextPrivacyStatusResponse:
        return ContextPrivacyStatusResponse.from_clock(
            self._clock,
            planner_mode=self._config.privacy.planner_mode,
            network_allowed_by_configuration=(
                self._config.privacy.external_transport_enabled and self._inner is not None
            ),
            pending_previews=self.pending_preview_count,
            provider=self._config.provider,
            retention=provider_retention_disclosure(self._config),
            transport_state=self.transport_state,
        )

    async def preview_external_request(
        self,
        request: ContextPreviewRequest,
    ) -> ContextPreviewResponse:
        self._require_transport()

        bundle = self._broker.sanitize(
            request.task_context,
            request.state_estimate,
            request.selection,
            extra_context=request.extra_context,
        )
        template_name = request.template_name or select_prompt_template(
            bundle.context,
            str(bundle.state.state),
        )
        if template_name not in PROMPT_TEMPLATES:
            raise ValueError(f"unknown prompt template {template_name!r}")
        user_prompt = build_user_prompt(
            bundle.context,
            bundle.state,
            request.constraints,
            template_name=template_name,
            extra_context=bundle.extra_context,
            disclosure_manifest=bundle.disclosures,
        )
        outbound_bytes = len(user_prompt.encode("utf-8"))
        if len(user_prompt) > 24_000 or outbound_bytes > 96_000:
            raise ValueError("redacted outbound prompt exceeds the hard privacy bound")

        digest = _request_digest(
            context=bundle.context,
            state=bundle.state,
            constraints=request.constraints,
            template_name=template_name,
            extra_context=bundle.extra_context,
            selection=request.selection,
        )
        preview_id = f"ctx_{secrets.token_urlsafe(32)}"
        duration_ms = self._config.privacy.preview_ttl_seconds * 1000
        deadline = BoundedDeadline.after(self._clock, duration_ms)
        prepared = _PreparedPreview(
            deadline=deadline,
            request_digest=digest,
            context=bundle.context,
            state=bundle.state,
            constraints=request.constraints,
            template_name=template_name,
            extra_context=bundle.extra_context,
            disclosures=bundle.disclosures,
            selection=request.selection.model_copy(deep=True),
        )

        async with self._lock:
            self._prune_expired()
            while len(self._pending) >= self._config.privacy.max_pending_previews:
                self._pending.popitem(last=False)
            self._pending[preview_id] = prepared

        omitted = sum(item.disposition == "omitted" for item in bundle.disclosures)
        return ContextPreviewResponse.from_clock(
            self._clock,
            preview_id=preview_id,
            request_digest=digest,
            expires_at_unix_ms=deadline.expires_at_unix_ms,
            provider=self._config.provider,
            model=self._model_for_template(template_name),
            template_name=template_name,
            retention=provider_retention_disclosure(self._config),
            selection=request.selection,
            outbound_context=bundle.context,
            outbound_user_prompt=user_prompt,
            field_disclosures=list(bundle.disclosures),
            redaction_count=bundle.redaction_count,
            omitted_field_count=omitted,
            outbound_utf8_bytes=outbound_bytes,
        )

    async def generate_intervention_plan(
        self,
        context: TaskContext,
        state: StateEstimate,
        constraints: SimplificationConstraints | None = None,
        *,
        template_name: str | None = None,
        extra_context: str = "",
        disclosure_manifest: tuple[ContextFieldDisclosure, ...] | None = None,
        privacy_preview_id: str | None = None,
        privacy_confirmation: str | None = None,
    ) -> InterventionPlan:
        # No caller may smuggle a self-authored manifest past the broker.
        del disclosure_manifest
        transport_state = self.transport_state
        if transport_state != TRANSPORT_READY or self._inner is None:
            return build_no_content_plan(reason=transport_state)
        if not privacy_preview_id:
            return build_no_content_plan(reason="context_preview_required")
        try:
            prepared = await self._consume_prepared_preview(
                privacy_preview_id,
                privacy_confirmation,
            )
        except PreviewAuthorizationError as exc:
            return build_no_content_plan(reason=str(exc))

        candidate = self._broker.sanitize(
            context,
            state,
            prepared.selection,
            extra_context=extra_context,
        )
        candidate_template = template_name or select_prompt_template(
            candidate.context,
            str(candidate.state.state),
        )
        digest = _request_digest(
            context=candidate.context,
            state=candidate.state,
            constraints=constraints,
            template_name=candidate_template,
            extra_context=candidate.extra_context,
            selection=prepared.selection,
        )
        if not secrets.compare_digest(digest, prepared.request_digest):
            return build_no_content_plan(reason="context_preview_payload_changed")

        return await self._inner.generate_intervention_plan(
            prepared.context,
            prepared.state,
            prepared.constraints,
            template_name=prepared.template_name,
            extra_context=prepared.extra_context,
            disclosure_manifest=prepared.disclosures,
        )

    async def _consume_prepared_preview(
        self,
        preview_id: str,
        confirmation: str | None,
    ) -> _PreparedPreview:
        """Burn and return one prepared payload before any external await.

        A wrong phrase deliberately consumes an otherwise valid handle.  This
        prevents a stale UI, compromised local client, or cancellation retry
        from turning a one-time gesture into reusable authority.
        """

        async with self._lock:
            prepared = self._pending.pop(preview_id, None)
        if prepared is None:
            raise PreviewAuthorizationError("context_preview_missing_or_replayed")
        if confirmation != CONTEXT_SEND_CONFIRMATION:
            raise PreviewAuthorizationError("context_preview_confirmation_invalid")
        if prepared.deadline.expired(self._clock):
            raise PreviewAuthorizationError("context_preview_expired")
        return prepared

    async def confirm_external_request(
        self,
        preview_id: str,
        confirmation: str,
    ) -> InterventionPlan:
        """Send the exact prepared payload once without re-reading context.

        This is the desktop privacy-sheet path.  The generic ``/llm/plan``
        contract still binds a caller-supplied body digest; the sheet instead
        confirms the already-inspected immutable broker snapshot directly.
        Neither path grants any workspace capability.
        """

        inner = self._require_transport()
        prepared = await self._consume_prepared_preview(preview_id, confirmation)
        return await inner.generate_intervention_plan(
            prepared.context,
            prepared.state,
            prepared.constraints,
            template_name=prepared.template_name,
            extra_context=prepared.extra_context,
            disclosure_manifest=prepared.disclosures,
        )

    async def cancel_preview(self, preview_id: str) -> bool:
        """Burn a prepared handle without contacting the provider.

        Cancellation is idempotent and intentionally reveals only whether the
        opaque handle was still live.  The bounded payload is removed while
        holding the same lock used by confirmation, so cancel and send cannot
        both win a race.
        """

        async with self._lock:
            return self._pending.pop(preview_id, None) is not None

    async def health_check(self) -> bool:
        # Local health is deliberately network-free; external connectivity is
        # learned only after a confirmed request, never by a background probe.
        return True

    def reload_credentials(self) -> bool:
        """Hot-reload provider credentials, building the transport if needed.

        First-run BYOK: the daemon started in external mode with no token,
        so ``inner`` is ``None``. Once the token is saved this constructs
        the transport through ``transport_factory`` instead of returning
        ``False`` and demanding a restart (audit D10). Returns ``True`` iff
        a working transport exists afterwards.
        """

        if self._inner is None:
            if not self._config.privacy.external_transport_enabled:
                return False
            if self._transport_factory is None:
                logger.warning("reload_credentials: no transport factory configured")
                return False
            try:
                self._inner = self._transport_factory()
            except RuntimeError as exc:
                logger.warning("reload_credentials: transport unavailable (%s)", exc)
                return False
            except Exception:
                logger.exception("reload_credentials: transport construction failed")
                return False
            logger.info("External planner transport constructed after credential reload")
            return True
        reload_method = getattr(self._inner, "reload_credentials", None)
        if not callable(reload_method):
            return False
        return bool(reload_method())

    def clear_previews(self) -> None:
        self._pending.clear()
