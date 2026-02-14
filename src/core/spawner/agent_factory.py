"""Agent factory for creating sub-agent instances."""

import logging
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
        system_prompt: str
    ):
        """Initialize sub-agent.

        Args:
            task: Task for this sub-agent
            api_client: Anthropic API client
            model: Model to use
            tools: Tool registry
            system_prompt: System prompt
        """
        self.task = task
        self.api_client = api_client
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

                # Call API
                response = await self.api_client.create_message(
                    model=self.model,
                    messages=messages,
                    tools=self.tools.get_tool_definitions(),
                    system=self.system_prompt,
                    max_tokens=4096
                )

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

                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result.output if result.success else f"Error: {result.error}"
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

    def __init__(self, api_client: AnthropicClient, config: AgentConfig):
        """Initialize agent factory.

        Args:
            api_client: Anthropic API client
            config: Agent configuration
        """
        self.api_client = api_client
        self.config = config
        self.tools = ToolRegistry()

        logger.info("Initialized AgentFactory")

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
            system_prompt=system_prompt
        )

    def _build_subagent_prompt(self, task: str, context: str) -> str:
        """Build system prompt for sub-agent.

        Args:
            task: Task description
            context: Context from parent

        Returns:
            System prompt string
        """
        prompt = f"""You are a specialized sub-agent focused on: {task}

Context from parent agent:
{context if context else "No additional context provided"}

Your goal is to complete this specific task autonomously and report back with:
1. Summary of what was implemented/accomplished
2. Files created or modified
3. Any issues encountered
4. Suggestions for improvements or next steps

You have access to tools for file operations, bash commands, and web fetching.
Work methodically and test your implementation before reporting completion.

Important:
- Be thorough and check your work
- Handle errors gracefully
- Report clear status updates
- Only report completion when truly done"""

        return prompt
