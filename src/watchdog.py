"""
Watchdog service for the Digital Twin.
Acts as a supervisor process to ensure the main agent starts and stays running.
If the agent crashes at startup (ImportError, syntax error, etc.), the watchdog
attempts to auto-fix the issue using the AI AutoFixer.
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

def setup_logging():
    # Configure logging for the watchdog itself
    Path("data/logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - [WATCHDOG] - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("data/logs/watchdog.log")
        ]
    )
logger = logging.getLogger("watchdog")

# Constants
MAX_RESTARTS_PER_WINDOW = 5
WINDOW_SECONDS = 300  # 5 minutes
BACKOFF_START_SECONDS = 5

class ServiceWatchdog:
    def __init__(self):
        self.restart_history = []
        self.project_root = Path(__file__).parent.parent
        self.main_script = self.project_root / "src/main.py"
        self.venv_python = self.project_root / "venv/bin/python"
        
        # Fallback to system python if venv not found (local dev)
        if not self.venv_python.exists():
            self.venv_python = Path(sys.executable)

    async def start(self):
        """Start the watchdog loop."""
        logger.info("Starting Digital Twin Watchdog")
        logger.info(f"Target: {self.main_script}")

        while True:
            # Check for crash loop
            if self._is_crashing_too_often():
                logger.critical("Too many restarts in short period. Backing off for 5 minutes.")
                await asyncio.sleep(300)
                self.restart_history.clear() # Reset after backoff

            self.restart_history.append(datetime.now())
            
            # Start the service
            logger.info("Starting main service...")
            process = await asyncio.create_subprocess_exec(
                str(self.venv_python),
                str(self.main_script),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Stream logs (pass-through)
            stdout_task = asyncio.create_task(self._stream_output(process.stdout, "stdout"))
            stderr_task = asyncio.create_task(self._stream_output(process.stderr, "stderr"))

            # Wait for exit
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)

            if exit_code == 0:
                logger.info("Service exited normally (0). Restarting immediately.")
                continue
            
            # Crash detected
            logger.error(f"Service crashed with exit code {exit_code}")
            
            # Attempt Auto-Fix
            await self._handle_crash(exit_code)
            
            # Exponential backoff based on recent crash frequency
            wait_time = max(BACKOFF_START_SECONDS, len(self.restart_history) * 2)
            logger.info(f"Restarting in {wait_time} seconds...")
            await asyncio.sleep(wait_time)

    async def _stream_output(self, stream, origin):
        """Stream output from subprocess to watchdog logs."""
        output_buffer = []  # Keep last ~50 lines for crash analysis
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode().strip()
            if text:
                if origin == "stderr":
                    # Check for log levels in the text to avoid false errors
                    level = logging.ERROR
                    upper_text = text.upper()
                    if "INFO" in upper_text or "INFO:" in upper_text:
                        level = logging.INFO
                    elif "WARNING" in upper_text or "WARN:" in upper_text:
                        level = logging.WARNING
                    elif "DEBUG" in upper_text:
                        level = logging.DEBUG
                        
                    logger.log(level, f"[AGENT] {text}")
                    # Capture for analysis
                    self._capture_crash_log(text)
                else:
                    logger.info(f"[AGENT] {text}")

    def _capture_crash_log(self, line):
        """Buffer stderr for analysis."""
        if not hasattr(self, '_crash_buffer'):
            self._crash_buffer = []
        self._crash_buffer.append(line)
        if len(self._crash_buffer) > 100:
            self._crash_buffer.pop(0)

    def _is_crashing_too_often(self):
        """Check if we are in a crash loop."""
        now = datetime.now()
        # Remove old history
        self.restart_history = [t for t in self.restart_history if (now - t).total_seconds() < WINDOW_SECONDS]
        return len(self.restart_history) >= MAX_RESTARTS_PER_WINDOW

    async def _handle_crash(self, exit_code):
        """Analyze crash and attempt auto-fix."""
        if not hasattr(self, '_crash_buffer') or not self._crash_buffer:
            logger.warning("No stderr output captured. Cannot analyze crash.")
            return

        error_context = "\n".join(self._crash_buffer[-20:]) # Last 20 lines
        logger.info("Analyzing crash context...")

        try:
            # Lazy import to avoid crash if these modules are broken
            # We need to add src to sys.path first
            sys.path.insert(0, str(self.project_root))
            
            from src.core.self_healing.auto_fixer import AutoFixer
            from src.core.self_healing.error_detector import DetectedError, ErrorType, ErrorSeverity
            from src.integrations.gemini_client import GeminiClient
            from src.core.config import load_config
            
            # Initialize minimal dependencies
            config = load_config()
            gemini_client = GeminiClient(
                api_key=os.getenv('GEMINI_API_KEY', ''),
                anthropic_api_key=config.api_key
            )
            fixer = AutoFixer(llm_client=gemini_client)
            
            # Create a synthetic detected error
            # We assume it's a SERVICE_CRASH or whatever we can infer
            # Ideally we parse it, but for now we create a generic high-severity error with context
            error = DetectedError(
                error_type=ErrorType.SERVICE_CRASH,
                severity=ErrorSeverity.CRITICAL,
                message=f"Service crashed with exit code {exit_code}",
                timestamp=datetime.now(),
                context=error_context,
                auto_fixable=True
            )
            
            # Attempt fix
            logger.info("Invoking AutoFixer...")
            result = await fixer.attempt_fix(error)
            
            if result.success:
                logger.info(f"Auto-fix successful: {result.action_taken}")
            else:
                logger.error(f"Auto-fix failed: {result.details}")
                
        except Exception as e:
            logger.error(f"Watchdog failed to auto-fix: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    try:
        # Ensure log dir exists and setup logging
        setup_logging()
        
        watchdog = ServiceWatchdog()
        logging.info("Watchdog initialized successfully")
        asyncio.run(watchdog.start())
    except KeyboardInterrupt:
        logger.info("Watchdog stopped by user")
    except Exception as e:
        # Critical failure in watchdog itself
        sys.stderr.write(f"CRITICAL WATCHDOG FAILURE: {e}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)
