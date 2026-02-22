"""Response interceptor for detecting inability patterns in agent responses.

Scans agent log for responses where Nova said "I am unable to..." and
extracts the missing capability. Maintains a backlog of capability gaps
for the CapabilityFixer to process and for daily update reporting.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


# ── Inability phrases that signal a capability gap ──────────────────

INABILITY_PATTERNS = [
    r"(?i)\bI am unable to\b",
    r"(?i)\bI cannot\b",
    r"(?i)\bI can'?t\b",
    r"(?i)\bI don'?t have the ability\b",
    r"(?i)\bnot supported\b",
    r"(?i)\bno way to\b",
    r"(?i)\bI need .+ to complete\b",
    r"(?i)\bcannot retrieve\b",
    r"(?i)\bcannot access\b",
    r"(?i)\bno method\b",
    r"(?i)\bnot available\b",
    r"(?i)\bmissing.*capability\b",
    r"(?i)\bI don'?t have access to\b",
    r"(?i)\bcurrently unable\b",
]

# Phrases that look like inability but aren't actual gaps
FALSE_POSITIVE_PATTERNS = [
    r"(?i)if you are unable",
    r"(?i)in case you cannot",
    r"(?i)the user (is|was) unable",
    r"(?i)they cannot",
]


def _descriptions_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """Return True if two gap descriptions share enough words to be the same gap.

    Uses word-overlap ratio relative to the shorter description.

    Args:
        a: First description (lowercase)
        b: Second description (lowercase)
        threshold: Minimum overlap ratio to consider them the same

    Returns:
        True if descriptions overlap beyond threshold
    """
    words_a = set(a.split())
    words_b = set(b.split())
    # Remove common stop words to avoid false matches on "the", "a", "to", etc.
    stop = {"the", "a", "an", "to", "of", "in", "for", "and", "or", "is", "be", "can"}
    words_a -= stop
    words_b -= stop
    if not words_a or not words_b:
        return a == b  # fallback: exact match
    smaller = min(len(words_a), len(words_b))
    overlap = len(words_a & words_b) / smaller
    return overlap >= threshold


@dataclass
class InabilityGap:
    """A detected capability gap."""
    response_text: str          # The full response containing the inability
    gap_description: str        # What capability is missing (LLM-extracted)
    likely_tool: Optional[str]  # Which tool needs the capability
    original_task: str          # The user's original request
    detected_at: str            # ISO timestamp
    status: str = "pending"     # pending | fixing | fixed | failed | wont_fix
    fix_details: Optional[str] = None  # What was done to fix it


class ResponseInterceptor:
    """Detects inability patterns in agent responses and manages capability backlog."""

    def __init__(
        self,
        llm_client=None,
        data_dir: str = "./data"
    ):
        """Initialize the response interceptor.

        Args:
            llm_client: LLM client for gap extraction (LiteLLM or Anthropic)
            data_dir: Directory for the capability backlog file
        """
        self.llm_client = llm_client
        self.data_dir = Path(data_dir)
        self.backlog_file = self.data_dir / "capability_backlog.json"

        logger.info("ResponseInterceptor initialized")

    # ── Pattern Detection ────────────────────────────────────────────

    def _detect_inability(self, text: str) -> bool:
        """Check if text contains an inability pattern.

        Args:
            text: Response text to check

        Returns:
            True if inability pattern found
        """
        # Check for false positives first
        for fp in FALSE_POSITIVE_PATTERNS:
            if re.search(fp, text):
                return False

        for pattern in INABILITY_PATTERNS:
            if re.search(pattern, text):
                return True

        return False

    # ── Log Scanning ─────────────────────────────────────────────────

    def scan_for_inability(
        self,
        log_file: str = "./data/logs/agent.log",
        minutes: int = 720,
        not_before: Optional[datetime] = None
    ) -> List[Dict[str, str]]:
        """Scan log file for agent responses containing inability patterns.

        Looks for response text logged after tool execution that contains
        inability phrases. Returns raw matches for LLM-based gap extraction.

        Args:
            log_file: Path to agent log
            minutes: How far back to scan
            not_before: Ignore entries before this time

        Returns:
            List of dicts with response_text and surrounding context
        """
        log_path = Path(log_file)
        if not log_path.exists():
            return []

        matches = []
        already_tracked = self._get_tracked_responses()

        try:
            with open(log_path, 'r') as f:
                lines = f.readlines()

            # Walk through log looking for response text blocks
            # Agent responses are typically logged after "Task completed (end_turn)"
            # and contain the actual text Nova sent back
            i = 0
            current_task = ""
            while i < len(lines):
                line = lines[i]

                # Track the original task/request
                if "Starting autonomous execution" in line:
                    # Extract task from log line
                    task_match = re.search(r"execution \[.*?\]: (.+)$", line)
                    if task_match:
                        current_task = task_match.group(1).strip()

                # Look for response content in log
                # Responses are typically logged as the final text output
                if "Task completed (end_turn)" in line:
                    # Collect the next few lines which may contain the response
                    response_block = []
                    for j in range(max(0, i - 20), i):
                        response_block.append(lines[j])

                    response_text = "".join(response_block)

                    if self._detect_inability(response_text):
                        # De-duplicate: skip if we've seen this exact response
                        response_hash = hash(response_text[:200])
                        if response_hash not in already_tracked:
                            matches.append({
                                "response_text": response_text.strip(),
                                "original_task": current_task,
                                "log_line": i + 1
                            })

                i += 1

        except Exception as e:
            logger.error(f"Error scanning logs for inability: {e}")

        if matches:
            logger.info(f"Found {len(matches)} inability responses in logs")

        return matches

    # ── Gap Extraction (LLM-powered) ─────────────────────────────────

    async def extract_gap(
        self,
        response_text: str,
        original_task: str,
        available_tools: List[str]
    ) -> Optional[InabilityGap]:
        """Use LLM to understand what capability is missing.

        Args:
            response_text: The agent's response containing inability
            original_task: What the user originally asked
            available_tools: List of registered tool names

        Returns:
            InabilityGap describing what's missing, or None
        """
        if not self.llm_client:
            # Fallback: simple keyword extraction
            return InabilityGap(
                response_text=response_text[:500],
                gap_description=f"Unable to complete: {original_task[:200]}",
                likely_tool=None,
                original_task=original_task,
                detected_at=datetime.now().isoformat()
            )

        prompt = f"""Analyze this AI agent response where it admitted inability to complete a task.

USER'S REQUEST: {original_task}

AGENT'S RESPONSE:
{response_text[:1000]}

AVAILABLE TOOLS: {', '.join(available_tools)}

What specific capability is missing? Reply in JSON:
{{
  "gap_description": "Brief description of the missing capability (e.g. 'retrieve latest tweets from user timeline')",
  "likely_tool": "name of the tool that should have this capability (from the available tools list), or null if new tool needed",
  "fixable": true/false (is this something that could be added as a new method to an existing tool?)
}}

Reply with ONLY the JSON, no other text."""

        try:
            response = await self.llm_client.create_message(
                model="gemini/gemini-2.0-flash",
                messages=[{"role": "user", "content": prompt}],
                system="You are a code analysis assistant. Reply with valid JSON only.",
                max_tokens=500
            )

            # Extract text from response
            text = ""
            for block in response.content:
                if hasattr(block, 'text'):
                    text += block.text

            # Parse JSON
            # Strip markdown code fences if present
            text = re.sub(r"```json?\s*", "", text)
            text = re.sub(r"```\s*$", "", text)
            data = json.loads(text.strip())

            return InabilityGap(
                response_text=response_text[:500],
                gap_description=data.get("gap_description", "Unknown gap"),
                likely_tool=data.get("likely_tool"),
                original_task=original_task,
                detected_at=datetime.now().isoformat()
            )

        except Exception as e:
            logger.error(f"Failed to extract gap via LLM: {e}")
            return InabilityGap(
                response_text=response_text[:500],
                gap_description=f"Unable to complete: {original_task[:200]}",
                likely_tool=None,
                original_task=original_task,
                detected_at=datetime.now().isoformat()
            )

    # ── Capability Backlog ───────────────────────────────────────────

    def _get_tracked_responses(self) -> set:
        """Get hashes of already-tracked responses to avoid duplicates."""
        backlog = self._load_backlog()
        return {hash(item.get("response_text", "")[:200]) for item in backlog}

    def _load_backlog(self) -> List[Dict[str, Any]]:
        """Load capability backlog from disk."""
        if not self.backlog_file.exists():
            return []
        try:
            with open(self.backlog_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []

    def _save_backlog(self, backlog: List[Dict[str, Any]]):
        """Save capability backlog to disk."""
        self.backlog_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.backlog_file, 'w') as f:
            json.dump(backlog, f, indent=2, default=str)

    def add_to_backlog(self, gap: InabilityGap):
        """Add a capability gap to the backlog.

        Args:
            gap: The detected capability gap
        """
        backlog = self._load_backlog()
        backlog.append(asdict(gap))
        self._save_backlog(backlog)
        logger.info(f"Added to capability backlog: {gap.gap_description}")

    def update_backlog_item(self, index: int, status: str, fix_details: Optional[str] = None):
        """Update status of a backlog item.

        Args:
            index: Index in the backlog list
            status: New status
            fix_details: Optional details about the fix
        """
        backlog = self._load_backlog()
        if 0 <= index < len(backlog):
            backlog[index]["status"] = status
            if fix_details:
                backlog[index]["fix_details"] = fix_details
            self._save_backlog(backlog)

    def get_pending_gaps(self) -> List[Dict[str, Any]]:
        """Get all pending (unfixed) capability gaps.

        Returns:
            List of pending backlog items
        """
        backlog = self._load_backlog()
        return [item for item in backlog if item.get("status") == "pending"]

    def is_gap_already_tracked(self, gap_description: str) -> bool:
        """Check if a gap with a similar description is already in the backlog.

        Prevents re-triggering the auto-fixer for the same capability gap
        when a fix branch has been pushed but not yet merged to main.

        Args:
            gap_description: The new gap description to check

        Returns:
            True if a similar gap is already being tracked (status != failed)
        """
        backlog = self._load_backlog()
        new_desc = gap_description.lower().strip()

        for item in backlog:
            # Allow re-trying failed fixes, but skip all others
            if item.get("status") == "failed":
                continue
            existing_desc = item.get("gap_description", "").lower().strip()
            if _descriptions_overlap(new_desc, existing_desc):
                logger.info(
                    f"Gap '{gap_description[:60]}' matches existing entry "
                    f"(status={item.get('status')})"
                )
                return True

        return False

    def get_backlog_summary(self) -> str:
        """Get a summary of the capability backlog for daily updates.

        Returns:
            Formatted string summary
        """
        backlog = self._load_backlog()
        if not backlog:
            return "No capability gaps detected."

        pending = [i for i in backlog if i.get("status") == "pending"]
        fixing = [i for i in backlog if i.get("status") == "fixing"]
        fix_pending = [i for i in backlog if i.get("status") == "fix_pending"]
        fixed = [i for i in backlog if i.get("status") == "fixed"]
        failed = [i for i in backlog if i.get("status") == "failed"]

        lines = ["📋 **Capability Backlog**"]

        if pending:
            lines.append(f"\n⏳ **Pending** ({len(pending)}):")
            for item in pending:
                tool = item.get('likely_tool', 'unknown')
                lines.append(f"  • Add `{item.get('gap_description', '?')}` to `{tool}`")

        if fixing:
            lines.append(f"\n🔧 **Generating Fix** ({len(fixing)}):")
            for item in fixing:
                lines.append(f"  • {item.get('gap_description', '?')}")

        if fix_pending:
            lines.append(f"\n🌿 **Fix in Branch — Awaiting Merge** ({len(fix_pending)}):")
            for item in fix_pending:
                branch = item.get('fix_details', 'unknown branch')
                lines.append(f"  • {item.get('gap_description', '?')}")
                lines.append(f"    ↳ {branch}")

        if fixed:
            lines.append(f"\n✅ **Fixed & Merged** ({len(fixed)}):")
            for item in fixed[-5:]:  # Last 5
                lines.append(f"  • {item.get('gap_description', '?')}")

        if failed:
            lines.append(f"\n❌ **Failed** ({len(failed)}):")
            for item in failed[-3:]:
                lines.append(f"  • {item.get('gap_description', '?')}")

        return "\n".join(lines)
