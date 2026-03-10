"""Twilio Programmable Voice channel.

Handles inbound phone calls via Twilio webhooks.
Uses TwiML to gather speech, passes it to the ConversationManager,
and responds with synthesized speech.

Supports outbound call missions — when Nova makes a call on behalf of
the user (e.g., "call the restaurant and reserve a table"), the mission
context is injected into every gather webhook so Nova knows its goal.
"""

import logging
import os
import time
from typing import Dict, Any, List, Optional
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)


class TwilioVoiceChannel:
    """Twilio Programmable Voice channel adapter."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        phone_number: str,
        conversation_manager,
        twilio_call_tool=None,
        allowed_numbers: Optional[List[str]] = None
    ):
        """Initialize Twilio Voice channel.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            phone_number: Twilio Phone Number
            conversation_manager: ConversationManager instance
            twilio_call_tool: Optional TwilioCallTool for ElevenLabs TTS
            allowed_numbers: Optional list of allowed numbers
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.phone_number = phone_number
        self.conversation_manager = conversation_manager
        self.twilio_call_tool = twilio_call_tool
        self.allowed_numbers = allowed_numbers or []

        self.enabled = bool(account_sid and auth_token and phone_number)

        # Default voice settings
        self.voice = "Google.en-US-Journey-F"
        self.language = "en-US"

        # Call Mission Context: {call_sid: {mission, originator, started_at}}
        # When Nova makes an outbound call with a goal (e.g. "reserve a table"),
        # the mission is stored here and injected into every gather webhook.
        self._call_missions: Dict[str, Dict[str, Any]] = {}

        if self.enabled:
            logger.info("✅ Twilio Voice channel initialized")
        else:
            logger.info("Twilio Voice channel disabled (missing credentials)")

    # ── Mission Context System ───────────────────────────────────────────

    def register_call_mission(
        self,
        call_sid: str,
        mission: str,
        originator: str = ""
    ):
        if not originator:
            from src.core.config import get_owner_name
            originator = f"{get_owner_name()} (Principal)"
        """Register a mission for an outbound call.

        When the gather webhook fires, the mission context is injected
        into the message so the AI knows what it's trying to accomplish.

        Args:
            call_sid: Twilio Call SID
            mission: What Nova is trying to accomplish on this call
            originator: Who requested the call (for post-call reporting)
        """
        self._call_missions[call_sid] = {
            "mission": mission,
            "originator": originator,
            "started_at": time.time(),
        }
        logger.info(f"📋 Call mission registered for {call_sid}: {mission[:80]}...")

    def _get_call_mission(self, call_sid: str) -> Optional[Dict[str, Any]]:
        """Get mission context for a call, if any."""
        mission = self._call_missions.get(call_sid)
        if mission:
            # Clean up stale missions (older than 30 minutes)
            if time.time() - mission["started_at"] > 1800:
                del self._call_missions[call_sid]
                logger.info(f"🗑️ Stale mission cleaned up for {call_sid}")
                return None
        return mission

    # ── TwiML Generation ─────────────────────────────────────────────────

    async def _generate_twiml(self, text: Optional[str] = None, prompt_for_input: bool = True) -> str:
        """Generate TwiML XML response, using ElevenLabs if available.

        Args:
            text: Text to speak (optional)
            prompt_for_input: Whether to add a Gather verb after speaking

        Returns:
            TwiML string
        """
        response = Element("Response")

        if text:
            # Try ElevenLabs first if we have the tool
            twiml_injected = False
            if self.twilio_call_tool and self.twilio_call_tool.elevenlabs_enabled:
                audio_filename = await self.twilio_call_tool._generate_elevenlabs_audio(text)
                if audio_filename and self.twilio_call_tool.base_url:
                    audio_url = f"{self.twilio_call_tool.base_url.rstrip('/')}/audio/{audio_filename}"
                    play = SubElement(response, "Play")
                    play.text = audio_url
                    twiml_injected = True

            # Fallback to Google Journey voices
            if not twiml_injected:
                say = SubElement(response, "Say", voice=self.voice, language=self.language)
                say.text = escape(text)

        if prompt_for_input:
            gather = SubElement(
                response,
                "Gather",
                input="speech",
                action="/twilio/voice/gather",
                timeout="5",
                speechTimeout="auto",
                language="en-US"
            )

        return tostring(response, encoding="unicode")

    # ── Number Resolution ────────────────────────────────────────────────

    def _get_user_number(self, form_data: Dict[str, str]) -> str:
        """Determine the actual user's phone number based on call direction.

        Inbound calls: user's number in 'From'.
        Outbound calls: recipient's number in 'To' (our number in 'From').
        """
        direction = form_data.get("Direction", "")

        if "outbound" in direction.lower() or form_data.get("From") == self.phone_number:
            raw_number = form_data.get("To", "unknown_outbound")
        else:
            raw_number = form_data.get("From", "unknown_inbound")

        # Strip prefixes for clean matching
        clean_number = raw_number.replace("whatsapp:", "")
        clean_allowed = [num.replace("whatsapp:", "") for num in self.allowed_numbers]

        # Explicitly identify Principal to prevent LLM hallucination
        if clean_number in clean_allowed:
            from src.core.config import get_owner_name
            return f"{get_owner_name()} (Principal)"

        return clean_number

    # ── Webhook Handlers ─────────────────────────────────────────────────

    def _is_caller_allowed(self, form_data: Dict[str, str]) -> bool:
        """Check if the caller is in the allowed_numbers list.

        Returns True if:
        - allowed_numbers is configured AND caller matches, OR
        - This is an outbound call (we initiated it), OR
        - The call has an active mission (outbound agent call)
        """
        if not self.allowed_numbers:
            # No allow-list configured — reject all inbound (fail-closed)
            return False

        direction = form_data.get("Direction", "")
        if "outbound" in direction.lower():
            return True  # We initiated this call

        call_sid = form_data.get("CallSid", "")
        if call_sid in self._call_missions:
            return True  # Active mission call

        raw_from = form_data.get("From", "")
        clean_from = raw_from.replace("whatsapp:", "")
        clean_allowed = [n.replace("whatsapp:", "") for n in self.allowed_numbers]
        return clean_from in clean_allowed

    async def handle_incoming_call(self, form_data: Dict[str, str]) -> str:
        """Handle initial incoming call webhook (/twilio/voice).

        For outbound mission calls, the initial greeting comes from
        the TwiML set by TwilioCallTool. This handler is for inbound only.
        """
        if not self.enabled:
            response = Element('Response')
            say = SubElement(response, 'Say')
            say.text = "System is currently offline. Please try again later."
            SubElement(response, 'Hangup')
            return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{tostring(response, encoding='utf-8').decode('utf-8')}"

        # Caller authorization — reject unknown callers (denial-of-wallet protection)
        if not self._is_caller_allowed(form_data):
            caller = form_data.get("From", "unknown")
            logger.warning(f"Rejected unauthorized voice call from {caller}")
            response = Element('Response')
            say = SubElement(response, 'Say')
            say.text = "Sorry, this number is not authorized. Goodbye."
            SubElement(response, 'Hangup')
            return tostring(response, encoding="unicode")

        user_number = self._get_user_number(form_data)
        call_sid = form_data.get("CallSid", "Unknown")
        direction = form_data.get("Direction", "inbound")

        logger.info(f"📞 {direction.title()} voice call with {user_number} (CallSid: {call_sid})")

        from src.core.config import get_bot_name
        return await self._generate_twiml(text=f"Hi, I'm {get_bot_name()} - a Non-Human Assistant. How can I help you?", prompt_for_input=True)

    async def handle_gather(self, form_data: Dict[str, str]) -> str:
        """Handle speech recognition result webhook (/twilio/voice/gather).

        For mission calls: injects mission context so Nova knows its goal.
        For regular calls: processes speech normally through ConversationManager.
        """
        if not self.enabled:
            return await self._generate_twiml(text="System offline.", prompt_for_input=False)

        user_number = self._get_user_number(form_data)
        speech_result = form_data.get("SpeechResult", "").strip()
        confidence = form_data.get("Confidence", "0.0")
        call_sid = form_data.get("CallSid", "")

        logger.info(f"🗣️ Speech from {user_number} (confidence {confidence}): {speech_result}")

        if not speech_result:
            return await self._generate_twiml(
                text="I'm sorry, I didn't catch that. Could you please repeat?",
                prompt_for_input=True
            )

        try:
            # Check if this call has an active mission (outbound agent call)
            mission = self._get_call_mission(call_sid)

            if mission:
                # MISSION MODE: Nova is the caller, pursuing a goal
                # Inject mission context so the AI knows what it's trying to accomplish
                contextualized_message = (
                    f"[ACTIVE CALL MISSION — You are making this call on behalf of {mission['originator']}]\n"
                    f"YOUR GOAL: {mission['mission']}\n\n"
                    f"The person you called just said: \"{speech_result}\"\n\n"
                    f"Respond naturally as {os.getenv('BOT_NAME', 'Nova')}, {os.getenv('OWNER_NAME', 'User')}'s Non-Human Assistant. "
                    f"Stay focused on the mission goal. Be polite but purposeful. "
                    f"If your goal is achieved or clearly impossible, say goodbye to end the call."
                )
                logger.info(f"📋 Mission-aware gather for CallSid {call_sid}")
            else:
                # NORMAL MODE: Someone called us or general conversation
                contextualized_message = speech_result

            # Fast-path voice processing — single LLM call, skips intent parsing + agent loop
            # Saves ~1s per turn vs full process_message() pipeline (critical for voice UX)
            if hasattr(self.conversation_manager, 'process_voice_message'):
                ai_response = await self.conversation_manager.process_voice_message(
                    message=contextualized_message,
                    user_id=user_number,
                )
            else:
                ai_response = await self.conversation_manager.process_message(
                    message=contextualized_message,
                    channel="voice",
                    user_id=user_number,
                    enable_periodic_updates=False
                )

            # Check for goodbye / end of call
            is_goodbye = any(
                phrase in ai_response.lower()
                for phrase in ["goodbye", "have a great day", "bye for now", "take care", "bye bye"]
            )

            if is_goodbye:
                logger.info("Call ending detected. Starting background WhatsApp report.")
                self._start_post_call_report(call_sid, user_number, mission)

                # Clean up mission
                if call_sid in self._call_missions:
                    del self._call_missions[call_sid]

            return await self._generate_twiml(text=ai_response, prompt_for_input=not is_goodbye)

        except Exception as e:
            logger.error(f"Voice process error: {e}", exc_info=True)
            return await self._generate_twiml(
                text="I'm sorry, I encountered an error processing your request.",
                prompt_for_input=True
            )

    # ── Post-Call Reporting ───────────────────────────────────────────────

    def _start_post_call_report(
        self,
        call_sid: str,
        user_number: str,
        mission: Optional[Dict[str, Any]]
    ):
        """Start async background task to summarize call and WhatsApp the Principal."""
        import asyncio

        async def run_reporting_task():
            try:
                await asyncio.sleep(5)  # Let the call disconnect first

                # Get recent conversation history
                history = await self.conversation_manager._get_recent_history_for_intent()

                if mission:
                    reporting_task = (
                        f"You just completed a phone call with {user_number} on behalf of {mission['originator']}.\n"
                        f"The mission was: {mission['mission']}\n\n"
                        f"Call transcript:\n{history}\n\n"
                        f"Please execute the 'send_whatsapp_message' tool to send a WhatsApp message to "
                        f"{mission['originator']} summarizing the outcome. "
                        f"Include: whether the mission succeeded or failed, key details "
                        f"(times, confirmations, reference numbers), and any follow-up needed."
                    )
                else:
                    reporting_task = (
                        f"You just finished a phone call with {user_number}. "
                        f"Here is the transcript of the call:\n\n{history}\n\n"
                        f"Please execute the 'send_whatsapp_message' tool to send a WhatsApp message to {os.getenv('OWNER_NAME', 'the principal')} "
                        "summarizing the outcome of this call. Be concise but include any relevant details."
                    )

                agent = getattr(self.conversation_manager, 'agent', None)
                if agent:
                    await agent.run(task=reporting_task)
                else:
                    logger.error("Could not find AutonomousAgent to execute background task.")
            except Exception as loop_e:
                logger.error(f"Background reporting task failed: {loop_e}", exc_info=True)

        asyncio.create_task(run_reporting_task())
