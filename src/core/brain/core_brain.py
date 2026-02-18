"""Core Brain for self-building meta-agent."""

import json
import logging
import os
from datetime import datetime
from typing import Dict, Any, List
from .vector_db import VectorDatabase

logger = logging.getLogger(__name__)


class CoreBrain:
    """Brain for self-building meta-agent. Stores build progress, patterns, and knowledge."""

    def __init__(self, path: str = "data/core_brain"):
        """Initialize core brain.

        Args:
            path: Path to store brain data
        """
        self.path = path
        self.db = VectorDatabase(
            path=path,
            collection_name="build_memory"
        )

        logger.info(f"Initialized coreBrain at {path}")

    async def store_build_state(
        self,
        phase: str,
        features_done: List[str],
        features_pending: List[str]
    ):
        """Store current build progress.

        Args:
            phase: Current phase name
            features_done: List of completed features
            features_pending: List of pending features
        """
        state = {
            "phase": phase,
            "features_done": features_done,
            "features_pending": features_pending,
            "timestamp": datetime.now().isoformat()
        }

        await self.db.store(
            text=f"Build State - Phase: {phase}, Done: {len(features_done)}, Pending: {len(features_pending)}",
            metadata={
                "type": "build_state",
                "phase": phase,
                **state
            },
            doc_id=f"build_state_{phase}"
        )

        logger.info(f"Stored build state for phase: {phase}")

    async def remember_pattern(self, pattern: str, context: str):
        """Remember code patterns discovered during build.

        Args:
            pattern: Pattern description
            context: Context where pattern was useful
        """
        await self.db.store(
            text=f"Pattern: {pattern}\nContext: {context}",
            metadata={
                "type": "pattern",
                "timestamp": datetime.now().isoformat()
            }
        )

        logger.debug(f"Remembered pattern: {pattern}")

    async def get_relevant_patterns(self, query: str, n_results: int = 3) -> List[str]:
        """Get relevant code patterns for a task.

        Args:
            query: Task description
            n_results: Number of patterns to return

        Returns:
            List of pattern descriptions
        """
        results = await self.db.search(
            query=query,
            n_results=n_results,
            filter_metadata={"type": "pattern"}
        )

        return [result["text"] for result in results]

    def export_snapshot(self, output_path: str = "data/core_brain_snapshot.json") -> str:
        """Export brain to JSON file for git commit.

        Args:
            output_path: Path for snapshot file

        Returns:
            Path to snapshot file
        """
        # Get all documents from collection
        # Note: This is a simplified export. In production, you'd export the full ChromaDB
        snapshot = {
            "export_timestamp": datetime.now().isoformat(),
            "document_count": self.db.count(),
            "path": self.path
        }

        # Create parent directory if needed
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Write snapshot
        with open(output_path, 'w') as f:
            json.dump(snapshot, f, indent=2)

        logger.info(f"Exported coreBrain snapshot to {output_path}")
        return output_path

    def import_snapshot(self, snapshot_path: str):
        """Import brain from snapshot file (on EC2 startup).

        Args:
            snapshot_path: Path to snapshot file
        """
        if not os.path.exists(snapshot_path):
            logger.warning(f"Snapshot file not found: {snapshot_path}")
            return

        with open(snapshot_path, 'r') as f:
            snapshot = json.load(f)

        logger.info(f"Imported coreBrain snapshot from {snapshot.get('export_timestamp')}")

    # ============================================================
    # BUILD CONVERSATION METHODS
    # These store build-related conversations (how to implement X,
    # architectural discussions, etc.) - semantically different
    # from DigitalCloneBrain's user conversations
    # ============================================================

    async def store_conversation_turn(
        self,
        user_message: str,
        assistant_response: str,
        model_used: str,
        metadata: Dict[str, Any] = None
    ):
        """Store a build conversation turn.

        Build conversations are about system architecture, implementation
        strategies, and development discussions.

        Args:
            user_message: Developer's question/request
            assistant_response: Assistant's response
            model_used: Which model generated the response
            metadata: Additional metadata
        """
        conversation_text = f"""Build Discussion:
Developer: {user_message}
Assistant ({model_used}): {assistant_response}"""

        await self.db.store(
            text=conversation_text,
            metadata={
                "type": "build_conversation",
                "model_used": model_used,
                "timestamp": datetime.now().isoformat(),
                "user_message": user_message,
                "assistant_response": assistant_response,
                **(metadata or {})
            }
        )

        logger.debug(f"Stored build conversation turn (model: {model_used})")

    async def get_recent_conversation(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Retrieve recent build conversation turns.

        Args:
            limit: Number of recent turns to retrieve

        Returns:
            List of conversation turn dicts
        """
        # Search for recent build conversations
        results = await self.db.search(
            query="recent build conversation",
            n_results=limit * 2
        )

        # Filter for build_conversation type and sort by timestamp
        conversations = [
            {
                "user_message": r["metadata"].get("user_message", ""),
                "assistant_response": r["metadata"].get("assistant_response", ""),
                "model_used": r["metadata"].get("model_used", "unknown"),
                "timestamp": r["metadata"].get("timestamp", "")
            }
            for r in results
            if r["metadata"].get("type") == "build_conversation"
        ]

        # Sort by timestamp (most recent first)
        conversations.sort(
            key=lambda x: x["timestamp"],
            reverse=True
        )

        return conversations[:limit]

    async def get_conversation_context(self, current_message: str, limit: int = 3) -> str:
        """Get formatted build conversation context.

        Args:
            current_message: Current developer message
            limit: Number of previous turns to include

        Returns:
            Formatted context string
        """
        recent = await self.get_recent_conversation(limit)

        if not recent:
            return ""

        context_parts = ["## Recent Build Discussions:"]
        for turn in reversed(recent):  # Chronological order
            context_parts.append(f"Developer: {turn['user_message']}")
            context_parts.append(f"Assistant: {turn['assistant_response']}")
            context_parts.append("")

        return "\n".join(context_parts)

    async def get_relevant_context(self, query: str, max_results: int = 3) -> str:
        """Get relevant build context for current query.

        Args:
            query: Current query
            max_results: Maximum number of relevant items

        Returns:
            Formatted context string
        """
        results = await self.db.search(
            query=query,
            n_results=max_results
        )

        if not results:
            return ""

        context_parts = ["## Relevant Build Knowledge:"]
        for r in results:
            context_parts.append(f"- {r['text'][:200]}")

        return "\n".join(context_parts)
