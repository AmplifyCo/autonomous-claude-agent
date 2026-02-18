"""File operations tool."""

import aiofiles
import logging
import os
import yaml
from pathlib import Path
from typing import Optional, List
from .base import BaseTool
from ..types import ToolResult

logger = logging.getLogger(__name__)


class FileTool(BaseTool):
    """Tool for reading, writing, and editing files."""

    name = "file_operations"
    description = "Read, write, create, and edit files. Supports text files."
    parameters = {
        "operation": {
            "type": "string",
            "description": "Operation: 'read', 'write', 'edit', 'create_dir', 'list_dir'",
            "enum": ["read", "write", "edit", "create_dir", "list_dir"]
        },
        "path": {
            "type": "string",
            "description": "File or directory path"
        },
        "content": {
            "type": "string",
            "description": "Content for write/edit operations (optional)"
        }
    }

    # Layer 14: Self-Protection - Hardcoded critical files
    # These are ALWAYS protected, even if config fails to load
    CRITICAL_PROTECTED_FILES = [
        "config/security.yaml",  # This config itself (prevent circular dependency)
        ".env",  # Environment variables with secrets
    ]

    def __init__(self, max_file_size_mb: int = 10):
        """Initialize FileTool.

        Args:
            max_file_size_mb: Maximum file size to read/write in MB
        """
        self.max_file_size = max_file_size_mb * 1024 * 1024
        self.protected_files: List[str] = []
        self.protected_directories: List[str] = []

        # Load protected files from config (Layer 14: Self-Protection)
        self._load_protected_files()

    def _load_protected_files(self):
        """Load protected files list from security config (Layer 14: Self-Protection)."""
        try:
            security_config_path = Path("config/security.yaml")

            if security_config_path.exists():
                with open(security_config_path, 'r') as f:
                    security_config = yaml.safe_load(f) or {}

                self.protected_files = security_config.get("protected_files", [])
                self.protected_directories = security_config.get("protected_directories", [])

                logger.info(
                    f"🔒 Layer 14: Self-Protection enabled - "
                    f"{len(self.protected_files)} files, "
                    f"{len(self.protected_directories)} directories protected"
                )
            else:
                logger.warning("config/security.yaml not found - using minimal protection")
                self.protected_files = []
                self.protected_directories = []

        except Exception as e:
            logger.error(f"Failed to load security config: {e} - using minimal protection")
            self.protected_files = []
            self.protected_directories = []

    def _is_protected_file(self, path: str) -> bool:
        """Check if file is protected from modifications (Layer 14: Self-Protection).

        Args:
            path: File path to check

        Returns:
            True if file is protected
        """
        try:
            path_normalized = str(Path(path).resolve())
        except Exception:
            # If path resolution fails, treat as potentially dangerous
            path_normalized = path

        # Check hardcoded critical files (always protected)
        for critical in self.CRITICAL_PROTECTED_FILES:
            if critical in path or critical in path_normalized:
                return True

        # Check config-loaded protected files
        for protected in self.protected_files:
            try:
                protected_normalized = str(Path(protected).resolve())
                if protected_normalized in path_normalized or path_normalized.endswith(protected):
                    return True
            except Exception:
                # If resolution fails, do string matching
                if protected in path or protected in path_normalized:
                    return True

        # Check protected directories
        for protected_dir in self.protected_directories:
            if protected_dir in path or protected_dir in path_normalized:
                return True

        return False

    async def execute(
        self,
        operation: str,
        path: str,
        content: Optional[str] = None
    ) -> ToolResult:
        """Execute file operation.

        Args:
            operation: Operation to perform
            path: File/directory path
            content: Content for write/edit

        Returns:
            ToolResult with operation result
        """
        try:
            # Layer 14: Self-Protection - Block writes to protected files
            if operation in ["write", "edit"] and self._is_protected_file(path):
                logger.warning(f"🚨 BLOCKED: Attempt to modify protected file: {path}")
                return ToolResult(
                    success=False,
                    error=f"⛔ Security: Cannot modify protected file: {path}\n"
                          f"Protected files: security config, audit logs, .env\n"
                          f"This is Layer 14: Self-Protection - prevents tampering with security."
                )

            if operation == "read":
                return await self._read_file(path)
            elif operation == "write":
                return await self._write_file(path, content or "")
            elif operation == "edit":
                return await self._edit_file(path, content or "")
            elif operation == "create_dir":
                return await self._create_dir(path)
            elif operation == "list_dir":
                return await self._list_dir(path)
            else:
                return ToolResult(
                    success=False,
                    error=f"Unknown operation: {operation}"
                )

        except Exception as e:
            logger.error(f"File operation error: {e}")
            return ToolResult(
                success=False,
                error=f"File operation failed: {str(e)}"
            )

    async def _read_file(self, path: str) -> ToolResult:
        """Read file contents.

        Args:
            path: File path

        Returns:
            ToolResult with file contents
        """
        try:
            # Check file exists
            if not os.path.exists(path):
                return ToolResult(
                    success=False,
                    error=f"File does not exist: {path}"
                )

            # Check file size
            file_size = os.path.getsize(path)
            if file_size > self.max_file_size:
                return ToolResult(
                    success=False,
                    error=f"File too large ({file_size} bytes). Max: {self.max_file_size}"
                )

            # Read file
            async with aiofiles.open(path, 'r', encoding='utf-8') as f:
                content = await f.read()

            logger.info(f"Read file: {path} ({len(content)} chars)")
            return ToolResult(
                success=True,
                output=content,
                metadata={"path": path, "size": len(content)}
            )

        except UnicodeDecodeError:
            return ToolResult(
                success=False,
                error=f"File is not a text file or has encoding issues: {path}"
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Error reading file: {str(e)}"
            )

    async def _write_file(self, path: str, content: str) -> ToolResult:
        """Write content to file (creates or overwrites).

        Args:
            path: File path
            content: Content to write

        Returns:
            ToolResult with write status
        """
        try:
            # Create parent directory if needed
            parent_dir = Path(path).parent
            if not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)

            # Write file
            async with aiofiles.open(path, 'w', encoding='utf-8') as f:
                await f.write(content)

            logger.info(f"Wrote file: {path} ({len(content)} chars)")
            return ToolResult(
                success=True,
                output=f"File written successfully: {path}",
                metadata={"path": path, "size": len(content)}
            )

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Error writing file: {str(e)}"
            )

    async def _edit_file(self, path: str, content: str) -> ToolResult:
        """Edit file with new content (append mode).

        Args:
            path: File path
            content: Content to append

        Returns:
            ToolResult with edit status
        """
        try:
            # For simplicity, this just overwrites. Could be enhanced to do partial edits.
            return await self._write_file(path, content)

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Error editing file: {str(e)}"
            )

    async def _create_dir(self, path: str) -> ToolResult:
        """Create directory.

        Args:
            path: Directory path

        Returns:
            ToolResult with creation status
        """
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
            logger.info(f"Created directory: {path}")
            return ToolResult(
                success=True,
                output=f"Directory created: {path}",
                metadata={"path": path}
            )

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Error creating directory: {str(e)}"
            )

    async def _list_dir(self, path: str) -> ToolResult:
        """List directory contents.

        Args:
            path: Directory path

        Returns:
            ToolResult with directory listing
        """
        try:
            if not os.path.exists(path):
                return ToolResult(
                    success=False,
                    error=f"Directory does not exist: {path}"
                )

            if not os.path.isdir(path):
                return ToolResult(
                    success=False,
                    error=f"Path is not a directory: {path}"
                )

            # List contents
            items = os.listdir(path)
            output = "\n".join(sorted(items))

            logger.info(f"Listed directory: {path} ({len(items)} items)")
            return ToolResult(
                success=True,
                output=output,
                metadata={"path": path, "count": len(items)}
            )

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Error listing directory: {str(e)}"
            )
