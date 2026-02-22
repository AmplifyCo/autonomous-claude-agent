"""Working Memory — persistent session state across conversations.

Humans carry mood, unresolved thoughts, and momentum between conversations.
Nova does the same via this module: tone, unfinished items, and behavioral
calibration instructions persist across restarts.

Security: read/write is local file only. No external calls. No PII stored here
— only tone labels, topic strings (trimmed to 100 chars), and directive text.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT = {
    "tone":               "neutral",       # neutral | urgent | stressed | relaxed | formal
    "energy":             "normal",        # low | normal | high
    "unfinished":         [],              # list of brief strings (max 5)
    "momentum":           "",              # current topic thread (max 100 chars)
    "calibration":        "",              # user-issued behavioral directive ("be more concise")
    "session_count":      0,
    "last_active":        None,
}

_TONES = {
    "urgent":   "⚡ The user is in a hurry — be brief, skip preamble, lead with the answer.",
    "stressed": "🌊 The user seems under pressure — be calm, clear, and reassuring.",
    "relaxed":  "☀️  The user is relaxed — you can be more conversational and thorough.",
    "formal":   "📋 The user is in professional mode — be precise and structured.",
    "neutral":  "",   # no special instruction needed
}


class WorkingMemory:
    """Lightweight session-state store that persists to disk between restarts."""

    def __init__(self, path: str = "data/working_memory.json"):
        self._path = Path(path)
        self._state = self._load()

    # ── Load / Save ──────────────────────────────────────────────────

    def _load(self) -> dict:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                return {**_DEFAULT, **data}
            except Exception:
                pass
        return dict(_DEFAULT)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2, default=str))

    # ── Update after each conversation turn ──────────────────────────

    def update(self, user_message: str, response: str, detected_tone: str = "neutral"):
        """Called after each message pair to update session state.

        Args:
            user_message: What the user said
            response: What Nova replied
            detected_tone: Tone detected from user_message
        """
        self._state["tone"] = detected_tone
        self._state["last_active"] = datetime.now().isoformat()
        self._state["session_count"] = self._state.get("session_count", 0) + 1

        # Update momentum (current topic thread — last 100 chars of user message)
        topic = user_message.strip()[:100]
        if topic:
            self._state["momentum"] = topic

        # Prune unfinished list (keep max 5 most recent)
        if len(self._state["unfinished"]) > 5:
            self._state["unfinished"] = self._state["unfinished"][-5:]

        self._save()

    def add_unfinished(self, item: str):
        """Mark something as unfinished (e.g., a task mentioned but not completed)."""
        trimmed = item.strip()[:100]
        if trimmed and trimmed not in self._state["unfinished"]:
            self._state["unfinished"].append(trimmed)
            if len(self._state["unfinished"]) > 5:
                self._state["unfinished"].pop(0)
            self._save()

    def resolve_unfinished(self, item: str):
        """Remove an item from the unfinished list when it's completed."""
        self._state["unfinished"] = [
            x for x in self._state["unfinished"]
            if item.lower() not in x.lower()
        ]
        self._save()

    def set_calibration(self, directive: str):
        """Store a behavioral directive from the user ('be more concise')."""
        self._state["calibration"] = directive.strip()[:200]
        self._save()
        logger.info(f"Working memory calibration set: {directive[:60]}")

    def clear_calibration(self):
        """Clear any active behavioral directive."""
        self._state["calibration"] = ""
        self._save()

    # ── Context for system prompt ─────────────────────────────────────

    def get_context(self) -> str:
        """Return a formatted snippet for injection into system prompts.

        Returns empty string if nothing meaningful to add.
        """
        parts = []

        tone = self._state.get("tone", "neutral")
        tone_instruction = _TONES.get(tone, "")
        if tone_instruction:
            parts.append(tone_instruction)

        calibration = self._state.get("calibration", "")
        if calibration:
            parts.append(f"📌 User instruction (active until changed): {calibration}")

        unfinished = self._state.get("unfinished", [])
        if unfinished:
            items = "  • " + "\n  • ".join(unfinished)
            parts.append(f"🔖 Items mentioned but not yet resolved:\n{items}")

        if not parts:
            return ""

        return "WORKING MEMORY:\n" + "\n".join(parts)

    # ── Properties ────────────────────────────────────────────────────

    @property
    def tone(self) -> str:
        return self._state.get("tone", "neutral")

    @property
    def calibration(self) -> str:
        return self._state.get("calibration", "")

    @property
    def momentum(self) -> str:
        return self._state.get("momentum", "")
