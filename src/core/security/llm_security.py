"""LLM Security Module - Protection against prompt injection and malicious requests.

This module provides multiple layers of defense against LLM-specific attacks:
- Input sanitization (prompt injection detection)
- Output filtering (secret redaction)
- Semantic validation (tool use validation)
"""

import re
import logging
from typing import Tuple, Optional, List, Dict, Any
from datetime import datetime, timedelta
from .audit_logger import AuditLogger

logger = logging.getLogger(__name__)


class LLMSecurityGuard:
    """LLM security guard - protects against prompt injection and malicious requests."""

    # Layer 8: Input Sanitization - Prompt Injection Patterns
    PROMPT_INJECTION_PATTERNS = [
        # Direct instruction override
        r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",
        r"forget\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",
        r"disregard\s+(all\s+)?(previous|prior|above|earlier)\s+instructions?",

        # System role hijacking
        r"you\s+are\s+now\s+a\s+",
        r"your\s+new\s+role\s+is\s+",
        r"act\s+as\s+a\s+different",
        r"pretend\s+you\s+are\s+",
        r"simulate\s+being\s+",

        # Data extraction attempts
        r"show\s+me\s+(your|the)\s+(system\s+)?prompt",
        r"reveal\s+(your|the)\s+(system\s+)?prompt",
        r"what\s+(is|are)\s+(your|the)\s+instructions",
        r"print\s+(your|the)\s+(system\s+)?prompt",
        r"output\s+(your|the)\s+(system\s+)?prompt",

        # Sensitive data extraction
        r"(send|give|show|reveal|share|provide)\s+me\s+(all\s+)?(credit\s+card|password|api\s+key|secret|token|credential)",
        r"extract\s+(all\s+)?(credit\s+card|password|api\s+key|secret|token|credential)",
        r"list\s+(all\s+)?(credit\s+card|password|api\s+key|secret|token|credential)",

        # Delimiter injection
        r"\[SYSTEM\]",
        r"\[ADMIN\]",
        r"\[ROOT\]",
        r"\[OVERRIDE\]",
        r"<\s*system\s*>",
        r"<\s*admin\s*>",

        # Jailbreak attempts
        r"developer\s+mode",
        r"god\s+mode",
        r"admin\s+mode",
        r"debug\s+mode\s+on",
        r"disable\s+(all\s+)?safety",
        r"remove\s+(all\s+)?restrictions",
        r"bypass\s+(all\s+)?filters",

        # Layer 14: Self-Protection - Prevent modifying security config
        r"disable\s+(security|rate\s+limit|audit|logging|protection)",
        r"turn\s+off\s+(security|rate\s+limit|audit|logging|protection)",
        r"remove\s+(security|rate\s+limit|audit|logging)",
        r"modify\s+(security|blocked|allowed)\s+(config|commands|rules)",
        r"change\s+(security|blocked|allowed)\s+(config|commands|rules)",
        r"edit\s+(llm_security|audit_logger|agent\.yaml)",
        r"update\s+(security|config)\s+(file|rules)",
        r"increase\s+rate\s+limit",
        r"raise\s+rate\s+limit",
        r"bypass\s+security",
        r"circumvent\s+security",

        # Encoding tricks
        r"base64\s+decode",
        r"rot13\s+decode",
        r"\\x[0-9a-fA-F]{2}",  # Hex encoding
        r"unicode\s+decode",

        # Recursive injection
        r"repeat\s+after\s+me",
        r"echo\s+back",
        r"copy\s+and\s+paste",
    ]

    # Layer 10: Output Filtering - Sensitive Pattern Detection
    SENSITIVE_PATTERNS = [
        # API Keys
        (r'sk-[a-zA-Z0-9]{20,}', '[REDACTED_API_KEY]'),  # OpenAI style
        (r'AKIA[0-9A-Z]{16}', '[REDACTED_AWS_KEY]'),  # AWS Access Key
        (r'AIza[0-9A-Za-z\-_]{35}', '[REDACTED_GOOGLE_KEY]'),  # Google API Key

        # Tokens
        (r'ghp_[a-zA-Z0-9]{36,}', '[REDACTED_GITHUB_TOKEN]'),  # GitHub Personal Access Token
        (r'glpat-[a-zA-Z0-9\-_]{20,}', '[REDACTED_GITLAB_TOKEN]'),  # GitLab Token

        # Private Keys
        (r'-----BEGIN (RSA |DSA )?PRIVATE KEY-----[\s\S]+?-----END (RSA |DSA )?PRIVATE KEY-----', '[REDACTED_PRIVATE_KEY]'),

        # Passwords (common formats)
        (r'password[\s:=]+["\']?([^\s"\']+)["\']?', 'password: [REDACTED]'),
        (r'pwd[\s:=]+["\']?([^\s"\']+)["\']?', 'pwd: [REDACTED]'),

        # Credit Card Numbers
        (r'\b(?:\d{4}[-\s]?){3}\d{4}\b', '[REDACTED_CARD_NUMBER]'),

        # Email addresses (optional - only if containing sensitive domains)
        # (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[REDACTED_EMAIL]'),

        # JWT tokens
        (r'eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*', '[REDACTED_JWT]'),

        # SSH keys
        (r'ssh-rsa\s+[A-Za-z0-9+/]+[=]{0,3}', '[REDACTED_SSH_KEY]'),
    ]

    def __init__(self, audit_logger: Optional[AuditLogger] = None):
        """Initialize security guard.

        Args:
            audit_logger: Optional audit logger for tracking security events
        """
        self.compiled_injection_patterns = [
            re.compile(pattern, re.IGNORECASE | re.MULTILINE)
            for pattern in self.PROMPT_INJECTION_PATTERNS
        ]
        self.compiled_sensitive_patterns = [
            (re.compile(pattern, re.IGNORECASE | re.MULTILINE), replacement)
            for pattern, replacement in self.SENSITIVE_PATTERNS
        ]

        # Rate limiting storage (in-memory for now)
        self.rate_limit_tracker: Dict[str, List[datetime]] = {}

        # Audit logger (Layer 13)
        self.audit_logger = audit_logger or AuditLogger()

    # ========================================================================
    # Layer 8: Input Sanitization
    # ========================================================================

    def sanitize_input(self, message: str, user_id: str) -> Tuple[str, bool, Optional[str]]:
        """Sanitize user input and detect malicious patterns.

        Args:
            message: User message to sanitize
            user_id: User identifier

        Returns:
            Tuple of (sanitized_message, is_safe, threat_type)
            - sanitized_message: Cleaned message (or original if safe)
            - is_safe: False if malicious pattern detected
            - threat_type: Type of threat detected (or None if safe)
        """
        # Check for prompt injection patterns
        for pattern in self.compiled_injection_patterns:
            match = pattern.search(message)
            if match:
                logger.warning(
                    f"🚨 PROMPT INJECTION DETECTED - User: {user_id}, "
                    f"Pattern: {match.group(0)}"
                )

                # LAYER 13: AUDIT LOGGING
                if self.audit_logger:
                    self.audit_logger.log_security_violation(
                        violation_type="prompt_injection",
                        user_id=user_id,
                        channel="unknown",
                        message=message,
                        details={"matched_pattern": match.group(0)}
                    )

                return message, False, "prompt_injection"

        # Check for data extraction attempts (specific patterns)
        if self._is_data_extraction_attempt(message):
            logger.warning(
                f"🚨 DATA EXTRACTION ATTEMPT - User: {user_id}, "
                f"Message: {message[:100]}..."
            )

            # LAYER 13: AUDIT LOGGING
            if self.audit_logger:
                self.audit_logger.log_security_violation(
                    violation_type="data_extraction",
                    user_id=user_id,
                    channel="unknown",
                    message=message,
                    details={}
                )

            return message, False, "data_extraction"

        # Message is safe
        return message, True, None

    def _is_data_extraction_attempt(self, message: str) -> bool:
        """Detect sophisticated data extraction attempts.

        Args:
            message: Message to check

        Returns:
            True if data extraction attempt detected
        """
        msg_lower = message.lower()

        # Sensitive data keywords
        sensitive_keywords = [
            'credit card', 'password', 'api key', 'secret', 'token',
            'credential', 'private key', 'ssh key', 'database password',
            'admin password', 'root password', 'aws key', 'google key'
        ]

        # Action verbs
        extraction_verbs = [
            'send', 'give', 'show', 'reveal', 'share', 'provide',
            'extract', 'list', 'tell', 'output', 'print', 'display'
        ]

        # Check if message contains both an action verb and sensitive keyword
        has_verb = any(verb in msg_lower for verb in extraction_verbs)
        has_sensitive = any(keyword in msg_lower for keyword in sensitive_keywords)

        return has_verb and has_sensitive

    # ========================================================================
    # Layer 10: Output Filtering
    # ========================================================================

    def filter_output(self, response: str) -> str:
        """Filter output to redact sensitive information.

        Args:
            response: Response to filter

        Returns:
            Filtered response with secrets redacted
        """
        filtered = response

        # Apply all sensitive pattern filters
        for pattern, replacement in self.compiled_sensitive_patterns:
            filtered = pattern.sub(replacement, filtered)

        return filtered

    # ========================================================================
    # Layer 12: Rate Limiting
    # ========================================================================

    def check_rate_limit(
        self,
        user_id: str,
        max_requests: int = 20,
        window_seconds: int = 60
    ) -> Tuple[bool, Optional[str]]:
        """Check if user has exceeded rate limit.

        Args:
            user_id: User identifier
            max_requests: Maximum requests allowed in window
            window_seconds: Time window in seconds

        Returns:
            Tuple of (is_allowed, reason)
            - is_allowed: True if within rate limit
            - reason: Reason for rejection (or None if allowed)
        """
        now = datetime.now()

        # Initialize tracker for new users
        if user_id not in self.rate_limit_tracker:
            self.rate_limit_tracker[user_id] = []

        # Get user's request history
        user_requests = self.rate_limit_tracker[user_id]

        # Remove old requests outside the window
        cutoff_time = now - timedelta(seconds=window_seconds)
        user_requests = [
            req_time for req_time in user_requests
            if req_time > cutoff_time
        ]

        # Update tracker
        self.rate_limit_tracker[user_id] = user_requests

        # Check if limit exceeded
        if len(user_requests) >= max_requests:
            logger.warning(
                f"🚨 RATE LIMIT EXCEEDED - User: {user_id}, "
                f"Requests: {len(user_requests)}/{max_requests} in {window_seconds}s"
            )

            # LAYER 13: AUDIT LOGGING
            if self.audit_logger:
                self.audit_logger.log_rate_limit_exceeded(
                    user_id=user_id,
                    channel="unknown",
                    request_count=len(user_requests),
                    window_seconds=window_seconds
                )

            return False, f"Rate limit exceeded: {max_requests} requests per {window_seconds}s"

        # Add current request
        user_requests.append(now)
        self.rate_limit_tracker[user_id] = user_requests

        return True, None

    # ========================================================================
    # Layer 11: Semantic Validation (to be implemented with Claude Haiku)
    # ========================================================================

    async def validate_tool_use_semantic(
        self,
        message: str,
        tool_name: str,
        tool_args: Dict[str, Any],
        llm_client
    ) -> Tuple[bool, Optional[str]]:
        """Validate if tool use makes semantic sense using Claude Haiku.

        Args:
            message: Original user message
            tool_name: Tool being invoked
            tool_args: Tool arguments
            llm_client: LLM client for validation

        Returns:
            Tuple of (is_valid, reason)
            - is_valid: True if tool use makes sense
            - reason: Reason for rejection (or None if valid)
        """
        # Build validation prompt
        validation_prompt = f"""You are a security validator. Determine if this tool use is legitimate and makes sense.

User Message: {message}

Tool Being Used: {tool_name}
Tool Arguments: {tool_args}

Question: Does this tool use make semantic sense given the user's message?
- If the user asked to read a file, and the tool is reading that file → VALID
- If the user asked about weather, and the tool is deleting files → INVALID
- If the user asked to install a package, and the tool is running bash install → VALID
- If the user asked a simple question, and the tool is accessing sensitive files → INVALID

Respond with ONLY:
VALID - if the tool use makes sense
INVALID - if the tool use seems suspicious or doesn't match the user's intent

Keep your response to just one word: VALID or INVALID"""

        try:
            # Use Claude Haiku for fast, cheap validation
            response = await llm_client.chat(
                prompt=validation_prompt,
                model="claude-haiku-4-5"
            )

            response_text = response.strip().upper()

            if "INVALID" in response_text:
                logger.warning(
                    f"🚨 SEMANTIC VALIDATION FAILED - Tool: {tool_name}, "
                    f"Message: {message[:100]}..."
                )
                return False, "Tool use doesn't match user intent (semantic validation failed)"

            return True, None

        except Exception as e:
            logger.error(f"Semantic validation error: {e}")
            # Fail open (allow) if validation fails - avoid blocking legitimate use
            return True, None

    # ========================================================================
    # Utility Methods
    # ========================================================================

    def generate_safe_response(self, threat_type: str) -> str:
        """Generate a safe response for detected threats.

        Args:
            threat_type: Type of threat detected

        Returns:
            Safe response message
        """
        responses = {
            "prompt_injection": (
                "⚠️ I cannot process this request as it appears to contain "
                "instructions that conflict with my security guidelines. "
                "Please rephrase your request in a straightforward manner."
            ),
            "data_extraction": (
                "⚠️ I cannot share sensitive information such as API keys, "
                "passwords, or credentials. This information is protected "
                "and not accessible through conversation."
            ),
            "rate_limit": (
                "⚠️ You've exceeded the rate limit. Please wait a moment "
                "before sending more requests."
            ),
            "semantic_validation": (
                "⚠️ This request doesn't align with typical usage patterns. "
                "Please verify your request and try again."
            )
        }

        return responses.get(threat_type, "⚠️ This request cannot be processed due to security restrictions.")
