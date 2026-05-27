"""P0 §3.13 — Persistent goal store for the desktop dashboard.

Goals entered by the user in the dashboard's "What are you working on?"
field are saved here so the next session can offer a recent-goals
dropdown above the input (instead of forcing the user to retype the
same phrase every day).

Storage layout
--------------

Goals live in a single JSON file at
``<config_dir>/goals.json`` (resolved via
:func:`cortex.libs.utils.platform.get_config_dir`). The file is a list
of :class:`Goal` records sorted descending by ``last_used_at`` — the
most recently used goal is element 0. The store keeps at most
:data:`MAX_GOALS` records; older entries are dropped on the next write.

Concurrency
-----------

Writes use :func:`atomic_write_json` so a SIGKILL mid-write cannot
truncate the file. The store is intentionally *not* a database — the
volume is small (≤ 50 entries × ~150 bytes) and the access pattern
(read on dashboard open, write on Enter) does not warrant SQLite.

The store does **not** stamp ``goal_id`` onto an in-progress session;
it merely retains the user-typed history. Session-attribution is the
daemon's responsibility (Phase 4b owns the SessionReport schema). The
desktop side re-emits the active ``goal_title`` via a ``GOAL_SET`` WS
message every session start; Phase 4b's daemon then stamps it onto the
session report.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cortex.libs.utils.atomic_write import atomic_write_json
from cortex.libs.utils.platform import get_config_dir

logger = logging.getLogger(__name__)

# Upper bound on how many distinct goals we retain. The dashboard
# dropdown shows the top ~8; beyond 50 the list is just noise and
# slows down JSON serialisation marginally.
MAX_GOALS: int = 50

# Maximum length of a goal title — matches the daemon's
# ``SessionReport.goal_title`` field length budget. Anything over this
# is silently truncated rather than rejected so the dashboard never
# loses user input.
MAX_TITLE_LENGTH: int = 200

_FILE_NAME = "goals.json"

# Module-level lock so concurrent add_goal calls (e.g. dashboard +
# WS-sync) don't race the read-modify-write.
_lock = threading.Lock()


@dataclass(frozen=True)
class Goal:
    """One user-entered goal record.

    The record is *frozen* so callers cannot mutate it after handing it
    out; the store always produces fresh instances on mutation.
    """

    id: str
    title: str
    created_at: datetime
    last_used_at: datetime
    sessions_count: int = 0
    # Open-ended metadata bag — the daemon may stamp a focus_score
    # average or other derived fields onto a goal record. Reserved for
    # future Phase 4b coordination; the store passes it through
    # transparently.
    metadata: dict = field(default_factory=dict)

    # ---- (de)serialisation helpers ----

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat(),
            "sessions_count": int(self.sessions_count),
            "metadata": dict(self.metadata or {}),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Goal:
        return cls(
            id=str(raw.get("id") or uuid.uuid4().hex),
            title=str(raw.get("title") or "")[:MAX_TITLE_LENGTH],
            created_at=_parse_dt(raw.get("created_at")),
            last_used_at=_parse_dt(raw.get("last_used_at")),
            sessions_count=int(raw.get("sessions_count") or 0),
            metadata=dict(raw.get("metadata") or {}),
        )


def _parse_dt(value: object) -> datetime:
    """Parse an ISO-8601 timestamp (with fallback to now)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
        # Coerce naive datetimes to UTC so comparisons + downstream
        # marshalling never bomb on "compare aware to naive" errors.
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt
    return datetime.now(UTC)


def _store_path() -> Path:
    return get_config_dir() / _FILE_NAME


def _normalise_title(title: str) -> str:
    """Strip whitespace + clamp to MAX_TITLE_LENGTH."""
    return (title or "").strip()[:MAX_TITLE_LENGTH]


def load_goals(*, path: Path | None = None) -> list[Goal]:
    """Read the persisted goals file and return the goals sorted by
    ``last_used_at`` descending (newest first). Returns an empty list
    when the file is missing or unreadable — the caller's UX is the
    same in either case.
    """
    target = path or _store_path()
    if not target.exists():
        return []
    try:
        with target.open("r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "goal store at %s is unreadable; returning empty list",
            target,
            exc_info=True,
        )
        return []
    items_raw = raw.get("goals") if isinstance(raw, dict) else raw
    if not isinstance(items_raw, list):
        return []
    goals: list[Goal] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        try:
            goals.append(Goal.from_dict(item))
        except Exception:
            logger.debug("skipping malformed goal record: %r", item, exc_info=True)
    goals.sort(key=lambda g: g.last_used_at, reverse=True)
    return goals[:MAX_GOALS]


def _save_goals(goals: list[Goal], *, path: Path | None = None) -> None:
    target = path or _store_path()
    payload = {
        "version": 1,
        "goals": [g.to_dict() for g in goals[:MAX_GOALS]],
    }
    atomic_write_json(target, payload)


def add_goal(title: str, *, path: Path | None = None) -> Goal:
    """Create or refresh a goal record for ``title``.

    If a record with the same (case-insensitive, whitespace-trimmed)
    title already exists, its ``last_used_at`` is bumped and the
    existing id is preserved. Otherwise a fresh record is minted.
    Returns the resulting Goal record (always non-None).
    """
    cleaned = _normalise_title(title)
    if not cleaned:
        raise ValueError("goal title cannot be empty")
    now = datetime.now(UTC)
    with _lock:
        existing = load_goals(path=path)
        match: Goal | None = None
        for g in existing:
            if g.title.lower() == cleaned.lower():
                match = g
                break
        if match is not None:
            updated = Goal(
                id=match.id,
                title=cleaned,
                created_at=match.created_at,
                last_used_at=now,
                sessions_count=match.sessions_count,
                metadata=match.metadata,
            )
            others = [g for g in existing if g.id != match.id]
            new_list = [updated, *others]
        else:
            new = Goal(
                id=uuid.uuid4().hex,
                title=cleaned,
                created_at=now,
                last_used_at=now,
                sessions_count=0,
                metadata={},
            )
            new_list = [new, *existing]
            updated = new
        _save_goals(new_list[:MAX_GOALS], path=path)
        return updated


def mark_used(goal_id: str, *, path: Path | None = None) -> Goal | None:
    """Increment ``sessions_count`` and bump ``last_used_at`` for a
    goal. Returns the updated Goal or ``None`` if no record matches.
    """
    if not goal_id:
        return None
    now = datetime.now(UTC)
    with _lock:
        existing = load_goals(path=path)
        target: Goal | None = None
        rest: list[Goal] = []
        for g in existing:
            if g.id == goal_id and target is None:
                target = g
            else:
                rest.append(g)
        if target is None:
            return None
        updated = Goal(
            id=target.id,
            title=target.title,
            created_at=target.created_at,
            last_used_at=now,
            sessions_count=int(target.sessions_count) + 1,
            metadata=target.metadata,
        )
        new_list = [updated, *rest]
        _save_goals(new_list[:MAX_GOALS], path=path)
        return updated


def delete_goal(goal_id: str, *, path: Path | None = None) -> bool:
    """Remove a goal by id. Returns True if a record was deleted."""
    if not goal_id:
        return False
    with _lock:
        existing = load_goals(path=path)
        filtered = [g for g in existing if g.id != goal_id]
        if len(filtered) == len(existing):
            return False
        _save_goals(filtered, path=path)
        return True


__all__ = [
    "Goal",
    "MAX_GOALS",
    "MAX_TITLE_LENGTH",
    "add_goal",
    "delete_goal",
    "load_goals",
    "mark_used",
]
