"""Agent factory for creating sub-agent instances.

Creates lightweight SubAgents that share the parent agent's tools and API client.
Each SubAgent runs its own ReAct loop with isolated message history, so multiple
sub-agents can execute concurrently via asyncio.gather without interference.
"""

import logging
import os
from typing import List, Optional, Dict, Any

from ..config import AgentConfig
from ..types import SubAgentResult
from ..tools.base import BaseTool
from ..tools.registry import ToolRegistry
from ...integrations.anthropic_client import AnthropicClient

logger = logging.getLogger(__name__)


class SubAgent:
    """A sub-agent created to handle a specific task."""

    def __init__(
        self,
        task: str,
        api_client: AnthropicClient,
        model: str,
        tools: ToolRegistry,
        system_prompt: str,
        gemini_client=None,
    ):
        """Initialize sub-agent.

        Args:
            task: Task for this sub-agent
            api_client: Anthropic API client
            model: Model to use
            tools: Tool registry
            system_prompt: System prompt
            gemini_client: Optional GeminiClient for Gemini models
        """
        self.task = task
        self.api_client = api_client
        self.gemini_client = gemini_client
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt

        logger.info(f"Created SubAgent for task: {task[:50]}...")

    async def run(self, max_iterations: int = 20) -> SubAgentResult:
        """Execute the sub-agent's task.

        Args:
            max_iterations: Maximum iterations

        Returns:
            SubAgentResult with execution summary
        """
        logger.info(f"SubAgent starting execution: {self.task[:50]}...")

        messages = [{"role": "user", "content": self.task}]
        files_created = []
        files_modified = []
        iteration = 0

        try:
            while iteration < max_iterations:
                iteration += 1

                # Route to correct client based on model prefix
                is_gemini = self.model.startswith("gemini/")
                if is_gemini and not self.gemini_client:
                    # Gemini client unavailable — fall back to Claude Sonnet
                    logger.warning(
                        f"SubAgent: model={self.model} but gemini_client is None — "
                        f"falling back to Claude Sonnet"
                    )
                    self.model = "claude-sonnet-4-20250514"
                    is_gemini = False
                client = (
                    self.gemini_client
                    if is_gemini and self.gemini_client
                    else self.api_client
                )
                # Retry with backoff for rate limits (429)
                for attempt in range(3):
                    try:
                        response = await client.create_message(
                            model=self.model,
                            messages=messages,
                            tools=self.tools.get_tool_definitions(),
                            system=self.system_prompt,
                            max_tokens=4096
                        )
                        break  # success
                    except Exception as api_err:
                        if "429" in str(api_err) or "rate_limit" in str(api_err):
                            wait = (attempt + 1) * 15  # 15s, 30s, 45s
                            logger.warning(f"SubAgent rate limited, waiting {wait}s (attempt {attempt+1}/3)")
                            import asyncio as _asyncio
                            await _asyncio.sleep(wait)
                            if attempt == 2:
                                raise  # exhausted retries
                        else:
                            raise

                # Check stop reason
                if response.stop_reason == "end_turn":
                    # Extract final response
                    summary = self._extract_text(response)
                    logger.info(f"SubAgent completed task")

                    return SubAgentResult(
                        success=True,
                        summary=summary,
                        files_created=files_created,
                        files_modified=files_modified
                    )

                elif response.stop_reason == "tool_use":
                    # Execute tools
                    messages.append({"role": "assistant", "content": response.content})

                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            result = await self.tools.execute_tool(
                                block.name,
                                **block.input
                            )

                            # Track file operations
                            if block.name == "file_operations":
                                op = block.input.get("operation")
                                path = block.input.get("path")
                                if op == "write" and path:
                                    files_created.append(path)
                                elif op in ["edit", "write"] and path:
                                    files_modified.append(path)

                            # Multimodal (screenshot+text) or plain string
                            if result.success and result.content_blocks is not None:
                                content = result.content_blocks
                            elif result.success:
                                content = result.output or ""
                            else:
                                content = f"Error: {result.error}"

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": content,
                            })

                    messages.append({"role": "user", "content": tool_results})

            # Max iterations reached
            logger.warning(f"SubAgent hit max iterations: {self.task[:50]}")
            return SubAgentResult(
                success=False,
                summary="Max iterations reached",
                error="Task incomplete after max iterations",
                files_created=files_created,
                files_modified=files_modified
            )

        except Exception as e:
            logger.error(f"SubAgent error: {e}", exc_info=True)
            return SubAgentResult(
                success=False,
                summary="Error during execution",
                error=str(e),
                files_created=files_created,
                files_modified=files_modified
            )

    def _extract_text(self, response) -> str:
        """Extract text from response."""
        parts = []
        for block in response.content:
            if hasattr(block, 'text'):
                parts.append(block.text)
        return "\n".join(parts)


class AgentFactory:
    """Factory for creating sub-agent instances via Claude API."""

    def __init__(self, api_client: AnthropicClient, config: AgentConfig, gemini_client=None):
        """Initialize agent factory.

        Args:
            api_client: Anthropic API client
            config: Agent configuration
            gemini_client: Optional GeminiClient for Gemini model routing
        """
        self.api_client = api_client
        self.gemini_client = gemini_client
        self.config = config
        self.tools = ToolRegistry()

        logger.info(f"Initialized AgentFactory (gemini={'yes' if gemini_client else 'no'})")

    def set_tools(self, tools: ToolRegistry):
        """Share parent agent's tool registry with sub-agents.

        Call this after registering all tools on the parent agent so
        sub-agents inherit the same capabilities (web_search, file_ops, etc.).
        """
        self.tools = tools
        tool_count = len(tools.tools) if hasattr(tools, 'tools') else 0
        logger.info(f"AgentFactory: shared {tool_count} tools from parent agent")

    async def create_agent(
        self,
        task: str,
        model: Optional[str] = None,
        context: str = ""
    ) -> SubAgent:
        """Create a new sub-agent for a specific task.

        Args:
            task: Task description
            model: Model to use (defaults to subagent model from config)
            context: Optional context from parent agent

        Returns:
            SubAgent instance
        """
        model = model or self.config.subagent_model

        system_prompt = self._build_subagent_prompt(task, context)

        return SubAgent(
            task=task,
            api_client=self.api_client,
            model=model,
            tools=self.tools,
            system_prompt=system_prompt,
            gemini_client=self.gemini_client,
        )

    def _build_subagent_prompt(self, task: str, context: str) -> str:
        """Build system prompt for sub-agent with Nova's identity context."""
        from ..config import get_bot_name, get_owner_name
        bot_name = get_bot_name()
        owner_name = get_owner_name()

        prompt = (
            f"IDENTITY: You are a worker sub-agent of {bot_name}, {owner_name}'s AI assistant.\n"
            f"You are executing one step of a larger task. Complete your step and report results.\n\n"
        )
        if context:
            prompt += f"CONTEXT FROM PREVIOUS STEPS:\n{context}\n\n"
        prompt += (
            f"YOUR TASK: {task}\n\n"
            f"RULES:\n"
            f"- Complete this specific step only — don't attempt other steps\n"
            f"- Use available tools to accomplish the task\n"
            f"- Report what you found/did clearly and concisely\n"
            f"- If a tool fails, try an alternative approach before giving up\n"
            f"- Never mention technical internals (tool names, APIs) in user-facing content\n"
            f"- When writing content as {owner_name}, maintain their voice and style\n"
        )
        return prompt
