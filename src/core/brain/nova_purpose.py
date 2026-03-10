"""Nova's Purpose — defines WHY Nova exists and WHAT it proactively cares about.

This module is the "soul" of Nova: a set of purpose-driven behaviors that guide
proactivity and curiosity. It tells the AttentionEngine not just HOW to observe,
but WHAT to look for and WHEN — turning a reactive assistant into a purposeful one.

Architecture:
    nova_purpose.py  (WHAT and WHEN — purpose drives)
           ↓
    attention_engine.py  (HOW — runtime loop, Telegram delivery, dedup)

Purpose drives (Full AGI):
    1. Morning briefing (7:30–9am)   — Today's agenda, events, follow-ups
    2. Evening summary  (7–9pm)      — What was done, what's pending tomorrow
    3. Weekly look-ahead (Sunday 6pm) — 7-day horizon: deadlines, events, prep
    4. Curiosity scan   (every 6h)   — Interesting patterns, connections, observations
    5. Spontaneous interest (any)    — Surface unexpected things unprompted

Security: All prompts operate within Nova's existing security guardrails.
No raw PII, no message content — only summaries and observations from memory.
"""

import logging
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)

MAX_ITEMS = 3  # Max bullet points per notification


class PurposeMode(Enum):
    """The active purpose drive based on time-of-day."""
    MORNING = "morning"
    EVENING = "evening"
    WEEKLY  = "weekly"
    CURIOSITY = "curiosity"


class NovaPurpose:
    """Defines Nova's purpose-driven proactivity and curiosity behaviors.

    Used by AttentionEngine to select the right observation style for the
    current time of day. Each mode has a distinct prompt and Telegram header.
    """

    MISSION_STATEMENT = (
        "Nova exists to be the proactive half of your intelligence — "
        "noticing what you'd notice if you had infinite time, "
        "acting on what needs doing before you ask, "
        "and learning enough about you to anticipate rather than just respond."
    )

    # Time windows for each mode (hour, minute)
    MORNING_START = (7, 30)
    MORNING_END   = (9, 0)
    EVENING_START = (19, 0)
    EVENING_END   = (21, 0)
    WEEKLY_DAY    = 6  # Sunday (Monday=0)
    WEEKLY_START  = (18, 0)
    WEEKLY_END    = (20, 0)

    def get_mode(self, now: datetime) -> PurposeMode:
        """Determine the active purpose drive for a given datetime.

        Priority order (checked in sequence):
          1. Weekly (Sunday evening) — most specific window
          2. Morning
          3. Evening
          4. Curiosity (default for all other times)
        """
        hour = now.hour
        minute = now.minute
        time_minutes = hour * 60 + minute

        # Weekly: Sunday 6–8pm (checked before EVENING to take priority)
        if now.weekday() == self.WEEKLY_DAY:
            weekly_start = self.WEEKLY_START[0] * 60 + self.WEEKLY_START[1]
            weekly_end   = self.WEEKLY_END[0]   * 60 + self.WEEKLY_END[1]
            if weekly_start <= time_minutes < weekly_end:
                return PurposeMode.WEEKLY

        # Morning: 7:30–9am
        morning_start = self.MORNING_START[0] * 60 + self.MORNING_START[1]
        morning_end   = self.MORNING_END[0]   * 60 + self.MORNING_END[1]
        if morning_start <= time_minutes < morning_end:
            return PurposeMode.MORNING

        # Evening: 7–9pm
        evening_start = self.EVENING_START[0] * 60 + self.EVENING_START[1]
        evening_end   = self.EVENING_END[0]   * 60 + self.EVENING_END[1]
        if evening_start <= time_minutes < evening_end:
            return PurposeMode.EVENING

        # Default: curiosity scan
        return PurposeMode.CURIOSITY

    def build_prompt(
        self,
        mode: PurposeMode,
        context: str,
        owner_name: str,
        now: datetime,
    ) -> str:
        """Build the LLM observation prompt for the active purpose mode.

        Args:
            mode: Active PurposeMode from get_mode().
            context: Memory snippets gathered by AttentionEngine.
            owner_name: The owner's name for personalisation.
            now: Current datetime.

        Returns:
            A complete LLM prompt string. The LLM should reply with a JSON array.
        """
        day = now.strftime("%A, %B %d")
        time_str = now.strftime("%I:%M %p")

        if mode == PurposeMode.MORNING:
            return self._morning_prompt(context, owner_name, day, time_str)
        elif mode == PurposeMode.EVENING:
            return self._evening_prompt(context, owner_name, day, time_str)
        elif mode == PurposeMode.WEEKLY:
            return self._weekly_prompt(context, owner_name, day)
        else:
            return self._curiosity_prompt(context, owner_name, day, time_str)

    def get_header(self, mode: PurposeMode, owner_name: str, now: datetime) -> str:
        """Return the Telegram message header for a given purpose mode.

        Args:
            mode: Active PurposeMode.
            owner_name: The owner's name.
            now: Current datetime for the time display.

        Returns:
            A single-line markdown-safe header string.
        """
        time_str = now.strftime("%I:%M %p")
        if mode == PurposeMode.MORNING:
            return f"🌅 **Good morning, {owner_name}!** ({time_str})"
        elif mode == PurposeMode.EVENING:
            return f"🌙 **End of day, {owner_name}** ({time_str})"
        elif mode == PurposeMode.WEEKLY:
            return f"📅 **Weekly look-ahead** ({now.strftime('%B %d')})"
        else:
            return f"💡 **Heads up, {owner_name}** ({time_str})"

    # ── Private prompt builders ────────────────────────────────────────────────

    def _morning_prompt(self, context: str, owner_name: str, day: str, time_str: str) -> str:
        return f"""It is {day} at {time_str}. You are {owner_name}'s proactive non-human assistant starting the day.

Memory context (recent activity, reminders, conversations):
{context}

Generate a useful morning briefing with up to {MAX_ITEMS} bullet points:
• What's on today's agenda — any reminders, events, or follow-ups due today
• Anything time-sensitive in the next 48 hours worth flagging
• Any pending tasks that need the owner's input (red dispatch items)
• One pending item or task the owner should not forget

Be concise, warm, and practically useful. Focus on what's actionable today.
If nothing meaningful is in memory yet, return an encouraging opener about the day.

Reply with a JSON array only. No explanation, no markdown:
["item1", "item2", "item3"]"""

    def _evening_prompt(self, context: str, owner_name: str, day: str, time_str: str) -> str:
        return f"""It is {day} at {time_str}. Time to reflect on the day.

Memory context (today's conversations, tasks, outcomes):
{context}

Generate an end-of-day summary with up to {MAX_ITEMS} bullet points:
• What was accomplished or resolved today (keep it positive and factual)
• What's still pending or coming due tomorrow
• One forward-looking note — something to think about or prepare for tomorrow

Be warm and brief. If memory doesn't show much activity, acknowledge the quieter day.

Reply with a JSON array only. No explanation, no markdown:
["item1", "item2", "item3"]"""

    def _weekly_prompt(self, context: str, owner_name: str, day: str) -> str:
        return f"""It is Sunday — a good time to look ahead at the coming week.

Memory context (recent conversations, upcoming items, contacts):
{context}

Look at the next 7 days and surface up to {MAX_ITEMS} useful things:
• Upcoming deadlines, events, or appointments worth preparing for
• People to follow up with or reach out to next week
• Anything that would benefit from advance planning or early attention

Be forward-looking and practical. Think like a thoughtful chief of staff.

Reply with a JSON array only. No explanation, no markdown:
["item1", "item2", "item3"]"""

    def _curiosity_prompt(self, context: str, owner_name: str, day: str, time_str: str) -> str:
        return f"""You are {owner_name}'s curious non-human assistant. It is {day} at {time_str}.

Memory context (recent activity and conversations):
{context}

Scan for up to {MAX_ITEMS} things genuinely worth surfacing proactively:
- Items mentioned but never resolved — loose ends, pending follow-ups
- People not contacted in a while, if relevant context exists
- Interesting patterns or connections across recent conversations
- Anything the owner would want to know but hasn't thought to ask about

Be selective — only surface things with real value. If nothing stands out, return [].

Reply with a JSON array only. No explanation, no markdown:
["item1", "item2"] or []"""
