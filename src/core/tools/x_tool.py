"""X (Twitter) posting tool using X API v2 with OAuth 1.0a User Context."""

import logging
from typing import Optional
from .base import BaseTool
from ..types import ToolResult

logger = logging.getLogger(__name__)


class XTool(BaseTool):
    """Tool for posting to X (Twitter) using the X API v2.

    Uses OAuth 1.0a User Context for write operations (post/delete tweets).
    Only posts when explicitly asked by the user — no auto-interactions.
    """

    name = "x_post"
    description = "Post tweets to X (Twitter). Can post new tweets and delete tweets."
    parameters = {
        "operation": {
            "type": "string",
            "description": "Operation: 'post_tweet' or 'delete_tweet'",
            "enum": ["post_tweet", "delete_tweet"]
        },
        "content": {
            "type": "string",
            "description": "Tweet text content (max 280 characters, for post_tweet)"
        },
        "tweet_id": {
            "type": "string",
            "description": "Tweet ID to delete (for delete_tweet)"
        }
    }

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_token_secret: str
    ):
        """Initialize X tool with OAuth 1.0a credentials.

        Args:
            api_key: X API Key (Consumer Key)
            api_secret: X API Secret (Consumer Secret)
            access_token: OAuth 1.0a Access Token
            access_token_secret: OAuth 1.0a Access Token Secret
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.api_base = "https://api.x.com/2"

    def _get_oauth1_session(self):
        """Create an OAuth 1.0a session using requests_oauthlib."""
        from requests_oauthlib import OAuth1Session
        return OAuth1Session(
            self.api_key,
            client_secret=self.api_secret,
            resource_owner_key=self.access_token,
            resource_owner_secret=self.access_token_secret
        )

    async def execute(
        self,
        operation: str,
        content: Optional[str] = None,
        tweet_id: Optional[str] = None,
        **kwargs
    ) -> ToolResult:
        """Execute X operation."""
        try:
            if operation == "post_tweet":
                return await self._post_tweet(content)
            elif operation == "delete_tweet":
                return await self._delete_tweet(tweet_id)
            else:
                return ToolResult(
                    success=False,
                    error=f"Unknown operation: {operation}"
                )
        except ImportError as e:
            return ToolResult(
                success=False,
                error="Missing dependency: pip install requests-oauthlib"
            )
        except Exception as e:
            logger.error(f"X operation error: {e}", exc_info=True)
            return ToolResult(
                success=False,
                error=f"X operation failed: {str(e)}"
            )

    async def _post_tweet(self, content: Optional[str]) -> ToolResult:
        """Post a tweet to X."""
        import asyncio

        if not content:
            return ToolResult(success=False, error="Tweet content is required")

        if len(content) > 280:
            return ToolResult(
                success=False,
                error=f"Tweet too long ({len(content)} chars). Max is 280."
            )

        def _do_post():
            oauth = self._get_oauth1_session()
            resp = oauth.post(
                f"{self.api_base}/tweets",
                json={"text": content}
            )
            return resp

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, _do_post)

        if resp.status_code in (200, 201):
            data = resp.json()
            tweet_id = data.get("data", {}).get("id", "unknown")
            logger.info(f"Tweet posted: {tweet_id}")
            return ToolResult(
                success=True,
                output=f"Posted to X. Tweet ID: {tweet_id}",
                metadata={"tweet_id": tweet_id}
            )
        else:
            try:
                error_data = resp.json()
                error_detail = error_data.get("detail", error_data.get("title", str(error_data)))
            except Exception:
                error_detail = resp.text
            logger.error(f"X API error: {resp.status_code} - {error_detail}")
            return ToolResult(
                success=False,
                error=f"X API error ({resp.status_code}): {error_detail}"
            )

    async def _delete_tweet(self, tweet_id: Optional[str]) -> ToolResult:
        """Delete a tweet from X."""
        import asyncio

        if not tweet_id:
            return ToolResult(success=False, error="tweet_id is required")

        def _do_delete():
            oauth = self._get_oauth1_session()
            return oauth.delete(f"{self.api_base}/tweets/{tweet_id}")

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, _do_delete)

        if resp.status_code == 200:
            logger.info(f"Tweet deleted: {tweet_id}")
            return ToolResult(
                success=True,
                output=f"Tweet {tweet_id} deleted."
            )
        else:
            try:
                error_data = resp.json()
                error_detail = error_data.get("detail", str(error_data))
            except Exception:
                error_detail = resp.text
            return ToolResult(
                success=False,
                error=f"Failed to delete tweet ({resp.status_code}): {error_detail}"
            )
