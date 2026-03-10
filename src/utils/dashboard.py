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

    def set_tool_registry(self, registry):
        """Wire tool registry for tool stats."""
        self._tool_registry = registry
        logger.info("Tool registry wired to dashboard")

    def set_working_memory(self, wm):
        """Wire working memory for open threads / pending actions."""
        self._working_memory = wm
        logger.info("Working memory wired to dashboard")

    def set_self_healing_monitor(self, monitor):
        """Wire self-healing monitor for health stats."""
        self._self_healing_monitor = monitor
        logger.info("Self-healing monitor wired to dashboard")

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

            # Tool count
            tools_active = 0
            if getattr(self, "_tool_registry", None):
                try:
                    tools_active = len(self._tool_registry.list_tools())
                except Exception:
                    pass

            # Errors detected
            errors_detected = 0
            monitor = getattr(self, "_self_healing_monitor", None)
            if monitor:
                try:
                    errors_detected = getattr(monitor, "total_errors_detected", 0)
                except Exception:
                    pass

            delta = datetime.now() - self._start_time
            return {
                "contacts": contacts,
                "tasks_pending": tasks_pending,
                "tasks_total": tasks_total,
                "messages_today": self._get_messages_today(),
                "uptime_seconds": int(delta.total_seconds()),
                "tools_active": tools_active,
                "errors_detected": errors_detected,
                "nova_status": "online",
            }

        @app.get("/api/status", response_class=JSONResponse)
        async def get_status():
            return self.status

        @app.get("/api/logs", response_class=JSONResponse)
        async def get_logs():
            return {"logs": self.logs[-50:]}

        # ── Mission Control APIs ─────────────────────────────────────

        @app.get("/api/tasks", response_class=JSONResponse)
        async def api_tasks():
            tasks = []
            if self._task_queue:
                try:
                    raw = self._task_queue.get_active_and_recent_tasks(6)
                    for t in raw:
                        tasks.append({
                            "id": t.id, "goal": t.goal, "status": t.status,
                            "channel": t.channel,
                            "created_at": t.created_at,
                            "completed_at": t.completed_at,
                            "subtasks": [
                                {"desc": s.description, "status": s.status}
                                for s in (t.subtasks or [])
                            ],
                        })
                except Exception as e:
                    logger.warning(f"api_tasks error: {e}")
            return {"tasks": tasks}

        @app.get("/api/tools", response_class=JSONResponse)
        async def api_tools():
            stats = {}
            if getattr(self, "_tool_registry", None):
                try:
                    stats = self._tool_registry.get_tool_stats()
                except Exception as e:
                    logger.warning(f"api_tools error: {e}")
            return {"tools": stats}

        @app.get("/api/threads", response_class=JSONResponse)
        async def api_threads():
            threads, actions = [], []
            if getattr(self, "_working_memory", None):
                try:
                    threads = self._working_memory.get_open_threads()
                    actions = self._working_memory.get_pending_actions()
                except Exception as e:
                    logger.warning(f"api_threads error: {e}")
            return {"threads": threads, "actions": actions}

        @app.get("/api/reminders", response_class=JSONResponse)
        async def api_reminders():
            reminders = []
            try:
                from pathlib import Path as _P
                path = _P("data/reminders.json")
                if path.exists():
                    all_r = json.loads(path.read_text())
                    reminders = [r for r in all_r if r.get("status") == "pending"]
                    reminders.sort(key=lambda r: r.get("remind_at", ""))
                    reminders = reminders[:10]
            except Exception as e:
                logger.warning(f"api_reminders error: {e}")
            return {"reminders": reminders}

        @app.get("/api/health-detail", response_class=JSONResponse)
        async def api_health_detail():
            data = {"errors_detected": 0, "fixes_attempted": 0, "auto_fix_enabled": False}
            monitor = getattr(self, "_self_healing_monitor", None)
            if monitor:
                try:
                    status = await monitor.get_status()
                    data.update(status)
                except Exception as e:
                    logger.warning(f"api_health_detail error: {e}")
            return data

        @app.get("/api/wallet", response_class=JSONResponse)
        async def api_wallet():
            wallets = {"base": None, "solana": None}
            registry = getattr(self, "_tool_registry", None)
            if registry:
                try:
                    wallet_tool = registry.get_tool("wallet")
                    if wallet_tool:
                        for chain in ("base", "solana"):
                            try:
                                result = await wallet_tool._balance(chain)
                                if result.success and result.metadata:
                                    wallets[chain] = result.metadata
                                elif not result.success:
                                    logger.warning(f"Wallet {chain} balance failed: {result.error}")
                            except Exception as we:
                                logger.warning(f"Wallet {chain} exception: {we}")
                except Exception as e:
                    logger.warning(f"api_wallet error: {e}")
            return {"wallets": wallets}

        # ── Tool Guides API ────────────────────────────────────────────
        @app.get("/api/guides", response_class=JSONResponse)
        async def api_guides():
            """List all available tool guides with their content."""
            from pathlib import Path as _P
            guides_dir = _P("data/guides")
            guides = []
            if guides_dir.exists():
                for md in sorted(guides_dir.glob("*.md")):
                    try:
                        content = md.read_text(encoding="utf-8")
                        guides.append({"name": md.stem, "content": content})
                    except Exception:
                        guides.append({"name": md.stem, "content": ""})
            return {"guides": guides}

        @app.get("/api/guides/{name}", response_class=JSONResponse)
        async def api_guide_get(name: str):
            """Get a single guide by name."""
            from pathlib import Path as _P
            path = _P(f"data/guides/{name}.md")
            if not path.exists():
                return JSONResponse({"error": "Guide not found"}, status_code=404)
            return {"name": name, "content": path.read_text(encoding="utf-8")}

        @app.put("/api/guides/{name}", response_class=JSONResponse)
        async def api_guide_save(name: str, request: Request):
            """Save/update a guide. Body: {"content": "..."}"""
            from pathlib import Path as _P
            # Sanitize name to prevent path traversal
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
            if not safe_name:
                return JSONResponse({"error": "Invalid guide name"}, status_code=400)
            guides_dir = _P("data/guides")
            guides_dir.mkdir(parents=True, exist_ok=True)
            try:
                body = await request.json()
                content = body.get("content", "")
                path = guides_dir / f"{safe_name}.md"
                path.write_text(content, encoding="utf-8")
                # Reload guides into conversation manager if wired
                if self._conversation_manager and hasattr(self._conversation_manager, '_load_tool_guides'):
                    self._conversation_manager._TOOL_GUIDES = self._conversation_manager._load_tool_guides()
                logger.info(f"Guide saved via dashboard: {safe_name}.md")
                return {"ok": True, "name": safe_name}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/guides/{name}/reset", response_class=JSONResponse)
        async def api_guide_reset(name: str):
            """Reset a guide to its source default."""
            from pathlib import Path as _P
            import shutil
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
            if not safe_name:
                return JSONResponse({"error": "Invalid guide name"}, status_code=400)
            tools_dir = _P("src/core/tools")
            # Try core tool default
            src = tools_dir / f"{safe_name}_guide.md"
            if not src.exists():
                # Try plugin default
                src = tools_dir / "plugins" / safe_name / "guide.md"
            if not src.exists():
                return JSONResponse({"error": "No default found for this guide"}, status_code=404)
            target = _P(f"data/guides/{safe_name}.md")
            shutil.copy2(src, target)
            # Reload
            if self._conversation_manager and hasattr(self._conversation_manager, '_load_tool_guides'):
                self._conversation_manager._TOOL_GUIDES = self._conversation_manager._load_tool_guides()
            logger.info(f"Guide reset to default: {safe_name}.md")
            return {"ok": True, "name": safe_name, "content": target.read_text(encoding="utf-8")}

        # ── Git Update API ─────────────────────────────────────────────
        @app.post("/api/git-pull", response_class=JSONResponse)
        async def api_git_pull():
            """Pull latest code from git. Guides in data/guides/ are preserved."""
            import subprocess
            try:
                result = subprocess.run(
                    ["git", "pull", "--ff-only"],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(_P(".").resolve())
                )
                output = result.stdout.strip() or result.stderr.strip()
                logger.info(f"Git pull via dashboard: {output[:200]}")
                return {"ok": result.returncode == 0, "output": output}
            except subprocess.TimeoutExpired:
                return JSONResponse({"error": "Git pull timed out"}, status_code=504)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── Settings API ───────────────────────────────────────────────
        @app.get("/api/settings", response_class=JSONResponse)
        async def api_settings_get():
            """Get current safe settings + integration status."""
            from ..core.config import load_settings, SAFE_SETTINGS
            settings = load_settings()
            # Build integration status (connected/not) without exposing values
            integrations = {
                "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
                "gemini": bool(os.getenv("GEMINI_API_KEY")),
                "grok": bool(os.getenv("GROK_API_KEY")),
                "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
                "twilio": bool(os.getenv("TWILIO_ACCOUNT_SID")),
                "whatsapp": bool(os.getenv("TWILIO_WHATSAPP_NUMBER") or os.getenv("WHATSAPP_API_TOKEN")),
                "email": bool(os.getenv("EMAIL_APP_PASSWORD")),
                "linkedin": bool(os.getenv("LINKEDIN_ACCESS_TOKEN")),
                "x_twitter": bool(os.getenv("X_API_KEY")),
                "calendar": bool(os.getenv("GOOGLE_CALENDAR_ID")),
            }
            return {"settings": settings, "safe_keys": sorted(SAFE_SETTINGS), "integrations": integrations}

        @app.put("/api/settings", response_class=JSONResponse)
        async def api_settings_save(request: Request):
            """Save safe settings. Body: {"settings": {...}}"""
            from ..core.config import save_settings, SAFE_SETTINGS
            try:
                body = await request.json()
                new_settings = body.get("settings", {})
                # Only allow whitelisted keys
                filtered = {k: v for k, v in new_settings.items() if k in SAFE_SETTINGS}
                save_settings(filtered)
                logger.info(f"Settings saved via dashboard: {list(filtered.keys())}")
                return {"ok": True, "saved": list(filtered.keys())}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

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
        """Render Mission Control dashboard."""
        bot_name = os.getenv("BOT_NAME", "Nova")
        show_logout = "inline-block" if self._is_auth_required() else "none"
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>{bot_name} — Mission Control</title>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0a0a;color:#e0e0e0;min-height:100vh;display:flex;flex-direction:column;overflow-y:auto}}

    /* ── Top bar ─────────── */
    .topbar{{background:#111;border-bottom:1px solid #222;padding:10px 20px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}}
    .brand{{display:flex;align-items:center;gap:10px}}
    .brand-icon{{font-size:1.3em}}
    .brand-name{{font-size:1.1em;font-weight:700;color:#4ade80}}
    .brand-sub{{font-size:0.78em;color:#555;margin-left:4px}}
    .uptime-badge{{background:#0d2818;border:1px solid #1a4a2a;color:#4ade80;font-size:0.72em;padding:2px 10px;border-radius:20px}}
    .topbar-right{{display:flex;align-items:center;gap:14px}}
    .status-pill{{display:flex;align-items:center;gap:6px;font-size:0.82em;color:#888}}
    .dot{{width:7px;height:7px;border-radius:50%;background:#4ade80;box-shadow:0 0 6px #4ade80;animation:pulse 2s infinite}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.4}}}}
    .logout-btn{{color:#666;text-decoration:none;font-size:0.8em;padding:5px 10px;border:1px solid #333;border-radius:6px;display:{show_logout}}}
    .logout-btn:hover{{color:#e0e0e0;border-color:#555}}

    /* ── Stats row ─────────── */
    .stats-row{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;padding:12px 20px;background:#0d0d0d;border-bottom:1px solid #222;flex-shrink:0}}
    .stat-card{{background:#141414;border:1px solid #222;border-radius:8px;padding:10px 14px;border-left:3px solid #4ade80}}
    .stat-card.warn{{border-left-color:#fbbf24}}
    .stat-card.err{{border-left-color:#f87171}}
    .stat-val{{font-size:1.4em;font-weight:700;color:#e0e0e0;line-height:1}}
    .stat-lbl{{font-size:0.7em;color:#555;margin-top:3px}}

    /* ── Grid layout ────── */
    .grid{{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;flex:1;min-height:60vh;overflow:hidden;gap:0}}
    .panel{{display:flex;flex-direction:column;overflow:hidden;border:1px solid #1a1a1a}}
    .panel-hdr{{padding:8px 14px;background:#141414;border-bottom:1px solid #222;font-size:0.82em;font-weight:600;color:#999;flex-shrink:0;display:flex;align-items:center;gap:6px}}
    .panel-body{{flex:1;overflow-y:auto;padding:10px 14px;font-size:0.84em}}

    /* ── Tasks panel ───── */
    .task-item{{padding:8px 10px;border-bottom:1px solid #1a1a1a;display:flex;align-items:flex-start;gap:10px}}
    .task-item:last-child{{border-bottom:none}}
    .badge{{font-size:0.7em;padding:2px 8px;border-radius:10px;font-weight:600;text-transform:uppercase;flex-shrink:0;margin-top:2px}}
    .badge-pending{{background:#2a2a1a;color:#fbbf24;border:1px solid #3a3a2a}}
    .badge-running{{background:#1a2a1a;color:#4ade80;border:1px solid #2a4a2a}}
    .badge-decomposing{{background:#1a2a3a;color:#60a5fa;border:1px solid #2a3a5a}}
    .badge-done{{background:#1a2a1a;color:#22c55e;border:1px solid #2a4a2a}}
    .badge-failed{{background:#2a1a1a;color:#f87171;border:1px solid #3a2a2a}}
    .task-goal{{color:#ccc;line-height:1.4}}
    .task-time{{font-size:0.75em;color:#555;margin-top:2px}}
    .subtask{{font-size:0.8em;color:#777;margin-left:20px;margin-top:2px}}

    /* ── Right panels ───── */
    .wallet-row{{display:flex;gap:12px;margin-bottom:8px}}
    .wallet-card{{flex:1;background:#141414;border:1px solid #222;border-radius:8px;padding:10px 12px}}
    .wallet-chain{{font-size:0.75em;color:#888;font-weight:600;text-transform:uppercase;margin-bottom:4px}}
    .wallet-bal{{font-size:0.92em;color:#e0e0e0}}
    .wallet-usdc{{color:#4ade80;font-weight:600}}

    .health-item{{padding:6px 0;border-bottom:1px solid #1a1a1a;display:flex;justify-content:space-between}}
    .health-item:last-child{{border-bottom:none}}
    .health-label{{color:#888}}
    .health-val{{color:#e0e0e0;font-weight:500}}

    .thread-item{{padding:5px 0;color:#aaa;border-bottom:1px solid #1a1a1a}}
    .reminder-item{{padding:5px 0;color:#aaa;border-bottom:1px solid #1a1a1a}}
    .reminder-time{{color:#fbbf24;font-size:0.85em;margin-right:6px}}

    .tool-table{{width:100%;border-collapse:collapse}}
    .tool-table th{{text-align:left;font-size:0.75em;color:#555;padding:4px 8px;border-bottom:1px solid #222}}
    .tool-table td{{padding:4px 8px;border-bottom:1px solid #1a1a1a;font-size:0.82em}}
    .tool-table tr:hover td{{background:#1a1a1a}}
    .good{{color:#4ade80}} .mid{{color:#fbbf24}} .bad{{color:#f87171}}

    /* ── Chat panel ──────── */
    .chat-messages{{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:8px}}
    .chat-msg{{display:flex;flex-direction:column;max-width:85%}}
    .chat-msg.nova{{align-self:flex-start}}
    .chat-msg.owner{{align-self:flex-end;align-items:flex-end}}
    .chat-msg.system{{align-self:center}}
    .bubble{{padding:8px 12px;border-radius:10px;font-size:0.88em;line-height:1.45;word-break:break-word}}
    .nova .bubble{{background:#1a2a1a;border:1px solid #2a3a2a;color:#d0f0d0;border-bottom-left-radius:3px}}
    .owner .bubble{{background:#1a2535;border:1px solid #2a3555;color:#d0e0f0;border-bottom-right-radius:3px}}
    .system .bubble{{background:#2a2a1a;border:1px solid #3a3a2a;color:#aaa;font-size:0.82em}}
    .msg-time{{font-size:0.68em;color:#444;margin-top:3px;padding:0 4px}}
    .chat-input-area{{display:flex;gap:6px;padding:8px 14px;background:#141414;border-top:1px solid #222;flex-shrink:0}}
    .chat-input-area input{{flex:1;padding:8px 12px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#e0e0e0;font-size:0.88em;outline:none}}
    .chat-input-area input:focus{{border-color:#4ade80}}
    .send-btn{{padding:8px 16px;background:#4ade80;color:#000;border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:0.88em}}
    .send-btn:hover{{background:#22c55e}}
    .send-btn:disabled{{background:#333;color:#666;cursor:not-allowed}}

    /* ── Logs panel ──────── */
    .logs-area{{flex:1;overflow-y:auto;padding:8px 10px;font-family:'Courier New',monospace;font-size:0.78em}}
    .log-entry{{padding:3px 4px;border-bottom:1px solid #141414;line-height:1.35}}
    .log-entry:hover{{background:#141414}}
    .log-time{{color:#444}}
    .log-info{{color:#60a5fa}} .log-warning{{color:#fbbf24}} .log-error{{color:#f87171}} .log-success{{color:#4ade80}}

    .empty{{color:#444;font-style:italic;padding:12px 0}}

    /* ── Scrollbars ──────── */
    ::-webkit-scrollbar{{width:5px;height:5px}}
    ::-webkit-scrollbar-track{{background:#0a0a0a}}
    ::-webkit-scrollbar-thumb{{background:#2a2a2a;border-radius:3px}}
    ::-webkit-scrollbar-thumb:hover{{background:#3a3a3a}}

    .section-title{{font-size:0.75em;color:#555;text-transform:uppercase;letter-spacing:0.5px;margin:10px 0 6px;padding-top:6px;border-top:1px solid #1a1a1a}}
    .section-title:first-child{{border-top:none;margin-top:0;padding-top:0}}

    /* ── Guides panel ───── */
    .guides-bar{{padding:0 20px;background:#0d0d0d;border-top:1px solid #222;flex-shrink:0}}
    .guides-toggle{{display:flex;align-items:center;gap:8px;padding:10px 0;cursor:pointer;user-select:none;color:#888;font-size:0.82em;font-weight:600}}
    .guides-toggle:hover{{color:#ccc}}
    .guides-toggle .arrow{{transition:transform .2s}}
    .guides-toggle .arrow.open{{transform:rotate(90deg)}}
    .guides-content{{display:none;padding-bottom:14px}}
    .guides-content.open{{display:flex;gap:12px;height:340px}}
    .guides-list{{width:200px;flex-shrink:0;overflow-y:auto;border:1px solid #222;border-radius:6px;background:#111}}
    .guide-item{{padding:8px 12px;cursor:pointer;font-size:0.82em;color:#888;border-bottom:1px solid #1a1a1a}}
    .guide-item:hover{{background:#1a1a1a;color:#ccc}}
    .guide-item.active{{background:#1a2a1a;color:#4ade80;border-left:3px solid #4ade80}}
    .guides-editor{{flex:1;display:flex;flex-direction:column;gap:8px}}
    .guides-editor textarea{{flex:1;background:#0a0a0a;border:1px solid #222;border-radius:6px;color:#ccc;font-family:'JetBrains Mono',monospace;font-size:0.8em;padding:10px;resize:none;outline:none}}
    .guides-editor textarea:focus{{border-color:#4ade80}}
    .guides-btns{{display:flex;gap:8px;align-items:center}}
    .guides-btns button{{padding:5px 14px;border-radius:6px;font-size:0.78em;cursor:pointer;border:1px solid #333;background:#1a1a1a;color:#ccc}}
    .guides-btns button:hover{{background:#2a2a2a}}
    .guides-btns .save-btn{{background:#1a3a1a;border-color:#2a5a2a;color:#4ade80}}
    .guides-btns .save-btn:hover{{background:#2a4a2a}}
    .guides-btns .status{{font-size:0.78em;color:#4ade80;opacity:0;transition:opacity .3s}}
    .git-btn{{padding:5px 14px;border-radius:6px;font-size:0.78em;cursor:pointer;border:1px solid #333;background:#1a1a2a;color:#60a5fa}}
    .git-btn:hover{{background:#2a2a3a}}

    /* ── Settings panel ──── */
    .settings-bar{{padding:0 20px;background:#0d0d0d;border-top:1px solid #222;flex-shrink:0}}
    .settings-toggle{{display:flex;align-items:center;gap:8px;padding:10px 0;cursor:pointer;user-select:none;color:#888;font-size:0.82em;font-weight:600}}
    .settings-toggle:hover{{color:#ccc}}
    .settings-toggle .arrow{{transition:transform .2s}}
    .settings-toggle .arrow.open{{transform:rotate(90deg)}}
    .settings-content{{display:none;padding-bottom:14px}}
    .settings-content.open{{display:flex;gap:20px;flex-wrap:wrap}}
    .settings-group{{flex:1;min-width:320px;max-width:500px}}
    .settings-group h3{{font-size:0.78em;color:#555;text-transform:uppercase;letter-spacing:0.5px;margin:0 0 8px;padding-bottom:4px;border-bottom:1px solid #1a1a1a}}
    .setting-row{{display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #111}}
    .setting-label{{flex:0 0 160px;font-size:0.8em;color:#888}}
    .setting-input{{flex:1;background:#0a0a0a;border:1px solid #222;border-radius:4px;color:#ccc;font-size:0.8em;padding:5px 8px;font-family:'JetBrains Mono',monospace;outline:none}}
    .setting-input:focus{{border-color:#4ade80}}
    .setting-input[type="checkbox"]{{flex:none;width:16px;height:16px;accent-color:#4ade80}}
    .setting-select{{flex:1;background:#0a0a0a;border:1px solid #222;border-radius:4px;color:#ccc;font-size:0.8em;padding:5px 8px;outline:none}}
    .setting-select:focus{{border-color:#4ade80}}
    .settings-btns{{display:flex;gap:8px;align-items:center;padding:10px 0;width:100%}}
    .settings-btns .save-btn{{padding:6px 18px;border-radius:6px;font-size:0.8em;cursor:pointer;border:1px solid #2a5a2a;background:#1a3a1a;color:#4ade80;font-weight:600}}
    .settings-btns .save-btn:hover{{background:#2a4a2a}}
    .settings-btns .status{{font-size:0.78em;color:#4ade80;opacity:0;transition:opacity .3s}}
    .integ-grid{{display:flex;flex-wrap:wrap;gap:6px;padding:4px 0}}
    .integ-chip{{font-size:0.72em;padding:3px 10px;border-radius:12px;border:1px solid #222;background:#111}}
    .integ-chip.on{{border-color:#2a5a2a;color:#4ade80}}
    .integ-chip.off{{border-color:#3a1a1a;color:#666}}
  </style>
</head>
<body>

  <!-- Top bar -->
  <div class="topbar">
    <div class="brand">
      <span class="brand-icon">⚡</span>
      <span class="brand-name">{bot_name}</span>
      <span class="brand-sub">Mission Control</span>
      <span class="uptime-badge" id="uptime-badge">starting…</span>
    </div>
    <div class="topbar-right">
      <div class="status-pill"><span class="dot"></span><span>Online</span></div>
      <button class="git-btn" onclick="gitPull()">Update Code</button>
      <a href="/logout" class="logout-btn">Sign out</a>
    </div>
  </div>

  <!-- Stats row -->
  <div class="stats-row">
    <div class="stat-card"><div class="stat-val" id="s-uptime">—</div><div class="stat-lbl">Uptime</div></div>
    <div class="stat-card"><div class="stat-val" id="s-messages">—</div><div class="stat-lbl">Messages Today</div></div>
    <div class="stat-card"><div class="stat-val" id="s-tasks">—</div><div class="stat-lbl">Tasks Pending</div></div>
    <div class="stat-card"><div class="stat-val" id="s-contacts">—</div><div class="stat-lbl">Contacts</div></div>
    <div class="stat-card"><div class="stat-val" id="s-tools">—</div><div class="stat-lbl">Tools Active</div></div>
    <div class="stat-card err"><div class="stat-val" id="s-errors">0</div><div class="stat-lbl">Errors</div></div>
  </div>

  <!-- Main grid: 2x2 -->
  <div class="grid">

    <!-- Top-left: Active Tasks -->
    <div class="panel">
      <div class="panel-hdr">📋 Active Tasks</div>
      <div class="panel-body" id="tasks-panel"><div class="empty">No tasks</div></div>
    </div>

    <!-- Top-right: Wallet + System Health + Tools -->
    <div class="panel">
      <div class="panel-hdr">🎛️ System Overview</div>
      <div class="panel-body" id="overview-panel">
        <!-- Wallet -->
        <div class="section-title">Wallet Balances</div>
        <div class="wallet-row" id="wallet-row">
          <div class="wallet-card"><div class="wallet-chain">Base</div><div class="wallet-bal" id="w-base">—</div></div>
          <div class="wallet-card"><div class="wallet-chain">Solana</div><div class="wallet-bal" id="w-sol">—</div></div>
        </div>

        <!-- Health -->
        <div class="section-title">System Health</div>
        <div id="health-section"><div class="empty">Loading…</div></div>

        <!-- Threads -->
        <div class="section-title">Open Threads</div>
        <div id="threads-section"><div class="empty">None</div></div>

        <!-- Reminders -->
        <div class="section-title">Upcoming Reminders</div>
        <div id="reminders-section"><div class="empty">None</div></div>

        <!-- Tools -->
        <div class="section-title">Tool Performance</div>
        <div id="tools-section"><div class="empty">Loading…</div></div>
      </div>
    </div>

    <!-- Bottom-left: Chat -->
    <div class="panel">
      <div class="panel-hdr">💬 Chat with {bot_name}</div>
      <div class="chat-messages" id="chat-messages"></div>
      <div class="chat-input-area">
        <input id="chat-input" type="text" placeholder="Message {bot_name}…" autocomplete="off"/>
        <button class="send-btn" id="send-btn" onclick="sendMessage()">Send</button>
      </div>
    </div>

    <!-- Bottom-right: Live Logs -->
    <div class="panel">
      <div class="panel-hdr">📜 Live Logs</div>
      <div class="logs-area" id="logs-area"></div>
    </div>

  </div>

  <!-- Guides bar (collapsible) -->
  <div class="guides-bar">
    <div class="guides-toggle" onclick="toggleGuides()">
      <span class="arrow" id="guides-arrow">&#9654;</span>
      <span>Tool Guides</span>
      <span style="color:#555;font-weight:400"> — customize how each tool behaves</span>
    </div>
    <div class="guides-content" id="guides-content">
      <div class="guides-list" id="guides-list"><div class="empty">Loading...</div></div>
      <div class="guides-editor">
        <textarea id="guide-editor" placeholder="Select a guide to edit..."></textarea>
        <div class="guides-btns">
          <button class="save-btn" onclick="saveGuide()">Save</button>
          <button onclick="resetGuide()">Reset to Default</button>
          <span class="status" id="guide-status"></span>
        </div>
      </div>
    </div>
  </div>

  <!-- Settings bar (collapsible) -->
  <div class="settings-bar">
    <div class="settings-toggle" onclick="toggleSettings()">
      <span class="arrow" id="settings-arrow">&#9654;</span>
      <span>Settings</span>
      <span style="color:#555;font-weight:400"> — configure behavior without touching code</span>
    </div>
    <div class="settings-content" id="settings-content">
      <div class="settings-group">
        <h3>Identity & Behavior</h3>
        <div class="setting-row"><span class="setting-label">Bot Name</span><input class="setting-input" id="set-bot_name" type="text"></div>
        <div class="setting-row"><span class="setting-label">Owner Name</span><input class="setting-input" id="set-owner_name" type="text"></div>
        <div class="setting-row"><span class="setting-label">Log Level</span>
          <select class="setting-select" id="set-log_level"><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select>
        </div>
        <div class="setting-row"><span class="setting-label">Timezone</span><input class="setting-input" id="set-user_timezone" type="text" placeholder="US/Pacific"></div>
        <div class="setting-row"><span class="setting-label">Auto Commit</span><input class="setting-input" id="set-auto_commit" type="checkbox"></div>
        <div class="setting-row"><span class="setting-label">Self Build Mode</span><input class="setting-input" id="set-self_build_mode" type="checkbox"></div>
      </div>
      <div class="settings-group">
        <h3>Models & Limits</h3>
        <div class="setting-row"><span class="setting-label">Default Model</span><input class="setting-input" id="set-default_model" type="text"></div>
        <div class="setting-row"><span class="setting-label">Subagent Model</span><input class="setting-input" id="set-subagent_model" type="text"></div>
        <div class="setting-row"><span class="setting-label">Chat Model</span><input class="setting-input" id="set-chat_model" type="text"></div>
        <div class="setting-row"><span class="setting-label">Intent Model</span><input class="setting-input" id="set-intent_model" type="text"></div>
        <div class="setting-row"><span class="setting-label">Max Iterations</span><input class="setting-input" id="set-max_iterations" type="number" min="1" max="200"></div>
        <div class="setting-row"><span class="setting-label">Timeout (seconds)</span><input class="setting-input" id="set-timeout_seconds" type="number" min="30" max="3600"></div>
        <div class="setting-row"><span class="setting-label">Dashboard Port</span><input class="setting-input" id="set-dashboard_port" type="number" min="1024" max="65535"></div>
      </div>
      <div class="settings-group">
        <h3>Integrations</h3>
        <p style="font-size:0.75em;color:#555;margin:0 0 8px">API keys are set via .env file (SSH only). Status shown below.</p>
        <div class="integ-grid" id="integ-grid"><div class="empty">Loading...</div></div>
      </div>
      <div class="settings-btns">
        <button class="save-btn" onclick="saveSettings()">Save Settings</button>
        <span class="status" id="settings-status"></span>
        <span style="font-size:0.72em;color:#555;margin-left:8px">Changes take effect on next restart (or immediately for some fields)</span>
      </div>
    </div>
  </div>

  <script>
    /* ── Helpers ─────────────────────── */
    function escHtml(t){{return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>')}}
    function fmtUptime(s){{const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h>=24?`${{Math.floor(h/24)}}d ${{h%24}}h ${{m}}m`:`${{h}}h ${{m}}m`}}

    /* ── WebSocket chat ─────────────── */
    const wsP=location.protocol==='https:'?'wss:':'ws:';
    let ws=null;
    function connectWS(){{
      ws=new WebSocket(`${{wsP}}//${{location.host}}/ws/chat`);
      ws.onopen=()=>{{document.getElementById('send-btn').disabled=false}};
      ws.onmessage=(e)=>{{const d=JSON.parse(e.data);appendMsg(d.sender,d.text,d.timestamp)}};
      ws.onclose=()=>{{document.getElementById('send-btn').disabled=true;appendMsg('system','Reconnecting…',new Date().toISOString());setTimeout(connectWS,5000)}};
      ws.onerror=()=>ws.close();
    }}
    connectWS();
    function sendMessage(){{
      const i=document.getElementById('chat-input'),m=i.value.trim();
      if(!m||!ws||ws.readyState!==1)return;
      appendMsg('owner',m,new Date().toISOString());
      ws.send(m);i.value='';
    }}
    document.getElementById('chat-input').addEventListener('keypress',e=>{{if(e.key==='Enter')sendMessage()}});
    function appendMsg(s,t,ts){{
      const d=document.createElement('div');d.className=`chat-msg ${{s}}`;
      const tm=new Date(ts).toLocaleTimeString();
      d.innerHTML=`<div class="bubble">${{escHtml(t)}}</div><div class="msg-time">${{tm}}</div>`;
      const c=document.getElementById('chat-messages');c.appendChild(d);c.scrollTop=c.scrollHeight;
    }}

    /* ── Stats polling ──────────────── */
    async function fetchStats(){{
      try{{
        const r=await fetch('/api/stats');if(!r.ok)return;const d=await r.json();
        const up=fmtUptime(d.uptime_seconds);
        document.getElementById('s-uptime').textContent=up;
        document.getElementById('uptime-badge').textContent=up;
        document.getElementById('s-messages').textContent=d.messages_today??'—';
        document.getElementById('s-tasks').textContent=d.tasks_pending??'—';
        document.getElementById('s-contacts').textContent=d.contacts??'—';
        document.getElementById('s-tools').textContent=d.tools_active??'—';
        document.getElementById('s-errors').textContent=d.errors_detected??0;
      }}catch(e){{}}
    }}

    /* ── Tasks polling ──────────────── */
    async function fetchTasks(){{
      try{{
        const r=await fetch('/api/tasks');if(!r.ok)return;const d=await r.json();
        const p=document.getElementById('tasks-panel');
        if(!d.tasks||!d.tasks.length){{p.innerHTML='<div class="empty">No active tasks</div>';return}}
        p.innerHTML=d.tasks.map(t=>{{
          const sub=t.subtasks&&t.subtasks.length?t.subtasks.map(s=>`<div class="subtask">↳ ${{escHtml(s.desc.substring(0,60))}} <span class="badge badge-${{s.status}}">${{s.status}}</span></div>`).join(''):'';
          const time=t.created_at?new Date(t.created_at).toLocaleTimeString():'';
          return `<div class="task-item"><span class="badge badge-${{t.status}}">${{t.status}}</span><div><div class="task-goal">${{escHtml(t.goal.substring(0,120))}}</div><div class="task-time">${{time}} · ${{t.channel||'—'}}</div>${{sub}}</div></div>`;
        }}).join('');
      }}catch(e){{}}
    }}

    /* ── Wallet polling ─────────────── */
    async function fetchWallet(){{
      try{{
        const r=await fetch('/api/wallet');if(!r.ok)return;const d=await r.json();
        const w=d.wallets||{{}};
        if(w.base){{document.getElementById('w-base').innerHTML=`${{w.base.eth||0}} ETH<br><span class="wallet-usdc">${{w.base.usdc||0}} USDC</span>`}}
        else{{document.getElementById('w-base').innerHTML='<span style="color:#555">Not configured</span>'}}
        if(w.solana){{document.getElementById('w-sol').innerHTML=`${{w.solana.sol||0}} SOL<br><span class="wallet-usdc">${{w.solana.usdc||0}} USDC</span>`}}
        else{{document.getElementById('w-sol').innerHTML='<span style="color:#555">Not configured</span>'}}
      }}catch(e){{}}
    }}

    /* ── Health + Threads + Reminders ── */
    async function fetchOverview(){{
      // Health
      try{{
        const r=await fetch('/api/health-detail');if(r.ok){{
          const d=await r.json();
          document.getElementById('health-section').innerHTML=
            `<div class="health-item"><span class="health-label">Auto-fix</span><span class="health-val">${{d.auto_fix_enabled?'Enabled':'Disabled'}}</span></div>`
            +`<div class="health-item"><span class="health-label">Errors detected</span><span class="health-val">${{d.total_errors_detected||0}}</span></div>`
            +`<div class="health-item"><span class="health-label">Fixes applied</span><span class="health-val">${{d.total_fixes_attempted||0}}</span></div>`;
        }}
      }}catch(e){{}}

      // Threads + Actions
      try{{
        const r=await fetch('/api/threads');if(r.ok){{
          const d=await r.json();
          const sec=document.getElementById('threads-section');
          const items=[...(d.threads||[]).map(t=>`<div class="thread-item">🧵 ${{escHtml(t.topic||'—')}} <span style="color:#555;font-size:0.85em">(${{t.status||'open'}})</span></div>`),...(d.actions||[]).map(a=>`<div class="thread-item">⏳ ${{escHtml(a.label||'Pending action')}}</div>`)];
          sec.innerHTML=items.length?items.join(''):'<div class="empty">None</div>';
        }}
      }}catch(e){{}}

      // Reminders
      try{{
        const r=await fetch('/api/reminders');if(r.ok){{
          const d=await r.json();
          const sec=document.getElementById('reminders-section');
          if(!d.reminders||!d.reminders.length){{sec.innerHTML='<div class="empty">None</div>';return}}
          sec.innerHTML=d.reminders.map(r=>{{
            const t=r.remind_at?new Date(r.remind_at).toLocaleString([], {{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}}):'—';
            const icon=r.action_goal?'🚀':'🔔';
            return `<div class="reminder-item">${{icon}} <span class="reminder-time">${{t}}</span>${{escHtml((r.message||'').substring(0,60))}}</div>`;
          }}).join('');
        }}
      }}catch(e){{}}
    }}

    /* ── Tools polling ──────────────── */
    async function fetchTools(){{
      try{{
        const r=await fetch('/api/tools');if(!r.ok)return;const d=await r.json();
        const tools=d.tools||{{}};
        const entries=Object.entries(tools).filter(([k,v])=>v.total_calls>0).sort((a,b)=>b[1].total_calls-a[1].total_calls);
        const sec=document.getElementById('tools-section');
        if(!entries.length){{sec.innerHTML='<div class="empty">No tool calls yet</div>';return}}
        let html='<table class="tool-table"><tr><th>Tool</th><th>Calls</th><th>Success</th><th>Latency</th></tr>';
        entries.forEach(([name,s])=>{{
          const rate=s.success_rate??0;
          const cls=rate>=90?'good':rate>=70?'mid':'bad';
          const lat=s.avg_latency?(s.avg_latency.toFixed(1)+'s'):'—';
          html+=`<tr><td>${{name}}</td><td>${{s.total_calls}}</td><td class="${{cls}}">${{rate.toFixed(0)}}%</td><td>${{lat}}</td></tr>`;
        }});
        html+='</table>';
        sec.innerHTML=html;
      }}catch(e){{}}
    }}

    /* ── Logs polling ──────────────── */
    async function fetchLogs(){{
      try{{
        const r=await fetch('/api/logs');if(!r.ok)return;const d=await r.json();
        const a=document.getElementById('logs-area');
        const atB=a.scrollHeight-a.scrollTop<=a.clientHeight+40;
        a.innerHTML=d.logs.map(l=>{{
          const t=new Date(l.timestamp).toLocaleTimeString();
          return `<div class="log-entry"><span class="log-time">${{t}} </span><span class="log-${{l.level}}">${{escHtml(l.message)}}</span></div>`;
        }}).join('');
        if(atB)a.scrollTop=a.scrollHeight;
      }}catch(e){{}}
    }}

    /* ── Guides ─────────────────────── */
    let _activeGuide=null;
    function toggleGuides(){{
      const c=document.getElementById('guides-content'),a=document.getElementById('guides-arrow');
      c.classList.toggle('open');a.classList.toggle('open');
      if(c.classList.contains('open')&&!_activeGuide)fetchGuides();
    }}
    async function fetchGuides(){{
      try{{
        const r=await fetch('/api/guides');const d=await r.json();
        const list=document.getElementById('guides-list');
        if(!d.guides||!d.guides.length){{list.innerHTML='<div class="empty">No guides found</div>';return}}
        list.innerHTML=d.guides.map(g=>`<div class="guide-item" onclick="selectGuide('${{g.name}}')">${{g.name}}</div>`).join('');
      }}catch(e){{console.error('fetchGuides',e)}}
    }}
    async function selectGuide(name){{
      _activeGuide=name;
      document.querySelectorAll('.guide-item').forEach(el=>el.classList.toggle('active',el.textContent===name));
      try{{
        const r=await fetch(`/api/guides/${{name}}`);const d=await r.json();
        document.getElementById('guide-editor').value=d.content||'';
      }}catch(e){{console.error('selectGuide',e)}}
    }}
    async function saveGuide(){{
      if(!_activeGuide)return;
      const content=document.getElementById('guide-editor').value;
      try{{
        const r=await fetch(`/api/guides/${{_activeGuide}}`,{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{content}})}});
        const d=await r.json();
        const s=document.getElementById('guide-status');
        s.textContent=d.ok?'Saved!':'Error';s.style.opacity=1;
        setTimeout(()=>s.style.opacity=0,2000);
      }}catch(e){{console.error('saveGuide',e)}}
    }}
    async function resetGuide(){{
      if(!_activeGuide||!confirm(`Reset "${{_activeGuide}}" guide to default?`))return;
      try{{
        const r=await fetch(`/api/guides/${{_activeGuide}}/reset`,{{method:'POST'}});const d=await r.json();
        if(d.content)document.getElementById('guide-editor').value=d.content;
        const s=document.getElementById('guide-status');
        s.textContent=d.ok?'Reset!':'No default found';s.style.opacity=1;
        setTimeout(()=>s.style.opacity=0,2000);
      }}catch(e){{console.error('resetGuide',e)}}
    }}
    async function gitPull(){{
      if(!confirm('Pull latest code from git? Your guide customizations will be preserved.'))return;
      try{{
        const r=await fetch('/api/git-pull',{{method:'POST'}});const d=await r.json();
        alert(d.ok?`Update complete:\\n${{d.output}}`:`Update failed:\\n${{d.error||d.output}}`);
      }}catch(e){{alert('Git pull failed: '+e.message)}}
    }}

    /* ── Settings ───────────────────── */
    const _checkboxSettings=new Set(['auto_commit','self_build_mode']);
    const _intSettings=new Set(['max_iterations','timeout_seconds','dashboard_port']);
    function toggleSettings(){{
      const c=document.getElementById('settings-content'),a=document.getElementById('settings-arrow');
      c.classList.toggle('open');a.classList.toggle('open');
      if(c.classList.contains('open'))fetchSettings();
    }}
    async function fetchSettings(){{
      try{{
        const r=await fetch('/api/settings');const d=await r.json();
        const s=d.settings||{{}};
        // Populate fields
        for(const[k,v] of Object.entries(s)){{
          const el=document.getElementById('set-'+k);
          if(!el)continue;
          if(_checkboxSettings.has(k))el.checked=!!v;
          else el.value=v;
        }}
        // Integrations
        const ig=document.getElementById('integ-grid');
        const integ=d.integrations||{{}};
        ig.innerHTML=Object.entries(integ).map(([name,on])=>
          `<span class="integ-chip ${{on?'on':'off'}}">${{on?'&#9679;':'&#9675;'}} ${{name.replace('_',' ')}}</span>`
        ).join('');
      }}catch(e){{console.error('fetchSettings',e)}}
    }}
    async function saveSettings(){{
      const settings={{}};
      document.querySelectorAll('[id^="set-"]').forEach(el=>{{
        const key=el.id.replace('set-','');
        if(_checkboxSettings.has(key))settings[key]=el.checked;
        else if(_intSettings.has(key))settings[key]=parseInt(el.value)||0;
        else settings[key]=el.value;
      }});
      try{{
        const r=await fetch('/api/settings',{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{settings}})}});
        const d=await r.json();
        const st=document.getElementById('settings-status');
        st.textContent=d.ok?'Saved!':'Error: '+(d.error||'unknown');st.style.opacity=1;
        setTimeout(()=>st.style.opacity=0,3000);
      }}catch(e){{console.error('saveSettings',e)}}
    }}

    /* ── Init + intervals ──────────── */
    fetchStats();fetchTasks();fetchWallet();fetchOverview();fetchTools();fetchLogs();
    setInterval(fetchStats,5000);
    setInterval(fetchTasks,10000);
    setInterval(fetchWallet,30000);
    setInterval(fetchOverview,10000);
    setInterval(fetchTools,15000);
    setInterval(fetchLogs,3000);
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
