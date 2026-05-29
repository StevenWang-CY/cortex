r"""
Cortex Session-History Reader (P0 §3.1).

Walks ``storage/sessions/`` and exposes paginated / single-session read
access for the History tab (desktop shell) and the REST/WebSocket
``REQUEST_SESSION_LIST`` / ``REQUEST_SESSION_DETAIL`` handlers.

Design decisions (from P0_IMPLEMENTATION_DESIGN.md §3.1):

* One source of truth: disk. The reader never caches a SessionReport
  body; it only caches the cheap header (start_time, flow %, ...) so
  the listing can be re-paginated without re-parsing every nested
  StateTransition / ActivitySummary list.
* The cache is keyed on the file's ``mtime``: if the on-disk file is
  newer than what we cached, we re-parse the header. A daemon write
  followed by ``SessionReader.invalidate(session_id)`` therefore makes
  the new entry immediately visible to the next ``list_sessions`` call,
  without restarting the daemon.
* Listing is resilient to malformed JSON — bad files are logged and
  skipped, not crashed-on. A heavy user might have a partial write
  from a SIGKILL; we still want to show the other 364 sessions.
* Sort order is ``start_time`` descending (newest first) with ties
  broken by ``session_id`` for deterministic pagination.
* Pagination is forward-only via ``since`` (epoch seconds). The first
  row of a page is the newest with ``start_time < since`` (or all rows
  if ``since`` is None). When a tied ``start_time`` sits on the page
  boundary the optional ``cursor_session_id`` carries the previous
  page's tail id so we resume *after* it instead of dropping it (fix
  #10).
* ``session_id`` is validated against the shared
  :data:`cortex.libs.schemas.session_history.SESSION_ID_PATTERN` before
  constructing any filesystem path (defense vs path traversal).

Public API:

* :class:`SessionReader` — instance bound to a sessions directory.
* :class:`_ListingCacheEntry` — internal cache row (one per session file).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cortex.libs.schemas.session_history import (
    SESSION_ID_PATTERN,
    SessionDetailResponse,
    SessionListResponse,
    SessionSummary,
)
from cortex.services.session_report.models import SessionReport

logger = logging.getLogger(__name__)


# Shared with the route + WS dispatch arm so the validation contract
# lives in exactly one place (see P0 §3.1 fix #11).
_SESSION_ID_RE: re.Pattern[str] = re.compile(SESSION_ID_PATTERN)

# Default page size for ``list_sessions`` when the caller passes 0 or
# None. The websocket handler clamps to [1, 100] so this is only the
# in-process default for direct callers (tests).
_DEFAULT_LIMIT: int = 30
_MAX_LIMIT: int = 100


@dataclass(frozen=True)
class _ListingCacheEntry:
    """In-memory header projection of a ``session_<id>.json`` file.

    Cheap to materialise (parse the JSON once, project the seven
    listing fields, drop the rest). ``mtime`` is the file's stat
    ``st_mtime`` at parse time; the cache lookup compares the current
    file ``mtime`` to this value to detect on-disk mutation.
    """

    session_id: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    flow_percentage: float
    peak_stress_integral: float
    top_distraction_domain: str | None
    intervention_count: int
    path: Path
    mtime: float


def _coerce_datetime(value: Any) -> datetime | None:
    """Best-effort parse of a datetime that may be ISO string, float epoch,
    or already a ``datetime`` instance. Returns ``None`` on failure.

    The on-disk format is ``model_dump(mode="json")`` which serialises
    datetimes as ISO strings; we accept floats too for forward-compat
    with future writers that may use epoch seconds.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            # ``fromisoformat`` accepts both naive and ``+HH:MM`` suffix.
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


def _project_header(path: Path, mtime: float) -> _ListingCacheEntry | None:
    """Parse the file header into a cache entry, or return ``None`` if
    the file is malformed / unreadable.

    Only the listing fields are pulled out; ``state_transitions`` and
    ``top_activities`` are NOT walked here — they're only touched by
    :meth:`SessionReader.read_session`.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("session-reader: cannot read %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("session-reader: malformed JSON %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("session-reader: unexpected root type for %s", path)
        return None

    # Phase-4a Debt-1: observe legacy documents written before the
    # ``schema_version`` field was added so we can bound migration risk.
    # Reader semantics are unchanged — a missing key implicitly means
    # version 1, but we surface a warning so silent drift is visible.
    if "schema_version" not in data:
        logger.warning(
            "session-reader: legacy session document without schema_version: %s",
            path,
        )

    session_id = data.get("session_id")
    start_time = _coerce_datetime(data.get("start_time"))
    end_time = _coerce_datetime(data.get("end_time"))
    if not isinstance(session_id, str) or start_time is None or end_time is None:
        logger.warning(
            "session-reader: skipping %s (missing/invalid id or timestamps)",
            path,
        )
        return None

    # Wave-2 P1 (audit §3.1): the schema declares ``duration_seconds`` as
    # required and ``flow_percentage`` defaults to 0.0 only for legitimately
    # zero-flow short sessions. A *missing* key on disk is storage
    # corruption, not zero flow — log a WARNING with the path and skip
    # the row so the History tab surfaces the gap rather than rendering
    # a deceptive 0.00-flow tile that masks the malformed write.
    if "duration_seconds" not in data or data.get("duration_seconds") is None:
        logger.warning(
            "session-reader: skipping %s (required field 'duration_seconds' missing/None)",
            path,
        )
        return None
    if "flow_percentage" not in data or data.get("flow_percentage") is None:
        logger.warning(
            "session-reader: skipping %s (required field 'flow_percentage' missing/None)",
            path,
        )
        return None

    try:
        duration_seconds = float(data["duration_seconds"])
    except (TypeError, ValueError):
        logger.warning(
            "session-reader: skipping %s (duration_seconds=%r not coercible to float)",
            path,
            data.get("duration_seconds"),
        )
        return None
    try:
        flow_percentage = float(data["flow_percentage"])
    except (TypeError, ValueError):
        logger.warning(
            "session-reader: skipping %s (flow_percentage=%r not coercible to float)",
            path,
            data.get("flow_percentage"),
        )
        return None
    # ``peak_stress_integral`` is allowed to default — the schema
    # carries ``0.0`` as a legitimate value for sessions that never
    # crossed a HYPER peak. Missing key → 0.0 is fine; a malformed
    # type is still grounds to skip (defense vs. partial writes).
    try:
        peak_stress_integral = float(data.get("peak_stress_integral") or 0.0)
    except (TypeError, ValueError):
        logger.warning(
            "session-reader: skipping %s (peak_stress_integral=%r not coercible to float)",
            path,
            data.get("peak_stress_integral"),
        )
        return None

    domains = data.get("top_distraction_domains") or []
    top_distraction_domain: str | None = None
    if isinstance(domains, list) and domains:
        first = domains[0]
        if isinstance(first, str) and first:
            top_distraction_domain = first

    # Intervention proxy: count state transitions whose target is HYPER.
    # This mirrors the contract the spec explicitly calls out — the
    # daemon doesn't persist an ``interventions_triggered`` counter on
    # SessionReport yet, so the count of HYPER entries is the best
    # proxy available without a schema bump.
    intervention_count = 0
    transitions = data.get("state_transitions") or []
    if isinstance(transitions, list):
        for t in transitions:
            if isinstance(t, dict) and t.get("to_state") == "HYPER":
                intervention_count += 1

    return _ListingCacheEntry(
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration_seconds,
        flow_percentage=flow_percentage,
        peak_stress_integral=peak_stress_integral,
        top_distraction_domain=top_distraction_domain,
        intervention_count=intervention_count,
        path=path,
        mtime=mtime,
    )


class SessionReader:
    """Paginated reader over ``storage/sessions/session_<id>.json``.

    Lives for the lifetime of the daemon. The constructor does NOT
    walk the directory eagerly — the first ``list_sessions`` call
    populates the cache, and subsequent calls re-walk and use mtimes
    to decide whether to re-parse each file.

    Thread-safety: every method holds the GIL throughout (no awaits,
    no threading primitives) so it is safe to call from a
    ``asyncio.to_thread(...)`` offload. The daemon does exactly that.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir
        # Keyed by session_id so ``invalidate(session_id)`` is O(1).
        # The path is stored on the entry; we re-walk the directory on
        # every ``_scan()`` so newly-arrived files are discovered.
        self._cache: dict[str, _ListingCacheEntry] = {}
        # Names of malformed files we've already warned about during
        # this daemon's lifetime — bounds log spam to one line per file
        # rather than one per ``list_sessions`` call (fix #12).
        self._warned_names: set[str] = set()

    # ── Public API ────────────────────────────────────────────────────

    def invalidate(self, session_id: str | None = None) -> None:
        """Drop ``session_id`` from the cache (or the whole cache if ``None``).

        The daemon calls this right after persisting a freshly-finished
        report so the next ``list_sessions`` call re-parses it.
        """
        if session_id is None:
            self._cache.clear()
            return
        self._cache.pop(session_id, None)

    def list_sessions(
        self,
        since: float | None,
        limit: int,
        *,
        cursor_session_id: str | None = None,
    ) -> SessionListResponse:
        """Return a paginated listing of sessions.

        Sorted by ``start_time`` desc; ties broken by ``session_id``
        for determinism. ``limit`` is clamped to [1, _MAX_LIMIT].

        ``cursor_session_id`` is the canonical tie-breaker companion to
        ``since``. When a previous page ended on the older of two rows
        that share a ``start_time``, the next call passes that row's
        ``session_id`` so we resume *after* it instead of either
        dropping it (a strict ``<`` filter) or duplicating it
        (a ``<=`` filter without a tiebreak). See P0 §3.1 fix #10.
        """
        entries = self._scan()
        # Newest first; deterministic tie-break.
        entries.sort(
            key=lambda e: (e.start_time, e.session_id),
            reverse=True,
        )

        if since is not None:
            try:
                cursor: float | None = float(since)
            except (TypeError, ValueError):
                cursor = None
            if cursor is not None:
                # P0 §3.1 fix #9: guard fromtimestamp against extreme
                # ``since`` values. A caller-supplied epoch second that
                # exceeds the platform's representable range raises
                # OverflowError/OSError/ValueError; we treat any of
                # those as "no cursor" so a hostile input degrades to
                # the first page rather than crashing the daemon.
                try:
                    cursor_dt: datetime | None = datetime.fromtimestamp(
                        cursor, tz=UTC,
                    )
                except (OverflowError, OSError, ValueError):
                    logger.debug(
                        "session-reader: invalid since=%r; treating as None",
                        since,
                    )
                    cursor_dt = None
                if cursor_dt is not None:
                    # P0 §3.1 fix #10: strict ``<`` would skip ties on
                    # the boundary; ``<=`` would re-emit the tail row.
                    # Resolve by emitting entries strictly older than
                    # ``cursor_dt`` OR equal but with a session_id that
                    # sorts before the supplied ``cursor_session_id``
                    # (descending sort → "after the cursor row" means
                    # session_id < cursor_session_id when start_time ties).
                    if cursor_session_id is None:
                        entries = [e for e in entries if e.start_time < cursor_dt]
                    else:
                        entries = [
                            e
                            for e in entries
                            if e.start_time < cursor_dt
                            or (
                                e.start_time == cursor_dt
                                and e.session_id < cursor_session_id
                            )
                        ]

        clamped_limit = max(
            1,
            min(_MAX_LIMIT, int(limit) if limit else _DEFAULT_LIMIT),
        )
        page = entries[:clamped_limit]

        items = [
            SessionSummary(
                session_id=e.session_id,
                start_time=e.start_time,
                end_time=e.end_time,
                duration_seconds=e.duration_seconds,
                flow_percentage=e.flow_percentage,
                peak_stress_integral=e.peak_stress_integral,
                top_distraction_domain=e.top_distraction_domain,
                intervention_count=e.intervention_count,
            )
            for e in page
        ]

        # ``next_cursor`` is the oldest row's start_time in this page.
        # If we returned fewer rows than the limit there are no more
        # rows after this page. We also surface ``cursor_session_id``
        # so the caller can pass it back and resume after a tied row.
        next_cursor: float | None = None
        next_cursor_session_id: str | None = None
        if len(page) == clamped_limit and len(entries) > clamped_limit:
            next_cursor = page[-1].start_time.timestamp()
            next_cursor_session_id = page[-1].session_id

        return SessionListResponse(
            items=items,
            next_cursor=next_cursor,
            cursor_session_id=next_cursor_session_id,
            total_known=len(self._cache),
        )

    def read_session(self, session_id: str) -> SessionDetailResponse:
        """Return the full ``SessionReport`` for one id.

        Validates the id against the safe-char regex before constructing
        any path. On missing / unparsable file, returns an envelope
        with ``report=None, error="not_found" | "unreadable"``.
        """
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            logger.warning("session-reader: rejecting invalid session_id %r", session_id)
            return SessionDetailResponse(report=None, error="not_found")

        path = self._sessions_dir / f"session_{session_id}.json"
        if not path.is_file():
            return SessionDetailResponse(report=None, error="not_found")

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("session-reader: cannot read %s: %s", path, exc)
            return SessionDetailResponse(report=None, error="unreadable")

        try:
            report = SessionReport.model_validate_json(raw)
        except Exception:
            logger.warning(
                "session-reader: failed to parse SessionReport at %s",
                path,
                exc_info=True,
            )
            return SessionDetailResponse(report=None, error="unreadable")

        return SessionDetailResponse(report=report, error=None)

    # ── Internal helpers ──────────────────────────────────────────────

    def _warn_once(self, name: str, fmt: str, *args: Any) -> None:
        """Emit ``logger.warning(fmt, *args)`` only the first time we see
        ``name``. Keeps daemon.log readable when a stray hostile file
        sits in storage/sessions for weeks (fix #12)."""
        if name in self._warned_names:
            return
        self._warned_names.add(name)
        logger.warning(fmt, *args)

    def _scan(self) -> list[_ListingCacheEntry]:
        """Walk ``sessions_dir`` and return one cache entry per file.

        Uses mtime-keyed invalidation: a cached entry whose file mtime
        has changed since cache time is re-parsed. Files that have
        disappeared since last scan are dropped from the cache.
        """
        if not self._sessions_dir.exists() or not self._sessions_dir.is_dir():
            return []

        seen_ids: set[str] = set()
        entries: list[_ListingCacheEntry] = []
        try:
            children = list(self._sessions_dir.iterdir())
        except OSError as exc:
            logger.warning(
                "session-reader: cannot iterate %s: %s",
                self._sessions_dir,
                exc,
            )
            return []

        for path in children:
            if not path.is_file() or path.suffix != ".json":
                continue
            name = path.name
            if not name.startswith("session_"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            session_id = name[len("session_") : -len(".json")]
            if not _SESSION_ID_RE.match(session_id):
                # Skip files whose id doesn't match the safe-char regex
                # (defense against a hostile file dropped into the dir).
                # Log at most once per filename per daemon lifetime so
                # daemon.log doesn't bloat on every list_sessions call.
                self._warn_once(
                    name,
                    "session-reader: skipping %s (id fails safe-char regex)",
                    name,
                )
                continue
            cached = self._cache.get(session_id)
            if cached is not None and cached.mtime == stat.st_mtime and cached.path == path:
                seen_ids.add(session_id)
                entries.append(cached)
                continue
            entry = _project_header(path, stat.st_mtime)
            if entry is None:
                # Malformed file; remove any stale cache row and keep going.
                # _project_header already logged at WARNING — bound the
                # repeat rate by registering the filename so subsequent
                # scans don't re-log.
                self._warned_names.add(name)
                self._cache.pop(session_id, None)
                continue
            self._cache[session_id] = entry
            seen_ids.add(session_id)
            entries.append(entry)

        # Drop cache rows whose backing files have disappeared.
        for stale in list(self._cache.keys()):
            if stale not in seen_ids:
                self._cache.pop(stale, None)

        return entries


__all__ = ["SessionReader", "_ListingCacheEntry"]
