"""Semantic Router for fast intent classification via vector similarity.

Uses ChromaDB to match user messages against "golden examples" of intents.
If a high-confidence match is found (>0.90), it bypasses the LLM.
"""

import json
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from .vector_db import VectorDatabase

logger = logging.getLogger(__name__)


class SemanticRouter:
    """Fast-path intent classifier using vector similarity."""

    def __init__(
        self,
        db_path: str = "data/chroma_semantic",
        golden_intents_path: str = "data/golden_intents.json",
        threshold: float = 0.90
    ):
        """Initialize Semantic Router.

        Args:
            db_path: Path to store ChromaDB collection
            golden_intents_path: Path to JSON file with golden examples
            threshold: Similarity threshold (0.0-1.0) for a match.
                       >0.90 is recommended for "lock" certainty.
        """
        self.db = VectorDatabase(
            path=db_path,
            collection_name="semantic_intents",
            embedding_model="all-MiniLM-L6-v2"
        )
        self.golden_intents_path = Path(golden_intents_path)
        self.threshold = threshold
        self._initialized = False

    async def initialize(self):
        """Load golden examples into ChromaDB if needed."""
        if self._initialized:
            return

        try:
            # Check if we need to populate/update
            # For simplicity, we'll reload if the collection is empty.
            # IN A PRODUCTION SYSTEM: You'd check hash/version of the JSON file.
            count = self.db.count()
            if count == 0:
                await self._populate_db()
            else:
                logger.info(f"Semantic Router ready ({count} examples loaded)")
            
            self._initialized = True
        except Exception as e:
            logger.error(f"Failed to initialize Semantic Router: {e}")

    async def _populate_db(self):
        """Read JSON and embed all examples."""
        if not self.golden_intents_path.exists():
            logger.warning(f"Golden intents file not found: {self.golden_intents_path}")
            return

        try:
            with open(self.golden_intents_path, 'r') as f:
                intents_data = json.load(f)

            logger.info(f"Loading {len(intents_data)} intent groups into Semantic Router...")

            count = 0
            for group in intents_data:
                intent_name = group.get("intent")
                examples = group.get("examples", [])
                tools = group.get("tools", [])

                for phrase in examples:
                    # Store phrase with metadata about intent & tools
                    await self.db.store(
                        text=phrase,
                        metadata={
                            "intent": intent_name,
                            "tools": ",".join(tools),
                            "is_golden": True
                        }
                    )
                    count += 1
            
            logger.info(f"Successfully embedded {count} golden examples")

        except Exception as e:
            logger.error(f"Error populating Semantic Router DB: {e}")

    async def route(self, message: str) -> Optional[Dict[str, Any]]:
        """Route a message to an intent if similarity is high enough.

        Args:
            message: User message

        Returns:
            Dict with intent details, or None if no match > threshold.
            Format: {
                "action": "status",
                "confidence": 0.95,
                "tool_hints": ["tool1"],
                "source": "semantic_router"
            }
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Search for closest match
            results = await self.db.search(message, n_results=1)
            
            if not results:
                return None

            best_match = results[0]
            distance = best_match.get("distance", 1.0)
            
            # Chroma returns DISTANCE (lower is better).
            # Cosine distance: 0.0 = identical, 1.0 = opposite.
            # Roughly: Similarity = 1 - distance (for normalized embeddings)
            # But Chroma's default distance metric might be L2 or Cosine. 
            # all-MiniLM-L6-v2 usually works well with < 0.2 distance for "very close".
            # Let's assume similarity = 1 - distance for now, treating distance < 0.1 as > 0.9 match.
            
            # Use distance threshold directly.
            # Distance < (1 - self.threshold)
            match_threshold = 1.0 - self.threshold
            
            if distance <= match_threshold:
                metadata = best_match.get("metadata", {})
                intent = metadata.get("intent")
                tools_str = metadata.get("tools", "")
                tools = [t for t in tools_str.split(",") if t]
                
                logger.info(f"🎯 Semantic Router HIT: '{message}' matched '{best_match['text']}' (dist: {distance:.4f}) -> {intent}")
                
                return {
                    "action": intent,
                    "confidence": 1.0 - distance, # approximation
                    "tool_hints": tools,
                    "source": "semantic_router",
                    "parameters": {}
                }
            else:
                logger.debug(f"Semantic Router miss: '{message}' closest was '{best_match['text']}' (dist: {distance:.4f})")
                return None

        except Exception as e:
            logger.error(f"Error in Semantic Router: {e}")
            return None
