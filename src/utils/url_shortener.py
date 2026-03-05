"""URL shortener utility — shortens long URLs in Nova's responses.

Uses free API (no auth). Falls back to original URL on failure.
"""

import logging
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_API_URL = "https://is.gd/create.php"
_MIN_URL_LENGTH = 60  # Only shorten URLs longer than this
_TIMEOUT = 5  # seconds

# Match URLs in text (http/https, no trailing punctuation)
_URL_PATTERN = re.compile(
    r'(https?://[^\s<>\"\'\)]+)',
    re.IGNORECASE,
)


async def shorten_url(long_url: str) -> Optional[str]:
    """Shorten a single URL. Returns short URL or None on failure."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _API_URL,
                params={"format": "simple", "url": long_url},
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    short = (await resp.text()).strip()
                    if short.startswith("http"):
                        return short
    except Exception as e:
        logger.debug(f"URL shortener failed for {long_url[:60]}: {e}")
    return None


async def shorten_urls_in_text(text: str) -> str:
    """Find long URLs in text and replace them with shortened versions.

    Only shortens URLs longer than _MIN_URL_LENGTH chars.
    Fail-open: returns original text if shortening fails.
    """
    urls = _URL_PATTERN.findall(text)
    if not urls:
        return text

    result = text
    for url in urls:
        # Strip trailing punctuation that got captured
        clean_url = url.rstrip(".,;:!?)")
        if len(clean_url) < _MIN_URL_LENGTH:
            continue
        short = await shorten_url(clean_url)
        if short:
            result = result.replace(clean_url, short)
            logger.debug(f"Shortened URL: {clean_url[:40]}... → {short}")

    return result
