"""Twilio Programmable Voice channel.

Handles inbound phone calls via Twilio webhooks.
Uses TwiML to gather speech, passes it to the ConversationManager,
and responds with synthesized speech.
"""

import logging
from typing import Dict, Any, Optional
from xml.etree.ElementTree import Element, SubElement, tostring

logger = logging.getLogger(__name__)

class TwilioVoiceChannel:
    """Twilio Programmable Voice channel adapter."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        phone_number: str,
        conversation_manager,
        twilio_call_tool = None
    ):
        """Initialize Twilio Voice channel.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            phone_number: Twilio Phone Number
            conversation_manager: ConversationManager instance
            twilio_call_tool: Optional TwilioCallTool for ElevenLabs TTS
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.phone_number = phone_number
        self.conversation_manager = conversation_manager
        self.twilio_call_tool = twilio_call_tool
        
        self.enabled = bool(account_sid and auth_token and phone_number)
        
        # Default voice settings - User requested Google's voice
        self.voice = "Google.en-US-Journey-F"  # High-quality Google Journey voice
        self.language = "en-US"

        if self.enabled:
            logger.info("✅ Twilio Voice channel initialized")
        else:
            logger.info("Twilio Voice channel disabled (missing credentials)")

    async def _generate_twiml(self, text: Optional[str] = None, prompt_for_input: bool = True) -> str:
        """Generate TwiML XML response, using ElevenLabs if available.
        
        Args:
            text: Text to speak (optional)
            prompt_for_input: Whether to add a Gather verb after speaking
            
        Returns:
            TwiML string
        """
        from xml.sax.saxutils import escape
        
        response = Element('Response')
        
        # Attempt ElevenLabs TTS if we have the tool and it's enabled
        audio_url = None
        if text and self.twilio_call_tool and self.twilio_call_tool.elevenlabs_enabled and self.twilio_call_tool.base_url:
            try:
                # Ask call tool to generate ElevenLabs MP3
                filename = await self.twilio_call_tool._generate_elevenlabs_audio(text, "female")
                if filename:
                    audio_url = f"{self.twilio_call_tool.base_url.rstrip('/')}/audio/{filename}"
            except Exception as e:
                logger.warning(f"Failed to generate ElevenLabs TTS in voice channel: {e}")
                
        def _add_speech(parent_el, speech_text):
            if audio_url:
                # Premium ElevenLabs Voice
                play = SubElement(parent_el, 'Play')
                play.text = escape(audio_url)
            else:
                # Fallback Google Journey Voice
                say = SubElement(parent_el, 'Say', {'voice': self.voice, 'language': self.language})
                say.text = escape(speech_text)

        if text and not prompt_for_input:
            # Just say it and hang up
            _add_speech(response, text)
            SubElement(response, 'Hangup')
            
        elif prompt_for_input:
            # Gather speech input
            # action="/twilio/voice/gather" is where Twilio POSTs the transcribed text
            gather = SubElement(response, 'Gather', {
                'input': 'speech',
                'action': '/twilio/voice/gather',
                'speechTimeout': 'auto',
                'language': self.language
            })
            
            if text:
                _add_speech(gather, text)
            else:
                _add_speech(gather, "Hello. How can I help you today?")

        # Convert to string with proper XML declaration
        xml_str = tostring(response, encoding='utf-8').decode('utf-8')
        return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{xml_str}"

    def _get_user_number(self, form_data: Dict[str, str]) -> str:
        """Determine who the user is.
        
        For inbound calls to us, the user is 'From'.
        For outbound calls from us, the user is 'To' (or 'Called').
        """
        direction = form_data.get("Direction", "inbound")
        if direction == "outbound-api":
            return form_data.get("To") or form_data.get("Called", "Unknown")
        return form_data.get("From", "Unknown")

    async def handle_incoming_call(self, form_data: Dict[str, str]) -> str:
        """Handle initial incoming call webhook (/twilio/voice).
        
        Args:
            form_data: POST form data from Twilio
            
        Returns:
            TwiML string
        """
        if not self.enabled:
            # Fallback if disabled
            response = Element('Response')
            say = SubElement(response, 'Say')
            say.text = "System is currently offline. Please try again later."
            SubElement(response, 'Hangup')
            return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{tostring(response, encoding='utf-8').decode('utf-8')}"
            
        user_number = self._get_user_number(form_data)
        call_sid = form_data.get("CallSid", "Unknown")
        direction = form_data.get("Direction", "inbound")
        
        logger.info(f"📞 {direction.title()} voice call with {user_number} (CallSid: {call_sid})")
        
        # Return initial greeting and start gathering speech
        return await self._generate_twiml(text="Hi, I am Nova. How can I help you?", prompt_for_input=True)

    async def handle_gather(self, form_data: Dict[str, str]) -> str:
        """Handle speech recognition result webhook (/twilio/voice/gather).
        
        Args:
            form_data: POST form data from Twilio containing SpeechResult
            
        Returns:
            TwiML string
        """
        if not self.enabled:
            return await self._generate_twiml(text="System offline.", prompt_for_input=False)
            
        user_number = self._get_user_number(form_data)
        speech_result = form_data.get("SpeechResult", "").strip()
        confidence = form_data.get("Confidence", "0.0")
        
        logger.info(f"🗣️ Speech from {user_number} (confidence {confidence}): {speech_result}")
        
        if not speech_result:
            # Didn't catch that
            return await self._generate_twiml(text="I'm sorry, I didn't catch that. Could you please repeat?", prompt_for_input=True)
            
        try:
            # Process via ConversationManager
            ai_response = await self.conversation_manager.process_message(
                message=speech_result,
                channel="voice",
                user_id=user_number,
                enable_periodic_updates=False
            )
            
            # Check if this is a goodbye/end call intent from the AI
            # A simple heuristic: if the AI says goodbye or similar, we might want to hang up.
            is_goodbye = any(phrase in ai_response.lower() for phrase in ["goodbye", "have a great day", "bye for now"])
            
            # Return the AI's response and prompt for more input (unless it's a goodbye)
            return await self._generate_twiml(text=ai_response, prompt_for_input=not is_goodbye)
            
        except Exception as e:
            logger.error(f"Voice process error: {e}", exc_info=True)
            return await self._generate_twiml(text="I'm sorry, I encountered an error processing your request.", prompt_for_input=True)
