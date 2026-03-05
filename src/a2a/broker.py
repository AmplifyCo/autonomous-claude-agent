"""AgentBroker — discovers agents, matches capabilities, tracks reliability.

Decides which external agent to delegate to based on capability tags
and historical reliability scores. Supports multi-agent orchestration:
race (first success wins) and fan-out (merge all results).
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .client import A2AClient

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "known_agents.json"
_RELIABILITY_PATH = Path("./data/agent_reliability.json")

# Auto-disable agents below this score (after minimum interactions)
_MIN_RELIABILITY = 0.3
_MIN_INTERACTIONS = 5


class AgentBroker:
    """Discovers agents, matches capabilities, tracks reliability.

    Used by GoalDecomposer (to list available agents for the planner)
    and TaskRunner (to delegate subtasks to external agents).
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        credential_resolver: Optional[Callable] = None,
    ):
        self._config_path = config_path or _DEFAULT_CONFIG
        self._client = A2AClient(credential_resolver=credential_resolver)
        self._agents: Dict[str, dict] = {}
        self._reliability: Dict[str, dict] = {}
        self._load_config()
        self._load_reliability()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self):
        """Load known agents from config/known_agents.json."""
        if not self._config_path.exists():
            logger.info("AgentBroker: no known_agents.json found — no delegation available")
            return
        try:
            with open(self._config_path, "r") as f:
                data = json.load(f)
            self._agents = data.get("agents", {})
            enabled = sum(1 for a in self._agents.values() if a.get("enabled", True))
            logger.info(f"AgentBroker: loaded {len(self._agents)} agents ({enabled} enabled)")
        except Exception as e:
            logger.error(f"AgentBroker: failed to load config: {e}")

    def _save_config(self):
        """Persist known_agents.json (used when discover() adds a new agent)."""
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_path, "w") as f:
                json.dump({"agents": self._agents}, f, indent=2)
        except Exception as e:
            logger.error(f"AgentBroker: failed to save config: {e}")

    # ── Reliability ───────────────────────────────────────────────────────────

    def _load_reliability(self):
        """Load reliability scores from data/agent_reliability.json."""
        if not _RELIABILITY_PATH.exists():
            return
        try:
            with open(_RELIABILITY_PATH, "r") as f:
                self._reliability = json.load(f)
        except Exception as e:
            logger.debug(f"AgentBroker: reliability load failed: {e}")

    def _save_reliability(self):
        """Persist reliability scores."""
        try:
            _RELIABILITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_RELIABILITY_PATH, "w") as f:
                json.dump(self._reliability, f, indent=2)
        except Exception as e:
            logger.error(f"AgentBroker: reliability save failed: {e}")

    def update_reliability(self, agent_name: str, success: bool):
        """Update reliability score after a delegation outcome.

        Score = successes / (successes + failures).
        After MIN_INTERACTIONS, agents below MIN_RELIABILITY are auto-disabled.
        """
        entry = self._reliability.setdefault(agent_name, {"successes": 0, "failures": 0, "score": 1.0})
        if success:
            entry["successes"] += 1
        else:
            entry["failures"] += 1
        total = entry["successes"] + entry["failures"]
        entry["score"] = entry["successes"] / total if total > 0 else 1.0

        # Auto-disable unreliable agents
        if total >= _MIN_INTERACTIONS and entry["score"] < _MIN_RELIABILITY:
            if agent_name in self._agents:
                self._agents[agent_name]["enabled"] = False
                self._save_config()
                logger.warning(
                    f"AgentBroker: auto-disabled '{agent_name}' "
                    f"(score={entry['score']:.2f} after {total} interactions)"
                )

        self._save_reliability()

    def _get_score(self, agent_name: str) -> float:
        """Get reliability score for an agent (default 1.0 for new agents)."""
        return self._reliability.get(agent_name, {}).get("score", 1.0)

    # ── Matching ──────────────────────────────────────────────────────────────

    def match(self, capability_tags: List[str]) -> List[dict]:
        """Return enabled agents matching any of the given capability tags.

        Sorted by reliability score (highest first).
        """
        if not capability_tags:
            return []

        tag_set = set(capability_tags)
        matches = []
        for name, agent in self._agents.items():
            if not agent.get("enabled", True):
                continue
            agent_caps = set(agent.get("capabilities", []))
            if agent_caps & tag_set:
                matches.append({**agent, "_name": name})

        matches.sort(key=lambda a: self._get_score(a["_name"]), reverse=True)
        return matches

    def select(self, subtask_description: str, tool_hints: List[str]) -> Optional[dict]:
        """Select the best agent for a subtask based on tool_hints as capability tags.

        Returns agent config dict (with _name) or None if no match.
        """
        matches = self.match(tool_hints)
        return matches[0] if matches else None

    # ── Delegation ────────────────────────────────────────────────────────────

    async def delegate(
        self,
        agent_config: dict,
        task_text: str,
        context_id: Optional[str] = None,
    ) -> str:
        """Send task to agent, poll until done, return result text.

        Updates reliability on success/failure.
        Raises on failure so TaskRunner can fall back to self-execute.
        """
        agent_name = agent_config.get("_name", agent_config.get("name", "unknown"))
        endpoint = agent_config["endpoint"]
        timeout = agent_config.get("timeout_seconds", 30)
        max_poll = agent_config.get("max_poll_seconds", 600)

        # Resolve auth token
        auth = agent_config.get("auth", {})
        raw_token = auth.get("token", "")
        token = self._client.resolve_token(raw_token)

        try:
            # 1. Send task
            result = await self._client.send_task(
                endpoint=endpoint,
                auth_token=token,
                task_text=task_text,
                context_id=context_id,
                timeout=timeout,
            )

            # 2. Extract task ID from response
            task_id = result.get("id")
            if not task_id:
                raise RuntimeError("No task ID in send_task response")

            # 3. Poll until done
            final = await self._client.poll_until_done(
                endpoint=endpoint,
                auth_token=token,
                task_id=task_id,
                poll_interval=5,
                max_wait=max_poll,
            )

            # 4. Check final state
            state = final.get("status", {}).get("state", "")
            if state == "completed":
                # Extract text from artifacts
                text_parts = []
                for artifact in final.get("artifacts", []):
                    for part in artifact.get("parts", []):
                        if part.get("kind") == "text" and part.get("text"):
                            text_parts.append(part["text"])
                result_text = "\n".join(text_parts)
                self.update_reliability(agent_name, success=True)
                logger.info(f"AgentBroker: delegation to '{agent_name}' succeeded ({len(result_text)} chars)")
                return result_text or "Delegated task completed (no output)"

            elif state == "failed":
                error_msg = final.get("status", {}).get("message", "Unknown failure")
                self.update_reliability(agent_name, success=False)
                raise RuntimeError(f"Agent '{agent_name}' returned FAILED: {error_msg}")

            elif state in ("canceled", "rejected"):
                self.update_reliability(agent_name, success=False)
                raise RuntimeError(f"Agent '{agent_name}' {state} the task")

            else:
                self.update_reliability(agent_name, success=False)
                raise RuntimeError(f"Agent '{agent_name}' ended in unexpected state: {state}")

        except TimeoutError:
            self.update_reliability(agent_name, success=False)
            raise RuntimeError(f"Delegation to '{agent_name}' timed out after {max_poll}s")
        except RuntimeError:
            raise  # Re-raise our own errors
        except Exception as e:
            self.update_reliability(agent_name, success=False)
            raise RuntimeError(f"Delegation to '{agent_name}' failed: {e}")

    # ── Multi-Agent Orchestration ────────────────────────────────────────────

    async def delegate_smart(
        self,
        subtask_description: str,
        tool_hints: List[str],
        delegate_to: str = "",
        context_id: Optional[str] = None,
        strategy: str = "auto",
    ) -> str:
        """Smart delegation — single or multi-agent based on available matches.

        Args:
            subtask_description: What the subtask needs to do
            tool_hints: Capability tags to match agents against
            delegate_to: Preferred agent name (gets priority)
            context_id: Parent task ID for correlation
            strategy: "auto" | "race" | "fan_out"

        Returns:
            Result text from the best agent

        Raises:
            RuntimeError: If no agents match or all fail
        """
        matches = self.match(tool_hints)

        # Prefer the named agent if specified and available
        if delegate_to:
            named = self._agents.get(delegate_to)
            if named and named.get("enabled", True):
                # Ensure named agent is first, deduplicate
                matches = [{**named, "_name": delegate_to}] + [
                    m for m in matches if m.get("_name") != delegate_to
                ]

        if not matches:
            raise RuntimeError(f"No agents available for capabilities: {tool_hints}")

        # Single agent — use existing delegate()
        if len(matches) == 1:
            return await self.delegate(matches[0], subtask_description, context_id)

        # Multi-agent — pick strategy
        if strategy == "auto":
            strategy = "race"  # Default: speed wins

        if strategy == "race":
            return await self._race(matches[:3], subtask_description, context_id)
        elif strategy == "fan_out":
            return await self._fan_out(matches[:3], subtask_description, context_id)
        else:
            return await self.delegate(matches[0], subtask_description, context_id)

    async def _race(
        self,
        agents: List[dict],
        task_text: str,
        context_id: Optional[str] = None,
    ) -> str:
        """Send task to all agents in parallel, return first successful result.

        Good for speed-critical commodity work — whoever finishes first wins.
        """
        logger.info(f"AgentBroker: racing {len(agents)} agents for: {task_text[:60]}")

        async def _try_one(agent_config: dict) -> Optional[str]:
            try:
                return await self.delegate(agent_config, task_text, context_id)
            except Exception as e:
                name = agent_config.get("_name", "unknown")
                logger.warning(f"AgentBroker: race participant '{name}' failed: {e}")
                return None

        results = await asyncio.gather(*[_try_one(a) for a in agents])
        for r in results:
            if r:
                return r
        raise RuntimeError(f"All {len(agents)} agents failed in race delegation")

    async def _fan_out(
        self,
        agents: List[dict],
        task_text: str,
        context_id: Optional[str] = None,
    ) -> str:
        """Send task to all agents, collect all results, merge with attribution.

        Good for research — multiple perspectives produce better synthesis.
        """
        logger.info(f"AgentBroker: fan-out to {len(agents)} agents for: {task_text[:60]}")

        async def _try_one(agent_config: dict) -> Optional[tuple]:
            name = agent_config.get("_name", "unknown")
            try:
                result = await self.delegate(agent_config, task_text, context_id)
                return (name, result)
            except Exception as e:
                logger.warning(f"AgentBroker: fan-out participant '{name}' failed: {e}")
                return None

        raw = await asyncio.gather(*[_try_one(a) for a in agents])
        results = [r for r in raw if r is not None]

        if not results:
            raise RuntimeError(f"All {len(agents)} agents failed in fan-out delegation")
        if len(results) == 1:
            return results[0][1]

        # Merge all results with source attribution
        merged = "MULTI-AGENT RESULTS:\n\n"
        for name, text in results:
            merged += f"--- From {name} ---\n{text}\n\n"
        return merged

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def discover(self, base_url: str, name: Optional[str] = None) -> Optional[dict]:
        """Fetch agent card from URL, add to known_agents if valid.

        Returns the agent card dict, or None on failure.
        """
        card = await self._client.fetch_agent_card(base_url)
        if not card:
            return None

        agent_name = name or card.get("name", "").lower().replace(" ", "-")
        if not agent_name:
            logger.warning("AgentBroker: discovered agent has no name")
            return None

        # Extract capabilities from skills
        capabilities = set()
        for skill in card.get("skills", []):
            capabilities.update(skill.get("tags", []))

        self._agents[agent_name] = {
            "name": card.get("name", agent_name),
            "endpoint": card.get("url", f"{base_url.rstrip('/')}/a2a"),
            "auth": {"type": "bearer", "token": ""},
            "capabilities": list(capabilities),
            "description": card.get("description", ""),
            "enabled": True,
            "timeout_seconds": 30,
            "max_poll_seconds": 600,
        }
        self._save_config()
        logger.info(f"AgentBroker: discovered and added '{agent_name}' ({len(capabilities)} capabilities)")
        return card

    # ── Status / Debug ────────────────────────────────────────────────────────

    def get_agents_for_prompt(self) -> str:
        """Format available agents for injection into the GoalDecomposer prompt.

        Returns a string like:
          research-bot (capabilities: research, web-search) — Specialized research agent
        Or "None (execute all steps locally)" if no agents configured.
        """
        lines = []
        for name, agent in self._agents.items():
            if not agent.get("enabled", True):
                continue
            caps = ", ".join(agent.get("capabilities", []))
            desc = agent.get("description", "")
            lines.append(f'  "{name}" (capabilities: {caps}) — {desc}')

        if not lines:
            return "None (execute all steps locally)"
        return "\n".join(lines)

    def get_status(self) -> dict:
        """Return all agents with their reliability scores (for dashboard/debug)."""
        status = {}
        for name, agent in self._agents.items():
            rel = self._reliability.get(name, {})
            status[name] = {
                "name": agent.get("name", name),
                "endpoint": agent.get("endpoint", ""),
                "enabled": agent.get("enabled", True),
                "capabilities": agent.get("capabilities", []),
                "reliability": {
                    "score": rel.get("score", 1.0),
                    "successes": rel.get("successes", 0),
                    "failures": rel.get("failures", 0),
                },
            }
        return status
