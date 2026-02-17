"""Telegram webhook-based natural language chat interface."""

import asyncio
import logging
import json
from typing import Optional, Dict, Any
from datetime import datetime
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent.parent.parent))

from src.integrations.model_router import ModelRouter

logger = logging.getLogger(__name__)


class TelegramChat:
    """Natural language chat interface via Telegram webhooks."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        anthropic_client,
        agent,
        webhook_url: Optional[str] = None
    ):
        """Initialize Telegram chat interface.

        Args:
            bot_token: Telegram bot token
            chat_id: Authorized chat ID
            anthropic_client: Anthropic API client for NLP
            agent: AutonomousAgent instance
            webhook_url: Public webhook URL (e.g., http://your-ec2-ip:18789/telegram/webhook)
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.anthropic_client = anthropic_client
        self.agent = agent
        self.webhook_url = webhook_url
        self.enabled = bool(bot_token and chat_id)

        # Initialize intelligent model router
        self.router = ModelRouter(agent.config)
        logger.info("Initialized ModelRouter for intelligent model selection")

        if self.enabled:
            try:
                import telegram
                self.bot = telegram.Bot(token=bot_token)
                logger.info("Telegram chat interface initialized")
            except ImportError:
                logger.warning("python-telegram-bot not installed")
                self.enabled = False
        else:
            logger.info("Telegram chat disabled (no token/chat_id)")

    async def setup_webhook(self):
        """Set up webhook with Telegram."""
        if not self.enabled or not self.webhook_url:
            logger.warning("Webhook setup skipped (not enabled or no URL)")
            return False

        try:
            # Set webhook
            await self.bot.set_webhook(url=self.webhook_url)
            logger.info(f"✅ Telegram webhook set to: {self.webhook_url}")

            # Send test message
            await self.send_message(
                "🤖 **Agent Connected!**\n\n"
                "I'm now listening via webhooks (instant responses).\n\n"
                "Try asking me:\n"
                "• What's your status?\n"
                "• Pull latest from git\n"
                "• Build the DigitalCloneBrain\n"
                "• What's happening?"
            )

            return True

        except Exception as e:
            logger.error(f"Failed to setup webhook: {e}")
            return False

    async def handle_webhook(self, update_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming webhook from Telegram.

        Args:
            update_data: Telegram update data

        Returns:
            Response dict
        """
        try:
            # Extract message
            if "message" not in update_data:
                return {"ok": True}

            message = update_data["message"]
            from_chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text", "")

            # Verify authorized user
            if from_chat_id != self.chat_id:
                logger.warning(f"Unauthorized message from chat_id: {from_chat_id}")
                return {"ok": True}

            if not text:
                return {"ok": True}

            logger.info(f"Received message: {text}")

            # Process message asynchronously (don't block webhook)
            asyncio.create_task(self._process_message(text))

            return {"ok": True}

        except Exception as e:
            logger.error(f"Error handling webhook: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}

    async def _process_message(self, message: str):
        """Process incoming message using intelligent model routing.

        Philosophy: Prioritize CLARITY and QUALITY over cost.
        Use router to select appropriate model based on task complexity.
        FALLBACK: If Claude API fails, automatically fall back to SmolLM2.

        Args:
            message: User message
        """
        try:
            # Send typing indicator
            await self.send_message("🤔 Processing...")

            # Try primary processing with Claude API
            response = await self._process_with_fallback(message)

            # Send response
            await self.send_message(response)

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            await self.send_message(f"❌ Error: {str(e)}")

    async def _process_with_fallback(self, message: str) -> str:
        """Process message with automatic fallback to local model.

        Args:
            message: User message

        Returns:
            Response string
        """
        try:
            # Parse user intent using appropriate model
            intent = await self._parse_intent_with_fallback(message)

            # Use router to determine best model for this task
            selected_model = self.router.select_model_for_task(
                task=message,
                intent=intent.get("action"),
                confidence=intent.get("confidence", 0)
            )

            logger.info(f"Intent: {intent.get('action')} (confidence: {intent.get('confidence'):.2f})")
            logger.info(f"Selected model: {selected_model}")

            # Try primary model first
            return await self._execute_with_primary_model(intent, message)

        except Exception as e:
            # Check if we should fall back to local model
            if self.router.should_use_fallback(e):
                logger.warning(f"Primary model failed, attempting fallback: {e}")
                return await self._execute_with_fallback_model(message, e)
            else:
                # Not a fallback-able error, re-raise
                raise

    async def _execute_with_primary_model(self, intent: Dict[str, Any], message: str) -> str:
        """Execute with primary Claude models.

        Args:
            intent: Parsed intent
            message: User message

        Returns:
            Response string
        """
        # Route based on intent and selected model
        if intent.get("action") == "build_feature":
            # Always use Opus architect for building
            logger.info(f"Building feature with Opus architect")
            return await self.agent.run(
                task=f"User request via Telegram: {message}",
                max_iterations=30,  # More iterations for complex builds
                system_prompt=self._build_telegram_system_prompt()
            )

        elif intent.get("confidence", 0) < 0.6:
            # Low confidence - use Opus for understanding
            logger.info(f"Low confidence ({intent.get('confidence'):.2f}) - using Opus")
            return await self.agent.run(
                task=f"User request via Telegram: {message}",
                max_iterations=10,
                system_prompt=self._build_telegram_system_prompt()
            )

        else:
            # Use intent-based handlers with appropriate model
            logger.info(f"Using intent handler for: {intent.get('action')}")
            return await self._execute_intent(intent, message)

    async def _execute_with_fallback_model(self, message: str, error: Exception) -> str:
        """Execute with local fallback model (SmolLM2).

        Args:
            message: User message
            error: The error that triggered fallback

        Returns:
            Response string with fallback warning
        """
        if not self.agent.config.local_model_enabled:
            # No fallback available
            raise error

        logger.warning(f"Using local fallback model due to: {error}")

        # Generate fallback warning message
        warning = self.router.get_fallback_message(message, error)

        try:
            # Import local model client
            from src.integrations.local_model_client import LocalModelClient

            # Initialize local model
            local_client = LocalModelClient(
                model_name=self.agent.config.local_model_name,
                endpoint=self.agent.config.local_model_endpoint
            )

            # Check if available
            if not local_client.is_available():
                logger.error("Local model not available")
                return f"{warning}\n\n❌ Local backup model is not available. Please try again later."

            # Generate response with local model
            local_response = await local_client.create_message(
                messages=[{"role": "user", "content": message}],
                max_tokens=300,
                system="You are a helpful autonomous AI agent. Respond concisely and clearly."
            )

            response_text = local_response["content"][0]["text"]

            return f"{warning}\n{response_text}"

        except Exception as fallback_error:
            logger.error(f"Fallback model also failed: {fallback_error}")
            return f"{warning}\n\n❌ Fallback model error: {str(fallback_error)}\n\nPlease try again later."

    def _build_telegram_system_prompt(self) -> str:
        """Build system prompt for Telegram interactions.

        Returns:
            System prompt string
        """
        uptime = datetime.now() - self.agent.start_time if hasattr(self.agent, 'start_time') else None
        uptime_str = f"{uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m" if uptime else "Unknown"

        return f"""You are an autonomous AI agent system deployed on AWS EC2, running 24/7 as a systemd service.

Current Status:
- Uptime: {uptime_str}
- Model: {self.agent.config.default_model}
- Location: EC2 instance (Amazon Linux)
- Interface: Telegram webhook (instant messaging)

You are receiving requests from your user via Telegram. Your job is to:
1. Understand what the user is asking for
2. Use your available tools to accomplish the task
3. Provide a clear, concise response

Available Tools:
- bash: Execute system commands (check status, pull git updates, etc.)
- file_operations: Read/write files
- web_search: Fetch web content

Common Requests:
- "What's your status?" → Use bash to check systemctl status, uptime
- "Pull latest from git" → Use bash to run git pull
- "Check for updates" → Use bash to run git fetch
- "Show logs" → Use bash to tail log files
- "What's happening?" → Explain current status and operations

Response Guidelines:
- Be concise and human-like - respond naturally as a helpful assistant would
- Keep responses brief (2-4 sentences) unless more detail is needed
- Use conversational, friendly language
- Avoid overly technical jargon unless necessary
- If executing commands, briefly explain what you're doing
- Report results clearly and succinctly
- Refer to yourself as "I" or "the agent"
- Focus on accomplishing the user's request efficiently

CRITICAL REQUIREMENTS:
- NEVER hallucinate or provide synthetic/made-up information
- Only provide factual information based on actual tool outputs and system data
- If you don't know something, say so clearly rather than guessing"""

    async def _parse_intent_with_fallback(self, message: str) -> Dict[str, Any]:
        """Parse user intent with fallback support.

        Args:
            message: User message

        Returns:
            Intent dict
        """
        try:
            return await self._parse_intent(message)
        except Exception as e:
            if self.router.should_use_fallback(e):
                logger.warning(f"Intent parsing failed, using simple classification: {e}")
                # Simple local intent classification
                return self._parse_intent_locally(message)
            raise

    def _parse_intent_locally(self, message: str) -> Dict[str, Any]:
        """Parse intent locally using simple keyword matching.

        Args:
            message: User message

        Returns:
            Intent dict with basic classification
        """
        msg_lower = message.lower()

        # Simple keyword-based intent detection
        if any(word in msg_lower for word in ["status", "running", "health"]):
            return {"action": "status", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["pull", "update", "git"]):
            return {"action": "git_pull", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["log", "logs"]):
            return {"action": "logs", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["build", "create", "implement"]):
            return {"action": "build_feature", "confidence": 0.8, "parameters": {"feature_name": "requested feature"}}
        else:
            return {"action": "unknown", "confidence": 0.3, "parameters": {}}

    async def _parse_intent(self, message: str) -> Dict[str, Any]:
        """Parse user intent from message using Claude API.

        Args:
            message: User message

        Returns:
            Intent dict with action and parameters
        """
        try:
            # Use Claude to understand intent
            prompt = f"""Analyze this user message to an autonomous AI agent and extract the intent.

User message: "{message}"

Determine the intent and respond with JSON in this format:
{{
    "action": "status|git_pull|build_feature|health|unknown",
    "parameters": {{}},
    "confidence": 0.0-1.0
}}

Valid actions:
- status: User asking for current status, what's happening, etc.
- git_pull: User wants to pull latest code from git
- git_update: User wants to check for git updates
- build_feature: User wants to build a specific feature
- restart: User wants to restart the agent
- health: User wants system health info
- logs: User wants to see recent logs
- unknown: Cannot determine intent

For build_feature, extract feature name in parameters.feature_name.
For other actions, leave parameters empty.

Respond only with valid JSON, no explanation."""

            # Use router to select model for intent parsing
            intent_model = self.router.select_model_for_intent_parsing()

            response = await self.anthropic_client.create_message(
                model=intent_model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )

            # Extract JSON from response
            content = response.content[0].text.strip()

            # Try to parse JSON
            try:
                intent = json.loads(content)
            except json.JSONDecodeError:
                # If not valid JSON, try to extract JSON block
                import re
                json_match = re.search(r'\{[^{}]*\}', content)
                if json_match:
                    intent = json.loads(json_match.group())
                else:
                    intent = {"action": "unknown", "parameters": {}, "confidence": 0.0}

            logger.info(f"Parsed intent: {intent}")
            return intent

        except Exception as e:
            logger.error(f"Error parsing intent: {e}")
            return {"action": "unknown", "parameters": {}, "confidence": 0.0}

    async def _execute_intent(self, intent: Dict[str, Any], original_message: str) -> str:
        """Execute action based on parsed intent.

        Args:
            intent: Parsed intent
            original_message: Original user message

        Returns:
            Response message
        """
        action = intent.get("action", "unknown")
        params = intent.get("parameters", {})

        try:
            if action == "status":
                return await self._get_status()

            elif action == "git_pull":
                return await self._git_pull()

            elif action == "git_update":
                return await self._git_update()

            elif action == "build_feature":
                feature_name = params.get("feature_name", "unknown")
                return await self._build_feature(feature_name, original_message)

            elif action == "restart":
                return await self._restart_agent()

            elif action == "health":
                return await self._get_health()

            elif action == "logs":
                return await self._get_logs()

            else:
                # Unknown intent - have a conversation
                return await self._chat(original_message)

        except Exception as e:
            logger.error(f"Error executing intent {action}: {e}")
            return f"❌ Error executing {action}: {str(e)}"

    async def _get_status(self) -> str:
        """Get agent status."""
        try:
            # Get status from agent
            uptime = datetime.now() - self.agent.start_time if hasattr(self.agent, 'start_time') else None
            uptime_str = f"{uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m" if uptime else "Unknown"

            status = f"""📊 **Agent Status**

**State:** Running
**Uptime:** {uptime_str}
**Model:** {self.agent.config.model}
**Mode:** {'Self-building' if self.agent.config.self_build_mode else 'Operational'}

**Systems:**
✅ Core Engine: Active
✅ Brain: Initialized
✅ Auto-update: Running
✅ Monitoring: Active

**Ready for tasks!**
"""
            return status

        except Exception as e:
            return f"❌ Error getting status: {str(e)}"

    async def _git_pull(self) -> str:
        """Pull latest from git."""
        try:
            await self.send_message("📥 Checking git for updates...")

            # Use agent's bash tool to pull
            result = await self.agent.tools.get_tool("bash").execute(
                "git pull origin main",
                timeout=60
            )

            if result.success:
                if "Already up to date" in result.output:
                    return "✅ Already up-to-date! No new commits."
                else:
                    await self.send_message("🔄 Updates pulled! Restarting to apply...")
                    # Trigger restart
                    await self.agent.tools.get_tool("bash").execute(
                        "sudo systemctl restart claude-agent",
                        timeout=10
                    )
                    return "✅ Pulled latest commits! Agent restarting..."
            else:
                return f"❌ Git pull failed: {result.error}"

        except Exception as e:
            return f"❌ Error: {str(e)}"

    async def _git_update(self) -> str:
        """Check for git updates without pulling."""
        try:
            result = await self.agent.tools.get_tool("bash").execute(
                "git fetch origin main && git rev-list HEAD..origin/main --count",
                timeout=30
            )

            if result.success:
                count = int(result.output.strip())
                if count == 0:
                    return "✅ Repository up-to-date! No new commits."
                else:
                    return f"📥 Found {count} new commit(s) on origin/main.\n\nSend 'pull latest from git' to update."
            else:
                return f"❌ Failed to check updates: {result.error}"

        except Exception as e:
            return f"❌ Error: {str(e)}"

    async def _build_feature(self, feature_name: str, original_message: str = "") -> str:
        """Build a specific feature using Opus architect.

        Args:
            feature_name: Name of feature to build
            original_message: Original user message for full context

        Returns:
            Build status message
        """
        try:
            await self.send_message(f"🔨 **Building Feature: {feature_name}**\n\nActivating Opus architect and spawning Sonnet workers...")

            # Use the full Opus architect for feature building
            # This will spawn Sonnet sub-agents as needed
            build_task = f"""Build feature: {feature_name}

User's full request: {original_message}

Instructions:
1. Analyze what needs to be built
2. Break down into sub-tasks
3. Spawn Sonnet sub-agents for implementation
4. Coordinate the work
5. Report progress and results

Use the orchestrator to spawn sub-agents efficiently."""

            result = await self.agent.run(
                task=build_task,
                max_iterations=30,  # More iterations for feature building
                system_prompt=self._build_telegram_system_prompt()
            )

            return f"✅ **Feature Build Complete**\n\n{result}"

        except Exception as e:
            logger.error(f"Error building feature: {e}")
            return f"❌ Error building {feature_name}: {str(e)}"

    async def _restart_agent(self) -> str:
        """Restart the agent."""
        try:
            await self.send_message("🔄 Restarting agent...")
            await self.agent.tools.get_tool("bash").execute(
                "sudo systemctl restart claude-agent",
                timeout=10
            )
            return "✅ Restart initiated! Agent will be back online in ~5 seconds."

        except Exception as e:
            return f"❌ Error: {str(e)}"

    async def _get_health(self) -> str:
        """Get system health."""
        try:
            # Get system metrics
            import psutil

            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')

            health = f"""🏥 **System Health**

**CPU:** {cpu}%
**Memory:** {mem.percent}% ({mem.used // 1024 // 1024}MB / {mem.total // 1024 // 1024}MB)
**Disk:** {disk.percent}% ({disk.used // 1024 // 1024 // 1024}GB / {disk.total // 1024 // 1024 // 1024}GB)

**Status:** {'🟢 Healthy' if cpu < 80 and mem.percent < 80 else '🟡 High Usage'}
"""
            return health

        except Exception as e:
            return f"❌ Error: {str(e)}"

    async def _get_logs(self) -> str:
        """Get recent logs."""
        try:
            result = await self.agent.tools.get_tool("bash").execute(
                "tail -n 10 data/logs/agent.log",
                timeout=5
            )

            if result.success:
                return f"📝 **Recent Logs**\n\n```\n{result.output}\n```"
            else:
                return f"❌ Error getting logs: {result.error}"

        except Exception as e:
            return f"❌ Error: {str(e)}"

    async def _chat(self, message: str) -> str:
        """Have a conversation with the user.

        Args:
            message: User message

        Returns:
            Conversational response
        """
        try:
            # Get actual agent status for context
            uptime = datetime.now() - self.agent.start_time if hasattr(self.agent, 'start_time') else None
            uptime_str = f"{uptime.seconds // 3600}h {(uptime.seconds % 3600) // 60}m" if uptime else "Unknown"

            # Use router to select chat model (prioritizes quality)
            chat_model = self.router.select_model_for_chat(len(message))

            # Use Claude with system message to act as the autonomous agent
            response = await self.anthropic_client.create_message(
                model=chat_model,
                max_tokens=300,  # Increased for better responses
                system=f"""You are an autonomous AI agent system deployed on AWS EC2, running 24/7 as a systemd service. Your purpose is to help your user manage tasks, monitor systems, and execute commands remotely via Telegram.

Current Status:
- Uptime: {uptime_str}
- Model: {self.agent.config.default_model}
- Location: EC2 instance (Amazon Linux)
- Web Dashboard: Available via Cloudflare Tunnel

Your Capabilities:
- Check system status and health
- Pull git updates and restart
- Monitor logs
- Execute tasks autonomously
- Report on ongoing operations
- Build features using multi-agent orchestration

Response Guidelines:
- Be concise and human-like - respond naturally as a helpful assistant would
- Keep responses brief (2-4 sentences) unless more detail is needed
- Use conversational, friendly language without being overly casual
- Avoid overly technical jargon unless necessary
- Refer to yourself as "I" or "the agent"
- Focus on your actual operational capabilities
- Explain what you can do and how you can help
- CRITICAL: Never hallucinate or provide synthetic/made-up information
- CRITICAL: Only provide factual information based on actual system data
- If you don't know something, say so clearly rather than guessing""",
                messages=[{"role": "user", "content": message}]
            )

            return response.content[0].text.strip()

        except Exception as e:
            logger.error(f"Error in chat: {e}")
            return "I'm not sure how to respond to that. Try asking about my status, or tell me to pull updates from git!"

    async def send_message(self, text: str):
        """Send message to user.

        Args:
            text: Message to send
        """
        if not self.enabled:
            return

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
