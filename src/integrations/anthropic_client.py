"""Anthropic API client wrapper for Claude."""

import anthropic
from typing import List, Dict, Any, Optional
import asyncio
import logging

logger = logging.getLogger(__name__)


class AnthropicClient:
    """Wrapper for Anthropic API to handle Claude interactions."""

    def __init__(self, api_key: str):
        """Initialize the Anthropic client.

        Args:
            api_key: Anthropic API key
        """
        self.client = anthropic.Anthropic(api_key=api_key)
        self.api_key = api_key

    async def create_message(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> anthropic.types.Message:
        """Create a message with Claude API.

        Args:
            model: Model ID (e.g., 'claude-opus-4-6')
            messages: List of messages in conversation
            tools: Optional list of tool definitions
            system: Optional system prompt
            max_tokens: Maximum tokens to generate
            temperature: Temperature for sampling

        Returns:
            Message response from Claude
        """
        try:
            # Run sync client in executor to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.messages.create(
                    model=model,
                    messages=messages,
                    tools=tools or [],
                    system=system or "You are a helpful AI assistant.",
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            )

            # Log token usage
            usage = response.usage
            logger.info(f"API call: {usage.input_tokens} in / {usage.output_tokens} out")

            return response

        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error calling Claude API: {e}")
            raise

    async def create_message_stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[str] = None,
        max_tokens: int = 4096,
    ):
        """Create a streaming message with Claude API.

        Args:
            model: Model ID
            messages: List of messages
            tools: Optional tool definitions
            system: Optional system prompt
            max_tokens: Maximum tokens

        Yields:
            Message chunks from Claude
        """
        try:
            with self.client.messages.stream(
                model=model,
                messages=messages,
                tools=tools or [],
                system=system or "You are a helpful AI assistant.",
                max_tokens=max_tokens,
            ) as stream:
                for chunk in stream:
                    yield chunk

        except anthropic.APIError as e:
            logger.error(f"Anthropic API streaming error: {e}")
            raise

    def count_tokens(self, text: str) -> int:
        """Estimate token count for text.

        Args:
            text: Text to count tokens for

        Returns:
            Approximate token count
        """
        # Rough estimation: ~4 characters per token
        return len(text) // 4

    async def test_connection(self) -> bool:
        """Test if API connection works.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            response = await self.create_message(
                model="claude-sonnet-4-5",  # Use cheaper model for test
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=10,
            )
            return response.stop_reason is not None
        except Exception as e:
            logger.error(f"API connection test failed: {e}")
            return False
