"""Tone Analyzer — detect emotional register from incoming messages.

Rule-based (zero latency, no LLM calls) with clear signal hierarchy.
Feeds into WorkingMemory so the detected tone persists and shapes responses.

Security: no external calls, no data storage. Pure text analysis only.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ToneSignal:
    """The detected tone of an incoming message."""
    register: str           # neutral | urgent | stressed | relaxed | formal
    urgency: float          # 0.0 – 1.0
    brevity_preferred: bool # True = keep replies short
    note: str               # human-readable reason (for logging)


# ── Signal patterns (ordered by specificity — first match wins) ──────

_URGENT_PATTERNS = [
    r"\basap\b", r"\burgent\b", r"\bquick(ly)?\b", r"\bnow\b",
    r"\bimmediately\b", r"\bfast\b", r"\bhurry\b", r"\bno time\b",
    r"\bin (\d+ )?(min|hour|sec)\b", r"!!+",
]

_STRESSED_PATTERNS = [
    r"\bstress(ed|ful)?\b", r"\bworried\b", r"\bpanic\b", r"\bproblem\b",
    r"\bcrisis\b", r"\bmess(ed)? up\b", r"\bwrong\b", r"\bfailed?\b",
    r"\bbroken\b", r"\bscrew(ed)?\b", r"\bugh\b", r"\bugh\b",
    r"\bhelp me\b", r"\bcan('t| not) figure\b",
]

_RELAXED_PATTERNS = [
    r"\bwhen you get a chance\b", r"\bno rush\b", r"\btake your time\b",
    r"\bwhenever\b", r"\bjust curious\b", r"\bby the way\b",
    r"\bfyi\b", r"\bthinking about\b", r"\bwondering\b",
]

_FORMAL_PATTERNS = [
    r"\bplease\b.*\bkindly\b", r"\bregarding\b", r"\bherewith\b",
    r"\bpursuant\b", r"\bforthwith\b", r"\benclosed\b",
    r"\bdear\b.*\bsincerely\b", r"\brespectfully\b",
]


def _matches_any(text: str, patterns: list) -> bool:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


def analyze(message: str) -> ToneSignal:
    """Detect the emotional tone of an incoming message.

    Args:
        message: Raw user message text

    Returns:
        ToneSignal with register, urgency, and brevity preference
    """
    text = message.strip()
    word_count = len(text.split())

    # Empty or very short messages — treat as neutral
    if word_count < 2:
        return ToneSignal("neutral", 0.2, True, "very short message")

    if _matches_any(text, _URGENT_PATTERNS):
        return ToneSignal("urgent", 0.9, True, "urgency keywords detected")

    if _matches_any(text, _STRESSED_PATTERNS):
        return ToneSignal("stressed", 0.7, False, "stress keywords detected")

    if _matches_any(text, _FORMAL_PATTERNS):
        return ToneSignal("formal", 0.3, False, "formal language detected")

    if _matches_any(text, _RELAXED_PATTERNS):
        return ToneSignal("relaxed", 0.1, False, "relaxed phrasing detected")

    # Heuristic: very short messages tend to be urgent / expect brief replies
    if word_count <= 5:
        return ToneSignal("neutral", 0.5, True, "short message")

    return ToneSignal("neutral", 0.2, False, "no strong signal")


def calibration_instruction(tone: ToneSignal) -> str:
    """Return a brief system-prompt instruction based on detected tone.

    Args:
        tone: ToneSignal from analyze()

    Returns:
        One-line instruction to append to the system prompt, or ""
    """
    instructions = {
        "urgent":  "Be brief and direct — the user is in a hurry. Lead with the answer.",
        "stressed": "Be calm and clear — the user seems under pressure. Avoid jargon.",
        "relaxed":  "The user is relaxed — you can be conversational and thorough.",
        "formal":   "Match professional tone — be precise and structured.",
        "neutral":  "",
    }
    return instructions.get(tone.register, "")
