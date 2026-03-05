"""AgentCardBuilder — constructs the A2A Agent Card from static config + runtime data."""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "agent_card.json"


class AgentCardBuilder:
    """Builds the A2A Agent Card JSON served at /.well-known/agent-card.json.

    Static metadata (name, description, skills) comes from config/agent_card.json.
    Dynamic data (url, capabilities) is injected at runtime.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        base_url: str = "",
    ):
        self._config_path = config_path or _DEFAULT_CONFIG
        self._base_url = base_url.rstrip("/")
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

    def build(self) -> dict:
        """Build the full agent card, merging static config with runtime data."""
        cfg = self._static_config

        # Build skills list with required A2A fields
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
