"""Tool registry for managing available tools."""

import logging
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

    def __init__(self, config: Dict[str, Any] = None):
        """Initialize tool registry with default tools.

        Args:
            config: Configuration dictionary for tools
        """
        self.tools: Dict[str, BaseTool] = {}
        self.config = config or {}

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

    async def execute_tool(self, tool_name: str, **params) -> ToolResult:
        """Execute a tool by name.

        Args:
            tool_name: Name of tool to execute
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
