"""Nova web dashboard — auth-gated chat interface and live monitoring."""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# HTTP paths exempt from auth (webhooks must be reachable without login)
_EXEMPT_PATHS = {"/health", "/login", "/logout", "/telegram/webhook", "/linkedin/auth", "/linkedin/callback", "/nova/chat", "/.well-known/agent-card.json"}
_EXEMPT_PREFIXES = ("/twilio/", "/audio/", "/ws/", "/a2a")


class Dashboard:
    """Web dashboard with session auth, chat window, and live stats."""

    def __init__(self, host: str = "0.0.0.0", port: int = 18789):
        self.host = host
        self.port = port
        self.status = {
            "state": "initializing",
            "phase": "N/A",
            "progress": "0/0",
            "uptime_seconds": 0,
            "last_update": datetime.now().isoformat()
        }
        self.logs: List[Dict] = []
        self.max_logs = 100
        self._start_time = datetime.now()

        # ── Auth ───────────────────────────────────────────────────────
        self._sessions: Dict[str, datetime] = {}   # token → expiry (24h TTL)
        self._dashboard_username = os.getenv("DASHBOARD_USERNAME", "nova")
        self._dashboard_password = os.getenv("DASHBOARD_PASSWORD", "")

        # ── LinkedIn OAuth CSRF state tokens ──────────────────────────
        self._oauth_states: Dict[str, datetime] = {}   # state → expiry (10min TTL)

        # ── Wired components ───────────────────────────────────────────
        self._conversation_manager = None
        self._owner_chat_id: Optional[str] = None
        self._task_queue = None
        self._brain = None
        self._agent_card_builder = None
        self._a2a_handler = None

        try:
            from fastapi import FastAPI
            from fastapi.responses import HTMLResponse, JSONResponse
            import uvicorn

            self.FastAPI = FastAPI
            self.HTMLResponse = HTMLResponse
            self.JSONResponse = JSONResponse
            self.uvicorn = uvicorn
            self.enabled = True

            logger.info(f"Dashboard initialized on {host}:{port}")
        except ImportError:
            logger.warning("FastAPI not installed. Dashboard disabled.")
            logger.warning("Install with: pip install fastapi uvicorn")
            self.enabled = False

    # ── Status helpers ─────────────────────────────────────────────────

    def update_status(self, **kwargs):
        self.status.update(kwargs)
        self.status["last_update"] = datetime.now().isoformat()

    def add_log(self, message: str, level: str = "info"):
        self.logs.append({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message
        })
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

    # ── Webhook security ───────────────────────────────────────────────

    def _configure_webhook_security(self, twilio_auth_token: str = "", base_url: str = ""):
        """Store credentials needed for webhook signature validation.

        Called from main.py after credentials are loaded from env.
        Must be called before start() so the webhook handlers can validate.
        """
        self._twilio_auth_token = twilio_auth_token
        self._base_url = base_url.rstrip("/")
        self._telegram_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET") or secrets.token_hex(32)
        logger.info("Webhook security configured (Twilio HMAC + Telegram secret token)")

    def _validate_twilio_signature(self, request_url: str, params: dict, signature: str) -> bool:
        """Validate Twilio webhook signature (HMAC-SHA1)."""
        auth_token = getattr(self, '_twilio_auth_token', '')
        if not auth_token:
            logger.warning("Twilio auth token not set — rejecting webhook (configure token first)")
            return False
        if not signature:
            logger.warning(f"Missing X-Twilio-Signature on request to {request_url}")
            return False
        try:
            from twilio.request_validator import RequestValidator
            validator = RequestValidator(auth_token)
            return validator.validate(request_url, params, signature)
        except Exception as e:
            logger.error(f"Twilio signature validation error: {e}")
            return False

    def _validate_telegram_secret(self, header_token: str) -> bool:
        """Validate Telegram webhook secret token (constant-time comparison)."""
        expected = getattr(self, '_telegram_secret', '')
        if not expected:
            logger.warning("Telegram webhook secret not set — rejecting webhook (configure secret first)")
            return False
        if not header_token:
            return False
        return hmac.compare_digest(expected, header_token)

    def get_telegram_webhook_secret(self) -> str:
        return getattr(self, '_telegram_secret', '')

    # ── Session auth ───────────────────────────────────────────────────

    def _is_auth_required(self) -> bool:
        """Auth is only enforced when DASHBOARD_PASSWORD is set."""
        return bool(self._dashboard_password)

    def _create_session(self) -> str:
        """Create a new session token with 24-hour TTL."""
        token = secrets.token_hex(32)
        self._sessions[token] = datetime.now() + timedelta(hours=24)
        # Prune expired sessions
        now = datetime.now()
        self._sessions = {t: exp for t, exp in self._sessions.items() if exp > now}
        return token

    def _is_valid_session(self, token: str) -> bool:
        """Return True if the session token is valid and not expired."""
        if not token or token not in self._sessions:
            return False
        if datetime.now() > self._sessions[token]:
            del self._sessions[token]
            return False
        return True

    # ── Component wiring ───────────────────────────────────────────────

    def set_telegram_chat(self, telegram_chat):
        self.telegram_chat = telegram_chat
        logger.info("Telegram chat handler registered with dashboard")

    def set_telegram_notifier(self, notifier):
        self._telegram_notifier = notifier

    def set_twilio_whatsapp_chat(self, twilio_whatsapp_chat):
        self.twilio_whatsapp_chat = twilio_whatsapp_chat
        logger.info("Twilio WhatsApp chat handler registered with dashboard")

    def set_twilio_voice_chat(self, twilio_voice_chat):
        self.twilio_voice_chat = twilio_voice_chat
        logger.info("Twilio Voice chat handler registered with dashboard")

    def set_nova_api_key(self, api_key: str):
        self._nova_api_key = api_key
        logger.info("Nova API key configured (POST /nova/chat enabled)")

    def set_conversation_manager(self, cm, owner_chat_id=None):
        """Wire the conversation manager so dashboard chat routes through it."""
        self._conversation_manager = cm
        self._owner_chat_id = str(owner_chat_id) if owner_chat_id else "dashboard"
        logger.info("Conversation manager wired to dashboard")

    def set_task_queue(self, tq):
        """Wire the task queue for the stats widget."""
        self._task_queue = tq
        logger.info("Task queue wired to dashboard")

    def set_brain(self, brain):
        """Wire the digital brain for the contacts stats widget."""
        self._brain = brain
        logger.info("Digital brain wired to dashboard")

    def set_agent_card_builder(self, builder):
        """Wire A2A agent card builder."""
        self._agent_card_builder = builder

    def set_a2a_handler(self, handler):
        """Wire A2A JSON-RPC handler."""
        self._a2a_handler = handler
        logger.info("A2A handler wired to dashboard")

    # ── Stats helpers ──────────────────────────────────────────────────

    def _get_messages_today(self) -> int:
        """Count messages handled today from in-memory logs."""
        today = datetime.now().date()
        count = 0
        for entry in self.logs:
            try:
                ts = datetime.fromisoformat(entry["timestamp"]).date()
                if ts == today and "Starting autonomous execution" in entry.get("message", ""):
                    count += 1
            except Exception:
                pass
        return count

    def _get_uptime_str(self) -> str:
        delta = datetime.now() - self._start_time
        total_seconds = int(delta.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        if hours >= 24:
            days = hours // 24
            return f"{days}d {hours % 24}h {minutes}m"
        return f"{hours}h {minutes}m"

    # ── Server ─────────────────────────────────────────────────────────

    async def start(self):
        """Start dashboard server with auth, chat, and stats."""
        if not self.enabled:
            logger.warning("Dashboard not enabled")
            return

        from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response as FR

        app = FastAPI(title="Nova Dashboard")

        # ── Auth middleware (HTTP routes only — /ws/ is handled inside handler) ──
        @app.middleware("http")
        async def auth_middleware(request: Request, call_next):
            if not self._is_auth_required():
                return await call_next(request)

            path = request.url.path
            # Exempt paths bypass auth
            if path in _EXEMPT_PATHS or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
                return await call_next(request)

            token = request.cookies.get("nova_session", "")
            if self._is_valid_session(token):
                return await call_next(request)

            # Unauthorized — redirect HTML requests, 401 API requests
            if path.startswith("/api/"):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return RedirectResponse(url="/login", status_code=303)

        # ── Login / logout ────────────────────────────────────────────
        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._get_login_html())

        @app.post("/login")
        async def login_submit(request: Request):
            form = await request.form()
            username = str(form.get("username", ""))
            password = str(form.get("password", ""))
            if (username == self._dashboard_username and
                    self._dashboard_password and
                    hmac.compare_digest(password, self._dashboard_password)):
                token = self._create_session()
                resp = RedirectResponse(url="/", status_code=303)
                resp.set_cookie(
                    "nova_session", token,
                    httponly=True, samesite="lax", max_age=86400,
                )
                return resp
            return HTMLResponse(self._get_login_html(error="Invalid username or password"))

        @app.get("/logout")
        async def logout(request: Request):
            token = request.cookies.get("nova_session", "")
            if token in self._sessions:
                del self._sessions[token]
            resp = RedirectResponse(url="/login", status_code=303)
            resp.delete_cookie("nova_session")
            return resp

        # ── Main dashboard ────────────────────────────────────────────
        @app.get("/", response_class=HTMLResponse)
        async def root():
            return HTMLResponse(self._get_dashboard_html())

        # ── Stats API ─────────────────────────────────────────────────
        @app.get("/api/stats", response_class=JSONResponse)
        async def api_stats():
            # Contacts count from brain
            contacts = 0
            if self._brain:
                try:
                    if hasattr(self._brain, 'get_contacts'):
                        c = self._brain.get_contacts()
                        if asyncio.iscoroutine(c):
                            c = await c
                        contacts = len(c) if c else 0
                    elif hasattr(self._brain, 'contacts'):
                        contacts = len(self._brain.contacts)
                except Exception:
                    pass

            # Task counts
            tasks_pending = 0
            tasks_total = 0
            if self._task_queue:
                try:
                    if hasattr(self._task_queue, 'get_pending_count'):
                        r = self._task_queue.get_pending_count()
                        tasks_pending = (await r) if asyncio.iscoroutine(r) else r
                    if hasattr(self._task_queue, 'get_total_count'):
                        r = self._task_queue.get_total_count()
                        tasks_total = (await r) if asyncio.iscoroutine(r) else r
                except Exception:
                    pass

            delta = datetime.now() - self._start_time
            return {
                "contacts": contacts,
                "tasks_pending": tasks_pending,
                "tasks_total": tasks_total,
                "messages_today": self._get_messages_today(),
                "uptime_seconds": int(delta.total_seconds()),
                "nova_status": "online",
            }

        @app.get("/api/status", response_class=JSONResponse)
        async def get_status():
            return self.status

        @app.get("/api/logs", response_class=JSONResponse)
        async def get_logs():
            return {"logs": self.logs[-50:]}

        # ── WebSocket chat ────────────────────────────────────────────
        @app.websocket("/ws/chat")
        async def chat_websocket(websocket: WebSocket):
            # Auth check on initial handshake
            if self._is_auth_required():
                token = websocket.cookies.get("nova_session", "")
                if not self._is_valid_session(token):
                    await websocket.close(code=1008)
                    return

            await websocket.accept()
            try:
                while True:
                    msg = (await websocket.receive_text()).strip()
                    if not msg:
                        continue

                    if not self._conversation_manager:
                        await websocket.send_text(json.dumps({
                            "sender": "nova",
                            "text": "Dashboard chat not connected yet. Please try again shortly.",
                            "timestamp": datetime.now().isoformat(),
                        }))
                        continue

                    try:
                        response = await self._conversation_manager.process_message(
                            user_message=msg,
                            channel="dashboard",
                            user_id="owner",
                            chat_id=self._owner_chat_id or "dashboard",
                        )
                        reply = response if isinstance(response, str) else str(response)
                    except Exception as e:
                        logger.error(f"Dashboard chat error: {e}", exc_info=True)
                        reply = "Sorry, something went wrong processing your message."

                    await websocket.send_text(json.dumps({
                        "sender": "nova",
                        "text": reply,
                        "timestamp": datetime.now().isoformat(),
                    }))

            except WebSocketDisconnect:
                logger.debug("Dashboard WebSocket client disconnected")

        # ── Health check ──────────────────────────────────────────────
        @app.get("/health")
        async def health():
            return {"status": "healthy", "timestamp": datetime.now().isoformat()}

        # ── A2A Protocol (Agent-to-Agent) ────────────────────────────

        @app.get("/.well-known/agent-card.json")
        async def agent_card():
            """Public agent card — no auth required (A2A spec)."""
            if not self._agent_card_builder:
                return JSONResponse({"error": "Agent card not configured"}, status_code=503)
            return JSONResponse(self._agent_card_builder.build())

        @app.post("/a2a")
        async def a2a_endpoint(request: Request):
            """A2A JSON-RPC 2.0 endpoint — Bearer token auth."""
            if not self._a2a_handler:
                return JSONResponse({"error": "A2A not configured"}, status_code=503)
            auth = request.headers.get("Authorization", "")
            expected = self._a2a_handler._api_key
            if not expected or not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], expected):
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Unauthorized"}},
                    status_code=401,
                )
            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                    status_code=400,
                )
            result = await self._a2a_handler.handle_jsonrpc(body)
            return JSONResponse(result)

        # ── Telegram webhook ──────────────────────────────────────────
        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request):
            tg_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not self._validate_telegram_secret(tg_secret):
                logger.warning(
                    f"Telegram webhook rejected: invalid secret token from "
                    f"{request.client.host if request.client else 'unknown'}"
                )
                return FR(status_code=403)

            if not hasattr(self, 'telegram_chat') or not self.telegram_chat:
                logger.warning("Telegram webhook called but chat handler not set")
                return {"ok": False, "error": "Chat handler not configured"}

            try:
                update_data = await request.json()
                logger.debug(f"Received Telegram webhook: {update_data}")
                result = await self.telegram_chat.handle_webhook(update_data)
                return result
            except Exception as e:
                logger.error(f"Error in Telegram webhook: {e}", exc_info=True)
                return {"ok": False, "error": str(e)}

        # ── Twilio WhatsApp webhook ────────────────────────────────────
        @app.post("/twilio/whatsapp")
        async def twilio_whatsapp_webhook(request: Request):
            form_data = dict(await request.form())
            base = getattr(self, '_base_url', '')
            url = f"{base}/twilio/whatsapp" if base else str(request.url)
            sig = request.headers.get("X-Twilio-Signature", "")
            if not self._validate_twilio_signature(url, form_data, sig):
                logger.warning(
                    f"Twilio WhatsApp webhook rejected: invalid signature from "
                    f"{request.client.host if request.client else 'unknown'}"
                )
                return FR(status_code=403)

            if not getattr(self, "twilio_whatsapp_chat", None):
                return FR(content="Online", media_type="text/xml")

            twiml = await self.twilio_whatsapp_chat.handle_webhook(form_data)
            return FR(content=twiml, media_type="text/xml")

        # ── iOS Shortcut / direct API ─────────────────────────────────
        @app.post("/nova/chat")
        async def nova_chat(request: Request):
            """Bearer-token-protected endpoint for iOS Shortcuts and direct API access.

            Request:
              Authorization: Bearer <NOVA_API_KEY>
              Content-Type: application/json
              {"message": "what's on my calendar?"}

            Response:
              {"response": "You have 3 meetings today..."}
            """
            api_key = getattr(self, "_nova_api_key", "") or ""
            if not api_key:
                return self.JSONResponse({"error": "API key not configured"}, status_code=503)

            auth = request.headers.get("Authorization", "")
            token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            if not token or not hmac.compare_digest(token, api_key):
                logger.warning(f"Unauthorized /nova/chat attempt from {request.client.host if request.client else 'unknown'}")
                return self.JSONResponse({"error": "Unauthorized"}, status_code=401)

            try:
                body = await request.json()
            except Exception:
                return self.JSONResponse({"error": "Invalid JSON body"}, status_code=400)

            message = (body.get("message") or "").strip()
            if not message:
                return self.JSONResponse({"error": "'message' is required"}, status_code=400)

            if not self._conversation_manager:
                return self.JSONResponse({"error": "Not ready yet"}, status_code=503)

            try:
                # Progress callback — sends friendly Telegram updates while Nova works
                _tg = getattr(self, "_telegram_notifier", None)
                async def _shortcut_progress(update: str):
                    try:
                        await _tg.notify(update, level="info")
                    except Exception:
                        pass

                response = await self._conversation_manager.process_message(
                    message=message,
                    channel="shortcut",
                    user_id="owner",
                    progress_callback=_shortcut_progress if _tg else None,
                    enable_periodic_updates=bool(_tg),
                )
                return self.JSONResponse({"response": response})
            except Exception as e:
                logger.error(f"/nova/chat error: {e}", exc_info=True)
                return self.JSONResponse({"error": "Internal error"}, status_code=500)

        # ── Audio file serving ────────────────────────────────────────
        @app.get("/audio/{filename}")
        async def serve_audio(filename: str):
            from fastapi.responses import FileResponse
            from pathlib import Path

            if not re.match(r'^[a-f0-9]+\.mp3$', filename):
                return self.JSONResponse({"error": "Invalid filename"}, status_code=400)

            filepath = Path("/tmp/nova_audio") / filename
            if not filepath.exists():
                return self.JSONResponse({"error": "Not found"}, status_code=404)

            return FileResponse(filepath, media_type="audio/mpeg")

        # ── Twilio Voice webhooks ─────────────────────────────────────
        @app.post("/twilio/voice")
        async def twilio_voice_webhook(request: Request):
            form_data = dict(await request.form())
            base = getattr(self, '_base_url', '')
            url = f"{base}/twilio/voice" if base else str(request.url)
            sig = request.headers.get("X-Twilio-Signature", "")
            if not self._validate_twilio_signature(url, form_data, sig):
                logger.warning(
                    f"Twilio Voice webhook rejected: invalid signature from "
                    f"{request.client.host if request.client else 'unknown'}"
                )
                return FR(status_code=403)

            if not getattr(self, "twilio_voice_chat", None):
                return FR(content="Online", media_type="text/xml")

            twiml = await self.twilio_voice_chat.handle_incoming_call(form_data)
            return FR(content=twiml, media_type="text/xml")

        @app.post("/twilio/voice/gather")
        async def twilio_voice_gather_webhook(request: Request):
            form_data = dict(await request.form())
            base = getattr(self, '_base_url', '')
            url = f"{base}/twilio/voice/gather" if base else str(request.url)
            sig = request.headers.get("X-Twilio-Signature", "")
            if not self._validate_twilio_signature(url, form_data, sig):
                logger.warning(
                    f"Twilio Voice/gather webhook rejected: invalid signature from "
                    f"{request.client.host if request.client else 'unknown'}"
                )
                return FR(status_code=403)

            if not getattr(self, "twilio_voice_chat", None):
                return FR(content="Online", media_type="text/xml")

            twiml = await self.twilio_voice_chat.handle_gather(form_data)
            return FR(content=twiml, media_type="text/xml")

        # ── LinkedIn OAuth initiation ─────────────────────────────────
        @app.get("/linkedin/auth")
        async def linkedin_oauth_start(request: Request):
            """Redirect to LinkedIn OAuth with a CSRF state token."""
            import os as _os
            from fastapi.responses import HTMLResponse as _HTML, RedirectResponse

            client_id = _os.getenv("LINKEDIN_CLIENT_ID", "")
            base_url = getattr(self, "_base_url", "").rstrip("/")
            redirect_uri = f"{base_url}/linkedin/callback"
            if not client_id:
                return _HTML("<h2>Setup incomplete</h2><p>Set LINKEDIN_CLIENT_ID first.</p>")
            # Generate CSRF state and store with 10-min expiry
            state = secrets.token_urlsafe(32)
            self._oauth_states[state] = datetime.now() + timedelta(minutes=10)
            # Prune expired states
            now = datetime.now()
            self._oauth_states = {k: v for k, v in self._oauth_states.items() if v > now}
            auth_url = (
                "https://www.linkedin.com/oauth/v2/authorization"
                f"?response_type=code&client_id={client_id}"
                f"&redirect_uri={redirect_uri}"
                f"&state={state}"
                "&scope=openid%20profile%20w_member_social"
            )
            return RedirectResponse(auth_url)

        # ── LinkedIn OAuth callback ────────────────────────────────────
        @app.get("/linkedin/callback")
        async def linkedin_oauth_callback(request: Request):
            """Handle LinkedIn OAuth 2.0 callback."""
            import os as _os
            import aiohttp as _aiohttp
            from pathlib import Path as _Path
            from fastapi.responses import HTMLResponse as _HTML

            code = request.query_params.get("code", "")
            error = request.query_params.get(
                "error_description", request.query_params.get("error", "")
            )

            if error:
                return _HTML(f"<h2>LinkedIn auth failed</h2><p>{html.escape(error)}</p>")

            # ── CSRF state validation ──
            state = request.query_params.get("state", "")
            now = datetime.now()
            expected_expiry = self._oauth_states.pop(state, None) if state else None
            if not expected_expiry or expected_expiry < now:
                logger.warning("LinkedIn OAuth callback: invalid or expired state parameter")
                return _HTML("<h2>Invalid or expired OAuth state</h2>"
                             "<p>Please restart the LinkedIn authorization flow.</p>")

            if not code:
                return _HTML("<h2>No authorization code in callback.</h2>")

            client_id = _os.getenv("LINKEDIN_CLIENT_ID", "")
            client_secret = _os.getenv("LINKEDIN_CLIENT_SECRET", "")
            base_url = getattr(self, "_base_url", "").rstrip("/")
            redirect_uri = f"{base_url}/linkedin/callback"

            if not client_id or not client_secret:
                return _HTML(
                    "<h2>Setup incomplete</h2>"
                    "<p>Add LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET to .env, then retry.</p>"
                )

            try:
                async with _aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://www.linkedin.com/oauth/v2/accessToken",
                        data={
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": redirect_uri,
                            "client_id": client_id,
                            "client_secret": client_secret,
                        },
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=_aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        token_data = await resp.json()

                access_token = token_data.get("access_token", "")
                if not access_token:
                    return _HTML(f"<h2>Token exchange failed</h2><pre>{html.escape(str(token_data))}</pre>")

                expires_days = token_data.get("expires_in", 0) // 86400
                env_path = _Path(__file__).parent.parent.parent / ".env"
                person_id = ""
                granted_scope = token_data.get("scope", "")

                if "openid" in granted_scope:
                    try:
                        async with _aiohttp.ClientSession() as session:
                            async with session.get(
                                "https://api.linkedin.com/v2/userinfo",
                                headers={
                                    "Authorization": f"Bearer {access_token}",
                                    "LinkedIn-Version": "202401",
                                },
                                timeout=_aiohttp.ClientTimeout(total=15),
                            ) as resp:
                                person_id = (await resp.json()).get("sub", "")
                    except Exception:
                        pass

                if not person_id:
                    try:
                        async with _aiohttp.ClientSession() as session:
                            async with session.get(
                                "https://api.linkedin.com/v2/me?projection=(id)",
                                headers={
                                    "Authorization": f"Bearer {access_token}",
                                    "X-Restli-Protocol-Version": "2.0.0",
                                },
                                timeout=_aiohttp.ClientTimeout(total=15),
                            ) as resp:
                                me_data = await resp.json()
                        person_id = me_data.get("id", "")
                    except Exception:
                        pass

                if person_id:
                    person_urn = f"urn:li:person:{person_id}"
                    self._update_env_keys(env_path, {
                        "LINKEDIN_ACCESS_TOKEN": access_token,
                        "LINKEDIN_PERSON_URN": person_urn,
                    })
                    logger.info(f"LinkedIn OAuth completed, person URN: {person_urn}")
                    return _HTML(f"""<!DOCTYPE html><html><body style="font-family:sans-serif;max-width:500px;margin:60px auto">
<h2>✅ LinkedIn Connected!</h2>
<p><strong>Person URN:</strong> <code>{person_urn}</code></p>
<p><strong>Token expires in:</strong> {expires_days} days</p>
<hr>
<p>LINKEDIN_ACCESS_TOKEN and LINKEDIN_PERSON_URN saved to <code>.env</code>.</p>
<p><strong>Restart Nova:</strong></p>
<pre>sudo systemctl restart novabot</pre>
</body></html>""")
                else:
                    self._update_env_keys(env_path, {"LINKEDIN_ACCESS_TOKEN": access_token})
                    logger.info("LinkedIn token saved; person URN needs manual setup")
                    return _HTML(f"""<!DOCTYPE html><html><body style="font-family:sans-serif;max-width:540px;margin:60px auto">
<h2>✅ Token saved — one more step</h2>
<p>Your access token was saved. LinkedIn requires a separate profile scope
to fetch your person ID automatically, so you need to set it once manually.</p>
<p><strong>Run this on EC2 to find your person ID:</strong></p>
<pre>curl -s -H "Authorization: Bearer {access_token}" \\
  "https://api.linkedin.com/v2/me?projection=(id)" \\
  | python3 -m json.tool</pre>
<p>Copy the <code>id</code> value, then on EC2:</p>
<pre>echo "LINKEDIN_PERSON_URN=urn:li:person:YOUR_ID" >> /home/ec2-user/novabot/.env
sudo systemctl restart novabot</pre>
<p><small>Token expires in {expires_days} days.</small></p>
</body></html>""")

            except Exception as e:
                logger.error(f"LinkedIn OAuth callback error: {e}", exc_info=True)
                return _HTML(f"<h2>Error during LinkedIn authorization</h2><p>{html.escape(str(e))}</p>")

        # ── Run server ────────────────────────────────────────────────
        config = self.uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        server = self.uvicorn.Server(config)

        logger.info(f"Starting dashboard server on http://{self.host}:{self.port}")
        logger.info(f"Telegram webhook endpoint: http://{self.host}:{self.port}/telegram/webhook")
        logger.info(f"Twilio WhatsApp webhook: http://{self.host}:{self.port}/twilio/whatsapp")
        logger.info(f"Twilio Voice webhook: http://{self.host}:{self.port}/twilio/voice")
        if getattr(self, "_nova_api_key", ""):
            logger.info(f"Nova chat API (iOS Shortcut): http://{self.host}:{self.port}/nova/chat")
        await server.serve()

    # ── HTML pages ─────────────────────────────────────────────────────

    def _get_login_html(self, error: str = "") -> str:
        """Render the login page."""
        bot_name = os.getenv("BOT_NAME", "Nova")
        error_html = (
            f'<div class="error-msg">{error}</div>' if error else ""
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>{bot_name} — Sign In</title>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: #0f0f0f;
      color: #e0e0e0;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .login-card {{
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 12px;
      padding: 40px;
      width: 360px;
    }}
    .logo {{
      text-align: center;
      margin-bottom: 28px;
    }}
    .logo-icon {{ font-size: 2.5em; }}
    .logo-name {{ font-size: 1.4em; font-weight: 700; color: #4ade80; margin-top: 8px; }}
    .logo-sub {{ font-size: 0.85em; color: #666; margin-top: 4px; }}
    label {{
      display: block;
      font-size: 0.85em;
      color: #888;
      margin-bottom: 6px;
      margin-top: 16px;
    }}
    input {{
      width: 100%;
      padding: 10px 14px;
      background: #111;
      border: 1px solid #333;
      border-radius: 8px;
      color: #e0e0e0;
      font-size: 0.95em;
      outline: none;
      transition: border-color 0.2s;
    }}
    input:focus {{ border-color: #4ade80; }}
    .btn {{
      width: 100%;
      padding: 12px;
      background: #4ade80;
      color: #000;
      border: none;
      border-radius: 8px;
      font-size: 1em;
      font-weight: 600;
      cursor: pointer;
      margin-top: 24px;
      transition: background 0.2s;
    }}
    .btn:hover {{ background: #22c55e; }}
    .error-msg {{
      background: #3f1515;
      border: 1px solid #7f2929;
      color: #f87171;
      border-radius: 6px;
      padding: 10px 14px;
      font-size: 0.9em;
      margin-top: 16px;
    }}
  </style>
</head>
<body>
  <div class="login-card">
    <div class="logo">
      <div class="logo-icon">⚡</div>
      <div class="logo-name">{bot_name}</div>
      <div class="logo-sub">Dashboard Access</div>
    </div>
    <form method="POST" action="/login">
      <label for="username">Username</label>
      <input id="username" name="username" type="text" autocomplete="username" required autofocus>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      {error_html}
      <button class="btn" type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""

    def _get_dashboard_html(self) -> str:
        """Render the main dashboard with chat window and stats."""
        bot_name = os.getenv("BOT_NAME", "Nova")
        show_logout = "inline-block" if self._is_auth_required() else "none"
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>{bot_name} Dashboard</title>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'Segoe UI', system-ui, sans-serif;
      background: #0f0f0f;
      color: #e0e0e0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}

    /* ── Top bar ─────────────────────── */
    .topbar {{
      background: #1a1a1a;
      border-bottom: 1px solid #2a2a2a;
      padding: 12px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }}
    .brand {{ display: flex; align-items: center; gap: 10px; }}
    .brand-icon {{ font-size: 1.4em; }}
    .brand-name {{ font-size: 1.1em; font-weight: 700; color: #4ade80; }}
    .uptime-badge {{
      background: #1a3a2a;
      border: 1px solid #2a5a3a;
      color: #4ade80;
      font-size: 0.75em;
      padding: 3px 10px;
      border-radius: 20px;
    }}
    .topbar-right {{ display: flex; align-items: center; gap: 14px; }}
    .status-pill {{
      display: flex; align-items: center; gap: 6px;
      font-size: 0.85em; color: #888;
    }}
    .dot {{
      width: 8px; height: 8px;
      border-radius: 50%;
      background: #4ade80;
      box-shadow: 0 0 6px #4ade80;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.5; }}
    }}
    .logout-btn {{
      color: #888;
      text-decoration: none;
      font-size: 0.85em;
      padding: 6px 12px;
      border: 1px solid #333;
      border-radius: 6px;
      transition: all 0.2s;
      display: {show_logout};
    }}
    .logout-btn:hover {{ color: #e0e0e0; border-color: #555; }}

    /* ── Stats row ───────────────────── */
    .stats-row {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      padding: 14px 20px;
      background: #111;
      border-bottom: 1px solid #2a2a2a;
      flex-shrink: 0;
    }}
    .stat-card {{
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      padding: 12px 16px;
      border-left: 3px solid #4ade80;
    }}
    .stat-val {{
      font-size: 1.6em;
      font-weight: 700;
      color: #e0e0e0;
      line-height: 1;
    }}
    .stat-lbl {{
      font-size: 0.75em;
      color: #666;
      margin-top: 4px;
    }}

    /* ── Main content ────────────────── */
    .main {{
      display: flex;
      flex: 1;
      overflow: hidden;
      gap: 0;
    }}

    /* ── Chat column ─────────────────── */
    .chat-col {{
      flex: 0 0 60%;
      display: flex;
      flex-direction: column;
      border-right: 1px solid #2a2a2a;
      overflow: hidden;
    }}
    .col-header {{
      padding: 12px 16px;
      background: #1a1a1a;
      border-bottom: 1px solid #2a2a2a;
      font-size: 0.9em;
      font-weight: 600;
      color: #ccc;
      flex-shrink: 0;
    }}
    .chat-messages {{
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .chat-msg {{
      display: flex;
      flex-direction: column;
      max-width: 80%;
    }}
    .chat-msg.nova {{ align-self: flex-start; }}
    .chat-msg.owner {{ align-self: flex-end; align-items: flex-end; }}
    .chat-msg.system {{ align-self: center; }}
    .bubble {{
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 0.92em;
      line-height: 1.5;
      word-break: break-word;
    }}
    .nova .bubble {{
      background: #1e2a1e;
      border: 1px solid #2a3a2a;
      color: #d0f0d0;
      border-bottom-left-radius: 4px;
    }}
    .owner .bubble {{
      background: #1e2535;
      border: 1px solid #2a3555;
      color: #d0e0f0;
      border-bottom-right-radius: 4px;
    }}
    .system .bubble {{
      background: #2a2a1e;
      border: 1px solid #3a3a2a;
      color: #aaa;
      font-size: 0.85em;
    }}
    .msg-time {{
      font-size: 0.72em;
      color: #555;
      margin-top: 4px;
      padding: 0 4px;
    }}
    .chat-input-area {{
      display: flex;
      gap: 8px;
      padding: 12px 16px;
      background: #1a1a1a;
      border-top: 1px solid #2a2a2a;
      flex-shrink: 0;
    }}
    .chat-input-area input {{
      flex: 1;
      padding: 10px 14px;
      background: #111;
      border: 1px solid #333;
      border-radius: 8px;
      color: #e0e0e0;
      font-size: 0.92em;
      outline: none;
      transition: border-color 0.2s;
    }}
    .chat-input-area input:focus {{ border-color: #4ade80; }}
    .send-btn {{
      padding: 10px 18px;
      background: #4ade80;
      color: #000;
      border: none;
      border-radius: 8px;
      font-weight: 600;
      cursor: pointer;
      font-size: 0.92em;
      transition: background 0.2s;
    }}
    .send-btn:hover {{ background: #22c55e; }}
    .send-btn:disabled {{ background: #333; color: #666; cursor: not-allowed; }}

    /* ── Logs column ─────────────────── */
    .logs-col {{
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}
    .logs-area {{
      flex: 1;
      overflow-y: auto;
      padding: 12px;
      font-family: 'Courier New', monospace;
      font-size: 0.82em;
    }}
    .log-entry {{
      padding: 4px 6px;
      border-bottom: 1px solid #1a1a1a;
      line-height: 1.4;
    }}
    .log-entry:hover {{ background: #1a1a1a; }}
    .log-time {{ color: #555; }}
    .log-info {{ color: #60a5fa; }}
    .log-warning {{ color: #fbbf24; }}
    .log-error {{ color: #f87171; }}
    .log-success {{ color: #4ade80; }}

    /* ── Scrollbars ──────────────────── */
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: #111; }}
    ::-webkit-scrollbar-thumb {{ background: #333; border-radius: 3px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: #444; }}
  </style>
</head>
<body>

  <!-- Top bar -->
  <div class="topbar">
    <div class="brand">
      <span class="brand-icon">⚡</span>
      <span class="brand-name">{bot_name}</span>
      <span class="uptime-badge" id="uptime-badge">starting…</span>
    </div>
    <div class="topbar-right">
      <div class="status-pill">
        <span class="dot"></span>
        <span>Online</span>
      </div>
      <a href="/logout" class="logout-btn">Sign out</a>
    </div>
  </div>

  <!-- Stats row -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-val" id="stat-contacts">—</div>
      <div class="stat-lbl">Contacts</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" id="stat-tasks">—</div>
      <div class="stat-lbl">Tasks Pending</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" id="stat-messages">—</div>
      <div class="stat-lbl">Messages Today</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" id="stat-uptime">—</div>
      <div class="stat-lbl">Uptime</div>
    </div>
  </div>

  <!-- Main content -->
  <div class="main">

    <!-- Chat column -->
    <div class="chat-col">
      <div class="col-header">💬 Chat with {bot_name}</div>
      <div class="chat-messages" id="chat-messages"></div>
      <div class="chat-input-area">
        <input id="chat-input" type="text"
               placeholder="Message {bot_name}…"
               autocomplete="off" />
        <button class="send-btn" id="send-btn" onclick="sendMessage()">Send</button>
      </div>
    </div>

    <!-- Logs column -->
    <div class="logs-col">
      <div class="col-header">📋 Live Logs</div>
      <div class="logs-area" id="logs-area"></div>
    </div>

  </div>

  <script>
    // ── WebSocket chat ─────────────────────────────────────────────
    const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws = null;

    function connectWS() {{
      ws = new WebSocket(`${{wsProto}}//${{location.host}}/ws/chat`);

      ws.onopen = () => {{
        document.getElementById('send-btn').disabled = false;
      }};

      ws.onmessage = (event) => {{
        const data = JSON.parse(event.data);
        appendMessage(data.sender, data.text, data.timestamp);
      }};

      ws.onclose = () => {{
        document.getElementById('send-btn').disabled = true;
        appendMessage('system', 'Connection lost — reconnecting in 5s…', new Date().toISOString());
        setTimeout(connectWS, 5000);
      }};

      ws.onerror = () => ws.close();
    }}

    connectWS();

    function sendMessage() {{
      const input = document.getElementById('chat-input');
      const msg = input.value.trim();
      if (!msg || !ws || ws.readyState !== WebSocket.OPEN) return;
      appendMessage('owner', msg, new Date().toISOString());
      ws.send(msg);
      input.value = '';
    }}

    document.getElementById('chat-input').addEventListener('keypress', (e) => {{
      if (e.key === 'Enter') sendMessage();
    }});

    function appendMessage(sender, text, timestamp) {{
      const div = document.createElement('div');
      div.className = `chat-msg ${{sender}}`;
      const time = new Date(timestamp).toLocaleTimeString();
      div.innerHTML = `<div class="bubble">${{escHtml(text)}}</div>
                       <div class="msg-time">${{time}}</div>`;
      const container = document.getElementById('chat-messages');
      container.appendChild(div);
      container.scrollTop = container.scrollHeight;
    }}

    function escHtml(text) {{
      return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\\n/g, '<br>');
    }}

    // ── Stats polling ──────────────────────────────────────────────
    async function fetchStats() {{
      try {{
        const res = await fetch('/api/stats');
        if (!res.ok) return;
        const d = await res.json();
        document.getElementById('stat-contacts').textContent  = d.contacts   ?? '—';
        document.getElementById('stat-tasks').textContent     = d.tasks_pending ?? '—';
        document.getElementById('stat-messages').textContent  = d.messages_today ?? '—';

        const h = Math.floor(d.uptime_seconds / 3600);
        const m = Math.floor((d.uptime_seconds % 3600) / 60);
        const uptime = h >= 24
          ? `${{Math.floor(h/24)}}d ${{h%24}}h ${{m}}m`
          : `${{h}}h ${{m}}m`;
        document.getElementById('stat-uptime').textContent = uptime;
        document.getElementById('uptime-badge').textContent = uptime;
      }} catch(e) {{}}
    }}

    // ── Logs polling ───────────────────────────────────────────────
    async function fetchLogs() {{
      try {{
        const res = await fetch('/api/logs');
        if (!res.ok) return;
        const data = await res.json();
        const area = document.getElementById('logs-area');
        const atBottom = area.scrollHeight - area.scrollTop <= area.clientHeight + 40;
        area.innerHTML = data.logs.map(log => {{
          const time = new Date(log.timestamp).toLocaleTimeString();
          const cls = `log-${{log.level}}`;
          return `<div class="log-entry"><span class="log-time">${{time}} </span>`
               + `<span class="${{cls}}">${{escHtml(log.message)}}</span></div>`;
        }}).join('');
        if (atBottom) area.scrollTop = area.scrollHeight;
      }} catch(e) {{}}
    }}

    fetchStats();
    fetchLogs();
    setInterval(fetchStats, 5000);
    setInterval(fetchLogs, 2000);
  </script>
</body>
</html>"""

    @staticmethod
    def _update_env_keys(env_path, updates: dict):
        """Write or update key=value pairs in a .env file in-place."""
        from pathlib import Path as _Path
        env_path = _Path(env_path)
        existing_lines = env_path.read_text().splitlines() if env_path.exists() else []

        key_to_idx = {}
        for i, line in enumerate(existing_lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                key_to_idx[k] = i

        for key, value in updates.items():
            new_line = f"{key}={value}"
            if key in key_to_idx:
                existing_lines[key_to_idx[key]] = new_line
            else:
                existing_lines.append(new_line)

        env_path.write_text("\n".join(existing_lines) + "\n")
        logger.info(f"Updated .env: {list(updates.keys())}")
