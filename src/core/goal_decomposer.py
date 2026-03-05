"""Goal decomposer — breaks a high-level goal into concrete, executable subtasks.

Uses Gemini Flash for cheap, fast structured planning before execution starts.

Design principles (from AOP research + Voyager):
  - Solvability: each subtask must be independently executable with available tools
  - Completeness: subtasks together fully accomplish the goal
  - Non-redundancy: no overlapping subtasks
  - Bounded: max 7 subtasks to prevent over-decomposition
  - Synthesis-last: final subtask always aggregates and writes a file
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from .task_queue import Subtask

logger = logging.getLogger(__name__)

_BOT_NAME = os.getenv("BOT_NAME", "Nova")

# Note: uses .format() at runtime — {bot_name}, {tools}, {goal}, {task_id} are runtime variables
_DECOMPOSE_PROMPT = """You are a task planner for an autonomous AI agent named {bot_name}.
Your job: break a high-level goal into 3–7 subtasks with explicit dependencies so independent work runs in parallel.

RULES:
1. Each subtask must be independently executable — it must have a clear single output
2. No redundant subtasks — don't repeat the same search with different wording
3. Max 7 subtasks total
4. Steps are 0-indexed. Use depends_on to declare which steps must finish first.
   - Steps with no dependencies (depends_on: []) run immediately
   - Steps with the same dependency set run in parallel automatically
   - Example: A(0)→B(1),C(2) in parallel→D(3) means B and C both have depends_on:[0], D has depends_on:[1,2]
5. The LAST subtask must always be a synthesis step: "Compile all findings into a file at ./data/tasks/{task_id}.txt and summarize in 3 bullet points"
6. Use ONLY tools from the Available Tools list
7. Assign model_tier: "flash" for searches/reads, "sonnet" for synthesis/writing
8. For each subtask set verification_criteria: a one-sentence test for how to confirm it succeeded (e.g. "Search returns ≥3 results", "File exists at path", "Tweet ID is returned")
9. Set reversible: false for subtasks that CANNOT be undone (sending email, posting to X, deleting files, making purchases). Set true for reads, searches, writes to local files.
10. EISENHOWER PRIORITY: Classify each subtask into a quadrant:
    - "q1" (DO) — urgent + important: identity-bound, security-sensitive, time-critical → execution_mode: "self"
    - "q2" (SCHEDULE) — important but not urgent: voice/brand work, synthesis, quality review → execution_mode: "self"
    - "q3" (DELEGATE) — urgent but routine: research, data gathering, bulk analysis → execution_mode: "delegate"
    - "q4" (ELIMINATE/DELEGATE) — neither urgent nor important: boilerplate, generic lookups → execution_mode: "delegate" or skip

    SELF-EXECUTE when: needs owner's voice/tone, uses identity tools (email, linkedin, calendar, contacts), is the final synthesis step, or is a quality review.
    DELEGATE when: commodity research, specialized analysis we can't do better, parallelizable grunt work, no identity needed.
11. If execution_mode is "delegate", set delegate_to with an agent name from Available Agents below.
    If no suitable agent exists, set execution_mode: "self" (we do it ourselves).

Available Agents: {agents}

Available Tools: {tools}{tool_performance}

Goal: {goal}

Respond ONLY with a JSON array. No explanation, no markdown fences.
Example format (step 0 first, then steps 1+2 run in parallel, step 3 waits for both):
[
  {{"description": "Search X for posts about OpenClaw", "tool_hints": ["x_tool"], "model_tier": "flash", "verification_criteria": "At least 3 relevant tweets returned", "reversible": true, "depends_on": [], "execution_mode": "self", "delegate_to": "", "priority": "q2"}},
  {{"description": "Search web for 'OpenClaw reviews 2025'", "tool_hints": ["web_search"], "model_tier": "flash", "verification_criteria": "Search returns relevant results", "reversible": true, "depends_on": [0], "execution_mode": "delegate", "delegate_to": "research-bot", "priority": "q3"}},
  {{"description": "Fetch the OpenClaw GitHub README", "tool_hints": ["web_fetch"], "model_tier": "flash", "verification_criteria": "Page content contains README text", "reversible": true, "depends_on": [0], "execution_mode": "delegate", "delegate_to": "research-bot", "priority": "q3"}},
  {{"description": "Compile all findings into ./data/tasks/{task_id}.txt with summary", "tool_hints": ["file_operations"], "model_tier": "sonnet", "verification_criteria": "File exists at ./data/tasks/{task_id}.txt with content", "reversible": true, "depends_on": [1, 2], "execution_mode": "self", "delegate_to": "", "priority": "q2"}}
]"""

# Fallback decomposition used when Gemini Flash is unavailable (built in _make_fallback)


class GoalDecomposer:
    """Breaks a high-level goal into concrete subtasks using Gemini Flash.

    Falls back gracefully to a 2-step default if Gemini is unavailable.
    """

    def __init__(self, gemini_client=None, template_library=None, extra_tool_names=None, agent_broker=None):
        """
        Args:
            gemini_client: GeminiClient (LiteLLM wrapper). If None, uses fallback decomposition.
            template_library: ReasoningTemplateLibrary for reusing past decompositions.
            extra_tool_names: Additional tool names from plugins (auto-discovered).
            agent_broker: AgentBroker for listing available external agents in the planner prompt.
        """
        self._extra_tool_names = set(extra_tool_names or [])
        self.gemini_client = gemini_client
        self.template_library = template_library
        self.agent_broker = agent_broker
        self._model = "gemini/gemini-2.0-flash"

    async def decompose(
        self,
        goal: str,
        task_id: str,
        available_tools: Optional[List[str]] = None,
        tool_performance: Optional[Dict[str, Any]] = None,
    ) -> List[Subtask]:
        """Decompose a goal into ordered subtasks.

        Args:
            goal: The high-level goal to accomplish
            task_id: Task ID (injected into the synthesis step file path)
            available_tools: List of registered tool names (for the planner prompt)
            tool_performance: Dict of tool → success_rate from EpisodicMemory (optional)

        Returns:
            List of Subtask objects ready for sequential execution
        """
        tools_str = ", ".join(available_tools or ["web_search", "file_operations", "x_tool", "web_fetch"])

        if not self.gemini_client or not self.gemini_client.enabled:
            logger.warning("GoalDecomposer: Gemini unavailable, using fallback decomposition")
            return self._make_fallback(goal, task_id)

        # Query template library for similar past decompositions
        template_context = ""
        if self.template_library:
            try:
                templates = await self.template_library.query_similar(goal, top_k=2)
                if templates:
                    template_context = "\n\n" + self.template_library.format_for_prompt(templates) + "\n"
            except Exception as e:
                logger.debug(f"GoalDecomposer: template query failed (continuing without): {e}")

        # Build tool performance context for the planner
        tool_perf_str = ""
        if tool_performance:
            perf_lines = []
            for tool, stats in tool_performance.items():
                rate = stats.get("rate", 1.0)
                total = stats.get("total", 0)
                if total >= 3:  # Only show tools with enough data to be meaningful
                    label = "reliable" if rate >= 0.8 else ("flaky" if rate >= 0.5 else "unreliable")
                    perf_lines.append(f"  {tool}: {rate:.0%} success ({total} uses) — {label}")
            if perf_lines:
                tool_perf_str = "\n\nTool Performance (from past experience):\n" + "\n".join(perf_lines) + "\nPrefer reliable tools where possible."

        # Build available agents list for delegation decisions
        agents_str = "None (execute all steps locally)"
        if self.agent_broker:
            agents_str = self.agent_broker.get_agents_for_prompt()

        prompt = _DECOMPOSE_PROMPT.format(
            bot_name=_BOT_NAME,
            tools=tools_str,
            goal=goal,
            task_id=task_id,
            tool_performance=tool_perf_str,
            agents=agents_str,
        )
        if template_context:
            prompt = template_context + "\n" + prompt

        try:
            # Single call to Gemini Flash — no tools needed, just JSON output
            response = await self.gemini_client.create_message(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
            )

            # Extract text from response
            text = self._extract_text(response)
            if not text:
                logger.warning("GoalDecomposer: empty response, using fallback")
                return self._make_fallback(goal, task_id)

            subtasks = self._parse_json(text, task_id)
            if not subtasks:
                return self._make_fallback(goal, task_id)

            logger.info(f"GoalDecomposer: decomposed into {len(subtasks)} subtasks for goal: {goal[:60]}")
            for i, st in enumerate(subtasks):
                logger.debug(f"  Subtask {i+1}: {st.description[:80]} [{st.model_tier}]")
            return subtasks

        except Exception as e:
            logger.error(f"GoalDecomposer error: {e}", exc_info=True)
            return self._make_fallback(goal, task_id)

    # Known registered tool names — tool_hints are validated against this set
    _VALID_TOOL_NAMES = {
        "bash", "file_operations", "web_search", "web_fetch", "browser",
        "email", "calendar", "x_tool", "reminder", "nova_task", "contacts",
        "linkedin", "send_whatsapp_message", "make_phone_call", "clock",
        "polymarket", "memory_query", "learn_skill",
    }
    _VALID_MODEL_TIERS = {"flash", "haiku", "sonnet", "opus"}
    _MAX_DESCRIPTION_LENGTH = 500  # Cap to prevent prompt injection via long descriptions

    def _parse_json(self, text: str, task_id: str) -> List[Subtask]:
        """Parse JSON array from LLM response into Subtask objects.

        Validates and sanitizes all fields from the LLM output:
        - tool_hints: only registered tool names allowed
        - model_tier: must be a valid tier
        - description: capped at _MAX_DESCRIPTION_LENGTH chars
        """
        # Strip markdown fences if present
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

        try:
            items: List[Dict[str, Any]] = json.loads(text)
            if not isinstance(items, list) or len(items) == 0:
                return []

            subtasks = []
            for item in items[:7]:  # cap at 7
                desc = item.get("description", "").strip()
                if not desc:
                    continue
                # Sanitize: cap description length
                desc = desc[:self._MAX_DESCRIPTION_LENGTH]
                # Sanitize: only allow registered tool names
                raw_hints = item.get("tool_hints", [])
                all_valid = self._VALID_TOOL_NAMES | self._extra_tool_names
                valid_hints = [h for h in raw_hints if h in all_valid]
                # Sanitize: validate model tier
                tier = item.get("model_tier", "flash")
                if tier not in self._VALID_MODEL_TIERS:
                    tier = "flash"
                # Eisenhower delegation fields
                exec_mode = item.get("execution_mode", "self")
                if exec_mode not in ("self", "delegate"):
                    exec_mode = "self"
                delegate_to = item.get("delegate_to", "") if exec_mode == "delegate" else ""
                priority = item.get("priority", "q2")
                if priority not in ("q1", "q2", "q3", "q4"):
                    priority = "q2"
                orch_strategy = item.get("orchestration_strategy", "auto")
                if orch_strategy not in ("auto", "race", "fan_out"):
                    orch_strategy = "auto"

                subtasks.append(Subtask(
                    description=desc,
                    tool_hints=valid_hints,
                    model_tier=tier,
                    status="pending",
                    verification_criteria=item.get("verification_criteria", "")[:200],
                    reversible=item.get("reversible", True),
                    depends_on=item.get("depends_on", []),
                    execution_mode=exec_mode,
                    delegate_to=delegate_to,
                    priority=priority,
                    orchestration_strategy=orch_strategy,
                ))

            # Ensure synthesis step mentions the task_id file path
            if subtasks:
                last = subtasks[-1]
                if "./data/tasks/" not in last.description:
                    last.description = f"Compile all findings into ./data/tasks/{task_id}.txt and summarize in 3 bullet points"
                    last.tool_hints = ["file_operations"]
                    last.model_tier = "sonnet"

            return subtasks

        except json.JSONDecodeError as e:
            logger.warning(f"GoalDecomposer: JSON parse error: {e} — text was: {text[:200]}")
            return []

    def _make_fallback(self, goal: str, task_id: str) -> List[Subtask]:
        """Minimal 2-step plan for when Gemini is unavailable."""
        return [
            Subtask(
                description=f"Research the following using web_search and web_fetch: {goal}",
                tool_hints=["web_search", "web_fetch"],
                model_tier="flash",
                verification_criteria="At least one search result or page content returned",
                reversible=True,
                depends_on=[],
            ),
            Subtask(
                description=f"Compile all findings into ./data/tasks/{task_id}.txt and summarize in 3 bullet points",
                tool_hints=["file_operations"],
                model_tier="sonnet",
                verification_criteria=f"File exists at ./data/tasks/{task_id}.txt with content",
                reversible=True,
                depends_on=[0],
            ),
        ]

    def _extract_text(self, response) -> str:
        """Extract text content from an LLM response object."""
        if hasattr(response, "content"):
            parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts)
        if isinstance(response, str):
            return response
        return ""
