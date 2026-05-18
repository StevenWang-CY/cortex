"""Per-day LLM cost ledger + kill-switch (audit F20).

The Anthropic planner has no per-user / per-day spend visibility. A
state machine oscillating at the HYPER/FLOW boundary can drive 60+
planner calls per hour without an alert anywhere; on direct-Anthropic
pricing that compounds into a six-figure annual bill on a single jittery
user before anyone notices.

This module ships the missing telemetry + safety rail:

1. ``CostTracker.record`` writes a per-call ``LLM_COST`` log line
   (cid, model, USD estimate, cancelled flag, per-cid attribution),
   atomically appends to a per-day rolling ledger on disk, and exposes
   the ledger for the dashboard banner.
2. ``CostTracker.check_budget`` returns ``OK``/``WARN``/``KILL``. The
   planner consults this before every SDK call and short-circuits to
   the deterministic fallback plan when ``KILL`` fires, stamping
   ``plan.metadata["budget_killed"] = True`` so the UI can surface the
   reason.

Ledger persistence uses :func:`atomic_write_json` so a SIGKILL midway
through a write leaves the prior known-good ledger intact. The on-disk
shape is a dict keyed by ISO-8601 local date (``YYYY-MM-DD``); old days
are kept for 90 days (small enough to never bloat) so a post-hoc
debugging session can still show per-day spend.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cortex.libs.logging.structured import EventType
from cortex.libs.utils.atomic_write import atomic_write_json

logger = logging.getLogger(__name__)

BudgetState = Literal["OK", "WARN", "KILL"]

# Days of history retained in the on-disk ledger. Keeps the file small
# enough for an unbounded daemon run while preserving enough trail to
# investigate spend regressions across a quarter.
_RETENTION_DAYS: int = 90


def _today_iso(now: datetime | None = None) -> str:
    """Return the local-midnight calendar date as ``YYYY-MM-DD``."""
    return (now or datetime.now()).date().isoformat()


def _prune_old(
    days: dict[str, dict[str, Any]],
    *,
    today: date,
    retention_days: int = _RETENTION_DAYS,
) -> dict[str, dict[str, Any]]:
    """Drop entries older than ``retention_days`` from the ledger.

    Tolerates ill-formed date keys (e.g. a manually-edited ledger).
    """
    cutoff = today - timedelta(days=retention_days)
    kept: dict[str, dict[str, Any]] = {}
    for key, value in days.items():
        try:
            d = date.fromisoformat(key)
        except ValueError:
            continue
        if d >= cutoff:
            kept[key] = value
    return kept


class CostTracker:
    """Tracks per-day LLM spend and enforces a soft + hard daily budget.

    Thread-safe via an internal lock — the daemon's asyncio code paths
    call ``record`` from the LLM coroutine and ``check_budget`` from the
    triage path; both can interleave under cancellation timing.

    Args:
        ledger_path: Where to persist the per-day rolling sum. Defaults
            to ``<config_dir>/cost_ledger.json``.
        warn_usd: Per-day spend above this fires a single ``WARN`` event
            and keeps returning ``WARN`` until the day rolls over.
        kill_usd: Per-day spend at or above this fires ``LLM_BUDGET_KILL``
            and forces the planner into deterministic fallback for the
            rest of the day.
    """

    def __init__(
        self,
        ledger_path: Path,
        *,
        warn_usd: float = 5.0,
        kill_usd: float = 20.0,
    ) -> None:
        if warn_usd <= 0 or kill_usd <= 0:
            raise ValueError("warn_usd and kill_usd must be positive USD amounts")
        if kill_usd < warn_usd:
            raise ValueError("kill_usd must be >= warn_usd")
        self._ledger_path = ledger_path
        self._warn_usd = float(warn_usd)
        self._kill_usd = float(kill_usd)
        self._lock = threading.Lock()
        self._days: dict[str, dict[str, Any]] = self._load()
        self._warned_today: str | None = None
        self._killed_today: str | None = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self._ledger_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "cost_tracker: ledger %s unreadable (%s); starting empty",
                self._ledger_path,
                type(exc).__name__,
            )
            return {}
        if not isinstance(data, dict):
            return {}
        # Normalise: drop entries with non-dict values and bound history.
        out: dict[str, dict[str, Any]] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, dict):
                out[k] = v
        return _prune_old(out, today=date.today())

    def _flush(self) -> None:
        try:
            atomic_write_json(self._ledger_path, self._days)
        except OSError as exc:
            logger.warning(
                "cost_tracker: failed to flush ledger to %s (%s)",
                self._ledger_path,
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _day(self, key: str) -> dict[str, Any]:
        entry = self._days.get(key)
        if entry is None:
            entry = {"total_usd": 0.0, "calls": 0, "by_cid": {}}
            self._days[key] = entry
        # Defensive coercion: if a manually-edited ledger lost a sub-field.
        entry.setdefault("total_usd", 0.0)
        entry.setdefault("calls", 0)
        entry.setdefault("by_cid", {})
        return entry

    def record(
        self,
        cid: str | None,
        model: str,
        usd: float,
        *,
        cancelled: bool = False,
        now: datetime | None = None,
    ) -> None:
        """Append a single LLM call's cost to today's bucket.

        Emits an ``LLM_COST`` structured log line for the aggregator;
        the on-disk ledger is the durable form for cross-restart spend.
        """
        if usd < 0:
            raise ValueError(f"usd must be non-negative; got {usd!r}")
        cid_key = cid or "-"
        moment = now or datetime.now()
        today = _today_iso(moment)
        with self._lock:
            day = self._day(today)
            day["total_usd"] = float(day["total_usd"]) + float(usd)
            day["calls"] = int(day["calls"]) + 1
            by_cid = day["by_cid"]
            if cid_key not in by_cid:
                by_cid[cid_key] = {"total_usd": 0.0, "calls": 0}
            by_cid[cid_key]["total_usd"] = (
                float(by_cid[cid_key].get("total_usd", 0.0)) + float(usd)
            )
            by_cid[cid_key]["calls"] = (
                int(by_cid[cid_key].get("calls", 0)) + 1
            )
            # Prune in-memory at every write so a long-running daemon
            # never accumulates beyond the retention window.
            self._days = _prune_old(self._days, today=moment.date())
            self._flush()
        logger.info(
            "%s cid=%s model=%s usd=%.6f cancelled=%s day_total=%.6f",
            EventType.LLM_COST.value,
            cid_key,
            model,
            usd,
            cancelled,
            self.today_total_usd(now=moment),
        )

    def today_total_usd(self, *, now: datetime | None = None) -> float:
        """Return the running spend for the current local day."""
        today = _today_iso(now)
        with self._lock:
            entry = self._days.get(today)
            return float(entry["total_usd"]) if entry else 0.0

    def per_cid_today(
        self,
        cid: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, float | int]:
        """Return the per-cid spend for the current local day.

        Used by support tooling: given a correlation id, how much did
        that single user-action chain cost?
        """
        today = _today_iso(now)
        with self._lock:
            entry = self._days.get(today)
            if not entry:
                return {"total_usd": 0.0, "calls": 0}
            by_cid = entry.get("by_cid", {})
            sub = by_cid.get(cid)
            if not sub:
                return {"total_usd": 0.0, "calls": 0}
            return {
                "total_usd": float(sub.get("total_usd", 0.0)),
                "calls": int(sub.get("calls", 0)),
            }

    def check_budget(self, *, now: datetime | None = None) -> BudgetState:
        """Return the current budget state for the local day.

        ``WARN`` and ``KILL`` events are emitted at most once per day so
        a high-frequency caller does not spam the log aggregator.
        """
        moment = now or datetime.now()
        today = _today_iso(moment)
        total = self.today_total_usd(now=moment)
        if total >= self._kill_usd:
            if self._killed_today != today:
                self._killed_today = today
                logger.error(
                    "%s usd=%.4f kill_usd=%.4f",
                    EventType.LLM_BUDGET_KILL.value,
                    total,
                    self._kill_usd,
                )
            return "KILL"
        if total >= self._warn_usd:
            if self._warned_today != today:
                self._warned_today = today
                logger.warning(
                    "llm.budget.warn usd=%.4f warn_usd=%.4f kill_usd=%.4f",
                    total,
                    self._warn_usd,
                    self._kill_usd,
                )
            return "WARN"
        return "OK"


__all__ = ["BudgetState", "CostTracker"]
