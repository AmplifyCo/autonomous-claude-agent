"""Twilio Programmable Messaging (WhatsApp) channel adapter.

Handles incoming Twilio webhooks for WhatsApp messages, passes
them to the ConversationManager, and can send outbound messages via Twilio REST API.
"""

import asyncio
import logging
import os
from typing import Dict, Any, List, Optional
from twilio.rest import Client

logger = logging.getLogger(__name__)

class TwilioWhatsAppChannel:
    """Twilio WhatsApp channel adapter."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        whatsapp_number: str,
        conversation_manager,
        allowed_numbers: Optional[List[str]] = None
    ):
        """Initialize Twilio WhatsApp channel.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            whatsapp_number: The Twilio number formatted as 'whatsapp:+1234567890'
            conversation_manager: ConversationManager instance
            allowed_numbers: Optional list of allowed WhatsApp numbers (without 'whatsapp:' prefix usually)
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.whatsapp_number = whatsapp_number
        self.conversation_manager = conversation_manager
        self.allowed_numbers = allowed_numbers or []
        
        self.enabled = bool(account_sid and auth_token and whatsapp_number)
        
        if self.enabled:
            # Initialize Twilio REST client for outbound messaging
            self.client = Client(self.account_sid, self.auth_token)
            logger.info(f"✅ Twilio WhatsApp channel initialized ({self.whatsapp_number})")
        else:
            logger.info("Twilio WhatsApp channel disabled (missing credentials)")

    def _is_allowed(self, phone_number: str) -> bool:
        """Check if number is allowed to interact with the bot.

        Fail-closed: if no allowed_numbers configured, reject everyone
        (prevents accidental open access when .env is incomplete).
        """
        if not self.allowed_numbers:
            logger.warning(f"WhatsApp rejected {phone_number}: WHATSAPP_ALLOWED_NUMBERS not configured (fail-closed)")
            return False

        # Strip 'whatsapp:' for checking against allowed list
        clean_number = phone_number.replace('whatsapp:', '')
        clean_allowed = [num.replace('whatsapp:', '') for num in self.allowed_numbers]
        return clean_number in clean_allowed

    async def handle_webhook(self, form_data: Dict[str, str]) -> str:
        """Handle incoming Twilio WhatsApp webhook (/twilio/whatsapp).
        
        Args:
            form_data: POST form data from Twilio
            
        Returns:
            Empty TwiML string (we reply asynchronously via REST API for better latency handling)
        """
        if not self.enabled:
            return "<Response></Response>"
            
        from_number = form_data.get("From", "")
        message_body = form_data.get("Body", "").strip()
        
        # Twilio sends images/media in MediaUrl0, MediaUrl1, etc.
        # For MVP we handle text.
        
        if not message_body:
            logger.debug(f"Ignoring empty message from {from_number}")
            return "<Response></Response>"
            
        if not self._is_allowed(from_number):
            logger.warning(f"Message from unauthorized number: {from_number}")
            return "<Response></Response>"
            
        logger.info(f"💬 Received WhatsApp message from {from_number}: {message_body[:50]}...")

        clean_number = from_number.replace('whatsapp:', '')
        clean_allowed = [num.replace('whatsapp:', '') for num in self.allowed_numbers]
        if clean_number in clean_allowed:
            from src.core.config import get_owner_name
            owner_name = get_owner_name()
            user_id = f"{owner_name} (Principal)"
        else:
            user_id = clean_number

        # Fire-and-forget: return 200 to Twilio immediately, process in background.
        # This prevents Twilio's 15s webhook timeout from triggering on slow AI responses.
        asyncio.create_task(self._process_and_reply(from_number, message_body, user_id))

        return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"

    async def _process_and_reply(self, from_number: str, message_body: str, user_id: str) -> None:
        """Process message with ConversationManager and send reply via REST API."""
        try:
            ai_response = await self.conversation_manager.process_message(
                message=message_body,
                channel="whatsapp",
                user_id=user_id,
                enable_periodic_updates=False,
                raw_contact=from_number,  # actual phone for task notification routing
            )
            self.send_message(to=from_number, body=ai_response)
        except Exception as e:
            logger.error(f"Error processing WhatsApp message: {e}", exc_info=True)
            self.send_message(to=from_number, body="Sorry, I encountered an error processing your request.")

    def send_message(self, to: str, body: str) -> bool:
        """Send an outbound WhatsApp message using Twilio REST API.
        
        Args:
            to: Destination number (e.g., 'whatsapp:+1234567890' or '+1234567890')
            body: Message content
            
        Returns:
            True if successful, False otherwise
        """
        if not self.enabled:
            logger.error("Cannot send message: Twilio WhatsApp channel disabled")
            return False
            
        # Ensure 'whatsapp:' prefix is present
        to_number = to if to.startswith('whatsapp:') else f"whatsapp:{to}"
        
        try:
            logger.debug(f"Sending Twilio WhatsApp message to {to_number}")
            message = self.client.messages.create(
                from_=self.whatsapp_number,
                body=body,
                to=to_number
            )
            logger.info(f"✅ Sent Twilio WhatsApp message (SID: {message.sid}) to {to_number}")
            return True
        except Exception as e:
            logger.error(f"Failed to send Twilio WhatsApp message: {e}")
            return False
