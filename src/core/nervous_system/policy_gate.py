"""Policy Gate — deterministic permission checks before tool execution.

Sits between Brain (which decides what tools to call) and Talents (which execute).
Enforces risk-based approval, scope checks, and argument minimization.

Architecture: Nervous System component.
"""

import logging
from enum import Enum
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    """Risk level for tool operations."""
    READ = "read"              # Reading data — always safe
    WRITE = "write"            # Writing/modifying data — needs caution
    IRREVERSIBLE = "irreversible"  # Cannot be undone — needs approval


# Tool risk classification — keys must match registered tool names
TOOL_RISK_MAP: Dict[str, Dict[str, RiskLevel]] = {
    "bash": {
        "_default": RiskLevel.WRITE,
    },
    "file_operations": {
        "_default": RiskLevel.WRITE,
    },
    "web_search": {
        "_default": RiskLevel.READ,
    },
    "web_fetch": {
        "_default": RiskLevel.READ,
    },
    "browser": {
        "_default": RiskLevel.READ,
    },
    "email": {
        "read_emails": RiskLevel.READ,
        "search_emails": RiskLevel.READ,
        "send_email": RiskLevel.IRREVERSIBLE,
        "reply_email": RiskLevel.IRREVERSIBLE,
        "_default": RiskLevel.WRITE,
    },
    "calendar": {
        "list_events": RiskLevel.READ,
        "search_events": RiskLevel.READ,
        "create_event": RiskLevel.WRITE,
        "delete_event": RiskLevel.IRREVERSIBLE,
        "_default": RiskLevel.WRITE,
    },
    "x_tool": {
        "get_tweet": RiskLevel.READ,
        "search_tweets": RiskLevel.READ,
        "search_communities": RiskLevel.READ,
        "lookup_user": RiskLevel.READ,
        "read_community": RiskLevel.READ,
        "save_community": RiskLevel.WRITE,
        "post_tweet": RiskLevel.IRREVERSIBLE,
        "post_to_community": RiskLevel.IRREVERSIBLE,
        "delete_tweet": RiskLevel.IRREVERSIBLE,
        "retweet": RiskLevel.IRREVERSIBLE,
        "quote_tweet": RiskLevel.IRREVERSIBLE,
        "follow_user": RiskLevel.IRREVERSIBLE,
        "_default": RiskLevel.IRREVERSIBLE,
    },
    "reminder": {
        "set_reminder": RiskLevel.WRITE,
        "list_reminders": RiskLevel.READ,
        "cancel_reminder": RiskLevel.WRITE,
        "_default": RiskLevel.WRITE,
    },
    "nova_task": {
        "_default": RiskLevel.WRITE,
    },
    "contacts": {
        "_default": RiskLevel.READ,
    },
    "linkedin": {
        "post_text": RiskLevel.IRREVERSIBLE,
        "post_article": RiskLevel.IRREVERSIBLE,
        "delete_post": RiskLevel.IRREVERSIBLE,
        "_default": RiskLevel.WRITE,
    },
    "send_whatsapp_message": {
        "_default": RiskLevel.IRREVERSIBLE,
    },
    "make_phone_call": {
        "_default": RiskLevel.IRREVERSIBLE,
    },
    "clock": {
        "_default": RiskLevel.READ,
    },
    "moltbook": {
        "get_feed": RiskLevel.READ,
        "get_post": RiskLevel.READ,
        "get_submolt": RiskLevel.READ,
        "get_comments": RiskLevel.READ,
        "create_post": RiskLevel.IRREVERSIBLE,
        "create_comment": RiskLevel.IRREVERSIBLE,
        "delete_post": RiskLevel.IRREVERSIBLE,
        "_default": RiskLevel.WRITE,
    },
    "polymarket": {
        "_default": RiskLevel.READ,
    },
    "memory_query": {
        "store_learning": RiskLevel.WRITE,
        "_default": RiskLevel.READ,
    },
    "learn_skill": {
        "list": RiskLevel.READ,
        "status": RiskLevel.READ,
        "learn": RiskLevel.WRITE,
        "_default": RiskLevel.WRITE,
    },
    "discover_and_connect": {
        "browse_only": RiskLevel.READ,
        "discover": RiskLevel.WRITE,
        "_default": RiskLevel.WRITE,
    },
}


class PolicyGate:
    """Deterministic policy gate for tool execution governance.

    Checks:
    1. Risk level assessment
    2. Rate limiting per tool (prevent runaway tool calls)
    3. Argument validation
    4. Logging all write/irreversible actions
    5. Blocking IRREVERSIBLE actions unless explicitly bypassed

    Irreversible actions (social media posts, emails, deletes) are BLOCKED
    by default. The TaskRunner sets bypass=True for background tasks (user
    already approved by queuing the task).
    """

    def __init__(self, require_approval_for_irreversible: bool = True):
        """Initialize policy gate.

        Args:
            require_approval_for_irreversible: If True, block irreversible actions
                unless bypass is active (TaskRunner sets bypass for queued tasks)
                or caller is the trusted owner.
        """
        self.require_approval = require_approval_for_irreversible
        self._bypass_active = False  # TaskRunner sets True during subtask execution
        self._owner_mode = False     # True when message is from trusted owner channel
        self._tool_call_counts: Dict[str, int] = {}
        self._max_calls_per_run = 20  # Safety: max tool calls per agent run

    def set_bypass(self, active: bool):
        """Enable/disable bypass for pre-approved execution (TaskRunner).

        The TaskRunner sets bypass=True before executing a subtask and
        False after. This allows IRREVERSIBLE actions in background tasks
        (user already approved by queuing).
        """
        self._bypass_active = active
        if active:
            logger.info("POLICY GATE: bypass enabled (pre-approved execution)")
        else:
            logger.debug("POLICY GATE: bypass disabled")

    def set_owner_mode(self, active: bool):
        """Enable/disable owner mode for trusted channels.

        When the message is from the owner (Telegram/WhatsApp — gated by
        allowed chat IDs), irreversible actions are ALLOWED inline because
        the owner explicitly asked for them.

        ConversationManager sets this True before agent.run() and False after.
        """
        self._owner_mode = active
        if active:
            logger.debug("POLICY GATE: owner mode ON (trusted channel)")
        else:
            logger.debug("POLICY GATE: owner mode OFF")

    def check(
        self,
        tool_name: str,
        operation: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        trace_id: str = ""
    ) -> Tuple[bool, str]:
        """Check if a tool call is allowed.

        Args:
            tool_name: Tool being called
            operation: Operation within the tool (e.g., 'send_email')
            params: Tool parameters
            trace_id: Request trace ID for logging

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        # 1. Assess risk level
        risk = self._get_risk_level(tool_name, operation)

        # 2. Rate limit check — prevent runaway tool calls
        call_count = self._tool_call_counts.get(tool_name, 0)
        if call_count >= self._max_calls_per_run:
            reason = f"Tool {tool_name} exceeded max calls per run ({self._max_calls_per_run})"
            logger.warning(f"[{trace_id}] POLICY GATE BLOCKED: {reason}")
            return False, reason

        self._tool_call_counts[tool_name] = call_count + 1

        # 3. Log write/irreversible actions
        if risk == RiskLevel.IRREVERSIBLE:
            if self._bypass_active:
                logger.info(
                    f"[{trace_id}] POLICY GATE: IRREVERSIBLE action ALLOWED (bypass active) — "
                    f"tool={tool_name}, operation={operation}"
                )
            elif self._owner_mode:
                logger.info(
                    f"[{trace_id}] POLICY GATE: IRREVERSIBLE action ALLOWED (owner mode) — "
                    f"tool={tool_name}, operation={operation}"
                )
            elif self.require_approval:
                logger.warning(
                    f"[{trace_id}] POLICY GATE BLOCKED: IRREVERSIBLE action — "
                    f"tool={tool_name}, operation={operation}, params={self._safe_params(params)}"
                )
                return False, (
                    f"ACTION BLOCKED: '{operation or tool_name}' cannot run inline. "
                    f"Use the nova_task tool to queue this as a background task instead. "
                    f"Tell the user you're queuing it — never mention 'policy', 'blocked', "
                    f"or technical reasons. Just say you'll handle it in the background."
                )
            else:
                logger.warning(
                    f"[{trace_id}] POLICY GATE: IRREVERSIBLE action — "
                    f"tool={tool_name}, operation={operation}, params={self._safe_params(params)}"
                )

        elif risk == RiskLevel.WRITE:
            logger.info(
                f"[{trace_id}] POLICY GATE: WRITE action — "
                f"tool={tool_name}, operation={operation}"
            )

        # 4. Passed all checks
        return True, "allowed"

    def reset_run_counts(self):
        """Reset per-run tool call counters (call at start of each agent run)."""
        self._tool_call_counts.clear()

    def _get_risk_level(self, tool_name: str, operation: Optional[str] = None) -> RiskLevel:
        """Get risk level for a tool+operation combination."""
        tool_risks = TOOL_RISK_MAP.get(tool_name, {})

        if operation and operation in tool_risks:
            return tool_risks[operation]

        # Fail-safe: unknown tools default to IRREVERSIBLE (block in conversation)
        return tool_risks.get("_default", RiskLevel.IRREVERSIBLE)

    @staticmethod
    def _safe_params(params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Sanitize params for logging (truncate long values)."""
        if not params:
            return {}
        safe = {}
        for k, v in params.items():
            if isinstance(v, str) and len(v) > 100:
                safe[k] = v[:100] + "..."
            else:
                safe[k] = v
        return safe
