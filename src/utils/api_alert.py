"""API subscription/billing alert — surfaces critical API issues to Telegram.

Detects authentication failures, billing problems, and quota exhaustion
from Anthropic and Gemini APIs. Sends a single alert per error type per hour
to avoid spam.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Singleton reference — set once at startup via init()
_notifier = None

# Dedup: {alert_key: last_sent_timestamp}
_last_alerted: dict[str, float] = {}
_COOLDOWN_SECS = 3600  # 1 hour between duplicate alerts


def init(telegram_notifier):
    """Wire the TelegramNotifier at startup. Call once from main.py."""
    global _notifier
    _notifier = telegram_notifier


def _is_subscription_error(error) -> Optional[str]:
    """Classify whether an API error is subscription/billing related.

    Returns a human-readable reason string, or None if it's a transient error.
    """
    err_str = str(error).lower()
    status = getattr(error, "status_code", None) or getattr(error, "status", None)

    # --- Anthropic SDK error types ---
    try:
        import anthropic
        if isinstance(error, anthropic.AuthenticationError):
            return "Anthropic API key is invalid or expired"
        if isinstance(error, anthropic.PermissionDeniedError):
            return "Anthropic account access denied (suspended or restricted)"
    except ImportError:
        pass

    # --- HTTP status codes (works for both providers) ---
    if status == 401:
        return "API key is invalid or expired"
    if status == 402:
        return "Payment required — billing issue"
    if status == 403:
        return "Account access denied — may be suspended"

    # --- Quota / billing keywords in error body ---
    billing_keywords = [
        "insufficient_quota", "insufficient quota",
        "billing", "payment required", "credit",
        "exceeded your current quota",
        "account has been deactivated",
        "your api key has been disabled",
        "plan limit", "spending limit",
    ]
    for kw in billing_keywords:
        if kw in err_str:
            return f"Billing/quota issue detected ({kw})"

    # --- 429 that mentions quota (not just rate limit) ---
    if status == 429 or "429" in err_str:
        quota_hints = ["quota", "billing", "credit", "limit exceeded", "spending"]
        for hint in quota_hints:
            if hint in err_str:
                return f"Quota exhausted ({hint})"

    return None


async def check_and_alert(error, provider: str):
    """Check if error is subscription-related and alert via Telegram.

    Call this from API client catch blocks. Safe to call for any error —
    non-subscription errors are silently ignored.

    Args:
        error: The exception from the API call
        provider: "Anthropic" or "Gemini"
    """
    reason = _is_subscription_error(error)
    if not reason:
        return  # Transient error, not subscription-related

    alert_key = f"{provider}:{reason}"
    now = time.time()

    # Dedup — skip if we already alerted for this exact issue within cooldown
    if alert_key in _last_alerted:
        if now - _last_alerted[alert_key] < _COOLDOWN_SECS:
            return

    _last_alerted[alert_key] = now

    message = (
        f"*{provider} API — Subscription Issue*\n\n"
        f"{reason}\n\n"
        f"Nova's {provider} calls are failing. "
        f"Please check your {provider} dashboard and fix the issue."
    )

    logger.critical(f"SUBSCRIPTION ALERT [{provider}]: {reason}")

    if _notifier:
        try:
            await _notifier.notify(message, level="error")
        except Exception as e:
            logger.error(f"Failed to send subscription alert to Telegram: {e}")
    else:
        logger.warning("api_alert: TelegramNotifier not wired — alert not sent")


def check_and_alert_sync(error, provider: str):
    """Sync wrapper for check_and_alert — safe to call from sync or async context."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(check_and_alert(error, provider))
    except RuntimeError:
        # No event loop — log only
        reason = _is_subscription_error(error)
        if reason:
            logger.critical(f"SUBSCRIPTION ALERT [{provider}]: {reason} (no event loop for Telegram)")
