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

    # Importance presets for different episode types
    IMPORTANCE_LEVELS = {
        "correction": 0.95,      # User corrections — highest priority
        "preference": 0.85,      # User preferences learned
        "learning": 0.80,        # Research insights stored
        "content_approved": 0.75, # Approved content patterns
        "content_rejected": 0.70, # Rejected content patterns
        "strategy": 0.70,        # Proven strategies
        "task_success": 0.50,    # Routine task completions
        "task_failure": 0.60,    # Failures (slightly higher — learn from mistakes)
        "routine": 0.30,         # Routine observations
    }

    async def record(
        self,
        action: str,
        outcome: str,
        success: bool = True,
        participants: Optional[List[str]] = None,
        tool_used: Optional[str] = None,
        context: Optional[str] = None,
        importance: Optional[float] = None,
        episode_type: Optional[str] = None,
        deduplicate: bool = False,
    ):
        """Record an event-outcome pair.

        Args:
            action: What was attempted (e.g. "emailed John about meeting")
            outcome: What happened (trimmed to 500 chars for safety)
            success: Whether the action succeeded
            participants: People involved (names only — no raw contact data)
            tool_used: Which tool was used (e.g. "email", "x_post")
            context: Brief context snippet (max 300 chars)
            importance: Explicit importance score (0.0-1.0). If None, auto-assigned
                        based on episode_type or success/failure.
            episode_type: Type hint for auto-importance (e.g. "correction", "learning",
                          "preference", "content_approved"). See IMPORTANCE_LEVELS.
            deduplicate: If True, merge with similar existing episode instead of duplicating.
        """
        ts = datetime.now().isoformat()
        who = ", ".join(participants) if participants else "nobody specific"

        # Trim to avoid storing raw sensitive content
        outcome_safe = outcome.strip()[:500]
        context_safe = (context or "").strip()[:300]

        # Auto-assign importance if not explicit
        if importance is None:
            if episode_type and episode_type in self.IMPORTANCE_LEVELS:
                importance = self.IMPORTANCE_LEVELS[episode_type]
            elif not success:
                importance = self.IMPORTANCE_LEVELS["task_failure"]
            else:
                importance = self.IMPORTANCE_LEVELS["task_success"]

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
                "type": episode_type or "episode",
                "action": action[:100],
                "success": success,
                "tool_used": tool_used or "unknown",
                "participants": who,
                "timestamp": ts,
                "date": ts[:10],
                "importance": importance,
            },
            deduplicate=deduplicate,
        )
        logger.debug(f"Recorded episode: {action[:50]} → {'ok' if success else 'fail'} (importance={importance:.2f})")

    async def recall(self, query: str, n: int = 3, days_back: int = 60) -> str:
        """Retrieve relevant past episodes using composite scoring.

        Uses VectorDatabase's composite scoring to rank by similarity + recency +
        importance in a single pass. Higher-importance memories (corrections,
        learnings) surface above routine episodes even if slightly less similar.

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
            composite_scoring=True,
            scoring_weights={"similarity": 0.5, "recency": 0.3, "importance": 0.2},
            recency_half_life_days=14.0,
        )

        if not results:
            return ""

        # Filter by date
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


    async def extract_and_store_facts(
        self,
        text: str,
        llm_client,
        source: str = "unknown",
        model: str = "gemini/gemini-2.0-flash",
    ) -> int:
        """Extract atomic facts from text and store each as a separate memory.

        Instead of storing one large blob, breaks it into granular facts that
        can be independently retrieved. Uses LLM to extract facts.

        Args:
            text: Large text to decompose (article, research, conversation)
            llm_client: LLM client (GeminiClient or AnthropicClient) with create_message()
            source: Where this text came from (e.g. "tweet", "article", "research")
            model: Model to use for extraction

        Returns:
            Number of facts extracted and stored
        """
        if len(text) < 50:
            # Too short to extract — store as-is
            await self.record(
                action=f"Fact from {source}",
                outcome=text,
                episode_type="learning",
                deduplicate=True,
            )
            return 1

        prompt = (
            "Extract distinct, atomic facts from the following text. "
            "Each fact should be a single, self-contained statement that can be "
            "understood without the rest of the text.\n\n"
            "Rules:\n"
            "- One fact per line\n"
            "- No numbering or bullets\n"
            "- Each fact must be independently meaningful\n"
            "- Skip filler, opinions, and marketing language\n"
            "- Keep technical details and specific claims\n"
            "- Maximum 15 facts\n\n"
            f"TEXT:\n{text[:3000]}\n\n"
            "FACTS (one per line):"
        )

        try:
            response = await llm_client.create_message(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                system="You extract atomic facts from text. Output only facts, one per line.",
                max_tokens=1024,
            )

            # Extract text from response
            facts_text = ""
            if hasattr(response, "content"):
                for block in response.content:
                    if hasattr(block, "text"):
                        facts_text += block.text
            elif isinstance(response, str):
                facts_text = response

            facts = [f.strip() for f in facts_text.strip().split("\n") if f.strip() and len(f.strip()) > 10]

            if not facts:
                logger.warning("extract_and_store_facts: LLM returned no facts")
                return 0

            stored = 0
            ts = datetime.now().isoformat()
            for fact in facts[:15]:
                await self.record(
                    action=f"Fact from {source}: {fact[:80]}",
                    outcome=fact,
                    episode_type="learning",
                    importance=0.75,
                    deduplicate=True,
                )
                stored += 1

            logger.info(f"Extracted {stored} atomic facts from {source}")
            return stored

        except Exception as e:
            logger.warning(f"extract_and_store_facts failed: {e}")
            # Fall back to storing the whole text
            await self.record(
                action=f"Learning from {source}",
                outcome=text[:500],
                episode_type="learning",
                deduplicate=True,
            )
            return 1

    async def forget_old(
        self,
        max_age_days: int = 90,
        min_importance: float = 0.3,
        dry_run: bool = False,
    ) -> int:
        """Intentional forgetting — remove old, low-importance episodes.

        Delegates to VectorDatabase.forget(). Only removes memories that are
        BOTH old AND unimportant. Corrections, learnings, and strategies are
        protected by their high importance scores.

        Args:
            max_age_days: Only forget memories older than this
            min_importance: Only forget memories with importance below this
            dry_run: If True, count but don't delete

        Returns:
            Number of memories forgotten
        """
        return await self.db.forget(
            max_age_days=max_age_days,
            min_importance=min_importance,
            dry_run=dry_run,
        )


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
