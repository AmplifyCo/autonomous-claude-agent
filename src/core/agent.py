"""Core autonomous agent with self-contained execution loop."""

import asyncio
import logging
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
from pathlib import Path
import json
import yaml

from .config import AgentConfig
from .types import ToolResult, Message
from .tools.registry import ToolRegistry
from .brain.core_brain import CoreBrain
from .brain.digital_clone_brain import DigitalCloneBrain
from ..integrations.anthropic_client import AnthropicClient
from .nervous_system.state_machine import AgentStateMachine, AgentState

logger = logging.getLogger(__name__)


class AutonomousAgent:
    """Main autonomous agent with self-contained execution loop."""

    def __init__(
        self,
        config: AgentConfig,
        brain: Union[CoreBrain, DigitalCloneBrain] = None,
        gemini_client=None
    ):
        """Initialize the autonomous agent.

        Args:
            config: Agent configuration
            brain: Brain instance (CoreBrain or DigitalCloneBrain)
            gemini_client: Optional GeminiClient for fallback when Claude fails
        """
        self.config = config
        self.api_client = AnthropicClient(config.api_key)
        self.gemini_client = gemini_client

        # Load YAML config for tool registry
        yaml_config = {}
        config_path = Path("config/agent.yaml")
        if config_path.exists():
            with open(config_path, 'r') as f:
                yaml_config = yaml.safe_load(f) or {}

        # Initialize tools with safety configuration
        tool_config = yaml_config.get("agent", {})
        self.tools = ToolRegistry(config=tool_config)
        self.brain = brain

        # Nervous System: state machine for tracking execution state + cancellation
        self.state_machine = AgentStateMachine()

        fallback = "+ Gemini fallback" if gemini_client else ""
        logger.info(f"Initialized AutonomousAgent with {config.default_model} {fallback}")

    async def run(
        self,
        task: str,
        max_iterations: Optional[int] = None,
        system_prompt: Optional[str] = None,
        pii_map: Optional[Dict[str, str]] = None,
        model_tier: str = "sonnet"
    ) -> str:
        """Execute task autonomously until completion.

        Args:
            task: Task description to execute
            max_iterations: Maximum iterations (defaults to config)
            system_prompt: Optional custom system prompt
            pii_map: Optional PII token mapping for de-tokenization
            model_tier: Model routing tier:
                - "flash": Gemini Flash (simple tools — reminders, contacts, clock)
                - "sonnet": Claude Sonnet (default — complex tasks, questions)
                - "quality": Claude Sonnet with retry, fallback to Gemini Pro (email compose)

        Returns:
            Final result as string
        """
        max_iterations = max_iterations or self.config.max_iterations

        # Store current task for semantic validation (Layer 11)
        self._current_task = task
        self._model_tier = model_tier

        # Reset state machine + policy gate for new run
        self.state_machine.reset()
        self.tools.policy_gate.reset_run_counts()
        self.state_machine.transition(AgentState.THINKING, task[:80])

        tier_label = {"flash": "⚡ Gemini Flash", "sonnet": "🧠 Claude Sonnet", "quality": "✍️ Claude Quality"}.get(model_tier, model_tier)
        logger.info(f"Starting autonomous execution [{tier_label}]: {task}")
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
                # Check for cancellation
                if self.state_machine.is_cancelled():
                    logger.info("Task cancelled by user")
                    self.state_machine.reset()
                    return "Task cancelled."

                # Call LLM — all calls go through LiteLLM
                self.state_machine.transition(AgentState.THINKING)
                response = await self._call_llm(
                    messages=messages,
                    tools=self.tools.get_tool_definitions(),
                    system_prompt=system_prompt,
                    max_tokens=4096,
                    model_tier=model_tier
                )

                # Process response
                if response.stop_reason == "end_turn":
                    # Task complete
                    final_result = self._extract_text_from_response(response)
                    logger.info("Task completed (end_turn)")
                    break

                elif response.stop_reason == "tool_use":
                    # Execute tools
                    self.state_machine.transition(AgentState.EXECUTING)
                    logger.info("Executing tool calls...")

                    # Add assistant message
                    messages.append({
                        "role": "assistant",
                        "content": response.content
                    })

                    # Execute all tool calls
                    tool_results = await self._execute_tool_calls(response.content, pii_map)

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
                error_str = str(e)
                logger.error(f"Error in iteration {iteration}: {e}", exc_info=True)

                # Rate limit — wait and retry instead of crashing
                if "429" in error_str or "rate_limit" in error_str:
                    wait_time = min(60 * (2 ** (iteration - 1)), 300)
                    logger.warning(f"Rate limited. Waiting {wait_time}s before retry...")
                    await asyncio.sleep(wait_time)
                    continue

                # Other errors — retry with limit
                if iteration < self.config.retry_attempts:
                    logger.info(f"Retrying... (attempt {iteration + 1})")
                    continue
                else:
                    raise

        # Handle max iterations reached — try Gemini summary
        if iteration >= max_iterations and not final_result:
            logger.warning(f"Reached max iterations ({max_iterations})")
            final_result = await self._summarize_with_fallback(messages, system_prompt)

        logger.info(f"Execution complete after {iteration} iterations")
        self.state_machine.transition(AgentState.RESPONDING)
        self.state_machine.reset()
        return final_result or "Task completed"

    # ── Model tier constants ──────────────────────────────────────────
    MODEL_GEMINI_FLASH = "gemini/gemini-2.0-flash"
    MODEL_CLAUDE_SONNET = "anthropic/claude-3-5-sonnet-20241022"
    MODEL_CLAUDE_HAIKU = "anthropic/claude-3-haiku-20240307"
    MODEL_GEMINI_PRO = "gemini/gemini-1.5-pro"

    async def _call_llm(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        system_prompt: str,
        max_tokens: int = 4096,
        model_tier: str = "sonnet"
    ):
        """Route LLM call through LiteLLM based on model tier.

        All calls go through LiteLLM for unified provider management.

        Tiers:
            flash:   Gemini Flash primary → Claude Sonnet fallback
            haiku:   Claude Haiku primary → Gemini Flash fallback
            sonnet:  Claude Sonnet primary → Gemini Flash fallback
            quality: Claude Sonnet → retry with wait → Gemini Pro fallback
        """
        has_litellm = self.gemini_client and self.gemini_client.enabled

        if not has_litellm:
            # No LiteLLM — use direct Anthropic client
            return await self.api_client.create_message(
                model=self.config.default_model,
                messages=messages, tools=tools,
                system=system_prompt, max_tokens=max_tokens
            )

        if model_tier == "flash":
            # ── Gemini Flash primary (simple tools) ──
            try:
                logger.info("⚡ LiteLLM → Gemini Flash")
                return await self.gemini_client.create_message(
                    model=self.MODEL_GEMINI_FLASH,
                    messages=messages, tools=tools,
                    system=system_prompt, max_tokens=max_tokens
                )
            except Exception as e:
                logger.warning(f"Gemini Flash failed ({str(e)[:60]}), trying Claude...")
                return await self.gemini_client.create_message(
                    model=self.MODEL_CLAUDE_SONNET,
                    messages=messages, tools=tools,
                    system=system_prompt, max_tokens=max_tokens
                )

        elif model_tier == "haiku":
            # ── Claude Haiku primary → Gemini Flash fallback ──
            try:
                logger.info("💨 LiteLLM → Claude Haiku")
                return await self.gemini_client.create_message(
                    model=self.MODEL_CLAUDE_HAIKU,
                    messages=messages, tools=tools,
                    system=system_prompt, max_tokens=max_tokens
                )
            except Exception as e:
                logger.warning(f"Claude Haiku failed ({str(e)[:60]}), falling back to Gemini Flash...")
                return await self.gemini_client.create_message(
                    model=self.MODEL_GEMINI_FLASH,
                    messages=messages, tools=tools,
                    system=system_prompt, max_tokens=max_tokens
                )

        elif model_tier == "quality":
            # ── Claude Sonnet primary → retry → Gemini Pro (email compose) ──
            for attempt in range(2):
                try:
                    logger.info(f"✍️ LiteLLM → Claude Sonnet (quality, attempt {attempt + 1})")
                    return await self.gemini_client.create_message(
                        model=self.MODEL_CLAUDE_SONNET,
                        messages=messages, tools=tools,
                        system=system_prompt, max_tokens=max_tokens
                    )
                except Exception as e:
                    error_str = str(e)
                    if attempt == 0 and ("429" in error_str or "rate_limit" in error_str or "overloaded" in error_str):
                        logger.warning(f"Claude rate-limited, waiting 30s before retry...")
                        await asyncio.sleep(30)
                        continue
                    # Final attempt failed — fall back to Gemini Pro
                    logger.warning(f"Claude failed after retry ({error_str[:60]}), using Gemini Pro...")
                    try:
                        return await self.gemini_client.create_message(
                            model=self.MODEL_GEMINI_PRO,
                            messages=messages, tools=tools,
                            system=system_prompt, max_tokens=max_tokens
                        )
                    except Exception as pro_error:
                        logger.error(f"Gemini Pro also failed: {pro_error}")
                        raise e

        else:
            # ── Claude Sonnet primary → Gemini Flash fallback (default) ──
            try:
                logger.info("🧠 LiteLLM → Claude Sonnet")
                return await self.gemini_client.create_message(
                    model=self.MODEL_CLAUDE_SONNET,
                    messages=messages, tools=tools,
                    system=system_prompt, max_tokens=max_tokens
                )
            except Exception as e:
                error_str = str(e)
                is_retriable = "429" in error_str or "rate_limit" in error_str or "overloaded" in error_str or "500" in error_str
                if is_retriable:
                    logger.warning(f"⚡ Claude failed ({error_str[:60]}), falling back to Gemini Flash...")
                    try:
                        return await self.gemini_client.create_message(
                            model=self.MODEL_GEMINI_FLASH,
                            messages=messages, tools=tools,
                            system=system_prompt, max_tokens=max_tokens
                        )
                    except Exception as flash_error:
                        logger.error(f"Gemini Flash also failed: {flash_error}")
                        raise e
                raise

    async def _summarize_with_fallback(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: str
    ) -> str:
        """When max iterations reached, try to generate a final response.

        Uses Gemini as fallback if available, since Claude may have been
        the reason we hit max iterations (rate limit, etc.).
        """
        summary_messages = messages + [{
            "role": "user",
            "content": "You have reached the maximum number of tool-use iterations. Based on all the tool results above, please provide a final, complete response to the user's original question. Do NOT call any more tools."
        }]

        # Try Gemini first (cheaper, less likely rate-limited)
        if self.gemini_client and self.gemini_client.enabled:
            try:
                logger.info("⚡ Max iterations — using Gemini to summarize")
                response = await self.gemini_client.create_message(
                    model="gemini/gemini-2.0-flash",
                    messages=summary_messages,
                    system=system_prompt,
                    max_tokens=2048
                )
                text = self._extract_text_from_response(response)
                if text:
                    return text
            except Exception as e:
                logger.warning(f"Gemini summary failed: {e}")

        # Try Claude
        try:
            logger.info("Max iterations — using Claude to summarize")
            response = await self.api_client.create_message(
                model=self.config.default_model,
                messages=summary_messages,
                system=system_prompt,
                max_tokens=2048
            )
            text = self._extract_text_from_response(response)
            if text:
                return text
        except Exception as e:
            logger.warning(f"Claude summary also failed: {e}")

        return "I gathered some information but couldn't complete the full analysis. Please try again."

    async def _execute_tool_calls(
        self,
        content: List[Any],
        pii_map: Optional[Dict[str, str]] = None
    ) -> List[Dict[str, Any]]:
        """Execute tool calls from Claude's response (parallel when safe).

        Args:
            content: Response content blocks

        Returns:
            List of tool result content blocks
        """
        # Collect all tool_use blocks
        tool_blocks = [b for b in content if b.type == "tool_use"]

        if not tool_blocks:
            return []

        # Execute all tool calls in parallel
        async def _run_single_tool(block):
            tool_name = block.name
            tool_input = block.input
            tool_use_id = block.id

            # De-tokenize PII in tool inputs if map provided
            if pii_map:
                # Recursive de-tokenization for nested dicts/lists
                def detokenize_value(val):
                    if isinstance(val, str):
                        for placeholder, original in pii_map.items():
                            val = val.replace(placeholder, original)
                        return val
                    elif isinstance(val, dict):
                        return {k: detokenize_value(v) for k, v in val.items()}
                    elif isinstance(val, list):
                        return [detokenize_value(v) for v in val]
                    return val

                try:
                    tool_input = detokenize_value(tool_input)
                    logger.debug(f"De-tokenized tool input for {tool_name}")
                except Exception as e:
                    logger.warning(f"Failed to de-tokenize tool input: {e}")

            logger.info(f"Executing tool: {tool_name}")
            logger.debug(f"Tool input: {tool_input}")

            result = await self.tools.execute_tool(
                tool_name,
                user_message=getattr(self, '_current_task', ''),
                llm_client=self.api_client,
                **tool_input
            )

            # Build content: multimodal (screenshot+text) or plain string
            if result.success and result.content_blocks is not None:
                content = result.content_blocks
            elif result.success:
                content = result.output or ""
            else:
                content = f"Error: {result.error}"

            tool_result = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
            }

            if not result.success:
                tool_result["is_error"] = True

            logger.info(f"Tool {tool_name}: {'Success' if result.success else 'Failed'}")
            return tool_result

        # Single tool: run directly. Multiple tools: run in parallel.
        if len(tool_blocks) == 1:
            return [await _run_single_tool(tool_blocks[0])]

        logger.info(f"Running {len(tool_blocks)} tool calls in parallel")
        results = await asyncio.gather(
            *[_run_single_tool(b) for b in tool_blocks],
            return_exceptions=True
        )

        # Handle any exceptions from gather
        tool_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Tool {tool_blocks[i].name} raised exception: {result}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_blocks[i].id,
                    "content": f"Error: {str(result)}",
                    "is_error": True
                })
            else:
                tool_results.append(result)

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

Be thorough, check for errors, and only report completion when the task is truly done.

COMMUNICATION STYLE:
- Be EXTREMELY concise — 1-2 sentences for confirmations, short paragraphs only when needed
- Never add filler, preambles, or unsolicited tips ("Here's what I did...", "You can now...")
- Use natural, conversational language
- Just confirm the action or answer the question, nothing more

CRITICAL REQUIREMENTS:
- NEVER hallucinate or provide synthetic/made-up information
- Only provide factual information based on actual data from tools
- If you don't know something, say so clearly rather than guessing
- Base all responses on verified information from tool outputs
- NEVER wrap your response in XML tags (no <result>, <attemptcompletion>, <analysis>, etc.)
- Respond in plain text or Markdown only"""

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
