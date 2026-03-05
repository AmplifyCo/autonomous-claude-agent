"""Skill Learner — acquires new tool capabilities from .md API spec files.

Phase 4A: Skill Acquisition.

Takes a URL to a markdown API specification, parses it with Gemini Flash,
generates a BaseTool plugin (tool.py + manifest.json), validates the code
for safety, and writes it to the plugins directory for hot-reload.

Pipeline: fetch → parse → generate → validate → write → reload → metadata.
Output: plugins/{skill_name}/tool.py + manifest.json (hub-spoke model).
"""

import ast
import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

# Safety: max spec file size
_MAX_SPEC_SIZE = 512 * 1024  # 512 KB

# Safety: banned AST constructs in generated code
_BANNED_CALLS = {
    "eval", "exec", "compile", "execfile",
    "__import__", "getattr", "setattr", "delattr",
    "globals", "locals", "vars",
}
_BANNED_MODULES = {
    "subprocess", "ctypes", "importlib", "shutil",
}
_BANNED_STRINGS = [
    "__import__", "os.system", "os.popen", "subprocess",
    "exec(", "eval(", "compile(",
]

# Where plugins live
_PLUGINS_DIR = Path(__file__).resolve().parents[1] / "tools" / "plugins"

# Where skill metadata is stored
_SKILLS_DATA_DIR = Path("data/skills")

# ── LLM Prompt Templates ────────────────────────────────────────────────

_PARSE_SPEC_PROMPT = """\
You are an API spec analyzer. Extract structured metadata from this markdown API specification.
Return ONLY valid JSON (no markdown fences, no explanation). The JSON must have these fields:

{{
  "name": "lowercase_snake_case name for the API (e.g. 'moltbook', 'stripe')",
  "description": "One-line description of what this API does",
  "base_url": "The base URL for all API calls",
  "auth_method": "bearer|api_key_header|api_key_query|none",
  "auth_header": "Header name (e.g. 'Authorization')",
  "auth_prefix": "Header value prefix (e.g. 'Bearer ')",
  "env_var_name": "Suggested env var name for the API key (e.g. 'MOLTBOOK_API_KEY')",
  "category": "social|finance|productivity|communication|research|developer|other",
  "version": "API version string",
  "rate_limits": {{"reads_per_min": 60, "writes_per_min": 30, "notes": "extra rules"}},
  "error_format": "Description of error response shape",
  "operations": [
    {{
      "name": "operation_name (snake_case, verb-noun)",
      "description": "What the user would say to trigger this",
      "method": "GET|POST|PUT|PATCH|DELETE",
      "path": "/endpoint/path",
      "params": {{"param_name": "type and description"}},
      "body_fields": {{"field_name": "type and description"}},
      "risk_level": "read|write|irreversible"
    }}
  ]
}}

RISK LEVEL RULES:
- "read": GET requests — listing, searching, fetching data
- "write": Creating, updating local data, subscribing, following
- "irreversible": Posting public content, sending messages, deleting data

Group related endpoints into 10-15 logical operations a user would invoke.
For example, GET /posts + GET /posts/{{id}} → "get_posts" operation.

API SPEC:
{spec_content}"""

_GENERATE_PLUGIN_PROMPT = """\
Generate a Python tool plugin for the Nova bot system.

CONSTRAINTS (violation = rejection):
1. NEVER use eval, exec, subprocess, __import__, os.system, compile, importlib
2. ONLY import from: aiohttp, json, logging, typing, re, time, urllib.parse
3. MUST start with these exact imports:
   from src.core.tools.base import BaseTool
   from src.core.types import ToolResult
4. MUST inherit from BaseTool
5. MUST return ToolResult(success=bool, output=str, error=str, metadata=dict)
6. Constructor takes api_key as kwarg (env var injected via manifest)
7. execute() dispatches on 'operation' parameter to private async methods
8. Shared async _request(method, path, **kw) helper handles auth headers + error parsing
9. Only 'operation' is required in to_anthropic_tool input_schema

ARCHITECTURE PATTERN (follow exactly):
- name = "{tool_name}"
- description includes what operations are available
- parameters dict with operation (enum of available ops) + other params
- execute(**kwargs) dispatches: if operation == "x": return await self._x(...)
- _request(method, path) handles: base URL, auth header, JSON parsing, rate limit errors

API SPECIFICATION:
{spec_json}

Generate TWO outputs separated by the exact marker "---MANIFEST---":

1. Complete tool.py Python code (no markdown fences)
2. Complete manifest.json content (no markdown fences)

The manifest must follow this schema:
{{
  "name": "{tool_name}",
  "version": "1.0",
  "description": "...",
  "class_name": "TheClassName",
  "module_file": "tool.py",
  "risk_map": {{"operation_name": "read|write|irreversible", "_default": "write"}},
  "env_vars": ["{env_var}"],
  "constructor_args": {{"api_key": "{env_var}"}},
  "safe_readonly": false,
  "persona": "researcher|communicator|content_writer|scheduler|operator",
  "dependencies": ["aiohttp"]
}}"""


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class ParsedSpec:
    """Structured representation of a parsed API spec."""

    name: str
    description: str
    base_url: str
    auth_method: str = "bearer"
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    env_var_name: str = ""
    category: str = "other"
    version: str = "1.0"
    rate_limits: Dict[str, Any] = field(default_factory=dict)
    error_format: str = ""
    operations: List[Dict[str, Any]] = field(default_factory=list)
    risk_map: Dict[str, str] = field(default_factory=dict)


@dataclass
class SkillMetadata:
    """Provenance tracking for a learned skill."""

    name: str
    source_url: str
    learned_at: str
    spec_version: str
    plugin_dir: str
    status: str  # "active" | "pending_env" | "validation_failed" | "reload_failed"
    env_vars_needed: List[str]
    description: str


# ── Main class ───────────────────────────────────────────────────────────

class SkillLearner:
    """Acquires new tool capabilities from markdown API specification files.

    Pipeline: fetch → parse → generate → validate → write → reload.
    Output: a plugin folder (tool.py + manifest.json) in plugins/.
    Leverages existing PluginLoader for zero-hub-edit integration.
    """

    def __init__(self, gemini_client=None, plugin_loader=None, credential_store=None):
        """
        Args:
            gemini_client: GeminiClient for spec parsing + code generation.
            plugin_loader: PluginLoader instance for hot-reload after generation.
            credential_store: NovaCredentialStore for saving obtained API keys.
        """
        self.llm = gemini_client
        self.plugin_loader = plugin_loader
        self.credential_store = credential_store
        self._plugins_dir = _PLUGINS_DIR
        self._learned_skills: Dict[str, SkillMetadata] = {}
        self._load_skill_metadata()

    # ── Public API ───────────────────────────────────────────────────────

    async def learn_from_url(self, url: str, user_instructions: str = "") -> Tuple[bool, str]:
        """Learn a new skill from a .md spec URL.

        Full pipeline: fetch → parse → generate → validate → write → reload.

        Args:
            url: HTTPS URL to a .md API spec file
            user_instructions: Optional instructions from user (e.g., "as NovaBotNonHuman",
                "register with name CoolBot", "use description: AI agent for Srinath")

        Returns:
            (success, human-readable message)
        """
        if not self.llm or not getattr(self.llm, "enabled", False):
            return False, "Skill learning requires Gemini. Not available right now."

        # Step 1: Fetch
        ok, content = await self._fetch_spec(url)
        if not ok:
            return False, f"Could not fetch spec: {content}"
        logger.info(f"SkillLearner: fetched spec from {url} ({len(content)} chars)")

        # Step 2: Parse
        ok, parsed = await self._parse_spec(content)
        if not ok:
            return False, f"Could not parse spec: {parsed}"
        logger.info(f"SkillLearner: parsed spec → {parsed.name} ({len(parsed.operations)} operations)")

        # Step 3: Generate tool.py + manifest.json
        ok, generated = await self._generate_plugin(parsed)
        if not ok:
            return False, f"Code generation failed: {generated}"

        tool_code = generated["tool_code"]
        manifest = generated["manifest"]
        logger.info(f"SkillLearner: generated plugin code ({len(tool_code)} chars)")

        # Step 4: Validate
        ok, reason = self._validate_code(tool_code)
        if not ok:
            self._store_metadata(SkillMetadata(
                name=parsed.name, source_url=url,
                learned_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                spec_version=parsed.version,
                plugin_dir=str(self._plugins_dir / parsed.name),
                status="validation_failed",
                env_vars_needed=manifest.get("env_vars", []),
                description=parsed.description,
            ))
            return False, f"Generated code failed safety check: {reason}"
        logger.info("SkillLearner: code passed safety validation")

        # Step 5: Write plugin
        ok, msg = self._write_plugin(parsed.name, tool_code, manifest)
        if not ok:
            return False, msg

        # Step 6: Check env vars (credential store → os.getenv)
        env_vars = manifest.get("env_vars", [])
        missing_env = [
            v for v in env_vars
            if not (self.credential_store and self.credential_store.resolve(v))
            and not os.getenv(v)
        ]

        # Step 6b: LLM reads the spec, figures out registration, executes it
        if missing_env and self.credential_store and self.llm:
            reg_result = await self._try_self_registration(parsed, missing_env, content, user_instructions=user_instructions)
            if reg_result:
                # Re-check after registration saved credentials
                missing_env = [
                    v for v in env_vars
                    if not self.credential_store.resolve(v) and not os.getenv(v)
                ]

        if missing_env:
            status = "pending_env"
            env_msg = (
                "\n\nTo activate it, set these environment variables:\n"
                + "\n".join(f"  - {v}" for v in missing_env)
                + "\n\nThen say 'reload plugins'."
                + "\n\nOr retry with a custom name: "
                + "'learn this skill: " + url + " as YourPreferredName'"
            )
        else:
            status = "active"
            env_msg = ""

        # Step 7: Hot-reload if env vars present
        reload_msg = ""
        if status == "active" and self.plugin_loader:
            try:
                ok, reload_msg = self.plugin_loader.reload_plugin(
                    parsed.name, self._get_registry()
                )
                if not ok:
                    status = "reload_failed"
                    reload_msg = f"\nPlugin saved but reload failed: {reload_msg}"
                else:
                    logger.info(f"SkillLearner: plugin {parsed.name} hot-reloaded successfully")
            except Exception as e:
                status = "reload_failed"
                reload_msg = f"\nPlugin saved but reload failed: {e}"

        # Step 8: Store metadata
        metadata = SkillMetadata(
            name=parsed.name,
            source_url=url,
            learned_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            spec_version=parsed.version,
            plugin_dir=str(self._plugins_dir / parsed.name),
            status=status,
            env_vars_needed=env_vars,
            description=parsed.description,
        )
        self._store_metadata(metadata)

        # Build response
        if status == "active":
            return True, (
                f"Learned the {parsed.name} skill! ({parsed.description})\n\n"
                f"It's already active — try asking me to use {parsed.name}."
            )
        elif status == "pending_env":
            return True, (
                f"Learned the {parsed.name} skill and saved the plugin. "
                f"({parsed.description}){env_msg}"
            )
        else:
            return False, (
                f"Skill {parsed.name} saved but failed to activate.{reload_msg}"
            )

    def get_learned_skills(self) -> List[SkillMetadata]:
        """Return metadata for all learned skills."""
        return list(self._learned_skills.values())

    def get_skill_status(self, name: str) -> Optional[SkillMetadata]:
        """Get status of a specific learned skill."""
        return self._learned_skills.get(name.strip().lower())

    # ── Pipeline Steps ───────────────────────────────────────────────────

    async def _fetch_spec(self, url: str) -> Tuple[bool, str]:
        """Fetch a .md spec from URL. HTTPS only. Max 512KB."""
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https":
            return False, "Only HTTPS URLs are supported for security"
        if not parsed_url.path.endswith((".md", ".markdown", ".json")):
            return False, "URL must point to a .md or .json file"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                    headers={"User-Agent": "Nova-SkillLearner/1.0"},
                ) as resp:
                    if resp.status != 200:
                        return False, f"HTTP {resp.status}"
                    content = await resp.text()
                    if len(content) > _MAX_SPEC_SIZE:
                        return False, f"Spec too large ({len(content)} bytes, max {_MAX_SPEC_SIZE})"
                    return True, content
        except aiohttp.ClientError as e:
            return False, f"Network error: {e}"
        except Exception as e:
            return False, f"Fetch failed: {e}"

    async def _parse_spec(self, markdown: str) -> Tuple[bool, Any]:
        """Parse markdown spec into ParsedSpec using Gemini Flash.

        Returns:
            (success, ParsedSpec or error_string)
        """
        prompt = _PARSE_SPEC_PROMPT.format(spec_content=markdown[:8000])

        try:
            resp = await asyncio.wait_for(
                self.llm.create_message(
                    model="gemini/gemini-2.0-flash",
                    messages=[{"role": "user", "content": prompt}],
                    system="You are an API spec analyzer. Output only valid JSON.",
                    max_tokens=2000,
                ),
                timeout=15.0,
            )
            text = ""
            if hasattr(resp, 'content') and resp.content:
                for block in resp.content:
                    if hasattr(block, 'text'):
                        text += block.text
            elif isinstance(resp, str):
                text = resp.strip()
            elif isinstance(resp, dict):
                text = resp.get("text", "").strip()
            text = text.strip()

            # Strip markdown fences
            text = text.replace("```json", "").replace("```", "").strip()

            data = json.loads(text)
            if not isinstance(data, dict) or "name" not in data:
                return False, "LLM returned invalid spec structure"

            # Build risk_map from operations
            risk_map = {"_default": "write"}
            for op in data.get("operations", []):
                if "name" in op and "risk_level" in op:
                    risk_map[op["name"]] = op["risk_level"]

            spec = ParsedSpec(
                name=data["name"].lower().replace("-", "_").replace(" ", "_"),
                description=data.get("description", ""),
                base_url=data.get("base_url", ""),
                auth_method=data.get("auth_method", "bearer"),
                auth_header=data.get("auth_header", "Authorization"),
                auth_prefix=data.get("auth_prefix", "Bearer "),
                env_var_name=data.get("env_var_name", f"{data['name'].upper()}_API_KEY"),
                category=data.get("category", "other"),
                version=data.get("version", "1.0"),
                rate_limits=data.get("rate_limits", {}),
                error_format=data.get("error_format", ""),
                operations=data.get("operations", []),
                risk_map=risk_map,
            )
            return True, spec

        except asyncio.TimeoutError:
            return False, "Spec parsing timed out"
        except json.JSONDecodeError as e:
            return False, f"LLM returned invalid JSON: {e}"
        except Exception as e:
            return False, f"Spec parsing failed: {e}"

    async def _generate_plugin(self, spec: ParsedSpec) -> Tuple[bool, Any]:
        """Generate tool.py + manifest.json from ParsedSpec using Gemini Flash.

        Returns:
            (success, {"tool_code": str, "manifest": dict} or error_string)
        """
        spec_json = json.dumps({
            "name": spec.name,
            "description": spec.description,
            "base_url": spec.base_url,
            "auth_method": spec.auth_method,
            "auth_header": spec.auth_header,
            "auth_prefix": spec.auth_prefix,
            "env_var_name": spec.env_var_name,
            "operations": spec.operations[:20],  # cap for token safety
            "rate_limits": spec.rate_limits,
            "error_format": spec.error_format,
        }, indent=2)

        prompt = _GENERATE_PLUGIN_PROMPT.format(
            tool_name=spec.name,
            env_var=spec.env_var_name,
            spec_json=spec_json,
        )

        try:
            resp = await asyncio.wait_for(
                self.llm.create_message(
                    model="gemini/gemini-2.0-flash",
                    messages=[{"role": "user", "content": prompt}],
                    system=(
                        "You are a Python code generator for the Nova bot plugin system. "
                        "Output only code and JSON, no explanations."
                    ),
                    max_tokens=4000,
                ),
                timeout=30.0,
            )
            text = ""
            if hasattr(resp, 'content') and resp.content:
                for block in resp.content:
                    if hasattr(block, 'text'):
                        text += block.text
            elif isinstance(resp, str):
                text = resp.strip()
            elif isinstance(resp, dict):
                text = resp.get("text", "").strip()
            text = text.strip()

            # Split on manifest marker
            if "---MANIFEST---" not in text:
                return False, "LLM output missing ---MANIFEST--- separator"

            parts = text.split("---MANIFEST---", 1)
            tool_code = parts[0].strip()
            manifest_text = parts[1].strip()

            # Clean up markdown fences from both parts
            tool_code = re.sub(r'^```(?:python)?\s*\n?', '', tool_code)
            tool_code = re.sub(r'\n?```\s*$', '', tool_code)
            manifest_text = re.sub(r'^```(?:json)?\s*\n?', '', manifest_text)
            manifest_text = re.sub(r'\n?```\s*$', '', manifest_text)

            if len(tool_code) < 100:
                return False, "Generated tool code too short"

            # Parse manifest JSON
            try:
                manifest = json.loads(manifest_text)
            except json.JSONDecodeError as e:
                # Fallback: build manifest from spec
                manifest = self._build_fallback_manifest(spec)

            # Ensure manifest has required fields
            manifest.setdefault("name", spec.name)
            manifest.setdefault("class_name", f"{spec.name.title().replace('_', '')}Tool")
            manifest.setdefault("module_file", "tool.py")
            manifest.setdefault("risk_map", spec.risk_map)
            manifest.setdefault("env_vars", [spec.env_var_name] if spec.env_var_name else [])
            manifest.setdefault("constructor_args", {"api_key": spec.env_var_name} if spec.env_var_name else {})
            manifest.setdefault("safe_readonly", False)
            manifest.setdefault("dependencies", ["aiohttp"])

            return True, {"tool_code": tool_code, "manifest": manifest}

        except asyncio.TimeoutError:
            return False, "Code generation timed out"
        except Exception as e:
            return False, f"Code generation failed: {e}"

    def _validate_code(self, code: str) -> Tuple[bool, str]:
        """AST-level safety validation of generated Python code.

        Rejects code containing eval, exec, subprocess, __import__,
        os.system, and other dangerous constructs.

        Returns:
            (safe, reason)
        """
        # Step 1: Parse AST
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"Syntax error in generated code: {e}"

        # Step 2: Walk AST for banned constructs
        for node in ast.walk(tree):
            # Check function calls
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in _BANNED_CALLS:
                    return False, f"Banned function call: {func.id}()"
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    full = f"{func.value.id}.{func.attr}"
                    if func.value.id in _BANNED_MODULES:
                        return False, f"Banned module call: {full}()"
                    if full in ("os.system", "os.popen", "os.exec"):
                        return False, f"Banned call: {full}()"

            # Check imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if mod in _BANNED_MODULES:
                        return False, f"Banned import: {alias.name}"

            if isinstance(node, ast.ImportFrom):
                if node.module:
                    mod = node.module.split(".")[0]
                    if mod in _BANNED_MODULES:
                        return False, f"Banned import from: {node.module}"

        # Step 3: String-level backup scan
        code_lower = code.lower()
        for pattern in _BANNED_STRINGS:
            if pattern.lower() in code_lower:
                # Allow "from src.core.tools.base import BaseTool" etc.
                if pattern == "subprocess" and "subprocess" not in code:
                    continue
                return False, f"Banned pattern in code: {pattern}"

        return True, "Code passes safety validation"

    def _write_plugin(
        self, name: str, tool_code: str, manifest: dict
    ) -> Tuple[bool, str]:
        """Write generated tool.py + manifest.json to plugins/{name}/."""
        plugin_dir = self._plugins_dir / name
        plugin_dir.mkdir(parents=True, exist_ok=True)

        try:
            tool_path = plugin_dir / "tool.py"
            tool_path.write_text(tool_code, encoding="utf-8")

            manifest_path = plugin_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=4, default=str), encoding="utf-8"
            )

            logger.info(f"SkillLearner: plugin written to {plugin_dir}")
            return True, f"Plugin written to {plugin_dir}"
        except Exception as e:
            return False, f"Failed to write plugin: {e}"

    def _get_registry(self):
        """Get the ToolRegistry that owns this PluginLoader.

        Walks back from plugin_loader to find its parent registry.
        Returns None if not found (reload will be skipped).
        """
        # The registry stores _plugin_loader; we need the reverse reference.
        # Since we can't easily do this, we pass registry through reload_plugin.
        # The caller (learn_from_url) gets registry from SkillTool.
        # For now, return None — the SkillTool overrides this.
        return None

    # ── Credential Management ─────────────────────────────────────────────

    def save_credential(self, key: str, value: str, source: str = "manual"):
        """Save a credential to the store. Called by tools or agent."""
        if self.credential_store:
            self.credential_store.set(key, value, source=source)
            return True
        return False

    async def _try_self_registration(
        self, spec: ParsedSpec, missing_env: List[str], raw_spec: str,
        user_instructions: str = "",
    ) -> bool:
        """Use LLM to read the spec, understand registration, and execute it.

        Instead of keyword matching, asks Gemini to read the raw API spec and
        figure out: (1) is there a registration endpoint? (2) what URL/method/body?
        (3) which response field contains the API key?

        Then executes the registration call and saves the credential.

        Returns True if credential was obtained and saved.
        """
        bot_name = os.getenv("BOT_NAME", "Nova")
        owner_name = os.getenv("OWNER_NAME", "User")

        # Ask LLM to analyze the spec and produce an executable registration plan
        prompt = (
            f"You are an AI agent named {bot_name}. You need to register yourself on this API.\n\n"
            f"Read this API specification and determine:\n"
            f"1. Is there a registration/signup endpoint that does NOT require authentication?\n"
            f"2. If yes, what is the exact URL, HTTP method, and JSON body to send?\n"
            f"3. Which field in the response contains the API key/token?\n\n"
            f"My details for registration:\n"
            f"- name: {bot_name}\n"
            f"- description: AI assistant for {owner_name}. Autonomous agent powered by Claude.\n\n"
            + (f"USER INSTRUCTIONS (override defaults above): {user_instructions}\n\n" if user_instructions else "")
            + f"API BASE URL: {spec.base_url}\n\n"
            f"API SPEC:\n{raw_spec[:6000]}\n\n"
            f"Return ONLY valid JSON (no markdown fences). If no registration endpoint exists, "
            f"return: {{\"can_register\": false}}\n"
            f"If registration is possible, return:\n"
            f"{{\n"
            f'  "can_register": true,\n'
            f'  "url": "full URL to call",\n'
            f'  "method": "POST",\n'
            f'  "headers": {{"Content-Type": "application/json"}},\n'
            f'  "body": {{...exact JSON body to send...}},\n'
            f'  "api_key_field": "field name in response that contains the API key (e.g. api_key, token)"\n'
            f"}}"
        )

        try:
            resp = await asyncio.wait_for(
                self.llm.create_message(
                    model="gemini/gemini-2.0-flash",
                    messages=[{"role": "user", "content": prompt}],
                    system="You are a precise API integration assistant. Output only valid JSON.",
                    max_tokens=1000,
                ),
                timeout=15.0,
            )
            text = ""
            if hasattr(resp, 'content') and resp.content:
                for block in resp.content:
                    if hasattr(block, 'text'):
                        text += block.text
            text = text.strip().replace("```json", "").replace("```", "").strip()

            plan = json.loads(text)
            if not plan.get("can_register"):
                logger.info(f"SkillLearner: LLM says no registration endpoint for {spec.name}")
                return False

            reg_url = plan["url"]
            method = plan.get("method", "POST").upper()
            headers = plan.get("headers", {"Content-Type": "application/json"})
            body = plan.get("body", {})
            api_key_field = plan.get("api_key_field", "api_key")

            logger.info(
                f"SkillLearner: LLM registration plan for {spec.name} → "
                f"{method} {reg_url} body_keys={list(body.keys())} "
                f"api_key_field={api_key_field}"
            )

        except Exception as e:
            logger.warning(f"SkillLearner: LLM registration analysis failed: {e}")
            return False

        # Execute registration with LLM-driven retry on failure
        max_attempts = 3  # Only count non-429 failures
        attempt = 0
        while attempt < max_attempts:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method=method,
                        url=reg_url,
                        json=body if body else None,
                        timeout=aiohttp.ClientTimeout(total=15),
                        headers=headers,
                    ) as resp:
                        resp_text = await resp.text()
                        logger.info(
                            f"SkillLearner: registration attempt {attempt+1} "
                            f"response {resp.status}: {resp_text[:500]}"
                        )

                        if resp.status in (200, 201):
                            try:
                                data = json.loads(resp_text)
                            except json.JSONDecodeError:
                                logger.warning("Registration response is not JSON")
                                return False

                            api_key = self._extract_api_key(data, api_key_field)

                            if api_key and spec.env_var_name:
                                self.credential_store.set(
                                    spec.env_var_name,
                                    str(api_key),
                                    source=f"self_registration:{spec.name}",
                                )
                                logger.info(
                                    f"SkillLearner: self-registered on {spec.name}, "
                                    f"saved {spec.env_var_name}"
                                )
                                return True
                            else:
                                logger.info(
                                    f"Registration succeeded but no API key found. "
                                    f"Looked for '{api_key_field}' in: {list(data.keys())}"
                                )
                                return False

                        # Rate limited — wait the actual retry_after, then retry
                        # (does NOT count as an attempt — not our fault)
                        if resp.status == 429:
                            try:
                                err = json.loads(resp_text)
                                wait = int(err.get("retry_after_seconds", 60))
                            except Exception:
                                wait = 60
                            # Cap at 10 min; if longer, give up gracefully
                            if wait > 600:
                                logger.info(
                                    f"SkillLearner: rate limit too long ({wait}s), "
                                    f"will retry on next skill learn"
                                )
                                return False
                            logger.info(
                                f"SkillLearner: rate limited, waiting {wait}s "
                                f"(resets at {err.get('reset_at', 'unknown')})"
                            )
                            await asyncio.sleep(wait)
                            continue  # Retry same request — don't increment attempt

                        # Non-success — ask LLM to analyze and retry
                        attempt += 1
                        if attempt < max_attempts:
                            retry = await self._llm_analyze_failure(
                                spec, reg_url, method, body, resp.status,
                                resp_text, api_key_field, raw_spec,
                            )
                            if retry:
                                reg_url = retry.get("url", reg_url)
                                method = retry.get("method", method).upper()
                                body = retry.get("body", body)
                                headers = retry.get("headers", headers)
                                api_key_field = retry.get("api_key_field", api_key_field)
                                logger.info(
                                    f"SkillLearner: LLM retry plan → "
                                    f"{method} {reg_url} body={json.dumps(body)[:200]}"
                                )
                                await asyncio.sleep(2)  # Brief pause before retry
                                continue
                            else:
                                logger.info("SkillLearner: LLM says no retry possible")
                                return False
                        else:
                            logger.info(
                                f"Self-registration failed after {max_attempts} attempts"
                            )
            except Exception as e:
                logger.warning(f"Self-registration request failed: {e}")
                attempt += 1

        return False

    async def _llm_analyze_failure(
        self, spec: ParsedSpec, url: str, method: str,
        body: dict, status: int, response_text: str,
        api_key_field: str, raw_spec: str,
    ) -> Optional[dict]:
        """Ask LLM to analyze a failed registration and produce a new plan.

        Handles: 409 name taken (retry with variant), 400 missing fields,
        422 validation errors, etc. Returns new request plan or None to stop.
        """
        bot_name = os.getenv("BOT_NAME", "Nova")
        owner_name = os.getenv("OWNER_NAME", "User")

        prompt = (
            f"You are an AI agent named {bot_name}. You tried to register on an API but it failed.\n\n"
            f"FAILED REQUEST:\n"
            f"  {method} {url}\n"
            f"  Body: {json.dumps(body)}\n\n"
            f"ERROR RESPONSE:\n"
            f"  Status: {status}\n"
            f"  Body: {response_text[:1000]}\n\n"
            f"API SPEC (for reference):\n{raw_spec[:4000]}\n\n"
            f"Analyze the error and decide the BEST recovery strategy:\n"
            f"1. If name/username is already taken → FIRST check the API spec for a "
            f"login, key recovery, or key rotation endpoint that could retrieve the existing key. "
            f"If found, return that request instead. If no recovery endpoint exists, "
            f"retry registration with a variant name "
            f"(e.g., '{bot_name}_agent', '{bot_name}_{owner_name}', '{bot_name}_bot')\n"
            f"2. If a required field is missing → add it\n"
            f"3. If the error is unrecoverable (auth required, service down) → give up\n\n"
            f"Return ONLY valid JSON (no markdown fences).\n"
            f"If you can retry or recover, return the NEW request:\n"
            f'{{"retry": true, "url": "...", "method": "POST", '
            f'"headers": {{"Content-Type": "application/json"}}, '
            f'"body": {{...new body...}}, "api_key_field": "{api_key_field}"}}\n\n'
            f"If unrecoverable, return: {{\"retry\": false, \"reason\": \"why\"}}"
        )

        try:
            resp = await asyncio.wait_for(
                self.llm.create_message(
                    model="gemini/gemini-2.0-flash",
                    messages=[{"role": "user", "content": prompt}],
                    system="You are a precise API integration assistant. Output only valid JSON.",
                    max_tokens=800,
                ),
                timeout=15.0,
            )
            text = ""
            if hasattr(resp, 'content') and resp.content:
                for block in resp.content:
                    if hasattr(block, 'text'):
                        text += block.text
            text = text.strip().replace("```json", "").replace("```", "").strip()

            result = json.loads(text)
            if result.get("retry"):
                return result
            else:
                logger.info(
                    f"SkillLearner: LLM says no retry — {result.get('reason', 'unknown')}"
                )
                return None

        except Exception as e:
            logger.warning(f"SkillLearner: LLM failure analysis failed: {e}")
            return None

    @staticmethod
    def _extract_api_key(data: dict, field_hint: str) -> Optional[str]:
        """Extract API key from response using LLM hint + fallback search."""
        # Direct field
        if field_hint in data:
            return str(data[field_hint])

        # Nested under common wrappers
        for wrapper in ("data", "result", "response", "agent"):
            nested = data.get(wrapper)
            if isinstance(nested, dict) and field_hint in nested:
                return str(nested[field_hint])

        # Fallback: search common key names at any level
        for key in ("api_key", "apiKey", "token", "access_token", "key", "secret"):
            if key in data:
                return str(data[key])
            for wrapper in ("data", "result", "response", "agent"):
                nested = data.get(wrapper)
                if isinstance(nested, dict) and key in nested:
                    return str(nested[key])

        return None

    # ── Metadata persistence ─────────────────────────────────────────────

    def _store_metadata(self, metadata: SkillMetadata):
        """Persist skill provenance to data/skills/{name}.json."""
        _SKILLS_DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = _SKILLS_DATA_DIR / f"{metadata.name}.json"
        try:
            path.write_text(json.dumps({
                "name": metadata.name,
                "source_url": metadata.source_url,
                "learned_at": metadata.learned_at,
                "spec_version": metadata.spec_version,
                "plugin_dir": metadata.plugin_dir,
                "status": metadata.status,
                "env_vars_needed": metadata.env_vars_needed,
                "description": metadata.description,
            }, indent=2))
        except Exception as e:
            logger.warning(f"SkillLearner: failed to store metadata: {e}")
        self._learned_skills[metadata.name] = metadata

    def _load_skill_metadata(self):
        """Load previously learned skills from data/skills/."""
        if not _SKILLS_DATA_DIR.exists():
            return
        for f in _SKILLS_DATA_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                self._learned_skills[data["name"]] = SkillMetadata(**data)
            except Exception:
                pass

    # ── Helpers ──────────────────────────────────────────────────────────

    def _build_fallback_manifest(self, spec: ParsedSpec) -> dict:
        """Build a manifest dict from ParsedSpec when LLM manifest fails."""
        class_name = "".join(w.title() for w in spec.name.split("_")) + "Tool"
        return {
            "name": spec.name,
            "version": "1.0",
            "description": spec.description,
            "class_name": class_name,
            "module_file": "tool.py",
            "risk_map": spec.risk_map,
            "env_vars": [spec.env_var_name] if spec.env_var_name else [],
            "constructor_args": {"api_key": spec.env_var_name} if spec.env_var_name else {},
            "safe_readonly": False,
            "persona": self._infer_persona(spec.category),
            "dependencies": ["aiohttp"],
        }

    @staticmethod
    def _infer_persona(category: str) -> str:
        """Map API category to Nova persona."""
        return {
            "social": "communicator",
            "communication": "communicator",
            "finance": "researcher",
            "research": "researcher",
            "productivity": "operator",
            "developer": "operator",
        }.get(category, "operator")
