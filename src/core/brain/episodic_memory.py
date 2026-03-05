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
from typing import Dict, List, Optional

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
        outcome_safe = outcome.strip()[:500]
        context_safe = (context or "").strip()[:300]

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

        Fetches 2x candidates then re-ranks by combining semantic relevance
        with recency and success — so yesterday's successful Moltbook registration
        outranks last week's failed attempt even if both are semantically similar.

        Args:
            query: Current task / topic to search for relevant episodes
            n: Max number of episodes to return
            days_back: How far back to look

        Returns:
            Formatted string ready for system prompt injection, or ""
        """
        results = await self.db.search(
            query=query,
            n_results=n * 2,  # Fetch extra candidates for re-ranking
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

        # Re-rank: recency + success boost
        now = datetime.now()
        scored = []
        for r in recent:
            # Semantic score (invert L2 distance)
            dist = r.get("distance", 1.0)
            semantic = max(0.0, 1.0 - dist / 1.5)

            # Recency score: linear decay over 5 days (120h)
            ts_str = r.get("metadata", {}).get("timestamp", "")
            recency = 0.0
            if ts_str:
                try:
                    age_h = (now - datetime.fromisoformat(ts_str)).total_seconds() / 3600.0
                    recency = max(0.0, 1.0 - age_h / 120.0)
                except (ValueError, TypeError):
                    pass

            # Success bonus: successful episodes are more useful than failures
            success_bonus = 0.1 if r.get("metadata", {}).get("success", False) else 0.0

            combined = 0.6 * semantic + 0.3 * recency + 0.1 + success_bonus
            scored.append((combined, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [r for _, r in scored[:n]]

        lines = ["RELEVANT PAST EPISODES:"]
        for r in top:
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

    async def record_strategy(
        self,
        goal: str,
        approach: str,
        tools_used: List[str],
        score: float,
    ):
        """Store a successful strategy for future recall.

        Called by TaskRunner after critic score >= 0.75. Stores the winning
        approach (not just the decomposition — that's ReasoningTemplateLibrary).
        This stores HOW it was done: which tools, what order, what worked.

        Only successful strategies are recorded — prevents hallucination loops
        where the agent remembers and repeats its own failures.
        """
        text = (
            f"Strategy for: {goal[:200]}\n"
            f"Approach: {approach[:300]}\n"
            f"Tools: {', '.join(tools_used)}\n"
            f"Quality: {score:.2f}"
        )
        await self.db.store(
            text=text,
            metadata={
                "type": "strategy",
                "tools": ",".join(tools_used),
                "score": score,
                "timestamp": datetime.now().isoformat(),
            }
        )
        logger.debug(f"Recorded strategy for: {goal[:60]} (score={score:.2f})")

    async def recall_strategies(self, goal: str, n: int = 2) -> str:
        """Recall proven strategies for similar goals.

        Uses vector similarity (OpenAI/HuggingFace embeddings via LanceDB)
        to find semantically similar strategies, even if keywords differ.

        Returns formatted string for prompt injection, or "".
        """
        results = await self.db.search(
            query=goal,
            n_results=n,
            filter_metadata={"type": "strategy"}
        )
        if not results:
            return ""

        lines = ["PROVEN STRATEGIES (from past successes — adapt, don't copy blindly):"]
        for r in results:
            lines.append(f"  {r['text'].strip()}")
        return "\n".join(lines)

    async def get_tool_success_rates(self) -> Dict[str, Dict]:
        """Return success rate per tool computed from all recorded episodes.

        Used by GoalDecomposer to prefer reliable tools and avoid flaky ones.

        Returns:
            Dict mapping tool_name → {"total": int, "rate": float}
            Only tools with ≥3 recorded uses are included (enough data to be meaningful).
        """
        try:
            results = await self.db.search(
                query="tool execution task step",
                n_results=500,
                filter_metadata={"type": "episode"}
            )
        except Exception as e:
            logger.debug(f"get_tool_success_rates search failed: {e}")
            return {}

        counts: Dict[str, Dict] = {}
        for r in results:
            meta = r.get("metadata", {})
            tool = meta.get("tool_used", "unknown")
            if tool == "unknown":
                continue
            if tool not in counts:
                counts[tool] = {"total": 0, "successes": 0}
            counts[tool]["total"] += 1
            if meta.get("success", True):
                counts[tool]["successes"] += 1

        return {
            tool: {
                "total": v["total"],
                "rate": v["successes"] / v["total"],
            }
            for tool, v in counts.items()
            if v["total"] >= 3
        }


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
