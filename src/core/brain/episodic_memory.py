"""Episodic Memory — stores what happened, not just what is true.

Semantic memory (DigitalCloneBrain) stores facts and preferences.
Episodic memory stores events — who, what, when, what happened, how it went.

This gives Nova the ability to say "last time I tried to reach Sarah
it went to voicemail" instead of just knowing Sarah's phone number.

Security: same LanceDB backend as the rest of Brain. No external calls.
No raw message content stored — only outcome summaries (trimmed to 200 chars).
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from .vector_db import VectorDatabase

logger = logging.getLogger(__name__)


class EpisodicMemory:
    """Stores and retrieves event-outcome pairs as episodic memories."""

    def __init__(self, path: str = "data/episodic_memory"):
        self.db = VectorDatabase(
            path=path,
            collection_name="episodes"
        )
        logger.info(f"EpisodicMemory initialized at {path}")

    async def record(
        self,
        action: str,
        outcome: str,
        success: bool,
        participants: Optional[List[str]] = None,
        tool_used: Optional[str] = None,
        context: Optional[str] = None,
    ):
        """Record an event-outcome pair.

        Args:
            action: What was attempted (e.g. "emailed John about meeting")
            outcome: What happened (trimmed to 200 chars for safety)
            success: Whether the action succeeded
            participants: People involved (names only — no raw contact data)
            tool_used: Which tool was used (e.g. "email", "x_post")
            context: Brief context snippet (max 100 chars)
        """
        ts = datetime.now().isoformat()
        who = ", ".join(participants) if participants else "nobody specific"

        # Trim to avoid storing raw sensitive content
        outcome_safe = outcome.strip()[:200]
        context_safe = (context or "").strip()[:100]

        text = (
            f"Episode [{ts[:10]}]: {action}\n"
            f"Participants: {who}\n"
            f"Outcome: {'✓' if success else '✗'} {outcome_safe}\n"
        )
        if context_safe:
            text += f"Context: {context_safe}\n"

        await self.db.store(
            text=text,
            metadata={
                "type": "episode",
                "action": action[:100],
                "success": success,
                "tool_used": tool_used or "unknown",
                "participants": who,
                "timestamp": ts,
                "date": ts[:10],
            }
        )
        logger.debug(f"Recorded episode: {action[:50]} → {'ok' if success else 'fail'}")

    async def recall(self, query: str, n: int = 3, days_back: int = 60) -> str:
        """Retrieve relevant past episodes for context injection.

        Args:
            query: Current task / topic to search for relevant episodes
            n: Max number of episodes to return
            days_back: How far back to look

        Returns:
            Formatted string ready for system prompt injection, or ""
        """
        results = await self.db.search(
            query=query,
            n_results=n,
            filter_metadata={"type": "episode"}
        )

        if not results:
            return ""

        # Filter by date (LanceDB returns metadata but doesn't filter on date natively)
        cutoff = (datetime.now() - timedelta(days=days_back)).date().isoformat()
        recent = [
            r for r in results
            if r["metadata"].get("date", "0000-00-00") >= cutoff
        ]

        if not recent:
            return ""

        lines = ["RELEVANT PAST EPISODES:"]
        for r in recent:
            lines.append(f"  {r['text'].strip()}")

        return "\n".join(lines)

    async def recall_failures(self, tool: str, n: int = 3) -> List[str]:
        """Return recent failure notes for a specific tool.

        Used by TaskRunner to avoid repeating strategies that didn't work.
        """
        results = await self.db.search(
            query=f"failed {tool}",
            n_results=n * 2,
            filter_metadata={"type": "episode"}
        )

        return [
            r["metadata"]["action"]
            for r in results
            if not r["metadata"].get("success", True)
            and r["metadata"].get("tool_used") == tool
        ][:n]


def confidence_label(score: float) -> str:
    """Convert a similarity score (0–1) to a human confidence label.

    Args:
        score: Cosine similarity from LanceDB search

    Returns:
        "clearly", "I believe", "I think", or "I'm not certain but"
    """
    if score >= 0.85:
        return "clearly"
    if score >= 0.70:
        return "I believe"
    if score >= 0.55:
        return "I think"
    return "I'm not certain, but"
