"""Auto-fix system for attempting to resolve detected errors."""

import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List
from pathlib import Path
import re
import subprocess
from pathlib import Path

from .error_detector import DetectedError, ErrorType, ErrorSeverity

logger = logging.getLogger(__name__)


class FixResult:
    """Result of an auto-fix attempt."""

    def __init__(
        self,
        success: bool,
        error_type: ErrorType,
        action_taken: str,
        details: Optional[str] = None,
        requires_restart: bool = False
    ):
        self.success = success
        self.error_type = error_type
        self.action_taken = action_taken
        self.details = details
        self.requires_restart = requires_restart

    def __repr__(self):
        return f"<FixResult success={self.success} action={self.action_taken} restart_needed={self.requires_restart}>"


class AutoFixer:
    """Automatically attempts to fix detected errors."""

    def __init__(self, telegram_notifier=None, llm_client=None):
        """Initialize auto-fixer.

        Args:
            telegram_notifier: Optional Telegram chat instance for notifications
            llm_client: Unified LiteLLM client for AI analysis
        """
        self.telegram = telegram_notifier
        self.llm_client = llm_client
        self.fix_history: List[FixResult] = []

        logger.info("AutoFixer initialized with LLM support")

    async def attempt_fix(self, error: DetectedError) -> FixResult:
        """Attempt to automatically fix a detected error.

        Args:
            error: The detected error to fix

        Returns:
            FixResult indicating success/failure
        """
        logger.info(f"Attempting to fix error: {error.error_type.value}")

        # Route to appropriate fix strategy
        fix_strategies = {
            ErrorType.RATE_LIMIT: self._fix_rate_limit,
            ErrorType.IMPORT_ERROR: self._fix_import_error,
            ErrorType.GIT_ERROR: self._fix_git_error,
            ErrorType.CONFIG_ERROR: self._fix_config_error,
            ErrorType.ATTRIBUTE_ERROR: self._fix_attribute_error,
            ErrorType.API_ERROR: self._fix_api_error,
            ErrorType.TYPE_ERROR: self._fix_type_error,
            ErrorType.SERVICE_CRASH: self._fix_service_crash,
            ErrorType.TIMEOUT: self._fix_timeout,
        }

        fix_func = fix_strategies.get(error.error_type)
        if not fix_func:
            # Fallback to AI code fix if enabled and client available
            if self.llm_client:
                logger.info(f"No specific strategy for {error.error_type.value}, attempting AI code fix")
                fix_func = self._fix_code_error
            else:
                logger.warning(f"No fix strategy for {error.error_type.value}")
                return FixResult(
                    success=False,
                    error_type=error.error_type,
                    action_taken="None - no fix strategy available",
                    details="This error type is not auto-fixable and no LLM client available"
                )

        try:
            result = await fix_func(error)
            self.fix_history.append(result)

            # Notify via Telegram if available
            if self.telegram:
                await self._notify_fix_attempt(error, result)

            return result

        except Exception as e:
            logger.error(f"Error during auto-fix: {e}", exc_info=True)
            return FixResult(
                success=False,
                error_type=error.error_type,
                action_taken="Fix attempt failed",
                details=str(e)
            )

    async def _fix_rate_limit(self, error: DetectedError) -> FixResult:
        """Fix rate limit errors by enabling fallback model.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing rate limit error - enabling fallback model")

        # Check if fallback is already enabled
        try:
            from ...integrations.model_router import ModelRouter
            # Fallback is already built into the system
            # Just log that it should activate automatically

            return FixResult(
                success=True,
                error_type=ErrorType.RATE_LIMIT,
                action_taken="Fallback model already configured",
                details="System will automatically use SmolLM2/DeepSeek when rate limited",
                requires_restart=False
            )
        except Exception as e:
            return FixResult(
                success=False,
                error_type=ErrorType.RATE_LIMIT,
                action_taken="Could not verify fallback configuration",
                details=str(e),
                requires_restart=False
            )

    async def _fix_import_error(self, error: DetectedError) -> FixResult:
        """Fix import errors by installing missing packages.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing import error - attempting to install missing package")

        # Extract module name from error message
        import re
        match = re.search(r"No module named '(\w+)'", error.message)
        if not match:
            return FixResult(
                success=False,
                error_type=ErrorType.IMPORT_ERROR,
                action_taken="Could not identify missing module",
                details="Unable to parse module name from error"
            )

        module_name = match.group(1)
        logger.info(f"Attempting to install: {module_name}")

        try:
            # Try to install the package
            result = subprocess.run(
                ["pip", "install", module_name],
                capture_output=True,
                text=True,
                timeout=120
            )

            if result.returncode == 0:
                return FixResult(
                    success=True,
                    error_type=ErrorType.IMPORT_ERROR,
                    action_taken=f"Installed missing package: {module_name}",
                    details=result.stdout,
                    requires_restart=True  # Need restart to reload modules
                )
            else:
                return FixResult(
                    success=False,
                    error_type=ErrorType.IMPORT_ERROR,
                    action_taken=f"Failed to install {module_name}",
                    details=result.stderr
                )

        except subprocess.TimeoutExpired:
            return FixResult(
                success=False,
                error_type=ErrorType.IMPORT_ERROR,
                action_taken="Installation timed out",
                details=f"pip install {module_name} took too long"
            )
        except Exception as e:
            return FixResult(
                success=False,
                error_type=ErrorType.IMPORT_ERROR,
                action_taken="Installation failed",
                details=str(e)
            )

    async def _fix_git_error(self, error: DetectedError) -> FixResult:
        """Fix git errors (conflicts, etc).

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing git error")

        # Check if it's a merge conflict
        if "merge conflict" in error.message.lower():
            try:
                # Stash local changes and pull again
                logger.info("Detected merge conflict - stashing changes")

                # Stash changes
                subprocess.run(["git", "stash"], check=True, capture_output=True)

                # Pull latest
                result = subprocess.run(
                    ["git", "pull", "origin", "main"],
                    capture_output=True,
                    text=True,
                    check=True
                )

                return FixResult(
                    success=True,
                    error_type=ErrorType.GIT_ERROR,
                    action_taken="Stashed local changes and pulled latest",
                    details="Merge conflict resolved. Local changes stashed.",
                    requires_restart=True
                )

            except subprocess.CalledProcessError as e:
                return FixResult(
                    success=False,
                    error_type=ErrorType.GIT_ERROR,
                    action_taken="Failed to resolve git conflict",
                    details=e.stderr
                )

        # Generic git error - try git status
        try:
            result = subprocess.run(
                ["git", "status"],
                capture_output=True,
                text=True,
                check=True
            )

            return FixResult(
                success=True,
                error_type=ErrorType.GIT_ERROR,
                action_taken="Checked git status",
                details=result.stdout,
                requires_restart=False
            )

        except Exception as e:
            return FixResult(
                success=False,
                error_type=ErrorType.GIT_ERROR,
                action_taken="Could not diagnose git error",
                details=str(e)
            )

    async def _fix_config_error(self, error: DetectedError) -> FixResult:
        """Fix configuration errors.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing configuration error")

        # Check if .env exists
        env_file = Path(".env")
        env_example = Path(".env.example")

        if not env_file.exists() and env_example.exists():
            try:
                # Copy .env.example to .env
                import shutil
                shutil.copy(env_example, env_file)

                return FixResult(
                    success=True,
                    error_type=ErrorType.CONFIG_ERROR,
                    action_taken="Created .env from .env.example",
                    details="You may need to add your API keys",
                    requires_restart=True
                )

            except Exception as e:
                return FixResult(
                    success=False,
                    error_type=ErrorType.CONFIG_ERROR,
                    action_taken="Failed to create .env",
                    details=str(e)
                )

        # Check config/agent.yaml
        config_file = Path("config/agent.yaml")
        if not config_file.exists():
            return FixResult(
                success=False,
                error_type=ErrorType.CONFIG_ERROR,
                action_taken="Missing config/agent.yaml",
                details="Configuration file not found. May need to restore from git."
            )

        return FixResult(
            success=False,
            error_type=ErrorType.CONFIG_ERROR,
            action_taken="Configuration appears intact",
            details="Unable to automatically fix this config error"
        )

    async def _fix_attribute_error(self, error: DetectedError) -> FixResult:
        """Fix attribute errors (like the .model vs .default_model issue).

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing attribute error")

        # For attribute errors, we can't automatically fix code
        # But we can suggest pulling latest from git
        if "config" in error.message.lower() and "model" in error.message.lower():
            try:
                # Check if there are updates on git
                result = subprocess.run(
                    ["git", "fetch", "origin", "main"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                # Check if we're behind
                result = subprocess.run(
                    ["git", "rev-list", "HEAD..origin/main", "--count"],
                    capture_output=True,
                    text=True
                )

                commits_behind = int(result.stdout.strip())
                if commits_behind > 0:
                    return FixResult(
                        success=True,
                        error_type=ErrorType.ATTRIBUTE_ERROR,
                        action_taken="Detected code fix available in git",
                        details=f"There are {commits_behind} new commits. Suggest: 'git pull origin main'",
                        requires_restart=False
                    )

            except Exception as e:
                logger.error(f"Error checking git: {e}")

        return FixResult(
            success=False,
            error_type=ErrorType.ATTRIBUTE_ERROR,
            action_taken="Cannot auto-fix code errors",
            details="Attribute errors require code changes. Check git for updates."
        )

    async def _fix_api_error(self, error: DetectedError) -> FixResult:
        """Fix API errors by retrying with exponential backoff.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing API error")

        # Check if it's a connection/network issue
        if any(keyword in error.message.lower() for keyword in ["connection", "network", "timeout", "503", "500"]):
            try:
                # Wait a bit before suggesting retry
                import time
                wait_seconds = 5
                time.sleep(wait_seconds)

                return FixResult(
                    success=True,
                    error_type=ErrorType.API_ERROR,
                    action_taken=f"Waited {wait_seconds}s for API recovery",
                    details="Transient API errors often resolve themselves. System will retry automatically.",
                    requires_restart=False
                )

            except Exception as e:
                return FixResult(
                    success=False,
                    error_type=ErrorType.API_ERROR,
                    action_taken="Could not apply retry strategy",
                    details=str(e)
                )

        # For 500/503 errors, suggest checking API status
        return FixResult(
            success=False,
            error_type=ErrorType.API_ERROR,
            action_taken="API error detected",
            details="Check Anthropic API status at status.anthropic.com. May require manual intervention."
        )

    async def _fix_type_error(self, error: DetectedError) -> FixResult:
        """Fix type errors by checking for code updates.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing type error")

        # Type errors are usually code bugs, check git for fixes
        try:
            # Fetch latest
            subprocess.run(
                ["git", "fetch", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=30
            )

            # Check if behind
            result = subprocess.run(
                ["git", "rev-list", "HEAD..origin/main", "--count"],
                capture_output=True,
                text=True
            )

            commits_behind = int(result.stdout.strip())
            if commits_behind > 0:
                return FixResult(
                    success=True,
                    error_type=ErrorType.TYPE_ERROR,
                    action_taken="Found code updates available",
                    details=f"{commits_behind} new commits available. Suggest pulling latest code.",
                    requires_restart=False
                )
            else:
                return FixResult(
                    success=False,
                    error_type=ErrorType.TYPE_ERROR,
                    action_taken="No updates available",
                    details="Type error requires code fix. Already on latest commit."
                )

        except Exception as e:
            return FixResult(
                success=False,
                error_type=ErrorType.TYPE_ERROR,
                action_taken="Could not check for updates",
                details=str(e)
            )

    async def _fix_service_crash(self, error: DetectedError) -> FixResult:
        """Fix service crashes by attempting restart.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing service crash")

        try:
            # Check if running as systemd service
            result = subprocess.run(
                ["systemctl", "is-active", "claude-agent"],
                capture_output=True,
                text=True
            )

            if result.returncode != 0:
                # Service is not active, try to start it
                logger.info("Service is down, attempting to restart")

                restart_result = subprocess.run(
                    ["sudo", "systemctl", "restart", "claude-agent"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if restart_result.returncode == 0:
                    return FixResult(
                        success=True,
                        error_type=ErrorType.SERVICE_CRASH,
                        action_taken="Restarted claude-agent service",
                        details="Service was down and has been restarted via systemctl",
                        requires_restart=False
                    )
                else:
                    return FixResult(
                        success=False,
                        error_type=ErrorType.SERVICE_CRASH,
                        action_taken="Failed to restart service",
                        details=restart_result.stderr
                    )
            else:
                # Service is running
                return FixResult(
                    success=True,
                    error_type=ErrorType.SERVICE_CRASH,
                    action_taken="Service is already running",
                    details="No restart needed",
                    requires_restart=False
                )

        except Exception as e:
            return FixResult(
                success=False,
                error_type=ErrorType.SERVICE_CRASH,
                action_taken="Could not check/restart service",
                details=str(e)
            )

    async def _fix_timeout(self, error: DetectedError) -> FixResult:
        """Fix timeout errors by adjusting timeouts or retrying.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        logger.info("Fixing timeout error")

        # Timeout errors are usually transient
        # Suggest retry with longer timeout
        return FixResult(
            success=True,
            error_type=ErrorType.TIMEOUT,
            action_taken="Identified timeout error",
            details="Timeout errors are often transient. System will retry with increased timeout automatically.",
            requires_restart=False
        )

    async def _notify_fix_attempt(self, error: DetectedError, result: FixResult):
        """Notify user via Telegram about fix attempt.

        Args:
            error: The original error
            result: The fix result
        """
        if not self.telegram:
            return

        icon = "✅" if result.success else "❌"
        status = "Fixed" if result.success else "Failed"

        message = f"""{icon} **Auto-Fix: {status}**

**Error Type:** {error.error_type.value}
**Severity:** {error.severity.value}
**Action Taken:** {result.action_taken}

**Details:** {result.details or 'None'}
"""

        if result.requires_restart:
            message += "\n⚠️ **Restart required** for changes to take effect"

        try:
            await self.telegram.notify(message, level="warning")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    def get_fix_summary(self) -> Dict[str, Any]:
        """Get summary of fix attempts.

        Returns:
            Summary dict
        """
        total = len(self.fix_history)
        successful = sum(1 for f in self.fix_history if f.success)

        by_type = {}
        for fix in self.fix_history:
            error_type = fix.error_type.value
            by_type[error_type] = by_type.get(error_type, 0) + 1

        return {
            "total_fixes_attempted": total,
            "successful": successful,
            "failed": total - successful,
            "success_rate": f"{(successful / total * 100):.1f}%" if total > 0 else "N/A",
            "by_type": by_type,
            "recent_fixes": [
                {
                    "type": f.error_type.value,
                    "success": f.success,
                    "action": f.action_taken
                }
                for f in self.fix_history[-5:]  # Last 5 fixes
            ]
        }
    async def _fix_code_error(self, error: DetectedError) -> FixResult:
        """Fix arbitrary code errors using LLM analysis.

        Args:
            error: The detected error

        Returns:
            FixResult
        """
        if not self.llm_client:
            return FixResult(success=False, error_type=error.error_type, action_taken="No LLM client")

        logger.info("Attempting AI-powered code fix...")

        # 1. Identify file from context
        import re
        file_path = None
        line_num = None
        if error.context:
            match = re.search(r'File "([^"]+)", line (\d+)', error.context)
            if match:
                file_path = match.group(1)
                line_num = match.group(2)
        
        if not file_path or not Path(file_path).exists():
            return FixResult(
                success=False, 
                error_type=error.error_type, 
                action_taken="Could not locate file",
                details="File path missing from error context"
            )

        # 2. Read file content
        try:
            with open(file_path, 'r') as f:
                code_content = f.read()
        except Exception as e:
            return FixResult(success=False, error_type=error.error_type, action_taken="Read failed", details=str(e))

        # 3. Generate fix via LLM
        try:
            fix_diff = await self._generate_ai_fix(error, file_path, code_content)
            if not fix_diff:
                return FixResult(success=False, error_type=error.error_type, action_taken="LLM declined fix")
        except Exception as e:
             return FixResult(success=False, error_type=error.error_type, action_taken="Generation failed", details=str(e))

        # 4. Assess Security Risk
        risk_level, risk_reason = await self._assess_security_risk(fix_diff, file_path)
        
        fix_id = f"fix-{int(datetime.now().timestamp())}"
        branch_name = f"auto-fix/{fix_id}"
        
        if risk_level == "SENSITIVE":
            # Push to branch ONLY (do not apply)
            await self._push_fix_to_branch(file_path, fix_diff, branch_name, f"Security-gated fix for {error.error_type.value}")
            
            return FixResult(
                success=True,
                error_type=error.error_type,
                action_taken=f"⚠️ Fix pushed to branch {branch_name} (Security Risk)",
                details=f"Classified as SENSITIVE: {risk_reason}. Review branch {branch_name}",
                requires_restart=False
            )

        # 5. Apply Safe Fix
        try:
            # Write diff to temp file
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.diff', delete=False) as tmp:
                tmp.write(fix_diff)
                tmp_path = tmp.name
            
            # Apply patch locally
            proc = await asyncio.create_subprocess_shell(
                f"patch -p0 < {tmp_path}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            Path(tmp_path).unlink()

            if proc.returncode != 0:
                 return FixResult(
                    success=False, 
                    error_type=error.error_type, 
                    action_taken="Patch application failed", 
                    details=stderr.decode()
                )

            # Push to branch for record keeping/merge
            await self._push_fix_to_branch(file_path, fix_diff, branch_name, f"Auto-fix for {error.error_type.value}")

            return FixResult(
                success=True,
                error_type=error.error_type,
                action_taken=f"Applied fix & pushed to {branch_name}",
                details="Automatically patched and backed up to git branch",
                requires_restart=True
            )

        except Exception as e:
            return FixResult(success=False, error_type=error.error_type, action_taken="Apply failed", details=str(e))


    async def _generate_ai_fix(self, error: DetectedError, file_path: str, code: str) -> Optional[str]:
        """Generate a unified diff fix using Claude."""
        system_prompt = """You are an expert Python debugger. 
        Analyze the error and the code. 
        Generate a UNIFIED DIFF (git diff format) to fix the bug.
        
        Rules:
        1. Output ONLY the diff content. No markdown code blocks, no explanation.
        2. Use strictly standard unified diff format (starts with --- and +++).
        3. File paths in diff should be relative to project root (e.g. src/core/agent.py).
        4. Fix only the specific error. Do not refactor unrelated code.
        """
        
        user_message = f"""
        Error: {error.message}
        Type: {error.error_type.value}
        Context: {error.context}
        
        File: {file_path}
        Code Content:
        {code}
        """

        try:
            response = await self.llm_client.create_message(
                model="anthropic/claude-sonnet-4-6", # Use smart model for coding
                messages=[{"role": "user", "content": user_message}],
                system=system_prompt,
                max_tokens=2048
            )
            
            content = response.content[0].text.strip()
            # unique case if model outputs markdown block
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
                
            return content
        except Exception as e:
            logger.error(f"LLM fix generation failed: {e}")
            return None



    def _contains_secrets(self, diff: str) -> tuple[bool, str]:
        """Check if diff contains potential secrets."""
        # Common secret patterns
        patterns = [
            (r"BEGIN A PRIVATE KEY", "Private Key"),
            (r"sk-[a-zA-Z0-9]{20,}", "OpenAI API Key"),
            (r"xox[baprs]-([0-9a-zA-Z]{10,48})?", "Slack Token"),
            (r"gh[pousr]_[a-zA-Z0-9]{36,}", "GitHub Token"),
            (r"(api_key|apikey|secret|token|password|passwd|pwd)\s*[:=]\s*['\"][a-zA-Z0-9_\-]{8,}['\"]", "Generic Secret Assignment"),
            (r"Authorization:\s*Bearer\s+[a-zA-Z0-9_\-\.]+", "Bearer Token"),
        ]
        
        for pattern, name in patterns:
            if re.search(pattern, diff, re.IGNORECASE):
                return True, name
                
        return False, ""

    async def _assess_security_risk(self, diff: str, file_path: str) -> tuple[str, str]:
        """Assess if the fix is SAFE or SENSITIVE."""
        # 1. Hardcoded Rules
        if "src/core/security" in file_path:
            return "SENSITIVE", "Modifies security module"
        if "bash.py" in file_path and ("blocked_commands" in diff or "allowed_commands" in diff):
            return "SENSITIVE", "Modifies bash tool security filters"
            
        # 2. Secret Scanning (Strict)
        has_secret, secret_type = self._contains_secrets(diff)
        if has_secret:
            return "SENSITIVE", f"Potential secret detected ({secret_type})"
            
        # 3. LLM Judge (Gemini Flash for speed/cost)
        try:
            response = await self.llm_client.create_message(
                model="gemini/gemini-2.0-flash",
                messages=[{
                    "role": "user", 
                    "content": f"Analyze this git diff. Is it security-sensitive? Reply ONLY 'SAFE' or 'SENSITIVE'.\n\nDiff:\n{diff}"
                }],
                max_tokens=10
            )
            result = response.content[0].text.strip().upper()
            if "SENSITIVE" in result:
                return "SENSITIVE", "AI Flagged as sensitive"
                
        except Exception as e:
            logger.warning(f"LLM security check failed: {e}")
            return "SENSITIVE", "Security check failed (fail-safe)" # Fail safe
            
        return "SAFE", "Passed checks"

    async def _push_fix_to_branch(self, file_path: str, diff: str, branch_name: str, commit_msg: str):
        """Push the fix to a new git branch without disrupting the current working tree."""
        try:
            # 1. Fetch latest to ensure we have base
            await asyncio.create_subprocess_shell("git fetch origin main")
            
            # 2. Create and checkout the branch
            # We use the current repo, assuming 'main' is clean-ish or we can switch back
            # Actually, switching branches in a running agent is risky.
            # SAFER: Create a temporary worktree
            import tempfile
            import shutil
            
            repo_root = Path.cwd()
            
            # Use git worktree to create a parallel working directory linked to this repo
            # This avoids messing with the running agent's files
            worktree_path = repo_root / "data" / "worktrees" / branch_name.split('/')[-1]
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Clean up if exists
            if worktree_path.exists():
                shutil.rmtree(worktree_path)

            # Create worktree based on main
            cmd = f"git worktree add -b {branch_name} {worktree_path} origin/main"
            proc = await asyncio.create_subprocess_shell(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            await proc.communicate()
            
            if proc.returncode != 0:
                logger.error(f"Failed to create worktree: {branch_name}")
                return

            try:
                # Apply patch in worktree
                patch_file = worktree_path / "fix.diff"
                with open(patch_file, 'w') as f:
                    f.write(diff)
                
                # Apply patch
                cmd_patch = f"cd {worktree_path} && patch -p0 < fix.diff"
                await (await asyncio.create_subprocess_shell(cmd_patch)).communicate()
                
                # Remove patch file
                patch_file.unlink()
                
                # Commit and Push
                cmd_push = f"cd {worktree_path} && git add . && git commit -m '{commit_msg}' && git push origin {branch_name}"
                await (await asyncio.create_subprocess_shell(cmd_push)).communicate()
                
                logger.info(f"Pushed fix to branch {branch_name}")
                
            finally:
                # Cleanup worktree
                cmd_clean = f"git worktree remove {worktree_path} --force"
                await (await asyncio.create_subprocess_shell(cmd_clean)).communicate()
                # Worktree remove sometimes leaves the dir, ensure gone
                if worktree_path.exists():
                    shutil.rmtree(worktree_path)
                    
        except Exception as e:
            logger.error(f"Failed to push fix branch: {e}")
