"""Outbound phone call tool via Twilio Programmable Voice."""

from typing import Dict, Any
import logging
from xml.sax.saxutils import escape
from .base import BaseTool, ToolResult
from twilio.rest import Client

logger = logging.getLogger(__name__)


class TwilioCallTool(BaseTool):
    """Tool for making outbound phone calls via Twilio.

    Uses Google's Journey voice — a premium, natural-sounding voice
    that doesn't sound robotic.
    """

    name = "make_phone_call"
    description = (
        "Make an outbound phone call to deliver a spoken message. "
        "Use this when the user asks you to call someone, phone someone, "
        "or deliver an urgent voice message. The recipient will hear "
        "the message spoken in a natural human-like voice."
    )

    parameters = {
        "to_number": {
            "type": "string",
            "description": "The phone number to call (e.g., '+14155551234')"
        },
        "message": {
            "type": "string",
            "description": "The message to speak to the recipient"
        },
        "voice": {
            "type": "string",
            "description": "Voice to use: 'female' (default) or 'male'"
        }
    }

    # Google Journey voices — premium, natural, conversational
    VOICES = {
        "female": "Google.en-US-Journey-F",
        "male": "Google.en-US-Journey-D",
    }

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        """Initialize the Twilio Call tool.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            from_number: Twilio phone number (e.g., '+14155551234')
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

        self.enabled = bool(account_sid and auth_token and from_number)

        if self.enabled:
            self.client = Client(account_sid, auth_token)

    def _build_twiml(self, message: str, voice_key: str = "female") -> str:
        """Build TwiML to speak a message with a natural Google voice.

        Args:
            message: Text to speak
            voice_key: 'female' or 'male'

        Returns:
            TwiML XML string
        """
        voice = self.VOICES.get(voice_key, self.VOICES["female"])
        safe_message = escape(message)

        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f'<Say voice="{voice}" language="en-US">{safe_message}</Say>'
            "<Pause length=\"1\"/>"
            f'<Say voice="{voice}" language="en-US">'
            "If you would like to respond, please send a message. Goodbye!"
            "</Say>"
            "</Response>"
        )

    async def execute(self, **kwargs) -> ToolResult:
        """Make an outbound phone call.

        Args:
            to_number: Phone number to call
            message: Message to speak
            voice: 'female' or 'male' (optional, default 'female')

        Returns:
            ToolResult with call SID and status
        """
        if not self.enabled:
            return ToolResult(
                error="Twilio Call tool is not configured (missing credentials)",
                success=False
            )

        to_number = kwargs.get("to_number")
        message = kwargs.get("message")
        voice_key = kwargs.get("voice", "female").lower()

        if not to_number or not message:
            return ToolResult(
                error="Missing required parameters: to_number and message",
                success=False
            )

        # Strip whatsapp: prefix if accidentally passed
        to_number = to_number.replace("whatsapp:", "")

        # Ensure + prefix for international format
        if not to_number.startswith("+"):
            to_number = f"+{to_number}"

        twiml = self._build_twiml(message, voice_key)

        try:
            logger.info(f"📞 Making outbound call to {to_number}")

            call = self.client.calls.create(
                twiml=twiml,
                to=to_number,
                from_=self.from_number
            )

            logger.info(f"📞 Call initiated: SID={call.sid}, status={call.status}")

            return ToolResult(
                output=f"Call initiated to {to_number} (SID: {call.sid}, status: {call.status})",
                success=True,
                data={"call_sid": call.sid, "status": call.status}
            )

        except Exception as e:
            error_msg = f"Failed to make phone call: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return ToolResult(
                error=error_msg,
                success=False
            )
