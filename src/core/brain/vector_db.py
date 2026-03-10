"""LanceDB vector database wrapper — ACID-compliant, crash-safe replacement for ChromaDB.

Uses sentence-transformers for embeddings (same all-MiniLM-L6-v2 model as before).
LanceDB stores data in the Lance columnar format — no SQLite, no WAL corruption.
"""

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Shared embedding model (loaded once, reused across all instances)
_embedding_model: Optional[SentenceTransformer] = None


def _get_embedding_model(model_name: str = "all-MiniLM-L6-v2") -> SentenceTransformer:
    """Lazy-load and cache the embedding model."""
    global _embedding_model
    if _embedding_model is None:
        logger.info(f"Loading embedding model: {model_name}")
        _embedding_model = SentenceTransformer(model_name)
        logger.info(f"Embedding model loaded (dim={_embedding_model.get_sentence_embedding_dimension()})")
    return _embedding_model


class VectorDatabase:
    """Wrapper for LanceDB to store and retrieve semantic memories.

    Drop-in replacement for the old ChromaDB wrapper — same interface,
    but ACID-compliant and crash-safe (no SQLite corruption issues).
    """

    def __init__(
        self,
        path: str,
        collection_name: str = "agent_memory",
        embedding_model: str = "all-MiniLM-L6-v2"
    ):
        """Initialize vector database.

        Args:
            path: Path to store LanceDB data
            collection_name: Name of the table (was 'collection' in ChromaDB)
            embedding_model: Sentence transformer model for embeddings
        """
        self.path = path
        self.collection_name = collection_name
        self.embedding_model_name = embedding_model

        # Ensure directory exists
        Path(path).mkdir(parents=True, exist_ok=True)

        # Connect to LanceDB
        self.db = lancedb.connect(path)
        self.model = _get_embedding_model(embedding_model)
        self._dim = self.model.get_sentence_embedding_dimension()

        # Open or create table
        self.table = self._get_or_create_table()

        # Backward compat: expose a .collection attribute for code that uses it
        self.collection = self

        logger.info(f"Initialized LanceDB at {path}, table: {collection_name}")

    def _get_or_create_table(self):
        """Get existing table or create a new empty one."""
        try:
            if self.collection_name in self.db.table_names():
                return self.db.open_table(self.collection_name)
        except Exception as e:
            logger.warning(f"Error opening table {self.collection_name}: {e}")

        # Create empty table with schema
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("metadata", pa.string()),  # JSON-encoded metadata
            pa.field("vector", pa.list_(pa.float32(), self._dim)),
        ])
        return self.db.create_table(self.collection_name, schema=schema)

    def _embed(self, text: str) -> List[float]:
        """Generate embedding vector for text."""
        return self.model.encode(text).tolist()

    async def store(
        self,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        doc_id: Optional[str] = None,
        deduplicate: bool = False,
        dedup_threshold: float = 0.85,
    ) -> str:
        """Store text with embeddings (non-blocking).

        Args:
            text: Text to store
            metadata: Optional metadata dict
            doc_id: Optional document ID (generated if not provided)
            deduplicate: If True, check for near-duplicates before storing.
                         If a similar record exists (cosine >= dedup_threshold),
                         update it instead of creating a new entry.
            dedup_threshold: Cosine similarity threshold for deduplication (0.0-1.0).
                             Higher = stricter matching. Default 0.85.

        Returns:
            Document ID (existing if deduplicated, new otherwise)
        """
        if not doc_id:
            doc_id = str(uuid.uuid4())

        vector = self._embed(text)

        # Deduplication: check for near-duplicates before storing
        if deduplicate:
            try:
                loop = asyncio.get_event_loop()
                existing = await loop.run_in_executor(
                    None, lambda: self._find_similar(vector, dedup_threshold)
                )
                if existing:
                    # Update existing record instead of creating duplicate
                    old_id = existing["id"]
                    logger.info(
                        f"Dedup: merging into existing record {old_id[:8]}... "
                        f"(similarity={existing['similarity']:.3f})"
                    )
                    doc_id = old_id
            except Exception as e:
                logger.debug(f"Dedup check failed (proceeding with insert): {e}")

        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        record = {
            "id": doc_id,
            "text": text,
            "metadata": meta_json,
            "vector": vector,
        }

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: self._upsert(record, doc_id))
        except Exception as e:
            logger.error(f"LanceDB store failed: {e}")
            raise

        logger.debug(f"Stored document {doc_id}")
        return doc_id

    def _find_similar(self, vector: List[float], threshold: float) -> Optional[Dict]:
        """Find the most similar existing record above the threshold.

        Uses L2 distance from LanceDB, converts to cosine-like similarity.
        L2 distance of 0.0 = identical, ~2.0 = completely different (for normalized vectors).
        Threshold is cosine similarity (0-1); we convert: max_l2 = 2*(1-threshold).

        Returns:
            Dict with 'id' and 'similarity' if found, else None.
        """
        try:
            if self.table.count_rows() == 0:
                return None
        except Exception:
            return None

        # Convert cosine similarity threshold to L2 distance threshold
        # For normalized vectors: L2^2 = 2*(1 - cosine_similarity)
        max_l2_dist = math.sqrt(2.0 * (1.0 - threshold))

        results = self.table.search(vector).limit(1).to_list()
        if not results:
            return None

        row = results[0]
        dist = row.get("_distance", 999.0)
        if dist <= max_l2_dist:
            similarity = 1.0 - (dist * dist) / 2.0
            return {"id": row.get("id", ""), "similarity": max(0.0, similarity)}
        return None

    @staticmethod
    def _escape_lance_string(value: str) -> str:
        """Escape a string for use in LanceDB filter expressions."""
        return value.replace("'", "''")

    def _upsert(self, record: dict, doc_id: str):
        """Insert or update a record."""
        try:
            # Try to delete existing record with same ID first
            safe_id = self._escape_lance_string(doc_id)
            self.table.delete(f"id = '{safe_id}'")
        except Exception:
            pass  # Table might be empty or ID doesn't exist

        self.table.add([record])

    async def search(
        self,
        query: str,
        n_results: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None,
        distance_threshold: Optional[float] = None,
        composite_scoring: bool = False,
        scoring_weights: Optional[Dict[str, float]] = None,
        recency_half_life_days: float = 30.0,
    ) -> List[Dict[str, Any]]:
        """Semantic search for relevant memories (non-blocking).

        Args:
            query: Search query
            n_results: Number of results to return
            filter_metadata: Optional metadata filter applied via LanceDB where() clause.
                             Keys are JSON metadata fields, values are exact-match strings.
                             Example: {"type": "conversation"} filters to records where
                             metadata JSON contains "type"="conversation".
            distance_threshold: Optional max L2 distance. Results farther than this
                                are discarded. Lower = stricter (0.0 = exact match).
                                Typical useful range: 0.5 (strict) to 1.2 (loose).
            composite_scoring: If True, re-rank results using composite score:
                               score = sim*w_sim + recency*w_rec + importance*w_imp
                               Requires metadata to have 'timestamp' and optionally 'importance'.
            scoring_weights: Override default weights. Keys: 'similarity', 'recency', 'importance'.
                             Defaults: {"similarity": 0.5, "recency": 0.3, "importance": 0.2}
            recency_half_life_days: Half-life for recency decay (default 30 days).

        Returns:
            List of matching documents with metadata and distances
        """
        loop = asyncio.get_event_loop()
        # Fetch extra candidates when composite scoring (re-ranking needs a wider pool)
        fetch_n = n_results * 3 if composite_scoring else n_results

        try:
            query_vector = self._embed(query)

            def _do_search():
                builder = self.table.search(query_vector)
                # Apply metadata filter via SQL where() clause on JSON fields
                if filter_metadata:
                    conditions = []
                    for key, value in filter_metadata.items():
                        safe_key = self._escape_lance_string(key)
                        safe_val = self._escape_lance_string(str(value))
                        # Match inside the JSON-encoded metadata string
                        conditions.append(f"metadata LIKE '%\"{safe_key}\": \"{safe_val}\"%'")
                    if conditions:
                        builder = builder.where(" AND ".join(conditions))
                return builder.limit(fetch_n).to_list()

            results = await loop.run_in_executor(None, _do_search)
        except Exception as e:
            logger.warning(f"LanceDB search failed: {e}")
            return []

        # Format results to match old ChromaDB interface
        matches = []
        for row in results:
            dist = row.get("_distance", 0.0)
            # Skip results beyond the distance threshold (irrelevant matches)
            if distance_threshold is not None and dist > distance_threshold:
                continue

            try:
                meta = json.loads(row.get("metadata", "{}"))
            except (json.JSONDecodeError, TypeError):
                meta = {}

            matches.append({
                "text": row.get("text", ""),
                "metadata": meta,
                "distance": dist,
                "id": row.get("id", None)
            })

        # Composite scoring: re-rank by similarity + recency + importance
        if composite_scoring and matches:
            weights = scoring_weights or {}
            w_sim = weights.get("similarity", 0.5)
            w_rec = weights.get("recency", 0.3)
            w_imp = weights.get("importance", 0.2)
            now = datetime.now()

            scored = []
            for m in matches:
                # Similarity: invert L2 distance → 0..1 (lower distance = higher score)
                sim = max(0.0, 1.0 - m["distance"] / 2.0)

                # Recency: exponential decay with half-life
                ts_str = m["metadata"].get("timestamp", "")
                recency = 0.0
                if ts_str:
                    try:
                        age_days = (now - datetime.fromisoformat(ts_str)).total_seconds() / 86400.0
                        recency = math.pow(0.5, age_days / recency_half_life_days)
                    except (ValueError, TypeError):
                        pass

                # Importance: stored in metadata (0.0 - 1.0), default 0.5
                importance = float(m["metadata"].get("importance", 0.5))

                composite = w_sim * sim + w_rec * recency + w_imp * importance
                m["composite_score"] = round(composite, 4)
                scored.append((composite, m))

            scored.sort(key=lambda x: x[0], reverse=True)
            matches = [m for _, m in scored[:n_results]]

        logger.debug(f"Found {len(matches)} matches for query (threshold={distance_threshold}, composite={composite_scoring})")
        return matches

    def count(self) -> int:
        """Get total number of documents in table.

        Returns:
            Document count
        """
        try:
            return self.table.count_rows()
        except Exception:
            return 0

    def delete(self, doc_id: str = None, ids: List[str] = None):
        """Delete document(s) by ID.

        Args:
            doc_id: Single document ID to delete
            ids: List of document IDs to delete (for backward compat with ChromaDB)
        """
        try:
            if ids:
                for did in ids:
                    safe_id = self._escape_lance_string(did)
                    self.table.delete(f"id = '{safe_id}'")
            elif doc_id:
                safe_id = self._escape_lance_string(doc_id)
                self.table.delete(f"id = '{safe_id}'")
            logger.debug(f"Deleted document(s)")
        except Exception as e:
            logger.warning(f"LanceDB delete failed: {e}")

    def store_sync(
        self,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        doc_id: Optional[str] = None
    ) -> str:
        """Synchronous version of store() — safe to call from __init__ or non-async contexts.

        Used by _auto_restore_from_backup() which runs before the event loop is available.
        """
        if not doc_id:
            doc_id = str(uuid.uuid4())
        vector = self._embed(text)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        record = {"id": doc_id, "text": text, "metadata": meta_json, "vector": vector}
        try:
            self._upsert(record, doc_id)
        except Exception as e:
            logger.error(f"LanceDB store_sync failed: {e}")
            raise
        logger.debug(f"Stored document (sync) {doc_id}")
        return doc_id

    async def forget(
        self,
        max_age_days: int = 90,
        min_importance: float = 0.3,
        dry_run: bool = False,
    ) -> int:
        """Intentional forgetting — remove old, low-importance memories.

        Removes records that are BOTH older than max_age_days AND have
        importance below min_importance. High-importance memories are kept
        regardless of age. Recent memories are kept regardless of importance.

        Args:
            max_age_days: Only forget memories older than this
            min_importance: Only forget memories with importance below this
            dry_run: If True, count but don't delete

        Returns:
            Number of records forgotten (or would be, if dry_run)
        """
        loop = asyncio.get_event_loop()

        def _do_forget():
            try:
                rows = self.table.to_pandas()
            except Exception:
                return 0

            if rows.empty:
                return 0

            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
            to_delete = []

            for _, row in rows.iterrows():
                try:
                    meta = json.loads(row.get("metadata", "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue

                ts = meta.get("timestamp", "")
                importance = float(meta.get("importance", 0.5))

                # Only forget if BOTH old AND low-importance
                if ts and ts < cutoff and importance < min_importance:
                    to_delete.append(row["id"])

            if not dry_run and to_delete:
                for doc_id in to_delete:
                    try:
                        safe_id = self._escape_lance_string(doc_id)
                        self.table.delete(f"id = '{safe_id}'")
                    except Exception:
                        pass

            return len(to_delete)

        count = await loop.run_in_executor(None, _do_forget)
        action = "Would forget" if dry_run else "Forgot"
        if count > 0:
            logger.info(f"{action} {count} old low-importance memories from {self.collection_name}")
        return count

    def clear(self):
        """Clear all documents from table."""
        try:
            self.db.drop_table(self.collection_name)
        except Exception:
            pass
        self.table = self._get_or_create_table()
        logger.info(f"Cleared table {self.collection_name}")
