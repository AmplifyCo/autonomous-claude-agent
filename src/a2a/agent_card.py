"""AgentCardBuilder — constructs the A2A Agent Card from static config + runtime data."""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "agent_card.json"


class AgentCardBuilder:
    """Builds the A2A Agent Card JSON served at /.well-known/agent-card.json.

    Static skills come from config/agent_card.json.
    Dynamic tools are discovered from ToolRegistry at build time (every request).
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        base_url: str = "",
        tool_registry=None,
    ):
        self._config_path = config_path or _DEFAULT_CONFIG
        self._base_url = base_url.rstrip("/")
        self._registry = tool_registry
        self._static_config = self._load_config()

    def _load_config(self) -> dict:
        """Load static agent card config from JSON file."""
        if not self._config_path.exists():
            logger.warning(f"Agent card config not found at {self._config_path}")
            return {}
        try:
            with open(self._config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load agent card config: {e}")
            return {}

    def _get_dynamic_tools(self) -> list:
        """Discover tools from ToolRegistry and convert to A2A skill format.

        Groups tools by source: native tools, plugins, MCP servers.
        Respects hidden_tools list from config — those tools are not exposed.
        """
        if not self._registry:
            return []

        hidden = set(self._static_config.get("hidden_tools", []))
        skills = []
        seen_ids = set()

        for name, tool in self._registry.tools.items():
            if name in hidden:
                continue
            skill_id = f"tool-{name}"
            if skill_id in seen_ids:
                continue
            seen_ids.add(skill_id)

            # Determine source tag
            tags = ["tool"]
            if name.startswith("mcp__") or "__" in name:
                tags.append("mcp")
                server = name.split("__")[0]
                tags.append(server)
            elif hasattr(tool, '_plugin_source'):
                tags.append("plugin")
            else:
                tags.append("native")

            description = getattr(tool, 'description', f"Tool: {name}")

            skills.append({
                "id": skill_id,
                "name": name,
                "description": description,
                "tags": tags,
                "examples": [],
                "inputModes": ["text"],
                "outputModes": ["text"],
            })

        return skills

    def build(self) -> dict:
        """Build the full agent card, merging static config with dynamic tools."""
        cfg = self._static_config

        # Static skills from config
        skills = []
        for skill in cfg.get("skills", []):
            skills.append({
                "id": skill.get("id", ""),
                "name": skill.get("name", ""),
                "description": skill.get("description", ""),
                "tags": skill.get("tags", []),
                "examples": skill.get("examples", []),
                "inputModes": ["text"],
                "outputModes": ["text"],
            })

        # Dynamic tools from registry
        dynamic = self._get_dynamic_tools()
        if dynamic:
            skills.extend(dynamic)

        return {
            "name": cfg.get("name", "Nova"),
            "description": cfg.get("description", "AI agent"),
            "version": cfg.get("version", "1.0.0"),
            "url": f"{self._base_url}/a2a",
            "protocolVersion": "0.2.0",
            "preferredTransport": "JSONRPC",
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
            },
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "provider": cfg.get("provider", {}),
            "skills": skills,
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                }
            },
        }
