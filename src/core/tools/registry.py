"""Tool registry for managing available tools."""

import asyncio
import logging
import os
import time
from typing import List, Dict, Any, Optional
from .base import BaseTool
from .bash import BashTool
from .file import FileTool
from .web import WebTool
from .browser import BrowserTool
from .search import WebSearchTool
from ..types import ToolResult
from ..nervous_system.policy_gate import PolicyGate

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry of all available tools for the agent."""

    # Tools that require semantic validation (potentially dangerous)
    TOOLS_REQUIRING_VALIDATION = ['bash', 'file_write']

    def __init__(self, config: Dict[str, Any] = None, security_guard=None):
        """Initialize tool registry with default tools.

        Args:
            config: Configuration dictionary for tools
            security_guard: Optional LLMSecurityGuard for semantic validation
        """
        self.tools: Dict[str, BaseTool] = {}
        self.config = config or {}
        self.security_guard = security_guard
        self.semantic_validation_enabled = self.config.get('semantic_validation', {}).get('enabled', False)

        # Tool performance tracking (Self-Learning)
        self._tool_stats: Dict[str, Dict[str, Any]] = {}
        self._consecutive_failure_limit = 5  # auto-disable after N consecutive failures

        # Policy Gate (Nervous System) — risk checks before tool execution
        # require_approval=True blocks IRREVERSIBLE actions (social posts, emails)
        # unless bypass is active (TaskRunner sets bypass for pre-approved tasks)
        self.policy_gate = PolicyGate(require_approval_for_irreversible=True)

        # Hot-reload safety:
        # _plugins_ready: normally set — tool calls proceed immediately.
        #   Reload clears it → new tool calls hold (await) instead of failing.
        #   In-flight calls finish naturally, then reload swaps, then sets it again.
        # _active_executions + _all_idle: lets reload wait for in-flight drain.
        self._plugins_ready = asyncio.Event()
        self._plugins_ready.set()  # normal state: tools can execute
        self._active_executions = 0
        self._all_idle = asyncio.Event()
        self._all_idle.set()  # starts idle

        # Get safety config
        safety_config = self.config.get('safety', {})

        # ── System Tools ─────────────────────────────────────────────────────
        self.register(BashTool(
            allowed_commands=safety_config.get('allowed_commands', []),
            blocked_commands=safety_config.get('blocked_commands', []),
            allow_sudo=safety_config.get('allow_sudo', False),
            allowed_sudo_commands=safety_config.get('allowed_sudo_commands', [])
        ))
        self.register(FileTool())

        # ── Web Tools ─────────────────────────────────────────────────────────
        # Use web_search first → web_fetch for specific URLs → browser for JS pages
        self.register(WebSearchTool())   # General queries (DuckDuckGo, no API key)
        self.register(WebTool())         # Fetch a specific URL
        self.register(BrowserTool())     # JS-heavy pages / screenshots

        # ── Communication Tools (credential-gated) ────────────────────────────
        self._register_email_tool()
        self._register_calendar_tool()
        self._register_x_tool()          # X (Twitter): search + post (renamed x_post → x_tool)
        self._register_linkedin_tool()   # LinkedIn: post text + articles via official API

        # ── Personal Assistant Tools (always available) ───────────────────────
        self._register_reminder_tool()
        self._register_contacts_tool()
        self._register_nova_task_tool()  # Background task queue (self-direction)
        self._register_memory_tool()    # Mid-task memory queries (active reasoning)

        # ── Market Data Tools (always available, no auth) ───────────────────
        self._register_polymarket_tool()

        # ── Skill Acquisition (Phase 4A) ─────────────────────────────────
        self._register_skill_tool()

        # ── Autonomous Discovery (browse → discover → connect) ────────
        self._register_discover_tool()

        # ── Plugin Tools (auto-discovered from plugins/ directory) ─────────
        # Drop a folder into plugins/ with tool.py + manifest.json → auto-loaded.
        # Zero hub edits needed for new tools.
        from .plugins.plugin_loader import PluginLoader
        self._plugin_loader = PluginLoader()
        self._plugin_loader.load_all(self)

        # ── MCP Server Tools (Phase 5A) ──────────────────────────────────
        # Config-driven: define servers in config/mcp_servers.json.
        # Discovery is async — called from main.py after init.
        from .mcp.mcp_server_manager import MCPServerManager
        self._mcp_manager = MCPServerManager()

    def register(self, tool: BaseTool):
        """Register a tool.

        Args:
            tool: Tool instance to register
        """
        self.tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def unregister(self, name: str) -> bool:
        """Unregister a tool by name. Used by hot-reload to swap out plugin tools.

        Args:
            name: Tool name to remove

        Returns:
            True if the tool was found and removed
        """
        if name in self.tools:
            del self.tools[name]
            logger.info(f"Unregistered tool: {name}")
            return True
        return False

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Get tool by name.

        Args:
            name: Tool name

        Returns:
            Tool instance or None if not found
        """
        return self.tools.get(name)

    def get_tool_file_path(self, name: str) -> Optional[str]:
        """Get the file path where a tool is defined.

        Args:
            name: Tool name

        Returns:
            Absolute file path or None if not found
        """
        import inspect
        tool = self.get_tool(name)
        if not tool:
            return None
        try:
            return inspect.getfile(tool.__class__)
        except Exception as e:
            logger.error(f"Failed to get file path for tool {name}: {e}")
            return None

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Get all tool definitions for Claude API.

        Returns:
            List of tool definitions in Anthropic format
        """
        return [tool.to_anthropic_tool() for tool in self.tools.values()]

    def get_scoped_definitions(self, allowed_tools: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Get tool definitions scoped to the allowed set (just-in-time tool access).

        If allowed_tools is None or empty, returns all definitions (backwards-compat).
        Unknown tool names are silently skipped. Only includes the tools specified
        in tool_hints — no longer auto-includes bash/file_operations for containment.

        Args:
            allowed_tools: List of tool names to expose (from subtask.tool_hints)

        Returns:
            Filtered list of tool definitions in Anthropic format
        """
        if not allowed_tools:
            return self.get_tool_definitions()

        # Only expose the tools explicitly requested — no blanket bash/file access
        scope = set(allowed_tools)

        scoped = [
            tool.to_anthropic_tool()
            for name, tool in self.tools.items()
            if name in scope
        ]

        # Log the scoping decision for audit visibility
        exposed = [t["name"] for t in scoped] if scoped else []
        logger.debug(f"Scoped tool access: {exposed}")
        return scoped

    async def execute_tool(
        self,
        tool_name: str,
        user_message: str = "",
        llm_client=None,
        **params
    ) -> ToolResult:
        """Execute a tool by name with optional semantic validation.

        Args:
            tool_name: Name of tool to execute
            user_message: Original user message (for semantic validation)
            llm_client: LLM client for semantic validation
            **params: Tool parameters

        Returns:
            ToolResult from tool execution
        """
        # Hold if a hot-reload is in progress — wait (not fail) until it's done
        await self._plugins_ready.wait()

        # Track active executions so reload can wait for in-flight drain
        self._active_executions += 1
        self._all_idle.clear()
        try:
            return await self._execute_tool_inner(tool_name, user_message, llm_client, **params)
        finally:
            self._active_executions -= 1
            if self._active_executions == 0:
                self._all_idle.set()

    async def _execute_tool_inner(
        self,
        tool_name: str,
        user_message: str = "",
        llm_client=None,
        **params
    ) -> ToolResult:
        """Inner tool execution (separated for reload-lock wrapping)."""
        tool = self.get_tool(tool_name)
        if not tool:
            logger.error(f"Tool not found: {tool_name}")
            return ToolResult(
                success=False,
                error=f"Tool not found: {tool_name}"
            )

        # Check if tool is temporarily disabled due to consecutive failures
        stats = self._tool_stats.get(tool_name, {})
        if stats.get("disabled", False):
            # Re-enable after 5 minutes cooldown
            if time.time() - stats.get("disabled_at", 0) > 300:
                stats["disabled"] = False
                stats["consecutive_failures"] = 0
                logger.info(f"Tool {tool_name} re-enabled after cooldown")
            else:
                return ToolResult(
                    success=False,
                    error=f"Tool {tool_name} is temporarily disabled after {self._consecutive_failure_limit} consecutive failures. Will retry in a few minutes."
                )

        # ========================================================================
        # POLICY GATE: Risk-based permission check (Nervous System)
        # ========================================================================
        operation = params.get("operation")
        allowed, reason = self.policy_gate.check(
            tool_name=tool_name,
            operation=operation,
            params=params
        )
        if not allowed:
            return ToolResult(success=False, error=f"Policy gate blocked: {reason}")

        # ========================================================================
        # LAYER 11: SEMANTIC VALIDATION (for dangerous tools only)
        # ========================================================================
        if (self.semantic_validation_enabled and
            self.security_guard and
            tool_name in self.TOOLS_REQUIRING_VALIDATION and
            user_message and
            llm_client):

            try:
                is_valid, reason = await self.security_guard.validate_tool_use_semantic(
                    message=user_message,
                    tool_name=tool_name,
                    tool_args=params,
                    llm_client=llm_client
                )

                if not is_valid:
                    logger.warning(
                        f"🚨 SEMANTIC VALIDATION FAILED - Tool: {tool_name}, "
                        f"Reason: {reason}"
                    )
                    return ToolResult(
                        success=False,
                        error=f"Security validation failed: {reason}"
                    )
            except Exception as e:
                logger.debug(f"Semantic validation error (allowing): {e}")
                # Fail open - don't block legitimate use if validation fails

        # Tool-specific timeout (bash has its own, others get 60s default)
        tool_timeout = params.pop('_timeout', 60)
        if tool_name == 'bash':
            # BashTool handles its own timeout internally
            tool_timeout = None

        exec_start = time.time()
        try:
            if tool_timeout:
                result = await asyncio.wait_for(
                    tool.execute(**params),
                    timeout=tool_timeout
                )
            else:
                result = await tool.execute(**params)

            self._record_tool_result(tool_name, result.success, time.time() - exec_start)
            return result
        except asyncio.TimeoutError:
            self._record_tool_result(tool_name, False, time.time() - exec_start, "timeout")
            logger.error(f"Tool {tool_name} timed out after {tool_timeout}s")
            return ToolResult(
                success=False,
                error=f"Tool {tool_name} timed out after {tool_timeout} seconds"
            )
        except Exception as e:
            self._record_tool_result(tool_name, False, time.time() - exec_start, str(e))
            logger.error(f"Error executing tool {tool_name}: {e}")
            return ToolResult(
                success=False,
                error=f"Tool execution error: {str(e)}"
            )

    def list_tools(self) -> List[str]:
        """List all registered tool names.

        Returns:
            List of tool names
        """
        return list(self.tools.keys())

    def get_talent_status(self):
        """Print the full talent catalog with active/coming-soon status."""
        try:
            from ..talents.catalog import TalentCatalog
            catalog = TalentCatalog()
            catalog.print_status()
        except Exception as e:
            logger.error(f"Failed to load talent catalog: {e}")

    def _record_tool_result(self, tool_name: str, success: bool, latency: float, error: str = None):
        """Record tool execution result for performance tracking."""
        if tool_name not in self._tool_stats:
            self._tool_stats[tool_name] = {
                "total_calls": 0, "successes": 0, "failures": 0,
                "total_latency": 0.0, "consecutive_failures": 0,
                "last_error": None, "disabled": False, "disabled_at": 0
            }

        stats = self._tool_stats[tool_name]
        stats["total_calls"] += 1
        stats["total_latency"] += latency

        if success:
            stats["successes"] += 1
            stats["consecutive_failures"] = 0
        else:
            stats["failures"] += 1
            stats["consecutive_failures"] += 1
            stats["last_error"] = error

            # Auto-disable after too many consecutive failures
            if stats["consecutive_failures"] >= self._consecutive_failure_limit:
                stats["disabled"] = True
                stats["disabled_at"] = time.time()
                logger.warning(f"Tool {tool_name} auto-disabled after {self._consecutive_failure_limit} consecutive failures")

    def get_tool_stats(self) -> Dict[str, Any]:
        """Get performance stats for all tools (for system prompt injection)."""
        summary = {}
        for name, stats in self._tool_stats.items():
            total = stats["total_calls"]
            if total == 0:
                continue
            summary[name] = {
                "success_rate": round(stats["successes"] / total * 100, 1),
                "avg_latency": round(stats["total_latency"] / total, 2),
                "total_calls": total,
                "disabled": stats["disabled"]
            }
        return summary

    def _register_email_tool(self):
        """Register Email tool if credentials provided in environment."""
        try:
            imap_server = os.getenv('EMAIL_IMAP_SERVER')
            smtp_server = os.getenv('EMAIL_SMTP_SERVER')
            email_address = os.getenv('EMAIL_ADDRESS')
            email_password = os.getenv('EMAIL_PASSWORD')

            if all([imap_server, smtp_server, email_address, email_password]):
                from .email import EmailTool

                imap_port = int(os.getenv('EMAIL_IMAP_PORT', '993'))
                smtp_port = int(os.getenv('EMAIL_SMTP_PORT', '587'))

                email_tool = EmailTool(
                    imap_server=imap_server,
                    smtp_server=smtp_server,
                    email_address=email_address,
                    password=email_password,
                    imap_port=imap_port,
                    smtp_port=smtp_port
                )

                self.register(email_tool)
                logger.info("📧 Email tool registered")
            else:
                logger.debug("Email tool not registered (missing credentials in .env)")

        except Exception as e:
            logger.warning(f"Failed to register Email tool: {e}")

    def _register_calendar_tool(self):
        """Register Calendar tool if credentials provided in environment."""
        try:
            caldav_url = os.getenv('CALDAV_URL')
            caldav_username = os.getenv('CALDAV_USERNAME')
            caldav_password = os.getenv('CALDAV_PASSWORD')

            if all([caldav_url, caldav_username, caldav_password]):
                from .calendar import CalendarTool

                calendar_name = os.getenv('CALDAV_CALENDAR_NAME')

                calendar_tool = CalendarTool(
                    caldav_url=caldav_url,
                    username=caldav_username,
                    password=caldav_password,
                    calendar_name=calendar_name
                )

                self.register(calendar_tool)
                logger.info("📅 Calendar tool registered")
            else:
                logger.debug("Calendar tool not registered (missing credentials in .env)")

        except Exception as e:
            logger.warning(f"Failed to register Calendar tool: {e}")

    def _register_x_tool(self):
        """Register X (Twitter) tool if credentials provided in environment."""
        try:
            api_key = os.getenv('X_API_KEY')
            api_secret = os.getenv('X_API_SECRET')
            access_token = os.getenv('X_ACCESS_TOKEN')
            access_token_secret = os.getenv('X_ACCESS_TOKEN_SECRET')

            if all([api_key, api_secret, access_token, access_token_secret]):
                from .x_tool import XTool

                x_tool = XTool(
                    api_key=api_key,
                    api_secret=api_secret,
                    access_token=access_token,
                    access_token_secret=access_token_secret,
                    data_dir="./data"
                )

                self.register(x_tool)
                logger.info("🐦 X (Twitter) tool registered")
            else:
                missing = [k for k, v in {
                    'X_API_KEY': api_key, 'X_API_SECRET': api_secret,
                    'X_ACCESS_TOKEN': access_token, 'X_ACCESS_TOKEN_SECRET': access_token_secret
                }.items() if not v]
                logger.debug(f"X tool not registered (missing: {', '.join(missing)})")

        except Exception as e:
            logger.warning(f"Failed to register X tool: {e}")

    def _register_linkedin_tool(self):
        """Register LinkedIn tool if OAuth credentials are in environment."""
        try:
            access_token = os.getenv("LINKEDIN_ACCESS_TOKEN")
            person_urn = os.getenv("LINKEDIN_PERSON_URN")

            if access_token and person_urn:
                from .linkedin import LinkedInTool
                linkedin_tool = LinkedInTool(
                    access_token=access_token,
                    person_urn=person_urn,
                )
                self.register(linkedin_tool)
                logger.info("💼 LinkedIn tool registered")
            else:
                missing = [k for k, v in {
                    "LINKEDIN_ACCESS_TOKEN": access_token,
                    "LINKEDIN_PERSON_URN": person_urn,
                }.items() if not v]
                logger.debug(f"LinkedIn tool not registered (missing: {', '.join(missing)}). Run: python scripts/linkedin_auth.py")

        except Exception as e:
            logger.warning(f"Failed to register LinkedIn tool: {e}")

    def _register_reminder_tool(self):
        """Register Reminder tool (always available, no credentials needed)."""
        try:
            from .reminder import ReminderTool
            reminder_tool = ReminderTool(data_dir="./data")
            self.register(reminder_tool)
            logger.info("⏰ Reminder tool registered")
        except Exception as e:
            logger.warning(f"Failed to register Reminder tool: {e}")

    def _register_contacts_tool(self):
        """Register Contacts tool if not already registered (always available)."""
        if "contacts" not in self.tools:
            try:
                from .contacts import ContactsTool
                self.register(ContactsTool(data_dir="./data"))
                logger.info("📇 Contacts tool registered")
            except Exception as e:
                logger.warning(f"Failed to register Contacts tool: {e}")

    def _register_nova_task_tool(self):
        """Register NovaTaskTool for background autonomous task execution."""
        try:
            from .nova_task_tool import NovaTaskTool
            # task_queue is injected later via set_task_queue()
            self._nova_task_tool = NovaTaskTool(task_queue=None)
            self.register(self._nova_task_tool)
            logger.info("🎯 NovaTask tool registered")
        except Exception as e:
            logger.warning(f"Failed to register NovaTask tool: {e}")

    def _register_polymarket_tool(self):
        """Register Polymarket tool (read-only, no auth needed)."""
        try:
            from .polymarket import PolymarketTool
            self.register(PolymarketTool())
            logger.info("📊 Polymarket tool registered")
        except Exception as e:
            logger.warning(f"Failed to register Polymarket tool: {e}")

    def _register_memory_tool(self):
        """Register MemoryQueryTool for mid-task memory access."""
        try:
            from .memory_tool import MemoryQueryTool
            self._memory_tool = MemoryQueryTool()
            self.register(self._memory_tool)
            logger.info("🧠 MemoryQuery tool registered")
        except Exception as e:
            logger.warning(f"Failed to register MemoryQuery tool: {e}")

    def _register_skill_tool(self):
        """Register SkillTool for learning new API integrations (Phase 4A)."""
        try:
            from .skill_tool import SkillTool
            self._skill_tool = SkillTool()
            self._skill_tool._registry = self  # for hot-reload passthrough
            self.register(self._skill_tool)
            logger.info("🎓 SkillTool registered")
        except Exception as e:
            logger.warning(f"Failed to register SkillTool: {e}")

    def set_skill_learner(self, skill_learner):
        """Inject SkillLearner into SkillTool after initialization (Phase 4A)."""
        if hasattr(self, '_skill_tool') and self._skill_tool:
            self._skill_tool.skill_learner = skill_learner
            logger.info("🎓 SkillTool connected to SkillLearner")

    def _register_discover_tool(self):
        """Register DiscoverTool for autonomous API discovery."""
        try:
            from .discover import DiscoverTool
            self._discover_tool = DiscoverTool()
            self.register(self._discover_tool)
            logger.info("🔍 DiscoverTool registered")
        except Exception as e:
            logger.warning(f"Failed to register DiscoverTool: {e}")

    def set_discover_deps(self, llm_client, skill_learner):
        """Wire DiscoverTool with LLM client and SkillLearner."""
        if hasattr(self, '_discover_tool') and self._discover_tool:
            self._discover_tool.llm_client = llm_client
            self._discover_tool.skill_learner = skill_learner
            logger.info("🔍 DiscoverTool connected to LLM + SkillLearner")

    def get_plugin_metadata(self) -> dict:
        """Return metadata for all loaded plugins + MCP tools.

        Used by ConversationManager (dynamic _SAFE_READONLY_TOOLS),
        GoalDecomposer (dynamic _VALID_TOOL_NAMES), and persona detection.
        """
        meta = {}
        if hasattr(self, '_plugin_loader'):
            meta.update(self._plugin_loader.get_plugin_metadata())
        if hasattr(self, '_mcp_manager'):
            meta.update(self._mcp_manager.get_metadata())
        return meta

    async def reload_plugins(self) -> str:
        """Hot-reload all plugins: hold new tasks, drain in-flight, reload.

        Flow:
          1. Clear _plugins_ready → new tool calls hold (await), not fail
          2. Wait for in-flight executions to finish naturally
          3. Swap plugins (unload old, load fresh)
          4. Set _plugins_ready → held calls proceed with new plugins

        Returns:
            Human-readable summary of what happened.
        """
        if not hasattr(self, '_plugin_loader'):
            return "Plugin system not initialized."

        # Step 1: hold new tool calls
        self._plugins_ready.clear()

        try:
            # Step 2: wait for in-flight executions to drain
            if self._active_executions > 0:
                logger.info(f"Reload: waiting for {self._active_executions} active tool execution(s) to finish...")
                await self._all_idle.wait()

            # Step 3: all clear — reload plugins
            new, reloaded, errors = self._plugin_loader.reload_all(self)
        finally:
            # Step 4: release held calls (even if reload errors out)
            self._plugins_ready.set()

        parts = []
        if new:
            parts.append(f"{new} new")
        if reloaded:
            parts.append(f"{reloaded} reloaded")
        if not parts:
            parts.append("no changes")
        summary = f"Plugins: {', '.join(parts)}."
        if errors:
            summary += f" Errors: {'; '.join(errors)}"

        # Also reload MCP servers
        if hasattr(self, '_mcp_manager'):
            mcp_summary = await self.reload_mcp_servers()
            summary += f" {mcp_summary}"

        return summary

    # ── MCP Server Management (Phase 5A) ────────────────────────────────

    async def discover_mcp_servers(self) -> str:
        """Discover and register MCP server tools. Called from main.py after init."""
        if not hasattr(self, '_mcp_manager'):
            return "MCP system not initialized."
        tools_count, errors = await self._mcp_manager.discover_and_register(self)
        parts = [f"{tools_count} MCP tool(s) registered"]
        if errors:
            parts.append(f"errors: {'; '.join(errors)}")
        summary = ". ".join(parts)
        logger.info(f"MCP discovery: {summary}")
        return summary

    async def reload_mcp_servers(self) -> str:
        """Hot-reload MCP servers: re-read config, reconnect, re-register."""
        if not hasattr(self, '_mcp_manager'):
            return "MCP: not initialized."
        new_tools, removed, errors = await self._mcp_manager.reload(self)
        parts = []
        if new_tools:
            parts.append(f"{new_tools} registered")
        if removed:
            parts.append(f"{removed} removed")
        if not parts:
            parts.append("no changes")
        summary = f"MCP: {', '.join(parts)}."
        if errors:
            summary += f" Errors: {'; '.join(errors)}"
        return summary

    async def shutdown_mcp(self):
        """Clean shutdown of all MCP connections."""
        if hasattr(self, '_mcp_manager'):
            await self._mcp_manager.shutdown()

    def set_mcp_credential_resolver(self, resolver):
        """Inject credential resolver into MCP manager (called from main.py)."""
        if hasattr(self, '_mcp_manager'):
            self._mcp_manager._credential_resolver = resolver

    def get_mcp_status(self) -> dict:
        """Get MCP server connection status (for debug/dashboard)."""
        if hasattr(self, '_mcp_manager'):
            return self._mcp_manager.get_status()
        return {}

    def set_task_queue(self, task_queue):
        """Inject task_queue into NovaTaskTool after initialization."""
        tool = self.tools.get("nova_task")
        if tool:
            tool.task_queue = task_queue
            logger.info("🎯 NovaTask tool connected to TaskQueue")

    def set_memory_sources(self, brain=None, episodic_memory=None, llm_client=None):
        """Wire memory sources into MemoryQueryTool (called from main.py after init)."""
        if hasattr(self, '_memory_tool') and self._memory_tool:
            self._memory_tool.brain = brain
            self._memory_tool.episodic_memory = episodic_memory
            if llm_client:
                self._memory_tool._llm_client = llm_client
            logger.info("🧠 MemoryQuery tool connected to Brain + EpisodicMemory")
