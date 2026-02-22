"""LanceDB vector database wrapper — ACID-compliant, crash-safe replacement for ChromaDB.

Uses sentence-transformers for embeddings (same all-MiniLM-L6-v2 model as before).
LanceDB stores data in the Lance columnar format — no SQLite, no WAL corruption.
"""

import asyncio
import json
import logging
import uuid
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
        doc_id: Optional[str] = None
    ) -> str:
        """Store text with embeddings (non-blocking).

        Args:
            text: Text to store
            metadata: Optional metadata dict
            doc_id: Optional document ID (generated if not provided)

        Returns:
            Document ID
        """
        if not doc_id:
            doc_id = str(uuid.uuid4())

        vector = self._embed(text)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        record = {
            "id": doc_id,
            "text": text,
            "metadata": meta_json,
            "vector": vector,
        }

        loop = asyncio.get_event_loop()
        try:
            # Check if doc_id already exists (for upsert behavior)
            await loop.run_in_executor(None, lambda: self._upsert(record, doc_id))
        except Exception as e:
            logger.error(f"LanceDB store failed: {e}")
            raise

        logger.debug(f"Stored document {doc_id}")
        return doc_id

    def _upsert(self, record: dict, doc_id: str):
        """Insert or update a record."""
        try:
            # Try to delete existing record with same ID first
            self.table.delete(f"id = '{doc_id}'")
        except Exception:
            pass  # Table might be empty or ID doesn't exist

        self.table.add([record])

    async def search(
        self,
        query: str,
        n_results: int = 5,
        filter_metadata: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Semantic search for relevant memories (non-blocking).

        Args:
            query: Search query
            n_results: Number of results to return
            filter_metadata: Optional metadata filter (not used with LanceDB simple mode)

        Returns:
            List of matching documents with metadata and distances
        """
        loop = asyncio.get_event_loop()
        try:
            query_vector = self._embed(query)
            results = await loop.run_in_executor(
                None,
                lambda: self.table.search(query_vector).limit(n_results).to_list()
            )
        except Exception as e:
            logger.warning(f"LanceDB search failed: {e}")
            return []

        # Format results to match old ChromaDB interface
        matches = []
        for row in results:
            try:
                meta = json.loads(row.get("metadata", "{}"))
            except (json.JSONDecodeError, TypeError):
                meta = {}

            matches.append({
                "text": row.get("text", ""),
                "metadata": meta,
                "distance": row.get("_distance", 0.0),
                "id": row.get("id", None)
            })

        logger.debug(f"Found {len(matches)} matches for query")
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
                    self.table.delete(f"id = '{did}'")
            elif doc_id:
                self.table.delete(f"id = '{doc_id}'")
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

    def clear(self):
        """Clear all documents from table."""
        try:
            self.db.drop_table(self.collection_name)
        except Exception:
            pass
        self.table = self._get_or_create_table()
        logger.info(f"Cleared table {self.collection_name}")
