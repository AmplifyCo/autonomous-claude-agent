"""Bash command execution tool."""

import asyncio
import logging
import re
from typing import List, Tuple
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
        allowed_sudo_commands: List[str] = None,
        audit_logger=None
    ):
        """Initialize BashTool.

        Args:
            allowed_commands: List of allowed command prefixes (None = all allowed)
            blocked_commands: List of blocked command patterns
            allow_sudo: Whether to allow limited sudo commands
            allowed_sudo_commands: List of allowed sudo command patterns
            audit_logger: Optional audit logger for tracking command execution
        """
        self.allowed_commands = allowed_commands or []
        self.audit_logger = audit_logger
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

    async def execute(self, command=None, timeout: int = 120, **kwargs) -> ToolResult:
        """Execute a bash command.

        Args:
            command: Command to execute (string, or dict with 'command' key)
            timeout: Timeout in seconds

        Returns:
            ToolResult with command output
        """
        # Handle case where command is passed as a dict (Claude API inconsistency)
        if isinstance(command, dict):
            command = command.get("command", str(command))
        if command is None:
            command = kwargs.get("command", "")
        if not isinstance(command, str):
            command = str(command)
        if not command.strip():
            return ToolResult(success=False, error="No command provided")

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

            # LAYER 13: AUDIT LOGGING
            if self.audit_logger:
                self.audit_logger.log_bash_command(
                    command=command,
                    user_id="system",  # Will be set properly when integrated
                    success=success,
                    output=stdout_str,
                    error=stderr_str if stderr_str else None
                )

            return ToolResult(
                success=success,
                output=stdout_str,
                error=stderr_str if stderr_str else None,
                metadata={"return_code": process.returncode}
            )

        except Exception as e:
            logger.error(f"Error executing command: {e}")

            # LAYER 13: AUDIT LOGGING (for exceptions)
            if self.audit_logger:
                self.audit_logger.log_bash_command(
                    command=command,
                    user_id="system",
                    success=False,
                    error=str(e)
                )

            return ToolResult(
                success=False,
                error=f"Exception during execution: {str(e)}"
            )

    def _is_blocked(self, command: str) -> bool:
        """Check if command is blocked using comprehensive security checks.

        Args:
            command: Command to check

        Returns:
            True if blocked, False otherwise
        """
        command_lower = command.lower().strip()

        # 1. Direct blocklist matching
        for blocked in self.blocked_commands:
            if str(blocked).lower() in command_lower:
                logger.warning(f"Blocked by pattern: {blocked}")
                return True

        # 2. Detect command injection attempts
        if self._has_command_injection(command):
            logger.warning("Blocked: Command injection attempt detected")
            return True

        # 3. Check for sensitive file access
        if self._accesses_sensitive_files(command):
            logger.warning("Blocked: Sensitive file access attempt")
            return True

        # 4. Detect resource exhaustion patterns
        if self._has_resource_exhaustion_pattern(command):
            logger.warning("Blocked: Resource exhaustion pattern detected")
            return True

        # 5. Check for network attacks
        if self._has_network_attack_pattern(command):
            logger.warning("Blocked: Network attack pattern detected")
            return True

        return False

    def _has_command_injection(self, command: str) -> bool:
        """Detect command injection bypass attempts.

        Catches: bash -c "dangerous", eval "dangerous", $(dangerous), etc.
        """
        dangerous_patterns = [
            r'bash\s+-c\s+["\'].*rm.*-rf',  # bash -c "rm -rf"
            r'sh\s+-c\s+["\'].*rm.*-rf',    # sh -c "rm -rf"
            r'eval\s+["\'].*rm',             # eval "rm..."
            r'\$\(.*rm.*-rf',                # $(rm -rf ...)
            r'`.*rm.*-rf',                   # `rm -rf ...`
            r'bash\s+-c\s+["\'].*shutdown',  # bash -c "shutdown"
            r'bash\s+-c\s+["\'].*reboot',    # bash -c "reboot"
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return True
        return False

    def _accesses_sensitive_files(self, command: str) -> bool:
        """Check if command attempts to access sensitive files."""
        sensitive_paths = [
            '/etc/shadow',
            '/etc/passwd',  # Reading is ok, but copying/modifying is not
            '~/.ssh/id_rsa',
            '/.ssh/',
            '/root/',
            '/etc/sudoers',
        ]

        # Only block write/copy operations to sensitive files
        write_commands = ['cp', 'mv', 'tee', '>', '>>', 'chmod 777']

        command_lower = command.lower()
        for path in sensitive_paths:
            if path in command_lower:
                # Check if it's a write operation
                for write_cmd in write_commands:
                    if write_cmd in command_lower:
                        return True
        return False

    def _has_resource_exhaustion_pattern(self, command: str) -> bool:
        """Detect potential resource exhaustion attacks."""
        exhaustion_patterns = [
            r'while\s+true',           # while true; do ...; done
            r'for\s+\(\(.*\)\)',       # Infinite for loop
            r':\(\)\{.*:\|:&.*\};:',   # Fork bomb
            r'yes\s+\|',               # yes | command (flood)
            r'cat\s+/dev/zero',        # Memory flood
            r'cat\s+/dev/urandom',     # CPU/memory flood
        ]

        for pattern in exhaustion_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return True
        return False

    def _has_network_attack_pattern(self, command: str) -> bool:
        """Detect potential network attack commands."""
        # Allow normal network tools, but block obvious attacks
        attack_patterns = [
            r'nmap.*-p.*1-65535',      # Port scan all ports
            r'hping3',                  # DDoS tool
            r'tcpdump.*-w',             # Network sniffing to file
            r'nc.*-e\s+/bin/',          # Netcat reverse shell
            r'nc.*-l.*-p',              # Netcat listener (backdoor)
        ]

        for pattern in attack_patterns:
            if re.search(pattern, command, re.IGNORECASE):
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
            if command_lower.startswith(str(allowed).lower()):
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
            if command_lower.startswith(str(allowed_pattern).lower()):
                return True
        return False
