"""LinkedIn Tool — post text and articles to LinkedIn on principal's behalf.

Uses LinkedIn Posts REST API (replaces legacy UGC Posts API).
Requires Linkedin-Version header and OAuth 2.0 3-legged token.

Setup (one-time):
    python scripts/linkedin_auth.py

Environment variables required:
    LINKEDIN_ACCESS_TOKEN   — OAuth access token (from setup script)
    LINKEDIN_PERSON_URN     — urn:li:person:XXXXXXX (from setup script)

Rate limits:
    150 posts/day per member (official LinkedIn limit)

API docs:
    https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
"""

import logging
from pathlib import Path
from typing import Optional

import aiohttp

from .base import BaseTool
from ..types import ToolResult

logger = logging.getLogger(__name__)

# ── LinkedIn Content Composition Guide ────────────────────────────────────────
# User-editable copy in data/guides/linkedin.md (survives git pulls).
# Falls back to source default if data/guides/ copy doesn't exist.
_USER_GUIDE = Path("data/guides/linkedin.md")
_SRC_GUIDE = Path(__file__).parent / "linkedin_guide.md"
_guide_path = _USER_GUIDE if _USER_GUIDE.exists() else _SRC_GUIDE
try:
    _CONTENT_GUIDE = _guide_path.read_text(encoding="utf-8").strip()
    logger.debug("LinkedIn content guide loaded from %s", _guide_path)
except FileNotFoundError:
    logger.warning("LinkedIn content guide not found — using empty")
    _CONTENT_GUIDE = ""

# New Posts REST API (replaces legacy /v2/ugcPosts)
_POSTS_URL = "https://api.linkedin.com/rest/posts"

# LinkedIn API version — YYYYMM format, update periodically
_LINKEDIN_VERSION = "202602"


def _base_headers(access_token: str) -> dict:
    """Standard headers required by all LinkedIn REST API calls."""
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": _LINKEDIN_VERSION,
    }


class LinkedInTool(BaseTool):
    """Tool to post to LinkedIn on principal's behalf via the official Posts REST API.

    Supports text posts and article/URL shares. Uses OAuth 2.0 — no browser
    automation, no ToS violation, no ban risk.

    Post URL returned on creation so user can verify directly.
    """

    name = "linkedin"
    description = (
        "Post to LinkedIn on principal's behalf using the official LinkedIn API. "
        "Operations: 'post_text' (text only), 'post_article' (URL + commentary), "
        "'delete_post' (remove a post by its URN/ID). "
        "Returns the post URL on success.\n\n"
        "IMPORTANT — You are a professional content writer for LinkedIn. "
        "Follow the content guide below when composing posts:\n"
        + _CONTENT_GUIDE
    )
    parameters = {
        "operation": {
            "type": "string",
            "enum": ["post_text", "post_article", "delete_post"],
            "description": (
                "'post_text': publish a text-only LinkedIn post. "
                "'post_article': share a URL with commentary (and optional title/description). "
                "'delete_post': delete a post by its URN (e.g. 'urn:li:share:12345')."
            ),
        },
        "text": {
            "type": "string",
            "description": (
                "The full post text to publish. You MUST compose this yourself following the "
                "content guide in the tool description. Use Unicode bold (𝗮-𝘇) for headers, "
                "Unicode italic (𝘢-𝘻) for emphasis, • for bullets, → for arrows, and blank "
                "lines between paragraphs. NEVER use markdown syntax."
            ),
        },
        "post_length": {
            "type": "string",
            "enum": ["short", "medium", "long"],
            "description": (
                "Length tier for the post. Determines structure and depth:\n"
                "  'short': 1-3 lines, punchy insight or hot take (~100-300 chars)\n"
                "  'medium': 5-10 lines, structured insight with hook+takeaway (~500-1200 chars)\n"
                "  'long': 12-25 lines, thought leadership with story+analysis+CTA (~1500-3000 chars)\n"
                "Default: 'medium'. When user says 'detailed post' or 'write a long post', use 'long'. "
                "When user says 'quick thought' or just a brief mention, use 'short'."
            ),
        },
        "url": {
            "type": "string",
            "description": "URL to share. Required for 'post_article'.",
        },
        "title": {
            "type": "string",
            "description": "Optional title for the shared article.",
        },
        "article_description": {
            "type": "string",
            "description": "Optional description for the shared article.",
        },
        "visibility": {
            "type": "string",
            "enum": ["PUBLIC", "CONNECTIONS"],
            "description": "Who can see the post. Default: PUBLIC.",
        },
        "post_urn": {
            "type": "string",
            "description": "Post URN for delete_post. Format: 'urn:li:share:12345'.",
        },
    }

    def to_anthropic_tool(self):
        """Override — only operation is required; other params are contextual."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": ["operation"],
            },
        }

    def __init__(self, access_token: str, person_urn: str):
        """
        Args:
            access_token: LinkedIn OAuth 2.0 access token
            person_urn: LinkedIn person URN, e.g. 'urn:li:person:XXXXXXX'
        """
        self.access_token = access_token
        self.person_urn = person_urn

    async def execute(
        self,
        operation: str,
        text: Optional[str] = None,
        url: Optional[str] = None,
        title: Optional[str] = None,
        article_description: Optional[str] = None,
        visibility: str = "PUBLIC",
        post_urn: Optional[str] = None,
        post_length: str = "medium",
        **kwargs,
    ) -> ToolResult:
        if operation == "delete_post":
            if not post_urn or not post_urn.strip():
                return ToolResult(success=False, error="'post_urn' is required for delete_post")
            return await self._delete_post(post_urn.strip())

        if not text or not text.strip():
            return ToolResult(success=False, error="'text' is required")

        # Log the requested post length for debugging
        logger.info(f"LinkedIn post_length={post_length}, text_len={len(text.strip())}")

        if operation == "post_text":
            return await self._post_text(text.strip(), visibility)
        elif operation == "post_article":
            if not url or not url.strip():
                return ToolResult(success=False, error="'url' is required for post_article")
            return await self._post_article(
                text.strip(), url.strip(), title, article_description, visibility
            )
        else:
            return ToolResult(success=False, error=f"Unknown operation: {operation}")

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _post_text(self, text: str, visibility: str) -> ToolResult:
        """Create a text-only LinkedIn post via Posts REST API."""
        body = {
            "author": self.person_urn,
            "commentary": text,
            "visibility": visibility,
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        return await self._create_post(body)

    async def _post_article(
        self,
        text: str,
        url: str,
        title: Optional[str],
        description: Optional[str],
        visibility: str,
    ) -> ToolResult:
        """Create an article/URL share post via Posts REST API."""
        article: dict = {"source": url}
        if title:
            article["title"] = title
        if description:
            article["description"] = description

        body = {
            "author": self.person_urn,
            "commentary": text,
            "visibility": visibility,
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "content": {
                "article": article,
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        return await self._create_post(body)

    async def _delete_post(self, post_urn: str) -> ToolResult:
        """Delete a LinkedIn post by URN via Posts REST API."""
        from urllib.parse import quote
        encoded_urn = quote(post_urn, safe="")
        delete_url = f"{_POSTS_URL}/{encoded_urn}"
        headers = {
            **_base_headers(self.access_token),
            "X-RestLi-Method": "DELETE",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    delete_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 204:
                        logger.info(f"LinkedIn post deleted: {post_urn}")
                        return ToolResult(success=True, output=f"Post deleted: {post_urn}")
                    elif resp.status == 401:
                        return ToolResult(
                            success=False,
                            error="LinkedIn access token expired. Run: python scripts/linkedin_auth.py to refresh.",
                        )
                    else:
                        error_body = await resp.text()
                        logger.error(f"LinkedIn delete {resp.status}: {error_body[:300]}")
                        return ToolResult(
                            success=False,
                            error=f"Failed to delete post ({resp.status}): {error_body[:200]}",
                        )
        except Exception as e:
            logger.error(f"LinkedIn delete failed: {e}", exc_info=True)
            return ToolResult(success=False, error=f"Delete failed: {e}")

    async def _create_post(self, body: dict) -> ToolResult:
        """Create a post via LinkedIn Posts REST API."""
        headers = {
            **_base_headers(self.access_token),
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _POSTS_URL, json=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 201:
                        post_id = resp.headers.get("x-restli-id", "unknown")
                        from urllib.parse import quote
                        post_url = f"https://www.linkedin.com/feed/update/{quote(post_id, safe=':')}"
                        logger.info(f"LinkedIn post created: {post_id} — {post_url}")
                        return ToolResult(
                            success=True,
                            output=(
                                f"LinkedIn post published!\n"
                                f"Post ID: {post_id}\n"
                                f"URL: {post_url}"
                            ),
                            metadata={"post_id": post_id, "post_url": post_url},
                        )
                    else:
                        error_body = await resp.text()
                        logger.error(f"LinkedIn API {resp.status}: {error_body[:300]}")
                        if resp.status == 401:
                            return ToolResult(
                                success=False,
                                error=(
                                    "LinkedIn access token expired. "
                                    "Run: python scripts/linkedin_auth.py to refresh."
                                ),
                            )
                        return ToolResult(
                            success=False,
                            error=f"LinkedIn API error {resp.status}: {error_body[:200]}",
                        )
        except aiohttp.ClientError as e:
            logger.error(f"LinkedIn network error: {e}")
            return ToolResult(success=False, error=f"LinkedIn network error: {e}")
        except Exception as e:
            logger.error(f"LinkedIn post failed: {e}", exc_info=True)
            return ToolResult(success=False, error=f"LinkedIn post failed: {e}")
