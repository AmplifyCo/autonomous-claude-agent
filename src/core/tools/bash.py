"""Bash command execution tool."""

import asyncio
import logging
from typing import List
from .base import BaseTool
from ..types import ToolResult

logger = logging.getLogger(__name__)


class BashTool(BaseTool):
    """Tool for executing bash commands in a sandboxed environment."""

    name = "bash"
    description = "Execute bash commands safely. Returns stdout, stderr, and return code."
    parameters = {
        "command": {
            "type": "string",
            "description": "The bash command to execute"
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default: 120)",
            "default": 120
        }
    }

    def __init__(
        self,
        allowed_commands: List[str] = None,
        blocked_commands: List[str] = None,
        allow_sudo: bool = False,
        allowed_sudo_commands: List[str] = None
    ):
        """Initialize BashTool.

        Args:
            allowed_commands: List of allowed command prefixes (None = all allowed)
            blocked_commands: List of blocked command patterns
            allow_sudo: Whether to allow limited sudo commands
            allowed_sudo_commands: List of allowed sudo command patterns
        """
        self.allowed_commands = allowed_commands or []
        self.blocked_commands = blocked_commands or [
            "rm -rf /",
            "sudo rm",
            "sudo shutdown",
            "sudo reboot",
            "sudo poweroff",
            "format",
            "mkfs",
            "dd if=",
            "sudo dd",
        ]
        self.allow_sudo = allow_sudo
        self.allowed_sudo_commands = allowed_sudo_commands or []

    async def execute(self, command: str, timeout: int = 120) -> ToolResult:
        """Execute a bash command.

        Args:
            command: Command to execute
            timeout: Timeout in seconds

        Returns:
            ToolResult with command output
        """
        # Security check - blocked commands first
        if self._is_blocked(command):
            logger.warning(f"Blocked dangerous command: {command}")
            return ToolResult(
                success=False,
                error=f"Command blocked for safety: {command}"
            )

        # Check if it's a sudo command
        if command.strip().lower().startswith('sudo '):
            if not self.allow_sudo:
                logger.warning(f"Sudo not allowed: {command}")
                return ToolResult(
                    success=False,
                    error="Sudo commands are not allowed. Configure allow_sudo=true to enable."
                )

            # Check if sudo command is in allowed list
            if not self._is_sudo_allowed(command):
                logger.warning(f"Sudo command not in allowed list: {command}")
                return ToolResult(
                    success=False,
                    error=f"This sudo command is not allowed. Allowed patterns: {', '.join(self.allowed_sudo_commands)}"
                )

        # Check allowed commands (non-sudo)
        elif self.allowed_commands and not self._is_allowed(command):
            logger.warning(f"Command not in allowed list: {command}")
            return ToolResult(
                success=False,
                error=f"Command not allowed: {command}"
            )

        try:
            logger.info(f"Executing bash command: {command}")

            # Create subprocess
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Wait for command to complete with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ToolResult(
                    success=False,
                    error=f"Command timed out after {timeout} seconds"
                )

            # Decode output
            stdout_str = stdout.decode('utf-8', errors='replace') if stdout else ""
            stderr_str = stderr.decode('utf-8', errors='replace') if stderr else ""

            success = process.returncode == 0

            if success:
                logger.info(f"Command executed successfully")
            else:
                logger.warning(f"Command failed with return code {process.returncode}")

            return ToolResult(
                success=success,
                output=stdout_str,
                error=stderr_str if stderr_str else None,
                metadata={"return_code": process.returncode}
            )

        except Exception as e:
            logger.error(f"Error executing command: {e}")
            return ToolResult(
                success=False,
                error=f"Exception during execution: {str(e)}"
            )

    def _is_blocked(self, command: str) -> bool:
        """Check if command is blocked.

        Args:
            command: Command to check

        Returns:
            True if blocked, False otherwise
        """
        command_lower = command.lower().strip()
        for blocked in self.blocked_commands:
            if blocked.lower() in command_lower:
                return True
        return False

    def _is_allowed(self, command: str) -> bool:
        """Check if command is in allowed list.

        Args:
            command: Command to check

        Returns:
            True if allowed, False otherwise
        """
        if not self.allowed_commands:
            return True  # No restrictions if list is empty

        command_lower = command.lower().strip()
        for allowed in self.allowed_commands:
            if command_lower.startswith(allowed.lower()):
                return True
        return False

    def _is_sudo_allowed(self, command: str) -> bool:
        """Check if sudo command is in allowed sudo list.

        Args:
            command: Sudo command to check

        Returns:
            True if allowed, False otherwise
        """
        if not self.allowed_sudo_commands:
            return False  # No sudo commands allowed if list is empty

        command_lower = command.lower().strip()
        for allowed_pattern in self.allowed_sudo_commands:
            if command_lower.startswith(allowed_pattern.lower()):
                return True
        return False
