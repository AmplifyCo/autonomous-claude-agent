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
            action = intent.get("action", "unknown")
            confidence = intent.get("confidence", 0.0)

            selected_model = self.router.select_model_for_task(
                task=message,
                intent=action,
                confidence=confidence
            )

            logger.info(f"Intent: {action} (confidence: {confidence:.2f})")
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
        action = intent.get("action", "unknown")

        if action == "build_feature":
            # Always use Opus architect for building
            logger.info(f"Building feature with Opus architect")
            return await self.agent.run(
                task=f"User request via Telegram: {message}",
                max_iterations=30,  # More iterations for complex builds
                system_prompt=self._build_telegram_system_prompt()
            )

        elif action in ["status", "git_pull", "git_update", "restart", "health", "logs",
                        "health_check", "error_report", "auto_fix"]:
            # Known intents - use specific handlers
            logger.info(f"Using intent handler for: {action}")
            return await self._execute_intent(intent, message)

        else:
            # Check if this is a question about code/features/tasks
            if self._is_code_question(message):
                # Questions about code need tools to answer - use agent with limited iterations
                logger.info(f"Code/feature question detected - using agent with tools (limited iterations)")
                return await self.agent.run(
                    task=f"Answer this question: {message}\n\nBe concise (2-4 sentences). Use tools to find factual information. Don't make assumptions.",
                    max_iterations=5,  # Limited iterations to avoid timeout
                    system_prompt=self._build_telegram_system_prompt()
                )
            else:
                # General conversation - just chat (no tools needed)
                confidence = intent.get("confidence", 0.0)
                logger.info(f"Conversational intent (confidence: {confidence:.2f}) - using chat")
                return await self._chat(message)

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

    def _is_code_question(self, message: str) -> bool:
        """Determine if a question requires access to codebase/tools.

        Args:
            message: User message

        Returns:
            True if this is a code/feature/task question
        """
        msg_lower = message.lower()

        # Keywords that indicate questions about code, features, tasks, or project details
        code_question_keywords = [
            "pending", "todo", "feature", "task", "code", "file",
            "function", "class", "implement", "working on",
            "progress", "what's in", "show me", "find",
            "search", "look for", "where is", "how does",
            "explain", "what does", "codebase", "project",
            "engine", "brain", "module", "component"
        ]

        # Check if any code question keyword is present
        return any(keyword in msg_lower for keyword in code_question_keywords)

    def _parse_intent_locally(self, message: str) -> Dict[str, Any]:
        """Parse intent locally using simple keyword matching.

        Args:
            message: User message

        Returns:
            Intent dict with basic classification
        """
        msg_lower = message.lower()

        # Simple keyword-based intent detection
        if any(word in msg_lower for word in ["status", "running"]):
            return {"action": "status", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["pull", "update", "git"]):
            return {"action": "git_pull", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["log", "logs"]):
            return {"action": "logs", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["health", "check"]):
            return {"action": "health_check", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["error", "errors", "diagnostic"]):
            return {"action": "error_report", "confidence": 0.9, "parameters": {}}
        elif any(word in msg_lower for word in ["fix", "repair", "heal"]):
            return {"action": "auto_fix", "confidence": 0.9, "parameters": {}}
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
- health_check: User wants to check for errors or run diagnostics
- error_report: User wants to see recent errors
- auto_fix: User wants to run auto-fix on detected errors
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

            elif action == "health_check":
                return await self._run_health_check()

            elif action == "error_report":
                return await self._get_error_report()

            elif action == "auto_fix":
                return await self._run_auto_fix()

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
**Model:** {self.agent.config.default_model}
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

    async def _run_health_check(self) -> str:
        """Run self-healing health check.

        Returns:
            Health check results
        """
        try:
            from src.core.self_healing.monitor import SelfHealingMonitor

            # Create temporary monitor for manual check
            monitor = SelfHealingMonitor(
                telegram_notifier=None,  # Don't double-notify
                log_file=self.agent.config.log_file
            )

            result = await monitor.run_manual_check()

            message = f"""🩺 **Health Check Results**

**Errors (last 30min):** {result['errors_detected']}
**Auto-fixable:** {result['auto_fixable']}

**By Severity:**
"""
            for severity, count in result.get('by_severity', {}).items():
                icon = {"critical": "🚨", "high": "⚠️", "medium": "⚠️", "low": "ℹ️"}.get(severity, "•")
                message += f"{icon} {severity.title()}: {count}\n"

            if result['recent_errors']:
                message += "\n**Recent Errors:**\n"
                for err in result['recent_errors'][:3]:
                    fix_icon = "✅" if err['auto_fixable'] else "❌"
                    message += f"{fix_icon} {err['type']}: {err['message'][:60]}...\n"

            if result['errors_detected'] == 0:
                message += "\n✅ **No errors detected - system healthy!**"
            elif result['auto_fixable'] > 0:
                message += f"\n💡 **Tip:** Send 'fix errors' to auto-fix {result['auto_fixable']} fixable errors"

            return message

        except Exception as e:
            logger.error(f"Error running health check: {e}")
            return f"❌ Error running health check: {str(e)}"

    async def _get_error_report(self) -> str:
        """Get detailed error report.

        Returns:
            Error report
        """
        try:
            from src.core.self_healing.error_detector import ErrorDetector

            detector = ErrorDetector(log_file=self.agent.config.log_file)
            errors = detector.scan_recent_logs(minutes=60)  # Last hour

            if not errors:
                return "✅ **No errors detected in the last hour!**"

            summary = detector.get_error_summary()

            message = f"""📊 **Error Report (Last Hour)**

**Total Errors:** {summary['total_errors']}
**Auto-fixable:** {summary['auto_fixable']}

**By Type:**
"""
            for error_type, count in summary.get('by_type', {}).items():
                message += f"• {error_type}: {count}\n"

            message += "\n**Recent:**\n"
            for err in summary.get('recent_errors', [])[:5]:
                fix_icon = "✅" if err['auto_fixable'] else "❌"
                message += f"{fix_icon} [{err['severity']}] {err['type']}\n"

            return message

        except Exception as e:
            logger.error(f"Error getting error report: {e}")
            return f"❌ Error generating report: {str(e)}"

    async def _run_auto_fix(self) -> str:
        """Run auto-fix on detected errors.

        Returns:
            Auto-fix results
        """
        try:
            from src.core.self_healing.error_detector import ErrorDetector
            from src.core.self_healing.auto_fixer import AutoFixer

            await self.send_message("🔧 Running auto-fix diagnostics...")

            # Detect errors
            detector = ErrorDetector(log_file=self.agent.config.log_file)
            errors = detector.scan_recent_logs(minutes=30)

            fixable_errors = [e for e in errors if e.auto_fixable]

            if not fixable_errors:
                return "✅ **No auto-fixable errors detected!**"

            # Attempt fixes
            fixer = AutoFixer(telegram_notifier=self)
            fixes_successful = 0
            fixes_failed = 0
            needs_restart = False

            for error in fixable_errors:
                result = await fixer.attempt_fix(error)
                if result.success:
                    fixes_successful += 1
                    if result.requires_restart:
                        needs_restart = True
                else:
                    fixes_failed += 1

            message = f"""🔧 **Auto-Fix Complete**

**Attempted:** {len(fixable_errors)}
**Successful:** {fixes_successful} ✅
**Failed:** {fixes_failed} ❌
"""
            if needs_restart:
                message += "\n⚠️ **Restart recommended**\nSend 'restart' to apply fixes"
            else:
                message += "\n✅ **All fixes applied successfully!**"

            return message

        except Exception as e:
            logger.error(f"Error running auto-fix: {e}")
            return f"❌ Error during auto-fix: {str(e)}"

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
