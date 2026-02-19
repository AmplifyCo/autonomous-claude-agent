"""WhatsApp Tool for sending messages via Meta Cloud API."""

from typing import Dict, Any, Optional
from .base import BaseTool
import logging
import asyncio
import aiohttp

logger = logging.getLogger(__name__)

class WhatsAppTool(BaseTool):
    """Tool for sending WhatsApp messages via Meta Cloud API."""

    def __init__(self, api_token: str, phone_id: str):
        """Initialize WhatsApp tool.

        Args:
            api_token: Meta System User Access Token
            phone_id: WhatsApp Phone Number ID
        """
        self.api_token = api_token
        self.phone_id = phone_id
        self.api_url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
        self.enabled = bool(api_token and phone_id)

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def description(self) -> str:
        return "Send WhatsApp messages to known contacts."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient phone number (e.g. 15551234567). No + or dashes."
                },
                "body": {
                    "type": "string",
                    "description": "Message content to send"
                }
            },
            "required": ["to", "body"]
        }

    async def execute(self, to: str, body: str, **kwargs) -> Dict[str, Any]:
        """Execute the tool.

        Args:
            to: Recipient phone number
            body: Message content

        Returns:
            Result dictionary
        """
        if not self.enabled:
            return {"success": False, "error": "WhatsApp tool disabled (missing credentials)"}

        # Meta API requires clean numbers (no + usually, but depends on region)
        # Stripping + just in case, but usually CountryCode + Number is required.
        clean_to = to.replace("+", "").replace("-", "").replace(" ", "")

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        
        data = {
            "messaging_product": "whatsapp",
            "to": clean_to,
            "type": "text",
            "text": {"body": body}
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=data) as resp:
                    resp_data = await resp.json()
                    
                    if resp.status == 200:
                        logger.info(f"📤 WhatsApp tool sent to {clean_to}")
                        return {
                            "success": True, 
                            "output": f"Message sent to {clean_to}",
                            "message_id": resp_data.get("messages", [{}])[0].get("id")
                        }
                    else:
                        logger.error(f"WhatsApp tool failed: {resp.status} {resp_data}")
                        return {
                            "success": False, 
                            "error": f"API Error: {resp_data.get('error', {}).get('message')}"
                        }

        except Exception as e:
            logger.error(f"WhatsApp tool error: {e}")
            return {"success": False, "error": str(e)}
