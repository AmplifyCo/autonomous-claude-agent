"""Autonomous Capability Discovery — browse, discover, review, connect.

Takes any URL (or search query), browses the site, discovers if it has
an API, evaluates usefulness and safety, then connects via SkillLearner.

Pipeline: browse → discover → review → connect → report.
"""

import asyncio
import json
import logging
import shlex
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp

from .base import BaseTool
from ..types import ToolResult

logger = logging.getLogger(__name__)

# Max page content to send to LLM
_MAX_CONTENT_FOR_LLM = 12_000

# ── LLM Prompts ────────────────────────────────────────────────────────

_DISCOVER_API_PROMPT = """\
You are an API discovery agent. Analyze this web page content and determine if the site has an API.

PAGE URL: {url}

PAGE CONTENT (truncated):
{content}

Analyze the page and return ONLY valid JSON (no markdown fences):
{{
  "has_api": true/false,
  "api_type": "REST|GraphQL|WebSocket|agent_protocol|MCP|unknown",
  "description": "One-line description of what this service does",
  "spec_urls": ["URLs to API spec files (openapi.json, swagger.yaml, .md specs, etc.)"],
  "doc_urls": ["URLs to API documentation pages (/docs, /api, /developer, etc.)"],
  "signup_url": "URL to register/signup for API access, or null",
  "confidence": 0.0-1.0,
  "notes": "Any relevant observations about the API"
}}

DETECTION HINTS — look for:
- Links containing: /docs, /api, /developer, /swagger, /openapi, /reference, /sdk
- Text mentioning: API, REST, GraphQL, webhook, endpoint, authentication, API key
- Files: openapi.json, swagger.yaml, api-spec.md, skill.md
- Developer portals, "Build with us", "For developers", "API access"
- Agent protocols: A2A, MCP, tool_use, function_calling

If a URL looks relative (e.g. /docs/api), make it absolute using the base URL.
If no API is found, set has_api to false and leave arrays empty."""

_REVIEW_SERVICE_PROMPT = """\
You are a security-aware AI agent evaluator. Review this discovered API and decide if it's safe and useful to connect.

SERVICE: {description}
API TYPE: {api_type}
SPEC URLS: {spec_urls}
DOC URLS: {doc_urls}
SIGNUP URL: {signup_url}

PAGE CONTENT (for context):
{content}

Evaluate and return ONLY valid JSON (no markdown fences):
{{
  "proceed": true/false,
  "risk_level": "low|medium|high",
  "capabilities": ["List of capabilities the agent would gain"],
  "recommendation": "One-line recommendation",
  "reason": "Brief explanation of why to proceed or skip",
  "concerns": ["Any security or safety concerns"]
}}

EVALUATION CRITERIA:
- LOW risk: read-only APIs, public data, no payment required
- MEDIUM risk: requires API key, can write data, free tier available
- HIGH risk: requires payment, can send messages/emails, can delete data
- SKIP if: site looks malicious, no clear API, requires manual approval process
- PROCEED if: clear API with docs, useful capabilities, manageable risk"""

_SYNTHESIZE_SPEC_PROMPT = """\
You are an API spec writer. Given these API documentation pages, synthesize a markdown API specification.

SERVICE: {description}
BASE URL: {base_url}

DOCUMENTATION CONTENT:
{doc_content}

Generate a complete markdown API spec with:
1. Service name and description
2. Base URL
3. Authentication method (bearer, api_key, etc.)
4. Available endpoints with:
   - HTTP method and path
   - Description
   - Parameters (query, path, body)
   - Response format
5. Rate limits if mentioned
6. Error format

Output ONLY the markdown spec (no code fences wrapping the whole thing).
Start with: # [Service Name] API Specification"""


class DiscoverTool(BaseTool):
    """Autonomously browse, discover, review, and connect to new APIs."""

    name = "discover_and_connect"
    description = (
        "Autonomously discover and connect to new APIs and services. "
        "Given a URL, browses the site, discovers if it has an API, "
        "evaluates usefulness and safety, and connects via skill learning. "
        "Operations: 'discover' (full pipeline), 'browse_only' (just report what the site is)."
    )

    parameters = {
        "operation": {
            "type": "string",
            "enum": ["discover", "browse_only"],
            "description": (
                "'discover': browse site, find API, evaluate, and connect. "
                "'browse_only': just browse and report what the site is."
            ),
        },
        "url": {
            "type": "string",
            "description": "URL of the site to explore.",
        },
    }

    def __init__(self):
        self.skill_learner = None  # Injected via registry
        self.llm_client = None     # Injected — GeminiClient (LiteLLM, routes to any model)
        self._model = "gemini/gemini-2.0-flash"  # Default model, can be overridden

    def to_anthropic_tool(self):
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": ["operation", "url"],
            },
        }

    async def execute(
        self,
        operation: str = "discover",
        url: str = "",
        **kwargs,
    ) -> ToolResult:
        if not url or not url.strip():
            return ToolResult(success=False, error="URL is required.")

        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        if not self.llm_client:
            return ToolResult(success=False, error="Discovery engine not initialized (no LLM client).")

        try:
            if operation == "discover":
                return await self._discover_pipeline(url)
            elif operation == "browse_only":
                return await self._browse_and_report(url)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            logger.error(f"DiscoverTool error: {e}")
            return ToolResult(success=False, error=f"Discovery failed: {e}")

    # ── Pipeline ────────────────────────────────────────────────────────

    async def _discover_pipeline(self, url: str) -> ToolResult:
        """Full pipeline: browse → discover → review → connect."""

        # Step 1: BROWSE
        logger.info(f"Discovery: browsing {url}")
        ok, content = await self._fetch_site(url)
        if not ok:
            return ToolResult(success=False, error=f"Could not browse site: {content}")

        # Step 2: DISCOVER
        logger.info(f"Discovery: analyzing {url} for APIs")
        discovery = await self._discover_api(url, content)
        if not discovery:
            return ToolResult(
                success=False,
                error="Failed to analyze the site for APIs.",
            )

        if not discovery.get("has_api"):
            return ToolResult(
                success=True,
                output=(
                    f"Browsed {url}.\n\n"
                    f"Description: {discovery.get('description', 'Unknown')}\n\n"
                    f"No API was detected on this site. "
                    f"Notes: {discovery.get('notes', 'None')}"
                ),
                metadata={"phase": "discover", "has_api": False},
            )

        # Step 3: REVIEW
        logger.info(f"Discovery: reviewing {discovery.get('description', url)}")
        review = await self._review_service(discovery, content)
        if not review:
            return ToolResult(
                success=False,
                error="Failed to evaluate the discovered API.",
            )

        if not review.get("proceed"):
            return ToolResult(
                success=True,
                output=(
                    f"Browsed {url} and found an API.\n\n"
                    f"Description: {discovery.get('description', 'Unknown')}\n"
                    f"API type: {discovery.get('api_type', 'Unknown')}\n"
                    f"Risk level: {review.get('risk_level', 'unknown')}\n\n"
                    f"Decision: NOT connecting.\n"
                    f"Reason: {review.get('reason', 'Not recommended')}\n"
                    f"Concerns: {', '.join(review.get('concerns', []))}"
                ),
                metadata={"phase": "review", "proceed": False, "review": review},
            )

        # Step 4: CONNECT
        logger.info(f"Discovery: connecting to {discovery.get('description', url)}")
        connect_result = await self._connect(url, discovery, content)

        # Step 5: REPORT
        capabilities = review.get("capabilities", [])
        cap_str = "\n".join(f"  - {c}" for c in capabilities) if capabilities else "  (unknown)"

        return ToolResult(
            success=connect_result.get("success", False),
            output=(
                f"Discovery complete for {url}.\n\n"
                f"Service: {discovery.get('description', 'Unknown')}\n"
                f"API type: {discovery.get('api_type', 'Unknown')}\n"
                f"Risk level: {review.get('risk_level', 'unknown')}\n\n"
                f"Capabilities gained:\n{cap_str}\n\n"
                f"Connection: {connect_result.get('message', 'Unknown status')}"
            ),
            metadata={
                "phase": "complete",
                "discovery": discovery,
                "review": review,
                "connection": connect_result,
            },
        )

    async def _browse_and_report(self, url: str) -> ToolResult:
        """Just browse and report — no connection."""
        ok, content = await self._fetch_site(url)
        if not ok:
            return ToolResult(success=False, error=f"Could not browse site: {content}")

        discovery = await self._discover_api(url, content)
        if not discovery:
            return ToolResult(
                success=True,
                output=f"Browsed {url} but could not analyze the page.",
            )

        has_api = discovery.get("has_api", False)
        output = (
            f"Site: {url}\n"
            f"Description: {discovery.get('description', 'Unknown')}\n"
            f"Has API: {'Yes' if has_api else 'No'}\n"
        )
        if has_api:
            output += (
                f"API type: {discovery.get('api_type', 'Unknown')}\n"
                f"Spec URLs: {', '.join(discovery.get('spec_urls', [])) or 'None found'}\n"
                f"Doc URLs: {', '.join(discovery.get('doc_urls', [])) or 'None found'}\n"
                f"Signup: {discovery.get('signup_url') or 'None found'}\n"
            )
        output += f"Notes: {discovery.get('notes', 'None')}"

        return ToolResult(success=True, output=output, metadata={"discovery": discovery})

    # ── Step 1: BROWSE ──────────────────────────────────────────────────

    async def _fetch_site(self, url: str) -> Tuple[bool, str]:
        """Fetch site content via aiohttp, fallback to w3m."""
        # Try aiohttp first (fast)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0.0.0 Safari/537.36"
                        ),
                    },
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        if len(content) > 500:  # Looks like real content
                            # Strip HTML tags for cleaner LLM input
                            return True, self._strip_html(content)
                    logger.info(f"aiohttp got status {resp.status} for {url}, trying w3m")
        except Exception as e:
            logger.info(f"aiohttp failed for {url}: {e}, trying w3m")

        # Fallback to w3m (handles JS-light pages better)
        try:
            safe_url = shlex.quote(url)
            process = await asyncio.create_subprocess_shell(
                f"w3m -dump {safe_url}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            if process.returncode == 0:
                content = stdout.decode("utf-8", errors="replace")
                if len(content) > 100:
                    return True, content
        except Exception as e:
            logger.warning(f"w3m also failed for {url}: {e}")

        return False, "Could not fetch the site content"

    @staticmethod
    def _strip_html(html: str) -> str:
        """Rough HTML → text conversion for LLM consumption."""
        import re
        # Remove script/style blocks
        text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    # ── Step 2: DISCOVER ────────────────────────────────────────────────

    async def _discover_api(self, url: str, content: str) -> Optional[Dict[str, Any]]:
        """Use LLM to analyze page content and find API indicators."""
        prompt = _DISCOVER_API_PROMPT.format(
            url=url,
            content=content[:_MAX_CONTENT_FOR_LLM],
        )

        try:
            resp = await asyncio.wait_for(
                self.llm_client.create_message(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    system="You are an API discovery agent. Output only valid JSON.",
                    max_tokens=1500,
                ),
                timeout=20.0,
            )
            text = self._extract_text(resp)
            text = text.replace("```json", "").replace("```", "").strip()
            result = json.loads(text)

            # Resolve relative URLs
            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            for key in ("spec_urls", "doc_urls"):
                if key in result and isinstance(result[key], list):
                    result[key] = [
                        urljoin(base, u) if not u.startswith("http") else u
                        for u in result[key]
                    ]
            if result.get("signup_url") and not result["signup_url"].startswith("http"):
                result["signup_url"] = urljoin(base, result["signup_url"])

            return result

        except asyncio.TimeoutError:
            logger.warning("API discovery prompt timed out")
            return None
        except json.JSONDecodeError as e:
            logger.warning(f"API discovery returned invalid JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"API discovery failed: {e}")
            return None

    # ── Step 3: REVIEW ──────────────────────────────────────────────────

    async def _review_service(
        self, discovery: Dict[str, Any], content: str
    ) -> Optional[Dict[str, Any]]:
        """Use LLM to evaluate whether to connect."""
        prompt = _REVIEW_SERVICE_PROMPT.format(
            description=discovery.get("description", "Unknown"),
            api_type=discovery.get("api_type", "Unknown"),
            spec_urls=json.dumps(discovery.get("spec_urls", [])),
            doc_urls=json.dumps(discovery.get("doc_urls", [])),
            signup_url=discovery.get("signup_url", "None"),
            content=content[:6000],
        )

        try:
            resp = await asyncio.wait_for(
                self.llm_client.create_message(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    system="You are a security-aware AI evaluator. Output only valid JSON.",
                    max_tokens=1000,
                ),
                timeout=15.0,
            )
            text = self._extract_text(resp)
            text = text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)

        except Exception as e:
            logger.error(f"Service review failed: {e}")
            return None

    # ── Step 4: CONNECT ─────────────────────────────────────────────────

    async def _connect(
        self, url: str, discovery: Dict[str, Any], content: str
    ) -> Dict[str, Any]:
        """Try to connect via SkillLearner.

        Strategy:
        1. Try spec URLs directly (openapi.json, .md spec, etc.)
        2. If no spec URLs, try doc pages → synthesize spec
        3. Pass to SkillLearner
        """
        if not self.skill_learner:
            return {"success": False, "message": "Skill learner not available"}

        # Strategy 1: Try spec URLs
        for spec_url in discovery.get("spec_urls", []):
            logger.info(f"Discovery: trying spec URL {spec_url}")
            try:
                success, msg = await self.skill_learner.learn_from_url(spec_url)
                if success:
                    return {"success": True, "message": msg, "method": "direct_spec"}
                logger.info(f"Spec URL {spec_url} failed: {msg}")
            except Exception as e:
                logger.warning(f"Spec URL {spec_url} error: {e}")

        # Strategy 2: Fetch doc pages and synthesize spec
        doc_urls = discovery.get("doc_urls", [])
        if doc_urls:
            logger.info(f"Discovery: synthesizing spec from {len(doc_urls)} doc pages")
            synth_result = await self._synthesize_and_learn(url, discovery, doc_urls)
            if synth_result.get("success"):
                return synth_result

        # Strategy 3: Synthesize from the original page content if it has enough info
        if discovery.get("confidence", 0) >= 0.6:
            logger.info("Discovery: synthesizing spec from original page content")
            synth_result = await self._synthesize_from_content(url, discovery, content)
            if synth_result.get("success"):
                return synth_result

        return {
            "success": False,
            "message": (
                "Found API indicators but could not obtain a usable spec. "
                "You may need to manually provide the API spec URL."
            ),
        }

    async def _synthesize_and_learn(
        self, base_url: str, discovery: Dict[str, Any], doc_urls: List[str]
    ) -> Dict[str, Any]:
        """Fetch doc pages, synthesize spec, learn from content."""
        # Fetch up to 3 doc pages
        doc_content_parts = []
        for doc_url in doc_urls[:3]:
            try:
                ok, content = await self._fetch_site(doc_url)
                if ok:
                    doc_content_parts.append(f"--- Page: {doc_url} ---\n{content[:4000]}")
            except Exception:
                continue

        if not doc_content_parts:
            return {"success": False, "message": "Could not fetch any doc pages"}

        doc_content = "\n\n".join(doc_content_parts)
        return await self._synthesize_from_content(base_url, discovery, doc_content)

    async def _synthesize_from_content(
        self, base_url: str, discovery: Dict[str, Any], content: str
    ) -> Dict[str, Any]:
        """Use LLM to synthesize a spec from page content, then learn it."""
        prompt = _SYNTHESIZE_SPEC_PROMPT.format(
            description=discovery.get("description", "Unknown service"),
            base_url=base_url,
            doc_content=content[:_MAX_CONTENT_FOR_LLM],
        )

        try:
            resp = await asyncio.wait_for(
                self.llm_client.create_message(
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    system="You are an API spec writer. Output only the markdown spec.",
                    max_tokens=4000,
                ),
                timeout=30.0,
            )
            spec_content = self._extract_text(resp)
            if len(spec_content) < 200:
                return {"success": False, "message": "Synthesized spec too short"}

            # Use learn_from_content if available, else write temp file
            if hasattr(self.skill_learner, 'learn_from_content'):
                success, msg = await self.skill_learner.learn_from_content(
                    spec_content, source_url=base_url
                )
            else:
                # Fallback: write to temp file and use learn_from_url
                import tempfile
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", delete=False, prefix="nova_spec_"
                ) as f:
                    f.write(spec_content)
                    temp_path = f.name

                # learn_from_url expects https URL, so use learn_from_content
                # For now, return partial success
                return {
                    "success": False,
                    "message": f"Synthesized spec but learn_from_content not available. Spec saved to {temp_path}",
                }

            return {"success": success, "message": msg, "method": "synthesized_spec"}

        except Exception as e:
            logger.error(f"Spec synthesis failed: {e}")
            return {"success": False, "message": f"Spec synthesis failed: {e}"}

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _extract_text(resp) -> str:
        """Extract text from LLM response (GeminiResponse or similar)."""
        if hasattr(resp, 'content') and resp.content:
            parts = []
            for block in resp.content:
                if hasattr(block, 'text'):
                    parts.append(block.text)
            return " ".join(parts).strip()
        if isinstance(resp, str):
            return resp.strip()
        if isinstance(resp, dict):
            return resp.get("text", "").strip()
        return ""
