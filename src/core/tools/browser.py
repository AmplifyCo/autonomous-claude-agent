"""Browser tool for web browsing and automation."""

import asyncio
import logging
from typing import Optional
from .base import BaseTool
from ..types import ToolResult

logger = logging.getLogger(__name__)


class BrowserTool(BaseTool):
    """Tool for web browsing using text-based or headless browser."""

    name = "browser"
    description = """Browse web pages using text-based (w3m) or full browser (Selenium).
    Use text mode for reading articles, documentation. Use full mode for JavaScript-heavy sites."""

    parameters = {
        "url": {
            "type": "string",
            "description": "The URL to browse"
        },
        "mode": {
            "type": "string",
            "description": "Browser mode: 'text' for w3m text dump, 'full' for headless Chromium",
            "enum": ["text", "full"],
            "default": "text"
        },
        "javascript": {
            "type": "boolean",
            "description": "Execute JavaScript (only for full mode)",
            "default": False
        }
    }

    def __init__(self):
        """Initialize BrowserTool."""
        self.selenium_available = False
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            self.webdriver = webdriver
            self.Options = Options
            self.selenium_available = True
            logger.info("Selenium available for full browser mode")
        except ImportError:
            logger.warning("Selenium not installed. Only text mode available. Install with: pip install selenium")

    async def execute(
        self,
        url: str,
        mode: str = "text",
        javascript: bool = False
    ) -> ToolResult:
        """Browse a web page.

        Args:
            url: URL to browse
            mode: 'text' for w3m, 'full' for Selenium
            javascript: Execute JavaScript (full mode only)

        Returns:
            ToolResult with page content
        """
        try:
            if mode == "text":
                return await self._browse_text(url)
            elif mode == "full":
                return await self._browse_full(url, javascript)
            else:
                return ToolResult(
                    success=False,
                    error=f"Invalid mode: {mode}. Use 'text' or 'full'"
                )

        except Exception as e:
            logger.error(f"Error browsing {url}: {e}")
            return ToolResult(
                success=False,
                error=f"Browser error: {str(e)}"
            )

    async def _browse_text(self, url: str) -> ToolResult:
        """Browse using text-based w3m browser.

        Args:
            url: URL to fetch

        Returns:
            ToolResult with text content
        """
        try:
            # Use w3m to dump text content
            process = await asyncio.create_subprocess_shell(
                f"w3m -dump '{url}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30
            )

            if process.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='replace')
                # Fallback to curl if w3m fails
                logger.warning(f"w3m failed, trying curl: {error_msg}")
                return await self._fallback_curl(url)

            content = stdout.decode('utf-8', errors='replace')

            return ToolResult(
                success=True,
                output=content,
                metadata={
                    "url": url,
                    "mode": "text",
                    "browser": "w3m"
                }
            )

        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error=f"Timeout browsing {url} with w3m"
            )
        except Exception as e:
            logger.error(f"w3m error: {e}")
            # Fallback to curl
            return await self._fallback_curl(url)

    async def _fallback_curl(self, url: str) -> ToolResult:
        """Fallback to curl if w3m fails.

        Args:
            url: URL to fetch

        Returns:
            ToolResult with raw content
        """
        try:
            process = await asyncio.create_subprocess_shell(
                f"curl -L -s '{url}'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30
            )

            if process.returncode == 0:
                content = stdout.decode('utf-8', errors='replace')
                return ToolResult(
                    success=True,
                    output=content,
                    metadata={
                        "url": url,
                        "mode": "text",
                        "browser": "curl"
                    }
                )
            else:
                return ToolResult(
                    success=False,
                    error=f"Failed to fetch {url}: {stderr.decode()}"
                )

        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Curl error: {str(e)}"
            )

    async def _browse_full(self, url: str, execute_js: bool = False) -> ToolResult:
        """Browse using headless Chromium via Selenium.

        Args:
            url: URL to browse
            execute_js: Whether to wait for JavaScript execution

        Returns:
            ToolResult with page content
        """
        if not self.selenium_available:
            return ToolResult(
                success=False,
                error="Selenium not available. Install with: pip install selenium"
            )

        driver = None
        try:
            # Configure headless Chrome
            options = self.Options()
            options.add_argument('--headless')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')

            # Create driver
            driver = self.webdriver.Chrome(options=options)
            driver.set_page_load_timeout(30)

            # Navigate to URL
            driver.get(url)

            # Wait for JavaScript if requested
            if execute_js:
                await asyncio.sleep(2)  # Wait for JS to execute

            # Get page content
            page_source = driver.page_source
            page_text = driver.find_element("tag name", "body").text

            return ToolResult(
                success=True,
                output=page_text,
                metadata={
                    "url": url,
                    "mode": "full",
                    "browser": "chromium",
                    "javascript": execute_js,
                    "page_source_length": len(page_source)
                }
            )

        except Exception as e:
            logger.error(f"Selenium error: {e}")
            return ToolResult(
                success=False,
                error=f"Selenium browser error: {str(e)}"
            )

        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
