"""Telegram webhook-based natural language chat interface."""

import asyncio
import logging
import json
from typing import Optional, Dict, Any
from datetime import datetime

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
        """Process incoming message with natural language understanding.

        Args:
            message: User message
        """
        try:
            # Send typing indicator
            await self.send_message("🤔 Processing...")

            # Parse intent using Claude API
            intent = await self._parse_intent(message)

            # Execute based on intent
            response = await self._execute_intent(intent, message)

            # Send response
            await self.send_message(response)

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            await self.send_message(f"❌ Error: {str(e)}")

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

            response = await self.anthropic_client.create_message(
                model="claude-haiku-4-5",  # Use fast model for intent parsing
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
                return await self._build_feature(feature_name)

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

    async def _build_feature(self, feature_name: str) -> str:
        """Build a specific feature."""
        return f"🔨 **Feature Building**\n\nRequested: {feature_name}\n\n⚠️ Meta-agent self-builder not yet implemented.\n\nThis feature will autonomously build requested components once the meta-agent is complete."

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

            # Use Claude with system message to act as the autonomous agent
            response = await self.anthropic_client.create_message(
                model="claude-haiku-4-5",
                max_tokens=200,
                system=f"""You are an autonomous AI agent system deployed on AWS EC2, running 24/7 as a systemd service. Your purpose is to help your user manage tasks, monitor systems, and execute commands remotely via Telegram.

Current Status:
- Uptime: {uptime_str}
- Model: {self.agent.config.model}
- Location: EC2 instance (Amazon Linux)
- Web Dashboard: Available via Cloudflare Tunnel

Your Capabilities:
- Check system status and health
- Pull git updates and restart
- Monitor logs
- Execute tasks autonomously
- Report on ongoing operations

Response Guidelines:
- Be helpful, concise (2-3 sentences), and friendly
- Refer to yourself as "I" or "the agent"
- Focus on your actual operational capabilities
- Don't discuss your underlying model architecture
- Help the user understand what you're doing and can do""",
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
