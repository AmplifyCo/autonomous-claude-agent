"""Tool registry for managing available tools."""

import logging
import os
from typing import List, Dict, Any, Optional
from .base import BaseTool
from .bash import BashTool
from .file import FileTool
from .web import WebTool
from .browser import BrowserTool
from ..types import ToolResult

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

        # Get safety config
        safety_config = self.config.get('safety', {})

        # Register default tools with configuration
        self.register(BashTool(
            allowed_commands=safety_config.get('allowed_commands', []),
            blocked_commands=safety_config.get('blocked_commands', []),
            allow_sudo=safety_config.get('allow_sudo', False),
            allowed_sudo_commands=safety_config.get('allowed_sudo_commands', [])
        ))
        self.register(FileTool())
        self.register(WebTool())
        self.register(BrowserTool())

        # Register Email tool if credentials provided
        self._register_email_tool()

        # Register Calendar tool if credentials provided
        self._register_calendar_tool()

        # Register X (Twitter) tool if credentials provided
        self._register_x_tool()

        # Register Reminder tool (always available, no credentials needed)
        self._register_reminder_tool()

    def register(self, tool: BaseTool):
        """Register a tool.

        Args:
            tool: Tool instance to register
        """
        self.tools[tool.name] = tool
        logger.info(f"Registered tool: {tool.name}")

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Get tool by name.

        Args:
            name: Tool name

        Returns:
            Tool instance or None if not found
        """
        return self.tools.get(name)

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Get all tool definitions for Claude API.

        Returns:
            List of tool definitions in Anthropic format
        """
        return [tool.to_anthropic_tool() for tool in self.tools.values()]

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
        tool = self.get_tool(tool_name)
        if not tool:
            logger.error(f"Tool not found: {tool_name}")
            return ToolResult(
                success=False,
                error=f"Tool not found: {tool_name}"
            )

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

        try:
            return await tool.execute(**params)
        except Exception as e:
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

    def _register_reminder_tool(self):
        """Register Reminder tool (always available, no credentials needed)."""
        try:
            from .reminder import ReminderTool
            reminder_tool = ReminderTool(data_dir="./data")
            self.register(reminder_tool)
            logger.info("⏰ Reminder tool registered")
        except Exception as e:
            logger.warning(f"Failed to register Reminder tool: {e}")
