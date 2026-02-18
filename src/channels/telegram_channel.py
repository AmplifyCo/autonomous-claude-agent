"""Telegram channel adapter - thin wrapper around ConversationManager.

This is just a transport layer that:
1. Receives messages from Telegram
2. Passes to ConversationManager (core intelligence)
3. Sends responses back to Telegram

ALL intelligence is in ConversationManager - making it channel-agnostic.
"""

import asyncio
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class TelegramChannel:
    """Telegram channel adapter - thin transport layer only."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        conversation_manager,
        webhook_url: Optional[str] = None
    ):
        """Initialize Telegram channel.

        Args:
            bot_token: Telegram bot token
            chat_id: Authorized chat ID
            conversation_manager: ConversationManager instance (core intelligence)
            webhook_url: Public webhook URL
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.conversation_manager = conversation_manager
        self.webhook_url = webhook_url
        self.enabled = bool(bot_token and chat_id)

        if self.enabled:
            try:
                import telegram
                self.bot = telegram.Bot(token=bot_token)
                logger.info("Telegram channel initialized (thin wrapper)")
            except ImportError:
                logger.warning("python-telegram-bot not installed")
                self.enabled = False
        else:
            logger.info("Telegram channel disabled")

    async def setup_webhook(self):
        """Set up webhook with Telegram."""
        if not self.enabled or not self.webhook_url:
            return False

        try:
            await self.bot.set_webhook(url=self.webhook_url)
            logger.info(f"✅ Telegram webhook set: {self.webhook_url}")

            await self.send_message(
                "🤖 **Agent Connected!**\n\n"
                "Using intelligent multi-model routing with Brain-based context.\n\n"
                "Try: What's your status?"
            )

            return True

        except Exception as e:
            logger.error(f"Webhook setup failed: {e}")
            return False

    async def handle_webhook(self, update_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming webhook from Telegram.

        This is just routing - intelligence is in ConversationManager.

        Args:
            update_data: Telegram update data

        Returns:
            Response dict
        """
        try:
            if "message" not in update_data:
                return {"ok": True}

            message = update_data["message"]
            from_chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text", "")

            # Verify authorized user
            if from_chat_id != self.chat_id:
                logger.warning(f"Unauthorized: {from_chat_id}")
                return {"ok": True}

            if not text:
                return {"ok": True}

            logger.info(f"Received: {text}")

            # Process asynchronously
            asyncio.create_task(self._process_and_respond(text, from_chat_id))

            return {"ok": True}

        except Exception as e:
            logger.error(f"Webhook error: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}

    async def _process_and_respond(self, message: str, user_id: str):
        """Process message and send response with conversational updates.

        Args:
            message: User message
            user_id: User ID
        """
        status_message = None
        try:
            # Send initial status
            status_message = await self.bot.send_message(
                chat_id=self.chat_id,
                text="💭 Thinking...",
                parse_mode="Markdown"
            )

            # Create progress callback for Telegram message editing
            async def update_progress(status: str):
                """Update status message with conversational text (Telegram-specific rendering)."""
                if status_message:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=self.chat_id,
                            message_id=status_message.message_id,
                            text=status,
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.debug(f"Status update skipped: {e}")

            # CORE INTELLIGENCE HERE (channel-agnostic)
            # ConversationManager handles periodic updates internally for ALL operations
            response = await self.conversation_manager.process_message(
                message=message,
                channel="telegram",
                user_id=user_id,
                progress_callback=update_progress  # Telegram-specific callback
            )

            # Delete status message
            try:
                await self.bot.delete_message(
                    chat_id=self.chat_id,
                    message_id=status_message.message_id
                )
            except:
                pass

            # Send final response
            await self.send_message(response)

        except Exception as e:
            logger.error(f"Process error: {e}", exc_info=True)
            # Try to clean up status message
            if status_message:
                try:
                    await self.bot.delete_message(
                        chat_id=self.chat_id,
                        message_id=status_message.message_id
                    )
                except:
                    pass
            await self.send_message(f"❌ Error: {str(e)}")

    async def send_message(self, text: str):
        """Send message to Telegram.

        This is just transport - no intelligence here.

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
            logger.error(f"Send failed: {e}")
