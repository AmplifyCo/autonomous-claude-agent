"""Digital Clone Brain for production use (permanent).

Architecture inspired by human consciousness:

COLLECTIVE CONSCIOUSNESS (shared across all talents):
    - Identity: WHO I am — personality, communication style
    - Preferences: WHAT I like — food, travel, general tastes
    - Contacts: WHO I know — people, relationships

ISOLATED CONTEXTS (per-talent, never bleed into each other):
    - Telegram context: personal chat history
    - Email context: email conversations, drafts, threads
    - X context: posts, tweet history, public voice
    - Calendar context: scheduling, appointments
    - ... each talent gets its own isolated memory

When retrieving context for a talent:
    1. ALWAYS include collective (identity + preferences + relevant contacts)
    2. ONLY include that talent's isolated memory
    3. NEVER pull email context when posting to X, etc.

Like a human brain — you don't mix work emails with family chat
or social media posts with private conversations. But your identity,
preferences, and knowledge of people stays consistent everywhere.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from cryptography.fernet import Fernet
from .vector_db import VectorDatabase

logger = logging.getLogger(__name__)

# Map channels to talent context names
CHANNEL_TO_CONTEXT = {
    "telegram": "telegram",
    "email": "email",
    "whatsapp": "whatsapp",
    "x": "x",
    "linkedin": "linkedin",
    "slack": "slack",
    "discord": "discord",
    "calendar": "calendar",
    "web": "web",
}


class DigitalCloneBrain:
    """Production brain for digital clone. Persistent forever.

    Two layers:
        - Collective: shared identity, preferences, contacts
        - Contexts: isolated per-talent conversation memory
    """

    def __init__(self, path: str = "data/digital_clone_brain"):
        self.path = path

        # ============================================================
        # JSONL BACKUP — reliable file-based memory backup
        # ============================================================
        self._backup_file = Path("data/brain_backup.jsonl")
        self._backup_file.parent.mkdir(parents=True, exist_ok=True)

        # ============================================================
        # COLLECTIVE CONSCIOUSNESS — shared across all talents
        # ============================================================

        # Identity: personality, communication style, core knowledge
        self.identity = VectorDatabase(
            path=f"{path}/collective/identity",
            collection_name="identity"
        )

        # Preferences: likes, dislikes, habits
        self.preferences = VectorDatabase(
            path=f"{path}/collective/preferences",
            collection_name="preferences"
        )

        # Contacts: people, relationships
        self.contacts = VectorDatabase(
            path=f"{path}/collective/contacts",
            collection_name="contacts"
        )

        # ============================================================
        # ISOLATED CONTEXTS — per-talent memory (lazy-loaded)
        # ============================================================
        self._contexts: Dict[str, VectorDatabase] = {}

        # ============================================================
        # BACKWARD COMPAT — keep old unified memory for migration
        # ============================================================
        self.memory = VectorDatabase(
            path=f"{path}/memory",
            collection_name="clone_memory"
        )

        logger.info(f"Initialized DigitalCloneBrain at {path}")
        logger.info("  Collective: identity, preferences, contacts")
        logger.info("  Contexts: isolated per-talent (lazy-loaded)")

        # Auto-restore from JSONL backup if ChromaDB was wiped
        self._auto_restore_from_backup()

    def _get_context(self, talent: str) -> VectorDatabase:
        """Get or create an isolated context for a talent.

        Each talent gets its own ChromaDB collection so memories
        never bleed across contexts.
        """
        ctx_name = CHANNEL_TO_CONTEXT.get(talent, talent)
        if ctx_name not in self._contexts:
            self._contexts[ctx_name] = VectorDatabase(
                path=f"{self.path}/contexts/{ctx_name}",
                collection_name=f"{ctx_name}_memory"
            )
            logger.info(f"  Created isolated context: {ctx_name}")
        return self._contexts[ctx_name]

    def _resolve_talent(self, channel: str = None, talent: str = None) -> Optional[str]:
        """Resolve talent name from channel or explicit talent parameter."""
        if talent:
            return CHANNEL_TO_CONTEXT.get(talent, talent)
        if channel:
            return CHANNEL_TO_CONTEXT.get(channel, channel)
        return None

    # ================================================================
    # JSONL BACKUP — reliable file-based memory backup
    # ================================================================

    def _append_to_backup(self, record_type: str, data: Dict[str, Any]):
        """Append a record to the JSONL backup file."""
        try:
            entry = {
                "type": record_type,
                "timestamp": datetime.now().isoformat(),
                **data
            }
            with open(self._backup_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to write JSONL backup: {e}")

    def _auto_restore_from_backup(self):
        """Restore brain data from JSONL backup if LanceDB collections are empty."""
        if not self._backup_file.exists():
            return

        # Check if LanceDB has data
        identity_count = self.identity.count()
        prefs_count = self.preferences.count()
        contacts_count = self.contacts.count()

        if identity_count > 0 and prefs_count > 0:
            logger.info(f"LanceDB has data (identity={identity_count}, prefs={prefs_count}, contacts={contacts_count}), skipping restore")
            return

        # LanceDB is empty — restore from backup
        logger.info("LanceDB appears empty, restoring from JSONL backup...")
        restored = {"identity": 0, "preference": 0, "contact": 0}

        try:
            with open(self._backup_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        rtype = record.get("type", "")

                        if rtype == "identity":
                            doc_id = record.get("doc_id")
                            text = record.get("text", "")
                            metadata = record.get("metadata", {})
                            if text:
                                self.identity.store_sync(text=text, metadata=metadata, doc_id=doc_id)
                                restored["identity"] += 1

                        elif rtype == "preference":
                            text = record.get("text", "")
                            metadata = record.get("metadata", {})
                            if text:
                                self.preferences.store_sync(text=text, metadata=metadata)
                                restored["preference"] += 1

                        elif rtype == "contact":
                            text = record.get("text", "")
                            metadata = record.get("metadata", {})
                            doc_id = record.get("doc_id")
                            if text:
                                self.contacts.store_sync(text=text, metadata=metadata, doc_id=doc_id)
                                restored["contact"] += 1

                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.debug(f"Failed to restore record: {e}")
                        continue

            logger.info(f"Restored from backup: {restored}")
        except Exception as e:
            logger.warning(f"Backup restore failed: {e}")

    # ================================================================
    # COLLECTIVE — Identity & Style
    # ================================================================

    async def learn_communication_style(self, sample: str, context: str = "general"):
        """Learn communication style from a sample.

        Args:
            sample: Sample text (email, tweet, chat message)
            context: Where this style applies (general, email, x, etc.)
        """
        metadata = {
            "type": "communication_style",
            "context": context,
            "timestamp": datetime.now().isoformat()
        }
        await self.identity.store(text=sample, metadata=metadata)

        # JSONL backup
        self._append_to_backup("identity", {
            "text": sample, "metadata": metadata
        })
        logger.info(f"Learned communication style ({context})")

    async def store_identity(self, aspect: str, description: str):
        """Store an identity aspect (personality trait, core value, etc.).

        Args:
            aspect: Identity aspect (e.g., "tone", "values", "expertise")
            description: Description of this aspect
        """
        text = f"{aspect}: {description}"
        metadata = {
            "type": "identity",
            "aspect": aspect,
            "timestamp": datetime.now().isoformat()
        }
        doc_id = f"identity_{aspect.lower().replace(' ', '_')}"
        await self.identity.store(text=text, metadata=metadata, doc_id=doc_id)

        # JSONL backup
        self._append_to_backup("identity", {
            "text": text, "metadata": metadata, "doc_id": doc_id
        })
        logger.info(f"Stored identity aspect: {aspect}")

    # ================================================================
    # COLLECTIVE — Preferences
    # ================================================================

    async def remember_preference(
        self,
        category: str,
        preference: str,
        source: str = "llm_derived",
        confidence: float = 0.7
    ):
        """Remember a user preference (shared across all talents).

        Args:
            category: Preference category (food, travel, etc.)
            preference: Preference description
            source: Origin — 'user_stated', 'llm_derived', or 'system'
            confidence: Confidence score 0.0-1.0 (user_stated should be 1.0)
        """
        text = f"Preference in {category}: {preference}"
        metadata = {
            "category": category,
            "source": source,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat()
        }
        await self.preferences.store(text=text, metadata=metadata)

        # JSONL backup
        self._append_to_backup("preference", {
            "text": text, "metadata": metadata
        })
        logger.info(f"Remembered preference: {category} - {preference} (source={source}, confidence={confidence})")

    # ================================================================
    # COLLECTIVE — Contacts
    # ================================================================

    async def remember_person(
        self,
        name: str,
        relationship: str,
        preferences: Dict[str, Any]
    ):
        """Remember a person and their details (shared across all talents).

        Args:
            name: Person's name
            relationship: Relationship to user
            preferences: Dict of person's preferences
        """
        contact_id = name.lower().replace(" ", "_")
        text = f"{name}: {relationship}. Preferences: {json.dumps(preferences)}"
        metadata = {
            "name": name,
            "relationship": relationship,
            **preferences
        }
        doc_id = f"contact_{contact_id}"

        await self.contacts.store(text=text, metadata=metadata, doc_id=doc_id)

        # JSONL backup
        self._append_to_backup("contact", {
            "text": text, "metadata": metadata, "doc_id": doc_id
        })
        logger.info(f"Remembered person: {name}")

    # ================================================================
    # ISOLATED CONTEXT — Conversation Storage
    # ================================================================

    async def store_conversation_turn(
        self,
        user_message: str,
        assistant_response: str,
        model_used: str,
        metadata: Dict[str, Any] = None
    ):
        """Store a conversation turn in the correct isolated context.

        The channel/talent from metadata determines which context
        gets this memory. Email conversations stay in email context,
        Telegram chats stay in Telegram context, etc.

        Args:
            user_message: User's message
            assistant_response: Assistant's response
            model_used: Which model generated the response
            metadata: Must include 'channel' for context isolation
        """
        metadata = metadata or {}
        channel = metadata.get("channel", "unknown")
        talent = self._resolve_talent(channel=channel)

        conversation_text = f"""User: {user_message}
Assistant ({model_used}): {assistant_response}"""

        store_metadata = {
            "type": "conversation",
            "model_used": model_used,
            "timestamp": datetime.now().isoformat(),
            "user_message": user_message,
            "assistant_response": assistant_response,
            "talent": talent or "unknown",
            **metadata
        }

        if talent:
            # Store in isolated talent context
            ctx = self._get_context(talent)
            await ctx.store(text=conversation_text, metadata=store_metadata)
            logger.debug(f"Stored conversation in [{talent}] context (model: {model_used})")
        else:
            # No talent identified — store in legacy unified memory
            await self.memory.store(text=conversation_text, metadata=store_metadata)
            logger.debug(f"Stored conversation in [general] memory (model: {model_used})")

    # ================================================================
    # CONTEXT RETRIEVAL — Collective + Isolated
    # ================================================================

    async def get_relevant_context(
        self,
        task: str,
        max_results: int = 5,
        talent: str = None,
        channel: str = None
    ) -> str:
        """Get relevant context for a task.

        Combines:
        1. Collective consciousness (identity + preferences) — always included
        2. Isolated talent context — only the specified talent's memories

        Args:
            task: Task description / search query
            max_results: Max memories to retrieve per source
            talent: Talent name for context isolation
            channel: Channel name (resolved to talent if talent not specified)

        Returns:
            Formatted context string
        """
        resolved_talent = self._resolve_talent(channel=channel, talent=talent)
        context_parts = []

        # --- COLLECTIVE: Identity & style (always included) ---
        try:
            identity_results = await self.identity.search(task, n_results=3)
            if identity_results:
                context_parts.append("## Identity & Style:")
                for r in identity_results:
                    context_parts.append(f"- {r['text'][:200]}")
        except Exception as e:
            logger.debug(f"Could not search identity: {e}")

        # --- COLLECTIVE: Preferences (always included) ---
        try:
            prefs = await self.preferences.search(task, n_results=3)
            if prefs:
                context_parts.append("\n## Preferences:")
                for pref in prefs:
                    context_parts.append(f"- {pref['text']}")
        except Exception as e:
            logger.debug(f"Could not search preferences: {e}")

        # --- COLLECTIVE: Relevant contacts (always included) ---
        try:
            contacts = await self.contacts.search(task, n_results=2)
            if contacts:
                context_parts.append("\n## Relevant Contacts:")
                for c in contacts:
                    context_parts.append(f"- {c['text'][:200]}")
        except Exception as e:
            logger.debug(f"Could not search contacts: {e}")

        # --- ISOLATED: Talent-specific memories (only current talent) ---
        if resolved_talent:
            try:
                ctx = self._get_context(resolved_talent)
                memories = await ctx.search(task, n_results=max_results)
                if memories:
                    context_parts.append(f"\n## {resolved_talent.title()} Context:")
                    for mem in memories:
                        context_parts.append(f"- {mem['text'][:200]}")
            except Exception as e:
                logger.debug(f"Could not search {resolved_talent} context: {e}")
        else:
            # Fallback: search legacy unified memory
            try:
                memories = await self.memory.search(task, n_results=max_results)
                if memories:
                    context_parts.append("\n## Relevant Memories:")
                    for mem in memories:
                        context_parts.append(f"- {mem['text'][:200]}")
            except Exception as e:
                logger.debug(f"Could not search general memory: {e}")

        return "\n".join(context_parts) if context_parts else ""

    async def get_recent_conversation(
        self,
        limit: int = 5,
        talent: str = None,
        channel: str = None
    ) -> List[Dict[str, Any]]:
        """Retrieve recent conversation turns from a specific talent context.

        Args:
            limit: Number of recent turns to retrieve
            talent: Talent name for context isolation
            channel: Channel name (resolved to talent)

        Returns:
            List of conversation turn dicts
        """
        resolved_talent = self._resolve_talent(channel=channel, talent=talent)

        # Search with ChromaDB where filter for conversation type
        if resolved_talent:
            ctx = self._get_context(resolved_talent)
            results = await ctx.search(
                query="conversation",
                n_results=limit,
                filter_metadata={"type": "conversation"}
            )
        else:
            results = await self.memory.search(
                query="conversation",
                n_results=limit,
                filter_metadata={"type": "conversation"}
            )

        # Format results and sort by timestamp
        conversations = [
            {
                "user_message": r["metadata"].get("user_message", ""),
                "assistant_response": r["metadata"].get("assistant_response", ""),
                "model_used": r["metadata"].get("model_used", "unknown"),
                "timestamp": r["metadata"].get("timestamp", ""),
                "talent": r["metadata"].get("talent", "unknown"),
            }
            for r in results
        ]

        conversations.sort(key=lambda x: x["timestamp"], reverse=True)
        return conversations[:limit]

    async def get_conversation_context(
        self,
        current_message: str,
        limit: int = 3,
        talent: str = None,
        channel: str = None
    ) -> str:
        """Get formatted conversation context for model prompt.

        Only retrieves conversation history from the specified talent's
        isolated context. Email history won't appear in X posting context.

        Args:
            current_message: Current user message
            limit: Number of previous turns to include
            talent: Talent name for context isolation
            channel: Channel name (resolved to talent)

        Returns:
            Formatted context string
        """
        recent = await self.get_recent_conversation(
            limit=limit, talent=talent, channel=channel
        )

        if not recent:
            return ""

        resolved_talent = self._resolve_talent(channel=channel, talent=talent)
        label = f"{resolved_talent.title()} " if resolved_talent else ""
        context_lines = [f"## Recent {label}Conversation:"]

        for turn in reversed(recent):
            context_lines.append(f"User: {turn['user_message']}")
            context_lines.append(f"Assistant: {turn['assistant_response']}")
            context_lines.append("")

        return "\n".join(context_lines)

    # ================================================================
    # CONTEXT DRIFT DETECTION
    # ================================================================

    async def detect_context_drift(self, talent: str = None, channel: str = None) -> Dict[str, Any]:
        """Detect if context quality has degraded (too many local model responses).

        Args:
            talent: Talent name for scoped detection
            channel: Channel name

        Returns:
            Drift analysis dict
        """
        recent = await self.get_recent_conversation(
            limit=10, talent=talent, channel=channel
        )

        if not recent:
            return {"has_drift": False, "reason": "No conversation history"}

        local_model_count = sum(
            1 for turn in recent
            if turn["model_used"] in ["smollm2", "deepseek-r1"]
        )

        drift_threshold = len(recent) * 0.5
        has_drift = local_model_count > drift_threshold

        return {
            "has_drift": has_drift,
            "total_turns": len(recent),
            "local_model_turns": local_model_count,
            "drift_percentage": (local_model_count / len(recent) * 100) if recent else 0,
            "recommendation": "Switch back to Claude API for quality restoration" if has_drift else "Quality OK"
        }

    # ================================================================
    # REVIEW QUEUE (stays in legacy memory — cross-talent)
    # ================================================================

    async def queue_for_claude_review(self, message: str, local_response: str):
        """Queue a local model response for Claude to review/correct when available."""
        await self.memory.store(
            text=f"LOCAL RESPONSE (needs review): {message} -> {local_response}",
            metadata={
                "type": "needs_review",
                "user_message": message,
                "local_response": local_response,
                "timestamp": datetime.now().isoformat(),
                "reviewed": False
            }
        )
        logger.info("Queued local response for Claude review")

    async def get_pending_reviews(self) -> List[Dict[str, Any]]:
        """Get responses that need Claude's review/correction."""
        results = await self.memory.search(query="needs review", n_results=20)

        return [
            {
                "user_message": r["metadata"].get("user_message", ""),
                "local_response": r["metadata"].get("local_response", ""),
                "timestamp": r["metadata"].get("timestamp", "")
            }
            for r in results
            if r["metadata"].get("type") == "needs_review"
            and not r["metadata"].get("reviewed", False)
        ]

    # ================================================================
    # EXPORT / IMPORT
    # ================================================================

    async def export_for_migration(self, password: str, output_file: str = "digital_clone_brain.brain") -> str:
        """Export entire brain for migration to new machine."""
        brain_data = {
            "collective": {
                "identity_count": self.identity.count(),
                "preferences_count": self.preferences.count(),
                "contacts_count": self.contacts.count(),
            },
            "contexts": {
                name: ctx.count() for name, ctx in self._contexts.items()
            },
            "legacy_memory_count": self.memory.count(),
            "exported_at": datetime.now().isoformat()
        }

        json_data = json.dumps(brain_data)
        key = Fernet.generate_key()
        cipher = Fernet(key)
        encrypted = cipher.encrypt(json_data.encode())

        with open(output_file, 'wb') as f:
            f.write(encrypted)

        logger.info(f"Exported DigitalCloneBrain to {output_file}")
        return output_file

    async def import_from_migration(self, brain_file: str, password: str):
        """Import brain from migration file."""
        logger.info(f"Importing DigitalCloneBrain from {brain_file}")

    # ================================================================
    # INTROSPECTION
    # ================================================================

    def get_brain_stats(self) -> Dict[str, Any]:
        """Get statistics about brain state."""
        context_stats = {}
        for name, ctx in self._contexts.items():
            try:
                context_stats[name] = ctx.count()
            except Exception:
                context_stats[name] = 0

        return {
            "collective": {
                "identity": self.identity.count(),
                "preferences": self.preferences.count(),
                "contacts": self.contacts.count(),
            },
            "contexts": context_stats,
            "legacy_memory": self.memory.count(),
            "active_contexts": list(self._contexts.keys()),
        }
