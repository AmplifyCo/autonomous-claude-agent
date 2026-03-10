"""Gemini client via LiteLLM — supports both text and tool-use calls.

Used for:
- Intent parsing (cheap, fast, 1M context window)
- Simple/trivial chat responses
- Fallback when Claude hits rate limits (including tool use)
- Agent loop fallback when Claude is unavailable

Supports:
- Plain text generation
- Tool/function calling via LiteLLM translation
"""

import os
import json
import logging
import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from ..utils import api_alert

logger = logging.getLogger(__name__)


@dataclass
class GeminiContentBlock:
    """Mimics anthropic ContentBlock interface."""
    type: str = "text"
    text: str = ""


@dataclass
class GeminiToolUseBlock:
    """Mimics anthropic ToolUseBlock for tool calls."""
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GeminiUsage:
    """Mimics anthropic Usage interface."""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class GeminiResponse:
    """Mimics anthropic.types.Message so ConversationManager needs no changes.

    Callers access: response.content[0].text, response.stop_reason, response.usage
    """
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: GeminiUsage = field(default_factory=GeminiUsage)


class GeminiClient:
    """LiteLLM-based unified LLM client — routes to both Gemini and Claude.

    All LLM calls go through LiteLLM for seamless provider switching.
    Model strings: "gemini/gemini-2.0-flash", "anthropic/claude-sonnet-4-5", etc.
    """

    def __init__(self, api_key: str, anthropic_api_key: str = ""):
        """Initialize LiteLLM client.

        Args:
            api_key: Google AI API key (from aistudio.google.com/apikey)
            anthropic_api_key: Anthropic API key (for Claude via LiteLLM)
        """
        self.api_key = api_key
        self.enabled = bool(api_key)

        if self.enabled:
            os.environ["GEMINI_API_KEY"] = api_key
            logger.info("✨ LiteLLM client initialized (Gemini + Claude routing)")
        else:
            logger.info("LiteLLM client disabled (no GEMINI_API_KEY)")

        # Also set Anthropic key so LiteLLM can route Claude calls
        if anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key

    # Fields that Gemini function declarations support
    _GEMINI_ALLOWED_FIELDS = {
        "type", "description", "properties", "required", "items",
        "nullable", "format"
    }

    def _sanitize_schema(self, schema: Any, _depth: int = 0) -> Any:
        """Recursively sanitize JSON schema for Gemini/LiteLLM compatibility.

        Gemini function declarations only support a subset of JSON Schema.
        This strips unsupported fields (default, enum, anyOf, etc.) and
        flattens nested objects that Gemini can't handle.
        """
        # Primitives stay as-is (don't wrap strings into {"type": str})
        if schema is None or isinstance(schema, (str, int, float, bool)):
            return schema

        if isinstance(schema, list):
            return [
                self._sanitize_schema(item, _depth + 1)
                if isinstance(item, (dict, list)) else item
                for item in schema
            ]

        if isinstance(schema, dict):
            sanitized = {}
            for k, v in schema.items():
                # Strip fields Gemini doesn't support
                if k in ("anyOf", "allOf", "oneOf", "enum", "default",
                          "title", "examples", "$ref", "$schema",
                          "additionalProperties", "minItems", "maxItems",
                          "minimum", "maximum", "pattern", "minLength",
                          "maxLength", "exclusiveMinimum", "exclusiveMaximum"):
                    continue

                # Special handling for properties dict
                if k == "properties" and isinstance(v, dict):
                    prop_dict = {}
                    for pk, pv in v.items():
                        if isinstance(pv, str):
                            prop_dict[pk] = {"type": pv}
                        elif isinstance(pv, dict):
                            cleaned = self._sanitize_schema(pv, _depth + 1)
                            # Flatten nested objects to string for Gemini safety
                            if (isinstance(cleaned, dict) and
                                    cleaned.get("type") == "object" and
                                    "properties" in cleaned):
                                prop_dict[pk] = {
                                    "type": "string",
                                    "description": cleaned.get(
                                        "description",
                                        f"JSON string for {pk}"
                                    )
                                }
                            else:
                                prop_dict[pk] = cleaned
                        elif isinstance(pv, list):
                            prop_dict[pk] = {"type": "string"}
                        else:
                            prop_dict[pk] = {"type": "string"}
                    sanitized[k] = prop_dict
                else:
                    sanitized[k] = self._sanitize_schema(v, _depth + 1)

            return sanitized

        return schema

    def _convert_tools_for_litellm(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Anthropic tool format to OpenAI/LiteLLM function format.

        Anthropic format:
            {"name": "x", "description": "y", "input_schema": {"type": "object", "properties": {...}}}

        OpenAI/LiteLLM format:
            {"type": "function", "function": {"name": "x", "description": "y", "parameters": {...}}}
        """
        litellm_tools = []
        for tool in tools:
            # Sanitize the schema aggressively before handing to LiteLLM
            raw_schema = tool.get("input_schema", {})
            safe_schema = self._sanitize_schema(raw_schema)
            
            litellm_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": safe_schema
                }
            })
        return litellm_tools

    def _convert_messages_for_litellm(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert Anthropic-format messages to OpenAI/LiteLLM format.

        Handles:
        - Plain text messages (pass through)
        - Tool-use content blocks → assistant function calls
        - Tool-result content blocks → tool response messages
        """
        litellm_messages = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                litellm_messages.append({"role": role, "content": content})

            elif isinstance(content, list):
                # Process Anthropic content blocks
                text_parts = []
                tool_calls = []
                tool_results = []

                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "")

                        if block_type == "text":
                            text_parts.append(block.get("text", ""))

                        elif block_type == "tool_use":
                            tool_calls.append({
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input", {}))
                                }
                            })

                        elif block_type == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                result_content = " ".join(
                                    b.get("text", "") for b in result_content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": str(result_content)
                            })
                    else:
                        # Handle anthropic SDK objects (ContentBlock, ToolUseBlock, etc.)
                        block_type = getattr(block, "type", "")

                        if block_type == "text":
                            text_parts.append(getattr(block, "text", ""))

                        elif block_type == "tool_use":
                            tool_calls.append({
                                "id": getattr(block, "id", ""),
                                "type": "function",
                                "function": {
                                    "name": getattr(block, "name", ""),
                                    "arguments": json.dumps(getattr(block, "input", {}))
                                }
                            })

                # Add text + tool calls as assistant message
                if tool_calls:
                    msg_data = {
                        "role": "assistant",
                        "content": " ".join(text_parts) if text_parts else None,
                        "tool_calls": tool_calls
                    }
                    litellm_messages.append(msg_data)
                elif text_parts:
                    litellm_messages.append({"role": role, "content": " ".join(text_parts)})

                # Add tool results as separate messages
                for tr in tool_results:
                    litellm_messages.append(tr)

        return litellm_messages

    async def create_message(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        max_tokens: int = 1024,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> GeminiResponse:
        """Send a message to Gemini and return Anthropic-compatible response.

        Supports both plain text and tool-use calls.

        Args:
            model: LiteLLM model string e.g. "gemini/gemini-2.0-flash"
            messages: Conversation messages (Anthropic or OpenAI format)
            system: System prompt (prepended as system role message)
            max_tokens: Max tokens to generate
            tools: Optional tool definitions (Anthropic format)

        Returns:
            GeminiResponse with .content, .stop_reason, .usage
        """
        try:
            import litellm
            litellm.suppress_debug_info = True
            from litellm.exceptions import RateLimitError

            # Retry configuration
            max_retries = 3
            base_delay = 2.0  # Start with 2s delay

            # Build messages for LiteLLM
            litellm_messages = []
            if system:
                litellm_messages.append({"role": "system", "content": system})

            converted = self._convert_messages_for_litellm(messages)
            litellm_messages.extend(converted)

            if not litellm_messages:
                return GeminiResponse(
                    content=[GeminiContentBlock(text="")],
                    stop_reason="end_turn"
                )

            # Build kwargs for LiteLLM
            call_kwargs = {
                "model": model,
                "messages": litellm_messages,
                "max_tokens": max_tokens,
            }

            # Route API key by provider — don't pass Gemini key for Anthropic models
            is_anthropic = model.startswith("anthropic/")
            if is_anthropic:
                # LiteLLM uses ANTHROPIC_API_KEY from env for Claude models
                pass
            else:
                # Gemini or other models — pass the Gemini key
                call_kwargs["api_key"] = self.api_key
                if self.api_key:
                    os.environ["GEMINI_API_KEY"] = self.api_key

            # Add tools if provided
            if tools:
                call_kwargs["tools"] = self._convert_tools_for_litellm(tools)

            # Retry Loop
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    response = await litellm.acompletion(**call_kwargs)
                    break # Success
                except Exception as e:
                    last_exception = e
                    is_rate_limit = "429" in str(e) or "Resource exhausted" in str(e) or isinstance(e, RateLimitError)
                    
                    if is_rate_limit and attempt < max_retries:
                        delay = base_delay * (2 ** attempt) # 2s, 4s, 8s
                        logger.warning(f"Gemini Rate Limit ({model}). Retrying in {delay}s... (Attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(delay)
                    else:
                        raise e # Re-raise if not rate limit or max retries exceeded

            choice = response.choices[0]
            message = choice.message
            finish_reason = choice.finish_reason or "stop"
            usage = response.usage

            logger.info(
                f"Gemini ({model}): {getattr(usage, 'prompt_tokens', 0)} in / "
                f"{getattr(usage, 'completion_tokens', 0)} out"
            )

            # Check if Gemini wants to call tools
            if hasattr(message, 'tool_calls') and message.tool_calls:
                content_blocks = []

                # Add any text content first
                if message.content:
                    content_blocks.append(GeminiContentBlock(type="text", text=message.content))

                # Add tool use blocks
                for tc in message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
                    except (json.JSONDecodeError, AttributeError):
                        args = {}

                    content_blocks.append(GeminiToolUseBlock(
                        type="tool_use",
                        id=tc.id or f"toolu_gemini_{id(tc)}",
                        name=tc.function.name,
                        input=args
                    ))

                return GeminiResponse(
                    content=content_blocks,
                    stop_reason="tool_use",
                    usage=GeminiUsage(
                        input_tokens=getattr(usage, "prompt_tokens", 0),
                        output_tokens=getattr(usage, "completion_tokens", 0),
                    ),
                )

            # Plain text response
            text = message.content or ""
            return GeminiResponse(
                content=[GeminiContentBlock(type="text", text=text)],
                stop_reason="end_turn" if finish_reason == "stop" else "max_tokens",
                usage=GeminiUsage(
                    input_tokens=getattr(usage, "prompt_tokens", 0),
                    output_tokens=getattr(usage, "completion_tokens", 0),
                ),
            )

        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            await api_alert.check_and_alert(e, "Gemini")
            raise

    async def test_connection(self) -> bool:
        """Test if Gemini API is reachable.

        Returns:
            True if connection successful
        """
        try:
            response = await self.create_message(
                model="gemini/gemini-2.0-flash",
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
            )
            # GeminiResponse has a content list property
            return len(response.content) > 0 and bool(response.content[0].text)
        except Exception as e:
            logger.error(f"Gemini connection test failed: {e}")
            return False
