"""Telegram bot for notifications and remote commands."""

import asyncio
import logging
from typing import Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Send notifications and handle commands via Telegram."""

    def __init__(self, bot_token: Optional[str], chat_id: Optional[str]):
        """Initialize Telegram notifier.

        Args:
            bot_token: Telegram bot token from @BotFather
            chat_id: Your Telegram chat ID
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)

        if self.enabled:
            # Import telegram only if enabled
            try:
                import telegram
                self.bot = telegram.Bot(token=bot_token)
                logger.info("Telegram bot initialized")
            except ImportError:
                logger.warning("python-telegram-bot not installed. Install with: pip install python-telegram-bot")
                self.enabled = False
        else:
            logger.info("Telegram notifications disabled (no token/chat_id)")

    async def notify(self, message: str, level: str = "info"):
        """Send notification to Telegram.

        Args:
            message: Message to send
            level: Message level (info/success/error/warning)
        """
        if not self.enabled:
            return

        try:
            # Add emoji based on level
            emoji = {
                "info": "ℹ️",
                "success": "✅",
                "error": "❌",
                "warning": "⚠️",
                "progress": "📊"
            }

            formatted_message = f"{emoji.get(level, 'ℹ️')} {message}"

            # Send message
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=formatted_message,
                parse_mode="Markdown"
            )

            logger.debug(f"Sent Telegram notification: {level}")

        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    async def send_progress(
        self,
        phase: str,
        current: int,
        total: int,
        details: str = ""
    ):
        """Send progress update.

        Args:
            phase: Current phase name
            current: Current progress
            total: Total items
            details: Optional details
        """
        if not self.enabled:
            return

        try:
            # Create progress bar
            progress_pct = int((current / total) * 100) if total > 0 else 0
            bar_length = 10
            filled = int((current / total) * bar_length) if total > 0 else 0
            progress_bar = "█" * filled + "░" * (bar_length - filled)

            message = f"""📊 *Progress Update*

Phase: {phase}
Progress: {current}/{total} ({progress_pct}%)
{progress_bar}
"""
            if details:
                message += f"\n{details}"

            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"Failed to send progress: {e}")

    async def send_build_start(self, total_features: int):
        """Notify build started.

        Args:
            total_features: Total number of features to build
        """
        await self.notify(
            f"🚀 *Starting Autonomous Build*\n\n"
            f"Total features to build: {total_features}\n"
            f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            level="info"
        )

    async def send_build_complete(self, duration_mins: int, features_built: int):
        """Notify build completed.

        Args:
            duration_mins: Build duration in minutes
            features_built: Number of features built
        """
        await self.notify(
            f"✅ *Build Complete!*\n\n"
            f"Features built: {features_built}\n"
            f"Duration: {duration_mins} minutes\n"
            f"System is now operational! 🎉",
            level="success"
        )

    async def send_error(self, error_msg: str, context: str = ""):
        """Send error notification.

        Args:
            error_msg: Error message
            context: Optional context
        """
        message = f"❌ *Error Occurred*\n\n{error_msg}"
        if context:
            message += f"\n\nContext: {context}"

        await self.notify(message, level="error")


class TelegramCommandHandler:
    """Handle incoming commands from Telegram."""

    def __init__(self, bot_token: str, chat_id: str):
        """Initialize command handler.

        Args:
            bot_token: Bot token
            chat_id: Authorized chat ID
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.handlers = {}

        try:
            from telegram import Update
            from telegram.ext import Application, CommandHandler, ContextTypes

            self.Application = Application
            self.CommandHandler = CommandHandler
            self.Update = Update
            self.ContextTypes = ContextTypes
            self.enabled = True

            logger.info("Telegram command handler initialized")
        except ImportError:
            logger.warning("python-telegram-bot not installed")
            self.enabled = False

    def register_command(self, command: str, handler: Callable):
        """Register a command handler.

        Args:
            command: Command name (without /)
            handler: Async function to handle command
        """
        self.handlers[command] = handler
        logger.info(f"Registered command: /{command}")

    async def start(self):
        """Start listening for commands."""
        if not self.enabled:
            logger.warning("Command handler not enabled")
            return

        try:
            # Create application
            app = self.Application.builder().token(self.bot_token).build()

            # Add default commands
            app.add_handler(self.CommandHandler("start", self._handle_start))
            app.add_handler(self.CommandHandler("help", self._handle_help))
            app.add_handler(self.CommandHandler("status", self._handle_status))

            # Add custom commands
            for cmd, handler in self.handlers.items():
                app.add_handler(self.CommandHandler(cmd, handler))

            logger.info("Starting Telegram bot polling...")
            await app.run_polling()

        except Exception as e:
            logger.error(f"Error starting command handler: {e}")

    async def _handle_start(self, update, context):
        """Handle /start command."""
        await update.message.reply_text(
            "🤖 *Autonomous Claude Agent*\n\n"
            "I'm your autonomous AI agent running 24/7!\n\n"
            "Available commands:\n"
            "/status - Get current status\n"
            "/help - Show help\n"
            "/logs - Get recent logs\n"
            "/build - Start self-building\n"
            "/pause - Pause execution\n"
            "/resume - Resume execution",
            parse_mode="Markdown"
        )

    async def _handle_help(self, update, context):
        """Handle /help command."""
        help_text = """🤖 *Agent Commands*

*Status & Monitoring:*
/status - Current build/execution status
/logs - Recent log entries
/progress - Build progress

*Control:*
/build - Start self-building mode
/pause - Pause current execution
/resume - Resume paused execution
/stop - Stop current task

*Info:*
/health - System health check
/features - List built features
/commits - Recent git commits
"""
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def _handle_status(self, update, context):
        """Handle /status command."""
        # This will be overridden by custom handler
        await update.message.reply_text(
            "📊 *Status*\n\n"
            "Agent is running.\n"
            "Register custom status handler for detailed info.",
            parse_mode="Markdown"
        )
