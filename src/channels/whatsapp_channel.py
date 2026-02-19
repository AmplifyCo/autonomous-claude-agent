"""WhatsApp channel adapter using Meta Cloud API.

This adapter handles:
1. Webhook Verification (GET)
2. Receiving inbound messages via Meta Webhook (POST)
3. Passing them to ConversationManager
4. Sending responses back via Meta Graph API
"""

import logging
import asyncio
import aiohttp
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class WhatsAppChannel:
    """WhatsApp channel adapter using Meta Cloud API."""

    def __init__(
        self,
        api_token: str,
        phone_id: str,
        verify_token: str,
        conversation_manager,
        allowed_numbers: Optional[list] = None
    ):
        """Initialize WhatsApp channel.

        Args:
            api_token: Meta System User Access Token
            phone_id: WhatsApp Phone Number ID
            verify_token: Webhook verification token
            conversation_manager: ConversationManager instance
            allowed_numbers: List of allowed phone numbers (whitelist)
        """
        self.api_token = api_token
        self.phone_id = phone_id
        self.verify_token = verify_token
        self.conversation_manager = conversation_manager
        self.allowed_numbers = allowed_numbers or []
        
        self.api_url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
        self.enabled = bool(api_token and phone_id and verify_token)

        if self.enabled:
            logger.info("✅ WhatsApp channel initialized (Meta Cloud API)")
        else:
            logger.info("WhatsApp channel disabled (missing credentials)")

    async def verify_webhook(self, params: Dict[str, str]) -> Optional[str]:
        """Handle Webhook Verification (GET).
        
        Args:
            params: Query parameters (hub.mode, hub.verify_token, hub.challenge)
            
        Returns:
            Challenge string if verified, None otherwise.
        """
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")

        if mode and token:
            if mode == "subscribe" and token == self.verify_token:
                logger.info("✅ Webhook verified successfully")
                return challenge
            else:
                logger.warning("🚫 Webhook verification failed (token mismatch)")
                return None
        return None

    async def handle_webhook_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming webhook payload (POST).
        
        Args:
            payload: JSON payload from Meta
            
        Returns:
            Response dict
        """
        if not self.enabled:
            return {"status": "disabled"}

        try:
            # Parse Meta Webhook Structure
            entry = payload.get("entry", [])
            if not entry:
                return {"status": "ignored", "reason": "no_entry"}
                
            changes = entry[0].get("changes", [])
            if not changes:
                return {"status": "ignored", "reason": "no_changes"}
                
            value = changes[0].get("value", {})
            messages = value.get("messages", [])
            
            if not messages:
                # Could be a status update (sent/delivered/read)
                return {"status": "ignored", "reason": "no_messages"}

            message = messages[0]
            from_number = message.get("from", "") # e.g. "16505551234"
            msg_type = message.get("type", "")
            
            # Extract text
            body = ""
            if msg_type == "text":
                body = message.get("text", {}).get("body", "")
            else:
                body = f"[{msg_type} message]"

            # Check whitelist if configured
            if self.allowed_numbers:
                if from_number not in self.allowed_numbers:
                    logger.warning(f"🚫 Unauthorized WhatsApp message from {from_number}")
                    return {"status": "ignored", "reason": "unauthorized"}

            logger.info(f"📩 WhatsApp received from {from_number}: {body[:50]}...")

            # Process asynchronously
            asyncio.create_task(self._process_and_respond(body, from_number))

            return {"status": "received"}

        except Exception as e:
            logger.error(f"WhatsApp webhook error: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

    async def _process_and_respond(self, message: str, user_id: str):
        """Process message and send response.

        Args:
            message: User message
            user_id: User ID (phone number)
        """
        try:
            # CORE INTELLIGENCE
            # Note: We passed the raw phone number as user_id
            response = await self.conversation_manager.process_message(
                message=message,
                channel="whatsapp",
                user_id=user_id,
                enable_periodic_updates=False 
            )

            # Send final response
            await self.send_message(response, user_id)

        except Exception as e:
            logger.error(f"WhatsApp process error: {e}", exc_info=True)
            await self.send_message(f"❌ Error: {str(e)}", user_id)

    async def send_message(self, text: str, to_number: str):
        """Send message via Meta Graph API.

        Args:
            text: Message text
            to_number: Recipient phone number
        """
        if not self.enabled:
            return

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        
        data = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": text}
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=data) as resp:
                    resp_data = await resp.json()
                    
                    if resp.status == 200:
                        logger.info(f"📤 WhatsApp sent to {to_number[:12]}...")
                    else:
                        logger.error(f"Failed to send WhatsApp: {resp.status} {resp_data}")

        except Exception as e:
            logger.error(f"Failed to send WhatsApp (Network): {e}")
