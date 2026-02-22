"""Outbound phone call tool via Twilio Programmable Voice.

Primary TTS: ElevenLabs (natural, premium voices)
Fallback TTS: Google Journey (built into Twilio, no extra API needed)
"""

import os
import uuid
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional
from xml.sax.saxutils import escape
from .base import BaseTool, ToolResult
from twilio.rest import Client

logger = logging.getLogger(__name__)

# Directory for temporary audio files served to Twilio
AUDIO_DIR = Path("/tmp/nova_audio")
AUDIO_DIR.mkdir(exist_ok=True)


class TwilioCallTool(BaseTool):
    """Tool for making outbound phone calls via Twilio.

    Uses ElevenLabs for premium natural voices, falls back to
    Google Journey voices if ElevenLabs is unavailable.
    """

    name = "make_phone_call"
    description = (
        "Make an outbound phone call to deliver a spoken message. "
        "Use this when the user asks you to call someone, phone someone, "
        "or deliver an urgent voice message. The recipient will hear "
        "the message spoken in a natural human-like voice. "
        "For task-oriented calls (e.g. making a reservation), set the 'mission' "
        "parameter so Nova knows the goal and can have a full conversation."
    )

    parameters = {
        "to_number": {
            "type": "string",
            "description": "The phone number to call (e.g., '+14155551234')"
        },
        "message": {
            "type": "string",
            "description": "The initial message to speak when the call connects"
        },
        "mission": {
            "type": "string",
            "description": "Optional goal for task-oriented calls (e.g. 'Reserve a table for 2 at 7:30 PM tonight. Negotiate for 7:00 or 8:00 if unavailable. Party name: Alex.'). When set, the bot will have a full back-and-forth conversation to achieve this goal."
        },
        "voice": {
            "type": "string",
            "description": "Voice to use: 'female' (default) or 'male'"
        }
    }

    # Google Journey voices — fallback (built into Twilio)
    GOOGLE_VOICES = {
        "female": "Google.en-US-Journey-F",
        "male": "Google.en-US-Journey-D",
    }

    # ElevenLabs voice IDs — always read from ELEVENLABS_VOICE_ID in .env (no hardcoded fallback)
    _default_voice_id = os.getenv("ELEVENLABS_VOICE_ID", "")

    ELEVENLABS_VOICES = {
        "female": _default_voice_id,
        "male": _default_voice_id,
    }

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        elevenlabs_api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        voice_channel=None,
    ):
        """Initialize the Twilio Call tool.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            from_number: Twilio phone number (e.g., '+14155551234')
            elevenlabs_api_key: Optional ElevenLabs API key for premium voices
            base_url: Public base URL for serving audio (e.g., 'https://example.com')
            voice_channel: Optional TwilioVoiceChannel for mission registration
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.elevenlabs_api_key = elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY")
        self.base_url = base_url
        self.voice_channel = voice_channel

        self.enabled = bool(account_sid and auth_token and from_number)
        self.elevenlabs_enabled = bool(self.elevenlabs_api_key)

        if self.enabled:
            self.client = Client(account_sid, auth_token)

        if self.elevenlabs_enabled:
            logger.info("📞 TwilioCallTool: ElevenLabs voices enabled (primary)")
        else:
            logger.info("📞 TwilioCallTool: Google Journey voices (no ELEVENLABS_API_KEY)")

    async def _generate_elevenlabs_audio(
        self, text: str, voice_key: str = "female"
    ) -> Optional[str]:
        """Generate speech audio via ElevenLabs API.

        Args:
            text: Text to synthesize
            voice_key: 'female' or 'male'

        Returns:
            Filename of generated audio, or None on failure
        """
        voice_id = self.ELEVENLABS_VOICES.get(voice_key, self.ELEVENLABS_VOICES["female"])
        filename = f"{uuid.uuid4().hex}.mp3"
        filepath = AUDIO_DIR / filename

        try:
            import httpx

            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {
                "xi-api-key": self.elevenlabs_api_key,
                "Content-Type": "application/json",
            }
            payload = {
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.4,
                }
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                filepath.write_bytes(response.content)
                logger.info(f"🎙️ ElevenLabs audio generated: {filename} ({len(response.content)} bytes)")
                return filename
            else:
                logger.warning(f"ElevenLabs API error {response.status_code}: {response.text[:200]}")
                return None

        except ImportError:
            logger.warning("httpx not installed — falling back to Google Journey voice")
            return None
        except Exception as e:
            logger.warning(f"ElevenLabs TTS failed: {e}")
            return None

    def _gather_action_url(self) -> str:
        """Build absolute gather action URL for outbound call TwiML.

        Twilio requires absolute URLs in TwiML passed inline to calls.create().
        Relative URLs like '/twilio/voice/gather' resolve to api.twilio.com — wrong.
        """
        if self.base_url:
            return f"{self.base_url.rstrip('/')}/twilio/voice/gather"
        return "/twilio/voice/gather"  # Fallback (works for inbound webhook calls)

    def _build_twiml_play(self, audio_url: str) -> str:
        """Build TwiML that plays a pre-generated audio file and starts listening.

        Args:
            audio_url: Public URL to the audio file

        Returns:
            TwiML XML string
        """
        gather_url = self._gather_action_url()
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f'<Gather input="speech" action="{gather_url}" speechTimeout="auto" language="en-US">'
            f"<Play>{escape(audio_url)}</Play>"
            "</Gather>"
            "</Response>"
        )

    def _build_twiml_say(self, message: str, voice_key: str = "female") -> str:
        """Build TwiML with Google Journey voice (fallback) and starts listening.

        Args:
            message: Text to speak
            voice_key: 'female' or 'male'

        Returns:
            TwiML XML string
        """
        voice = self.GOOGLE_VOICES.get(voice_key, self.GOOGLE_VOICES["female"])
        safe_message = escape(message)
        gather_url = self._gather_action_url()

        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f'<Gather input="speech" action="{gather_url}" speechTimeout="auto" language="en-US">'
            f'<Say voice="{voice}" language="en-US">{safe_message}</Say>'
            "</Gather>"
            "</Response>"
        )

    async def execute(self, **kwargs) -> ToolResult:
        """Make an outbound phone call.

        Tries ElevenLabs for premium voice, falls back to Google Journey.

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
        mission = kwargs.get("mission")
        voice_key = kwargs.get("voice", "female").lower()

        if not to_number or not message:
            return ToolResult(
                error="Missing required parameters: to_number and message",
                success=False
            )

        # Strip whatsapp: prefix if accidentally passed
        to_number = to_number.replace("whatsapp:", "")
        
        # Clean up the number by removing spaces, dashes, parentheses
        import re
        to_number = re.sub(r'[^\d+]', '', to_number)

        # If it's a 10-digit number without a country code, assume US/Canada and add +1
        if len(to_number) == 10 and not to_number.startswith("+"):
            to_number = f"+1{to_number}"
        # Otherwise, just ensure it starts with a + for Twilio's E.164 format requirement
        elif not to_number.startswith("+"):
            to_number = f"+{to_number}"

        # Try ElevenLabs first, fall back to Google Journey
        twiml = None
        voice_used = "Google Journey"

        if self.elevenlabs_enabled and self.base_url:
            audio_filename = await self._generate_elevenlabs_audio(message, voice_key)
            if audio_filename:
                audio_url = f"{self.base_url.rstrip('/')}/audio/{audio_filename}"
                twiml = self._build_twiml_play(audio_url)
                voice_used = "ElevenLabs"

        if not twiml:
            twiml = self._build_twiml_say(message, voice_key)

        try:
            logger.info(f"📞 Making outbound call to {to_number} (voice: {voice_used})")

            # Outbound API calls pass 'To' to the webhook in the subsequent `/twilio/voice/gather` steps 
            call = self.client.calls.create(
                twiml=twiml,
                to=to_number,
                from_=self.from_number
            )

            logger.info(f"📞 Call initiated: SID={call.sid}, status={call.status}")

            # Register mission with voice channel for two-way conversation
            if mission and self.voice_channel:
                self.voice_channel.register_call_mission(
                    call_sid=call.sid,
                    mission=mission,
                )
                logger.info(f"📋 Mission registered for call {call.sid}")

            result_msg = f"Call initiated to {to_number} (SID: {call.sid}, voice: {voice_used})."
            if mission:
                result_msg += f" Mission registered — Nova will have a full conversation to achieve: {mission}"
            else:
                result_msg += " The recipient will be able to reply via subsequent voice messages."

            return ToolResult(
                output=result_msg,
                success=True,
                metadata={"call_sid": call.sid, "status": call.status, "voice": voice_used, "has_mission": bool(mission)}
            )

        except Exception as e:
            error_msg = f"Failed to make phone call: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return ToolResult(
                error=error_msg,
                success=False
            )

