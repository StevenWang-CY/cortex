"""
Handover — Morning Briefing

On daemon start, checks for a previous day's handover and generates
a "Where you left off" summary with action items.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BriefingContent:
    """Morning briefing content to show in extensions."""
    title: str
    summary: str
    action_items: list[str]
    handover_path: str
    raw_markdown: str
    recent_activities: list[dict] | None = None


class MorningBriefing:
    """
    Generates morning briefing from previous handover.

    On daemon start, checks for yesterday's (or most recent) handover
    file and prepares a briefing to show in the VS Code panel and
    browser extension popup.
    """

    def __init__(self, storage_path: str = "./storage") -> None:
        self._storage_path = Path(storage_path)
        self._handovers_dir = self._storage_path / "handovers"

    async def check_and_generate(self) -> BriefingContent | None:
        """
        Check for a previous handover and generate briefing.

        Returns:
            BriefingContent if a handover exists, None otherwise.
        """
        from cortex.services.handover.snapshot import HandoverSnapshot

        snapshot = HandoverSnapshot(str(self._storage_path))

        # Check yesterday first, then most recent
        handover_path = snapshot.get_yesterday_handover()
        if handover_path is None:
            handover_path = snapshot.get_latest_handover()

        if handover_path is None:
            logger.debug("No previous handover found for morning briefing")
            return None

        try:
            content = handover_path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("Failed to read handover file: %s", handover_path)
            return None

        # Parse the markdown for key information
        briefing = self._parse_handover(content, handover_path)
        logger.info("Morning briefing generated from %s", handover_path.name)
        return briefing

    def _parse_handover(self, markdown: str, path: Path) -> BriefingContent:
        """Parse a handover markdown file into a briefing."""
        lines = markdown.split("\n")

        # Extract title
        title = "Welcome back"
        for line in lines:
            if line.startswith("# "):
                title = line[2:].strip()
                break

        # Extract summary section
        summary = ""
        in_summary = False
        for line in lines:
            if "## Summary" in line:
                in_summary = True
                continue
            if in_summary:
                if line.startswith("## "):
                    break
                summary += line + "\n"
        summary = summary.strip()

        if not summary:
            # Fall back to first few meaningful lines
            meaningful = [
                l for l in lines
                if l.strip() and not l.startswith("#") and not l.startswith(">")
            ][:3]
            summary = " ".join(meaningful)

        # Extract TODO items
        action_items = []
        for line in lines:
            if line.strip().startswith("- [ ]"):
                item = line.strip()[5:].strip()
                action_items.append(item)

        if not action_items:
            action_items = [
                "Review your previous session context",
                "Check for uncommitted changes",
                "Continue from where you left off",
            ]

        # Extract learning activities section
        recent_activities: list[dict] = []
        in_activity = False
        for line in lines:
            if "## Learning Activity" in line:
                in_activity = True
                continue
            if in_activity:
                if line.startswith("## "):
                    break
                if line.strip().startswith("- **"):
                    # Parse: "- **Platform**: Title — Position (Pct%) [Duration]"
                    recent_activities.append({"raw": line.strip()})

        return BriefingContent(
            title=title,
            summary=summary[:500],
            action_items=action_items[:5],
            handover_path=str(path),
            raw_markdown=markdown,
            recent_activities=recent_activities if recent_activities else None,
        )

    def to_ws_payload(self, briefing: BriefingContent) -> dict:
        """Convert briefing to WebSocket message payload."""
        payload: dict = {
            "title": briefing.title,
            "summary": briefing.summary,
            "action_items": briefing.action_items,
            "handover_path": briefing.handover_path,
        }
        if briefing.recent_activities:
            payload["recent_activities"] = briefing.recent_activities
        return payload
