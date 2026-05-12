"""
Eval — Tab Relevance Tracker

Learns per-domain relevance from user feedback on tab close recommendations.
When users dismiss close recommendations or undo tab closures, the domain's
relevance score increases for the current goal context. When users confirm
closures, it decreases.

Uses exponential moving average (alpha=0.3) per (domain, goal_keywords) pair.
Stored in the shared Store with 90-day TTL.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_ALPHA = 0.3  # EMA smoothing factor
_TTL_SECONDS = 90 * 86400  # 90 days
_DEFAULT_RELEVANCE = 0.5  # Neutral prior


def _extract_domain(url: str) -> str:
    """Extract the base domain from a URL."""
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.")
    except Exception:
        return ""


def _goal_hash(goal: str) -> str:
    """Create a stable hash from goal keywords for storage key."""
    words = sorted(set(goal.lower().split()))
    key = " ".join(w for w in words if len(w) > 1)
    return hashlib.md5(key.encode()).hexdigest()[:8]


def _store_key(domain: str, goal: str) -> str:
    return f"tab_relevance:{domain}:{_goal_hash(goal)}"


class TabRelevanceTracker:
    """Learns per-domain relevance from user feedback on tab close recommendations.

    Also tracks modality preferences (video vs text vs interactive) per topic,
    enabling proactive resource switching when zombie-reading is detected.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    async def get_domain_relevance(self, domain: str, goal: str) -> float:
        """Returns learned relevance score [0, 1] for domain+goal pair."""
        if not domain or not goal:
            return _DEFAULT_RELEVANCE
        data = await self._store.get_json(_store_key(domain, goal))
        if data is None:
            return _DEFAULT_RELEVANCE
        return float(data.get("relevance", _DEFAULT_RELEVANCE))

    async def record_kept(self, url: str, goal: str) -> None:
        """User dismissed/undid a close recommendation — domain is relevant."""
        domain = _extract_domain(url)
        if not domain or not goal:
            return
        await self._update(domain, goal, kept=True)

    async def record_closed(self, url: str, goal: str) -> None:
        """User confirmed closing a tab — domain may be less relevant."""
        domain = _extract_domain(url)
        if not domain or not goal:
            return
        await self._update(domain, goal, kept=False)

    async def get_overrides(self, goal: str) -> dict[str, float]:
        """Get all learned domain relevance overrides for a goal.

        Returns a dict of {domain: relevance_score} for domains where
        the user has expressed a preference (score != default).
        """
        if not goal:
            return {}
        # Scan known domains from recent interactions
        f":{_goal_hash(goal)}"
        overrides: dict[str, float] = {}

        # Use store's scan if available, otherwise check cached domains
        cached = await self._store.get_json("tab_relevance_domains")
        if cached and isinstance(cached.get("domains"), list):
            for domain in cached["domains"]:
                score = await self.get_domain_relevance(domain, goal)
                if abs(score - _DEFAULT_RELEVANCE) > 0.05:
                    overrides[domain] = score
        return overrides

    async def _update(self, domain: str, goal: str, *, kept: bool) -> None:
        """Update relevance score using exponential moving average."""
        key = _store_key(domain, goal)
        data = await self._store.get_json(key)
        current = float(data["relevance"]) if data else _DEFAULT_RELEVANCE
        target = 1.0 if kept else 0.0
        new_score = current * (1 - _ALPHA) + target * _ALPHA
        await self._store.set_json(
            key, {"relevance": round(new_score, 4), "domain": domain}, _TTL_SECONDS
        )
        logger.info(
            "Tab relevance updated: %s (goal=%s) %.2f → %.2f (%s)",
            domain, goal[:30], current, new_score, "kept" if kept else "closed",
        )
        # Track known domains for get_overrides scanning
        await self._track_domain(domain)

    async def record_modality_engagement(
        self, topic: str, modality: str, engagement_s: float,
    ) -> None:
        """Record time spent in a learning modality for a topic.

        When zombie-reading fires, the system can query preferred modalities
        and suggest switching to a format the user engages with better.

        Args:
            topic: Topic tag (e.g., "algorithms", "calculus").
            modality: One of "video", "text", "interactive", "code", "other".
            engagement_s: Seconds of productive engagement in this modality.
        """
        if not topic or not modality:
            return
        key = f"modality_pref:{_goal_hash(topic)}:{modality}"
        data = await self._store.get_json(key)
        current = float(data.get("total_s", 0.0)) if data else 0.0
        await self._store.set_json(
            key,
            {"total_s": current + engagement_s, "modality": modality, "topic": topic},
            _TTL_SECONDS,
        )

    async def get_preferred_modality(self, topic: str) -> str | None:
        """Get the user's preferred learning modality for a topic.

        Returns the modality with the most engagement time, or None
        if insufficient data.
        """
        if not topic:
            return None
        modalities = ["video", "text", "interactive", "code"]
        best: str | None = None
        best_time = 0.0
        for mod in modalities:
            key = f"modality_pref:{_goal_hash(topic)}:{mod}"
            data = await self._store.get_json(key)
            if data:
                t = float(data.get("total_s", 0.0))
                if t > best_time:
                    best_time = t
                    best = mod
        # Need at least 5 minutes of data
        return best if best_time >= 300.0 else None

    async def _track_domain(self, domain: str) -> None:
        """Keep a list of known domains for override scanning."""
        data = await self._store.get_json("tab_relevance_domains")
        domains: list[str] = data.get("domains", []) if data else []
        if domain not in domains:
            domains.append(domain)
            # Cap at 200 domains
            if len(domains) > 200:
                domains = domains[-200:]
            await self._store.set_json(
                "tab_relevance_domains", {"domains": domains}, _TTL_SECONDS
            )
