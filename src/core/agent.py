"""Core autonomous agent with self-contained execution loop."""

import logging
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
import json

from .config import AgentConfig
from .types import ToolResult, Message
from .tools.registry import ToolRegistry
from .brain.core_brain import CoreBrain
from .brain.digital_clone_brain import DigitalCloneBrain
from ..integrations.anthropic_client import AnthropicClient

logger = logging.getLogger(__name__)


class AutonomousAgent:
    """Main autonomous agent with self-contained execution loop."""

    def __init__(
        self,
        config: AgentConfig,
        brain: Union[CoreBrain, DigitalCloneBrain] = None
    ):
        """Initialize the autonomous agent.

        Args:
            config: Agent configuration
            brain: Brain instance (CoreBrain or DigitalCloneBrain)
        """
        self.config = config
        self.api_client = AnthropicClient(config.api_key)
        self.tools = ToolRegistry()
        self.brain = brain

        logger.info(f"Initialized AutonomousAgent with {config.default_model}")

    async def run(
        self,
        task: str,
        max_iterations: Optional[int] = None,
        system_prompt: Optional[str] = None
    ) -> str:
        """Execute task autonomously until completion.

        Args:
            task: Task description to execute
            max_iterations: Maximum iterations (defaults to config)
            system_prompt: Optional custom system prompt

        Returns:
            Final result as string
        """
        max_iterations = max_iterations or self.config.max_iterations

        logger.info(f"Starting autonomous execution: {task}")
        logger.info(f"Max iterations: {max_iterations}")

        # Load context from brain if available
        context = ""
        if self.brain:
            if hasattr(self.brain, 'get_relevant_context'):
                context = await self.brain.get_relevant_context(task)
                logger.debug(f"Loaded context from brain ({len(context)} chars)")

        # Build system prompt
        if not system_prompt:
            system_prompt = self._build_system_prompt(context)

        # Initialize conversation with task
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": task}
        ]

        # Autonomous execution loop
        iteration = 0
        final_result = None

        while iteration < max_iterations:
            iteration += 1
            logger.info(f"Iteration {iteration}/{max_iterations}")

            try:
                # Call Claude API
                response = await self.api_client.create_message(
                    model=self.config.default_model,
                    messages=messages,
                    tools=self.tools.get_tool_definitions(),
                    system=system_prompt,
                    max_tokens=4096
                )

                # Process response
                if response.stop_reason == "end_turn":
                    # Task complete
                    final_result = self._extract_text_from_response(response)
                    logger.info("Task completed (end_turn)")
                    break

                elif response.stop_reason == "tool_use":
                    # Execute tools
                    logger.info("Executing tool calls...")

                    # Add assistant message
                    messages.append({
                        "role": "assistant",
                        "content": response.content
                    })

                    # Execute all tool calls
                    tool_results = await self._execute_tool_calls(response.content)

                    # Add tool results as user message
                    messages.append({
                        "role": "user",
                        "content": tool_results
                    })

                elif response.stop_reason == "max_tokens":
                    # Continue with more tokens
                    logger.warning("Hit max tokens, continuing...")
                    messages.append({
                        "role": "assistant",
                        "content": response.content
                    })
                    continue

                else:
                    # Unknown stop reason
                    logger.warning(f"Unknown stop reason: {response.stop_reason}")
                    final_result = self._extract_text_from_response(response)
                    break

                # Store interaction in brain
                if self.brain and hasattr(self.brain, 'memory'):
                    await self._store_interaction(task, response)

            except Exception as e:
                logger.error(f"Error in iteration {iteration}: {e}", exc_info=True)

                # Retry logic
                if iteration < self.config.retry_attempts:
                    logger.info(f"Retrying... (attempt {iteration + 1})")
                    continue
                else:
                    raise

        # Handle max iterations reached
        if iteration >= max_iterations and not final_result:
            logger.warning(f"Reached max iterations ({max_iterations})")
            final_result = "Max iterations reached. Task may be incomplete."

        logger.info(f"Execution complete after {iteration} iterations")
        return final_result or "Task completed"

    async def _execute_tool_calls(self, content: List[Any]) -> List[Dict[str, Any]]:
        """Execute tool calls from Claude's response.

        Args:
            content: Response content blocks

        Returns:
            List of tool result content blocks
        """
        tool_results = []

        for block in content:
            if block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                logger.info(f"Executing tool: {tool_name}")
                logger.debug(f"Tool input: {tool_input}")

                # Execute tool
                result = await self.tools.execute_tool(tool_name, **tool_input)

                # Format result for Claude
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result.output if result.success else f"Error: {result.error}"
                }

                if not result.success:
                    tool_result["is_error"] = True

                tool_results.append(tool_result)

                logger.info(f"Tool {tool_name}: {'Success' if result.success else 'Failed'}")

        return tool_results

    def _extract_text_from_response(self, response) -> str:
        """Extract text content from Claude response.

        Args:
            response: API response

        Returns:
            Extracted text
        """
        text_parts = []

        for block in response.content:
            if hasattr(block, 'text'):
                text_parts.append(block.text)

        return "\n".join(text_parts) if text_parts else ""

    def _build_system_prompt(self, context: str = "") -> str:
        """Build system prompt for the agent.

        Args:
            context: Optional context from brain

        Returns:
            System prompt string
        """
        prompt = """You are an autonomous AI agent with the ability to execute tasks independently.

You have access to tools for:
- Executing bash commands
- Reading and writing files
- Fetching web content

Your goal is to complete the given task autonomously. Work methodically:
1. Analyze the task
2. Break it into steps
3. Use available tools to accomplish each step
4. Verify your work
5. Report completion

Be thorough, check for errors, and only report completion when the task is truly done."""

        if context:
            prompt += f"\n\nRelevant context:\n{context}"

        return prompt

    async def _store_interaction(self, task: str, response):
        """Store interaction in brain for future reference.

        Args:
            task: Original task
            response: API response
        """
        try:
            # Extract key information
            text = self._extract_text_from_response(response)

            # Store in brain's memory
            await self.brain.memory.store(
                text=f"Task: {task}\nResponse: {text}",
                metadata={
                    "type": "interaction",
                    "task": task,
                    "timestamp": datetime.now().isoformat(),
                    "stop_reason": response.stop_reason
                }
            )

            logger.debug("Stored interaction in brain")

        except Exception as e:
            logger.warning(f"Failed to store interaction in brain: {e}")

    @classmethod
    def from_config(cls, config_path: str = ".env"):
        """Create agent from configuration file.

        Args:
            config_path: Path to .env file

        Returns:
            AutonomousAgent instance
        """
        from .config import load_config

        config = load_config(config_path)

        # Initialize appropriate brain
        if config.self_build_mode:
            brain = CoreBrain(config.core_brain_path)
        else:
            brain = DigitalCloneBrain(config.digital_clone_brain_path)

        return cls(config, brain)
