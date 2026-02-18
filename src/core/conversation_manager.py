"""Channel-agnostic conversation manager.

Handles conversation intelligence regardless of communication channel.
Channels (Telegram, WhatsApp, Discord) just send/receive messages.

## Brain Selection Based on Mode:

**BUILD MODE** (agent building itself):
- Uses: CoreBrain
- Purpose: Track build progress, learn patterns, store feature states
- Storage: Build phases, completed features, pending tasks, code patterns
- Lifecycle: Persistent (no longer auto-purges)

**ASSISTANT MODE** (day-to-day operations):
- Uses: DigitalCloneBrain
- Purpose: Handle conversations, remember preferences, maintain context
- Storage: Conversation history, user preferences, contacts, context
- Lifecycle: Permanent

The ConversationManager automatically selects the correct brain based on
agent.config.mode, but you can switch manually with switch_brain_mode().

## Example:

```python
# Auto-selection based on mode
manager = ConversationManager(agent, client, router)
# If agent.config.mode == "build" → Uses CoreBrain
# If agent.config.mode == "assistant" → Uses DigitalCloneBrain

# Manual switching
manager.switch_brain_mode("build")      # Switch to CoreBrain
manager.switch_brain_mode("assistant")  # Switch to DigitalCloneBrain
```
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class ConversationManager:
    """Manages conversations across all channels with Brain integration.

    This is the core intelligence layer that:
    - Routes messages to appropriate models
    - Stores context in Brain
    - Handles fallback to local models
    - Maintains conversation continuity

    Channels are just transport layers - they receive/send messages.
    """

    def __init__(
        self,
        agent,
        anthropic_client,
        model_router,
        brain=None
    ):
        """Initialize conversation manager.

        Args:
            agent: AutonomousAgent instance
            anthropic_client: Anthropic API client
            model_router: ModelRouter for intelligent model selection
            brain: Brain instance (optional, will auto-select if not provided)
        """
        self.agent = agent
        self.anthropic_client = anthropic_client
        self.router = model_router

        # SELECT BRAIN BASED ON AGENT MODE
        if brain:
            # Explicit brain provided
            self.brain = brain
            brain_type = brain.__class__.__name__
        else:
            # Auto-select based on mode
            agent_mode = getattr(agent.config, 'mode', 'assistant')

            if agent_mode == "build":
                # BUILD MODE: Use CoreBrain for self-building
                self.brain = getattr(agent, 'core_brain', None)
                brain_type = "CoreBrain"
                logger.info("BUILD MODE detected - using CoreBrain for build tracking")
            else:
                # ASSISTANT MODE: Use DigitalCloneBrain for operations
                self.brain = getattr(agent, 'digital_brain', None) or getattr(agent, 'brain', None)
                brain_type = "DigitalCloneBrain"
                logger.info("ASSISTANT MODE detected - using DigitalCloneBrain for conversations")

        self._last_model_used = "claude-sonnet-4-5"

        logger.info(f"ConversationManager initialized (channel-agnostic, using {brain_type})")

    def switch_brain_mode(self, mode: str):
        """Switch between CoreBrain (build) and DigitalCloneBrain (assistant).

        Args:
            mode: "build" or "assistant"
        """
        if mode == "build":
            self.brain = getattr(self.agent, 'core_brain', None)
            logger.info("Switched to BUILD MODE - using CoreBrain")
        elif mode == "assistant":
            self.brain = getattr(self.agent, 'digital_brain', None) or getattr(self.agent, 'brain', None)
            logger.info("Switched to ASSISTANT MODE - using DigitalCloneBrain")
        else:
            logger.warning(f"Unknown mode: {mode}. Valid modes: build, assistant")

    def get_current_brain(self) -> str:
        """Get current brain type.

        Returns:
            Brain type name
        """
        if self.brain:
            return self.brain.__class__.__name__
        return "None"

    async def process_message(
        self,
        message: str,
        channel: str = "unknown",
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Process a message and return response.

        This is channel-agnostic - works for Telegram, WhatsApp, Discord, etc.

        Args:
            message: User's message
            channel: Channel name (telegram, whatsapp, discord, etc.)
            user_id: User identifier
            metadata: Additional metadata

        Returns:
            Response string
        """
        try:
            logger.info(f"Processing message from {channel}: {message[:50]}...")

            # Try primary processing with Claude API
            response = await self._process_with_fallback(message)

            # Store conversation turn in Brain for context continuity
            # (Both CoreBrain and DigitalCloneBrain have conversation methods)
            if self.brain and hasattr(self.brain, 'store_conversation_turn'):
                await self.brain.store_conversation_turn(
                    user_message=message,
                    assistant_response=response,
                    model_used=self._last_model_used,
                    metadata={
                        "channel": channel,
                        "user_id": user_id,
                        **(metadata or {})
                    }
                )

            return response

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            return f"❌ Error: {str(e)}"

    async def _process_with_fallback(self, message: str) -> str:
        """Process message with automatic fallback to local model.

        Args:
            message: User message

        Returns:
            Response string
        """
        try:
            # Parse user intent
            intent = await self._parse_intent_with_fallback(message)

            # Use router to determine best model
            action = intent.get("action", "unknown")
            confidence = intent.get("confidence", 0.0)

            selected_model = self.router.select_model_for_task(
                task=message,
                intent=action,
                confidence=confidence
            )

            logger.info(f"Intent: {action} (confidence: {confidence:.2f})")
            logger.info(f"Selected model: {selected_model}")

            # Execute with primary model
            return await self._execute_with_primary_model(intent, message)

        except Exception as e:
            # Check if we should fall back to local model
            if self.router.should_use_fallback(e):
                logger.warning(f"Primary model failed, attempting fallback: {e}")
                return await self._execute_with_fallback_model(message, e)
            else:
                raise

    async def _execute_with_primary_model(
        self,
        intent: Dict[str, Any],
        message: str
    ) -> str:
        """Execute with primary Claude models.

        Args:
            intent: Parsed intent
            message: User message

        Returns:
            Response string
        """
        action = intent.get("action", "unknown")

        if action == "build_feature":
            # Always use Opus architect
            logger.info("Building feature with Opus architect")
            self._last_model_used = "claude-opus-4-6"
            return await self.agent.run(
                task=f"User request: {message}",
                max_iterations=30,
                system_prompt=self._build_system_prompt()
            )

        elif action in ["status", "git_pull", "git_update", "restart", "health", "logs"]:
            # Known intents - delegate to agent handlers
            logger.info(f"Using intent handler for: {action}")
            self._last_model_used = "claude-sonnet-4-5"
            # These would be implemented by the agent
            return f"Intent recognized: {action}\n(Handler implementation needed)"

        else:
            # Check if this is a code question
            if self._is_code_question(message):
                logger.info("Code question - using agent with tools")
                self._last_model_used = "claude-sonnet-4-5"
                return await self.agent.run(
                    task=f"Answer: {message}\n\nBe concise. Use tools for facts.",
                    max_iterations=5,
                    system_prompt=self._build_system_prompt()
                )
            else:
                # General conversation
                logger.info("General conversation - using chat")
                self._last_model_used = "claude-sonnet-4-5"
                return await self._chat(message)

    async def _execute_with_fallback_model(
        self,
        message: str,
        error: Exception
    ) -> str:
        """Execute with local fallback model using Brain for context.

        Args:
            message: User message
            error: The error that triggered fallback

        Returns:
            Response string with fallback warning
        """
        if not self.agent.config.local_model_enabled:
            raise error

        logger.warning(f"Using local fallback model due to: {error}")
        self._last_model_used = "smollm2"

        # Generate warning
        warning = self.router.get_fallback_message(message, error)

        try:
            from src.integrations.local_model_client import LocalModelClient

            local_client = LocalModelClient(
                model_name=self.agent.config.local_model_name,
                endpoint=self.agent.config.local_model_endpoint
            )

            if not local_client.is_available():
                return f"{warning}\n\n❌ Local model not available."

            # RETRIEVE CONTEXT FROM BRAIN
            # (Both CoreBrain and DigitalCloneBrain have conversation methods)
            messages = []
            conversation_context = ""

            if self.brain and hasattr(self.brain, 'get_conversation_context'):
                conversation_context = await self.brain.get_conversation_context(
                    current_message=message,
                    limit=3
                )

                # Build message history
                if hasattr(self.brain, 'get_recent_conversation'):
                    recent_turns = await self.brain.get_recent_conversation(limit=3)
                    for turn in reversed(recent_turns):
                        messages.append({"role": "user", "content": turn["user_message"]})
                        messages.append({"role": "assistant", "content": turn["assistant_response"]})

            # Add current message
            messages.append({"role": "user", "content": message})

            # Enhanced system prompt with context
            system_prompt = f"""You are a FALLBACK assistant with LIMITED capabilities.

{conversation_context}

IMPORTANT:
- Continue conversation naturally based on context
- Keep responses SHORT (1-2 sentences)
- If asked complex question: "I'll handle that when main system is back."

Capabilities:
✅ Simple questions, acknowledgments
❌ Code writing, complex reasoning"""

            # Generate response
            local_response = await local_client.create_message(
                messages=messages,
                max_tokens=300,
                system=system_prompt
            )

            response_text = local_response["content"][0]["text"]

            # Queue for Claude review (both brains support this)
            if self.brain and hasattr(self.brain, 'queue_for_claude_review'):
                await self.brain.queue_for_claude_review(
                    message=message,
                    local_response=response_text
                )

            return f"{warning}\n\n{response_text}"

        except Exception as fallback_error:
            logger.error(f"Fallback model failed: {fallback_error}")
            return f"{warning}\n\n❌ Fallback error: {str(fallback_error)}"

    async def _chat(self, message: str) -> str:
        """Have a conversation.

        Args:
            message: User message

        Returns:
            Response
        """
        try:
            # Get context from Brain if available
            system_prompt = "You are a helpful AI assistant. Be concise and clear."

            if self.brain and hasattr(self.brain, 'get_relevant_context'):
                context = await self.brain.get_relevant_context(message, max_results=3)
                if context:
                    system_prompt += f"\n\n{context}"

            chat_model = self.router.select_model_for_chat(len(message))

            response = await self.anthropic_client.create_message(
                model=chat_model,
                max_tokens=300,
                system=system_prompt,
                messages=[{"role": "user", "content": message}]
            )

            return response.content[0].text.strip()

        except Exception as e:
            logger.error(f"Chat error: {e}")
            return "I'm not sure how to respond. Try asking about my status!"

    async def _parse_intent_with_fallback(self, message: str) -> Dict[str, Any]:
        """Parse user intent with fallback.

        Args:
            message: User message

        Returns:
            Intent dict
        """
        try:
            return await self._parse_intent(message)
        except Exception as e:
            if self.router.should_use_fallback(e):
                logger.warning(f"Intent parsing failed, using local: {e}")
                return self._parse_intent_locally(message)
            raise

    async def _parse_intent(self, message: str) -> Dict[str, Any]:
        """Parse user intent using Claude.

        Args:
            message: User message

        Returns:
            Intent dict
        """
        # Simplified - would use full implementation
        return {"action": "unknown", "confidence": 0.5, "parameters": {}}

    def _parse_intent_locally(self, message: str) -> Dict[str, Any]:
        """Parse intent locally using keywords.

        Args:
            message: User message

        Returns:
            Intent dict
        """
        msg_lower = message.lower()

        if any(word in msg_lower for word in ["status", "running"]):
            return {"action": "status", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["build", "create"]):
            return {"action": "build_feature", "confidence": 0.8, "parameters": {}}
        else:
            return {"action": "unknown", "confidence": 0.3, "parameters": {}}

    def _is_code_question(self, message: str) -> bool:
        """Check if message is a code-related question.

        Args:
            message: User message

        Returns:
            True if code question
        """
        msg_lower = message.lower()
        keywords = [
            "pending", "todo", "feature", "task", "code", "file",
            "function", "class", "implement", "progress",
            "codebase", "project", "engine", "brain"
        ]
        return any(keyword in msg_lower for keyword in keywords)

    def _build_system_prompt(self) -> str:
        """Build system prompt for agent tasks.

        Returns:
            System prompt string
        """
        uptime = datetime.now() - self.agent.start_time if hasattr(self.agent, 'start_time') else None
        uptime_str = f"{uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m" if uptime else "Unknown"

        return f"""You are an autonomous AI agent system.

Current Status:
- Uptime: {uptime_str}
- Model: {self.agent.config.default_model}

Guidelines:
- Be concise and human-like
- Use tools to get factual information
- NEVER hallucinate or make up information
- If you don't know, say so clearly"""
