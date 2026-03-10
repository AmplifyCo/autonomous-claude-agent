"""Background Task Runner — Nova's autonomous execution engine.

Runs as a persistent asyncio loop (like ReminderScheduler).
Picks up tasks from TaskQueue, decomposes them via GoalDecomposer,
executes each subtask via agent.run(), and notifies the user when done.

Flow per task:
  1. Dequeue next pending task
  2. Decompose goal into subtasks (Gemini Flash)
  3. Execute each subtask sequentially via agent.run()
  4. Collect results, synthesize (last subtask writes the file)
  5. Notify user via WhatsApp + Telegram
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .task_queue import Task, TaskQueue, Subtask
from .goal_decomposer import GoalDecomposer

logger = logging.getLogger(__name__)


class TaskRunner:
    """Background autonomous task executor.

    Runs every CHECK_INTERVAL seconds, picks up one task at a time,
    decomposes + executes it via the existing agent.run() ReAct loop.
    """

    CHECK_INTERVAL = 15       # seconds between queue polls
    MAX_SUBTASK_RETRIES = 3   # retries per subtask

    # ── Task budget limits (systemic resilience) ──────────────────────────────
    # A task that drifts beyond these bounds is stopped and partial results delivered.
    # Tokens: ~200k covers 3–5 research subtasks with generous context windows.
    # Wall time: 30 min is ample; runaway tasks are a sign of a planning failure.
    MAX_TASK_TOKENS = 200_000   # cumulative tokens across all subtasks
    MAX_TASK_WALL_SECONDS = 1800  # 30 minutes absolute wall-clock cap

    def __init__(
        self,
        task_queue: TaskQueue,
        goal_decomposer: GoalDecomposer,
        agent,                       # AutonomousAgent
        telegram_notifier,           # TelegramNotifier
        brain=None,                  # DigitalCloneBrain (for storing results)
        whatsapp_channel=None,       # TwilioWhatsAppChannel (for WhatsApp notifications)
        critic=None,                 # CriticAgent (validates results before delivery)
        template_library=None,       # ReasoningTemplateLibrary (stores successful decompositions)
        owner_whatsapp_number: str = "",  # Fallback WhatsApp number from .env for task notifications
        episodic_memory=None,        # EpisodicMemory (for tool performance tracking)
        agent_broker=None,           # AgentBroker (for delegating subtasks to external agents)
        orchestrator=None,           # Orchestrator (for spawning internal sub-agents)
    ):
        self.task_queue = task_queue
        self.goal_decomposer = goal_decomposer
        self.agent = agent
        self.telegram = telegram_notifier
        self.brain = brain
        self.whatsapp_channel = whatsapp_channel
        self.critic = critic
        self.template_library = template_library
        self.owner_whatsapp_number = owner_whatsapp_number
        self.episodic_memory = episodic_memory
        self.agent_broker = agent_broker
        self.orchestrator = orchestrator
        self._running = False
        self._current_task_id: Optional[str] = None
        self._FALLBACK_MODELS = ["gemini/gemini-2.0-flash", "claude-haiku-4-5-20251001"]
        Path("./data/tasks").mkdir(parents=True, exist_ok=True)

    async def _llm_call_with_fallback(self, prompt: str, max_tokens: int = 256) -> Optional[str]:
        """Try Gemini Flash, fall back to Claude Haiku if Gemini fails.

        Returns extracted text or None if all models fail.
        """
        gemini = getattr(self.agent, "gemini_client", None) if self.agent else None
        if not gemini or not getattr(gemini, "enabled", False):
            return None

        for model in self._FALLBACK_MODELS:
            try:
                response = await gemini.create_message(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                )
                text = self._extract_text_from_response(response).strip()
                if text:
                    return text
            except Exception as e:
                logger.debug(f"_llm_call_with_fallback: {model} failed: {e}")
        return None

    async def start(self):
        """Main background loop. Runs indefinitely."""
        self._running = True
        logger.info("🚀 TaskRunner background loop started")
        while self._running:
            try:
                await self._process_next_task()
            except Exception as e:
                logger.error(f"TaskRunner loop error: {e}", exc_info=True)
            await asyncio.sleep(self.CHECK_INTERVAL)

    def stop(self):
        self._running = False

    # ── Channel-aware notification helper ─────────────────────────────────────

    async def _notify_channel(self, task: Task, message: str):
        """Send a notification to the same channel the task was initiated from.

        - task.channel == "whatsapp" → WhatsApp
        - anything else → Telegram
        """
        if task.channel == "whatsapp" and self.whatsapp_channel:
            wa_to = task.user_id if (task.user_id or "").startswith(("whatsapp:", "+")) else self.owner_whatsapp_number
            if wa_to:
                try:
                    await asyncio.to_thread(self.whatsapp_channel.send_message, wa_to, message)
                    return
                except Exception as e:
                    logger.warning(f"WhatsApp notification failed, falling back to Telegram: {e}")
        # Default: Telegram
        try:
            await self.telegram.notify(message, level="info")
        except Exception as e:
            logger.warning(f"Telegram notification failed: {e}")

    # ── Core execution ────────────────────────────────────────────────────────

    async def _process_next_task(self):
        """Pick up and execute the next pending task (if any)."""
        task = self.task_queue.dequeue_next()
        if not task:
            return

        self._current_task_id = task.id
        logger.info(f"TaskRunner picked up task {task.id}: {task.goal[:60]}")

        # Notify owner on the originating channel that the task has started
        start_msg = f"🚀 Task started: {self._safe(task.goal, 80)}"
        await self._notify_channel(task, start_msg)

        try:
            # Step 1: Fetch tool performance from episodic memory (#4)
            tool_performance = None
            if self.episodic_memory:
                try:
                    tool_performance = await self.episodic_memory.get_tool_success_rates()
                except Exception as e:
                    logger.debug(f"Could not fetch tool performance: {e}")

            # Step 2: Decompose into subtasks (with tool performance context)
            available_tools = list(self.agent.tools.tools.keys()) if hasattr(self.agent, 'tools') else []
            subtasks = await self.goal_decomposer.decompose(
                goal=task.goal,
                task_id=task.id,
                available_tools=available_tools,
                tool_performance=tool_performance,
            )
            self.task_queue.set_subtasks(task.id, subtasks)
            task.subtasks = subtasks

            logger.info(f"Task {task.id}: decomposed into {len(subtasks)} subtasks")

            # Notify the plan upfront so user knows what's coming
            if task.notify_on_complete:
                await self._notify_plan(task, subtasks)

            # Step 3: Execute subtasks — sequential or parallel depending on subtask.parallel
            all_results = []
            num_subtasks = len(subtasks)
            delegation_log: List[Dict[str, Any]] = []  # Audit trail (#6)
            total_tokens = 0
            task_start_time = time.time()
            budget_exceeded = False

            # Group consecutive parallel subtasks into waves; each sequential subtask
            # is its own wave of size 1.
            waves = self._build_waves(subtasks)
            replanned = False  # Max 1 replan per task

            wave_idx = 0
            while wave_idx < len(waves):
                wave_start_idx, wave = waves[wave_idx]
                # #3: Continuous authorization — re-check before every wave
                fresh = self.task_queue.get_task(task.id)
                if fresh and fresh.status == "failed":
                    logger.info(f"Task {task.id}: cancelled externally before wave at step {wave_start_idx+1}, stopping")
                    raise asyncio.CancelledError("Task cancelled externally")

                if len(wave) == 1:
                    # ── Single sequential subtask ──────────────────────────
                    idx = wave_start_idx
                    subtask = wave[0]
                    logger.info(f"Task {task.id}: executing step {idx+1}/{num_subtasks}: {subtask.description[:60]}")
                    self.task_queue.update_subtask(task.id, idx, "running")

                    # Reversibility gate (#2)
                    if not subtask.reversible:
                        await self._notify_irreversible_gate(task, idx + 1, num_subtasks, subtask.description)

                    if task.notify_on_complete:
                        await self._notify_step_start(task, idx + 1, num_subtasks, subtask.description)

                    step_start = datetime.utcnow().isoformat()
                    result = await self._execute_subtask(task, subtask, idx, all_results)
                    step_end = datetime.utcnow().isoformat()

                    # Adaptive re-delegation (#3)
                    re_delegated = False
                    if result.startswith("ERROR:"):
                        alt_result = await self._try_adaptive_redelegate(task, subtask, idx, all_results, result)
                        if alt_result and not alt_result.startswith("ERROR:"):
                            result = alt_result
                            re_delegated = True
                            logger.info(f"Task {task.id}: step {idx+1} recovered via re-delegation")

                    # Cap result stored in memory to prevent context overflow in subsequent steps
                    capped_result = result[:3000] + "..." if len(result) > 3000 else result
                    all_results.append(f"Step {idx+1}: {capped_result}")
                    failed = result.startswith("ERROR:")
                    if failed and idx < num_subtasks - 1:
                        logger.warning(f"Step {idx+1} failed, continuing: {result[:200]}")
                        self.task_queue.update_subtask(task.id, idx, "failed", error=result[:500])
                    else:
                        self.task_queue.update_subtask(task.id, idx, "done", result=result[:500])

                    await self._record_subtask_episode(subtask, result, failed)

                    subtask_tokens = getattr(self.agent, "last_run_tokens", 0)
                    total_tokens += subtask_tokens

                    delegation_log.append({
                        "step": idx + 1,
                        "description": subtask.description,
                        "tool_hints": subtask.tool_hints,
                        "verification_criteria": subtask.verification_criteria,
                        "reversible": subtask.reversible,
                        "depends_on": subtask.depends_on,
                        "started_at": step_start,
                        "completed_at": step_end,
                        "success": not failed,
                        "re_delegated": re_delegated,
                        "tokens": subtask_tokens,
                        "outcome": result[:300],
                    })

                    if task.notify_on_complete:
                        await self._notify_step_done(task, idx + 1, num_subtasks, result, failed)

                else:
                    # ── Parallel wave: run all steps concurrently ──────────
                    step_nums = list(range(wave_start_idx + 1, wave_start_idx + len(wave) + 1))
                    logger.info(
                        f"Task {task.id}: running steps {step_nums[0]}–{step_nums[-1]} in parallel "
                        f"({len(wave)} subtasks)"
                    )

                    for i, subtask in enumerate(wave):
                        self.task_queue.update_subtask(task.id, wave_start_idx + i, "running")

                    if task.notify_on_complete:
                        wave_desc = " | ".join(self._safe(st.description, 40) for st in wave)
                        await self._notify_channel(task, f"⚡ Running {len(wave)} steps in parallel: {wave_desc}")

                    wave_start_ts = datetime.utcnow().isoformat()
                    wave_results = await self._execute_parallel_wave(task, wave, wave_start_idx, all_results)
                    wave_end_ts = datetime.utcnow().isoformat()

                    # Tokens for a parallel wave: estimate from output length (agent._run_tokens
                    # is not reliable across concurrent runs on the same instance)
                    wave_tokens = sum(len(r) // 3 for _, _, r, _ in wave_results)
                    total_tokens += wave_tokens

                    for i, (idx, subtask, result, re_delegated) in enumerate(wave_results):
                        capped_result = result[:3000] + "..." if len(result) > 3000 else result
                        all_results.append(f"Step {idx+1}: {capped_result}")
                        failed = result.startswith("ERROR:")
                        if failed and idx < num_subtasks - 1:
                            self.task_queue.update_subtask(task.id, idx, "failed", error=result[:500])
                        else:
                            self.task_queue.update_subtask(task.id, idx, "done", result=result[:500])
                        await self._record_subtask_episode(subtask, result, failed)

                        delegation_log.append({
                            "step": idx + 1,
                            "description": subtask.description,
                            "tool_hints": subtask.tool_hints,
                            "verification_criteria": subtask.verification_criteria,
                            "reversible": subtask.reversible,
                            "depends_on": subtask.depends_on,
                            "started_at": wave_start_ts,
                            "completed_at": wave_end_ts,
                            "success": not failed,
                            "re_delegated": re_delegated,
                            "tokens": len(result) // 3,
                            "outcome": result[:300],
                        })

                        if task.notify_on_complete:
                            await self._notify_step_done(task, idx + 1, num_subtasks, result, failed)

                # ── Budget check after every wave ──────────────────────────
                elapsed = time.time() - task_start_time
                if total_tokens > self.MAX_TASK_TOKENS:
                    logger.warning(
                        f"Task {task.id}: token budget exceeded "
                        f"({total_tokens:,} > {self.MAX_TASK_TOKENS:,})"
                    )
                    await self._notify_budget_exceeded(task, "token", total_tokens, elapsed)
                    budget_exceeded = True
                    break

                if elapsed > self.MAX_TASK_WALL_SECONDS:
                    logger.warning(
                        f"Task {task.id}: wall-time budget exceeded "
                        f"({elapsed/60:.1f} min > {self.MAX_TASK_WALL_SECONDS/60:.0f} min)"
                    )
                    await self._notify_budget_exceeded(task, "time", total_tokens, elapsed)
                    budget_exceeded = True
                    break

                # ── Observe→Plan→Act→Correct: replan if needed ────────────
                if not replanned and wave_idx < len(waves) - 1:
                    # Check if any step in this wave failed
                    wave_had_failure = any(
                        r.startswith("Step") and "ERROR:" in r
                        for r in all_results[-len(wave):]
                    )
                    remaining_sts = []
                    for future_idx in range(wave_idx + 1, len(waves)):
                        remaining_sts.extend(waves[future_idx][1])

                    if remaining_sts:
                        revised = await self._replan_between_waves(
                            task, all_results, remaining_sts, wave_had_failure
                        )
                        if revised:
                            # Rebuild waves from revised subtasks
                            waves = waves[:wave_idx + 1] + self._build_waves(revised)
                            num_subtasks = wave_start_idx + len(wave) + len(revised)
                            replanned = True
                            await self._notify_channel(
                                task, "📋 Adjusting plan based on what I've learned so far..."
                            )

                wave_idx += 1

            # Step 3: Critic validation — evaluate quality before delivery
            critic_score = 0.8  # default if critic unavailable
            if self.critic:
                try:
                    critic_result = await self.critic.evaluate(task.goal, subtasks, all_results)
                    critic_score = critic_result.score
                    logger.info(f"Task {task.id}: critic score {critic_result.score:.2f} (passed={critic_result.passed})")
                    if not critic_result.passed and critic_result.refinement_hint:
                        logger.info(f"Task {task.id}: running refinement pass — {critic_result.refinement_hint[:80]}")
                        refined = await self.critic.refine(task.goal, all_results, critic_result.refinement_hint)
                        if refined:
                            all_results.append(f"Step {len(all_results)+1} (refined): {refined}")
                            critic_score = 0.8  # assume acceptable after refinement
                except Exception as e:
                    logger.warning(f"Task {task.id}: critic evaluation failed (proceeding): {e}")

            # Step 4: Store successful decomposition as a reusable template
            if self.template_library and critic_score >= 0.7:
                try:
                    await self.template_library.store(task.goal, subtasks, critic_score)
                except Exception as e:
                    logger.warning(f"Task {task.id}: template store failed: {e}")

            # Step 5: Record winning strategy for future recall (complementary to templates)
            # Templates cache the DECOMPOSITION, strategies cache the APPROACH (tools, order, what worked)
            if self.episodic_memory and critic_score >= 0.75:
                try:
                    approach = "; ".join(
                        f"Step {i+1}: {st.description[:80]}"
                        for i, st in enumerate(subtasks)
                    )
                    tools_used = list({t for st in subtasks for t in (st.tool_hints or [])})
                    await self.episodic_memory.record_strategy(
                        goal=task.goal,
                        approach=approach,
                        tools_used=tools_used,
                        score=critic_score,
                    )
                except Exception:
                    pass  # non-critical — fail silently

            # Step 6: Build summary from results
            summary = self._build_summary(task.goal, all_results)
            self.task_queue.mark_done(task.id, result=summary)

            # Save delegation audit trail (#6)
            await self._save_delegation_audit(task, delegation_log)

            # Step 7: Notify user
            if task.notify_on_complete:
                await self._notify_user(task, summary)

            logger.info(f"Task {task.id} completed successfully")

        except asyncio.CancelledError:
            logger.info(f"Task {task.id} cancelled")
            self.task_queue.mark_failed(task.id, "Task runner stopped during execution")
            raise
        except Exception as e:
            logger.error(f"Task {task.id} failed: {e}", exc_info=True)
            self.task_queue.mark_failed(task.id, str(e)[:300])
            # Still try to notify user about the failure
            if task.notify_on_complete:
                await self._notify_failure(task, str(e))
        finally:
            self._current_task_id = None

    @staticmethod
    def _build_waves(subtasks: List) -> List[tuple]:
        """Group subtasks into ordered execution waves using their depends_on graph.

        Returns a list of (start_index, [subtask, ...]) tuples.

        Algorithm: assign each subtask to the earliest wave where all its
        dependencies are already in earlier waves. Subtasks in the same wave
        have no dependency between them and can run in parallel.

        Example — A(0) → B(1), C(2) → D(3):
          Wave 0: [A]       (depends_on=[])
          Wave 1: [B, C]    (both depends_on=[0])
          Wave 2: [D]       (depends_on=[1,2])
        """
        n = len(subtasks)
        wave_assignment = [-1] * n

        for i, st in enumerate(subtasks):
            deps = getattr(st, "depends_on", [])
            if not deps:
                wave_assignment[i] = 0
            else:
                # Place in the wave after the latest dependency
                max_dep_wave = max(
                    (wave_assignment[d] for d in deps if 0 <= d < n and wave_assignment[d] >= 0),
                    default=-1
                )
                wave_assignment[i] = max_dep_wave + 1

        # Group by wave number preserving original order within each wave
        waves_dict: Dict[int, List[tuple]] = {}
        for i, st in enumerate(subtasks):
            w = wave_assignment[i]
            waves_dict.setdefault(w, []).append((i, st))

        result = []
        for w in sorted(waves_dict.keys()):
            members = waves_dict[w]
            start_idx = members[0][0]
            sts = [st for _, st in members]
            result.append((start_idx, sts))

        return result

    async def _execute_parallel_wave(
        self,
        task: Task,
        wave: List,
        start_idx: int,
        prior_results: List[str],
    ) -> List[tuple]:
        """Execute multiple independent subtasks concurrently.

        Uses Orchestrator (internal sub-agents) when available for true isolation.
        Falls back to asyncio.gather on shared agent instance otherwise.

        Returns list of (idx, subtask, result_str, re_delegated_bool).
        """
        # ── Orchestrator path: each subtask gets its own SubAgent ──────────
        if self.orchestrator and len(wave) > 1:
            # Check if all subtasks in wave are self-execute (not external delegation)
            all_self = all(getattr(st, 'execution_mode', 'self') == 'self' for st in wave)
            if all_self:
                try:
                    return await self._orchestrator_parallel(task, wave, start_idx, prior_results)
                except Exception as e:
                    logger.warning(f"Orchestrator parallel failed: {e} — falling back to shared agent")

        # ── Fallback: existing asyncio.gather on shared agent instance ─────
        async def _run_one(idx: int, subtask) -> tuple:
            result = await self._execute_subtask(task, subtask, idx, prior_results)
            re_delegated = False
            if result.startswith("ERROR:"):
                alt = await self._try_adaptive_redelegate(task, subtask, idx, prior_results, result)
                if alt and not alt.startswith("ERROR:"):
                    result = alt
                    re_delegated = True
                    logger.info(f"Task {task.id}: step {idx+1} (parallel) recovered via re-delegation")
            return idx, subtask, result, re_delegated

        coros = [_run_one(start_idx + i, st) for i, st in enumerate(wave)]
        return await asyncio.gather(*coros, return_exceptions=False)

    async def _orchestrator_parallel(
        self,
        task: Task,
        wave: List,
        start_idx: int,
        prior_results: List[str],
    ) -> List[tuple]:
        """Execute wave via Orchestrator — each subtask gets its own SubAgent.

        SubAgents have isolated message history and run truly concurrently.
        """
        logger.info(f"Task {task.id}: using Orchestrator for {len(wave)} parallel sub-agents")

        context = "\n".join(prior_results[-3:]) if prior_results else ""
        task_specs = []
        for i, subtask in enumerate(wave):
            prompt = self._build_subtask_prompt(task, subtask, start_idx + i, prior_results)
            task_specs.append({
                "description": prompt,
                "model": self._resolve_model(subtask.model_tier),
                "context": context,
            })

        # Enable policy gate bypass for sub-agents (pre-approved background tasks)
        self.agent.tools.policy_gate.set_bypass(True)
        try:
            results = await self.orchestrator.spawn_parallel(task_specs, max_concurrent=3)
        finally:
            self.agent.tools.policy_gate.set_bypass(False)

        return [
            (
                start_idx + i,
                wave[i],
                r.summary if r.success else f"ERROR: {r.error}",
                False,
            )
            for i, r in enumerate(results)
        ]

    def _resolve_model(self, model_tier: str) -> str:
        """Map model_tier to a LiteLLM model string for sub-agents.

        Uses Claude models as primary — Gemini billing is unreliable.
        All models route through LiteLLM regardless of provider.
        """
        tier_map = {
            "flash": "claude-haiku-4-5-20251001",
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-20250514",
            "opus": "claude-opus-4-20250514",
        }
        return tier_map.get(model_tier, "claude-sonnet-4-20250514")

    def _build_subtask_prompt(self, task: Task, subtask, idx: int, prior_results: list) -> str:
        """Build the execution prompt for a subtask (used by both self-execute and Orchestrator)."""
        context = ""
        if prior_results:
            # Cap each prior result to prevent context overflow (214K+ token errors)
            recent = [r[:2000] + "..." if len(r) > 2000 else r for r in prior_results[-3:]]
            context = "\n\nPREVIOUS STEPS COMPLETED:\n" + "\n".join(recent) + "\n\n---\n"

        bot_name = os.getenv("BOT_NAME", "Nova")
        owner_name = os.getenv("OWNER_NAME", "User")
        signature = f"{bot_name} — {owner_name}'s Non-Human Assistant"

        prompt = (
            f"IDENTITY: You are {bot_name}, {owner_name}'s Non-Human Assistant.\n"
            f"SIGNATURE RULE (MANDATORY): When signing off on any content "
            f"(LinkedIn posts, tweets, emails, reports), you MUST sign as:\n"
            f"  {signature}\n"
            f"NEVER use placeholders like \"[Your Name]\", \"[Author]\", or generic "
            f"phrases. The signature above is final — copy it exactly.\n\n"
            f"CONTENT QUALITY (MANDATORY for LinkedIn/social posts):\n"
            f"- You are a professional content writer. Write posts that are engaging, "
            f"insightful, and well-structured.\n"
            f"- LinkedIn does NOT support markdown. Use Unicode formatting instead:\n"
            f"  \U0001d5d5\U0001d5fc\U0001d5f9\U0001d5f1 (Mathematical Sans-Serif Bold) for headers/key phrases\n"
            f"  \U0001d608\U0001d61d\U0001d5ee\U0001d5f9\U0001d5f6\U0001d5f0 (Mathematical Sans-Serif Italic) for emphasis\n"
            f"  \u2022 for bullet points, \u2192 for arrows, \u2726 for highlights\n"
            f"  \u2500\u2500\u2500 for section dividers, \u2460\u2461\u2462 for numbered lists\n"
            f"- HOOK FIRST: First line must stop the scroll. Bold claim, question, or stat.\n"
            f"- Use the post_length parameter: short (1-3 lines), medium (5-10 lines), "
            f"long (12-25 lines with full structure).\n"
            f"- Default to 'medium' unless the task specifies otherwise.\n\n"
            f"{context}"
            f"BACKGROUND TASK (ID: {task.id})\n"
            f"Overall goal: {task.goal}\n\n"
            f"Current step ({idx+1}): {subtask.description}\n\n"
            f"Complete this step and report what you found/did. Be thorough."
        )

        if subtask.tool_hints:
            prompt += f"\n\nSuggested tools for this step: {', '.join(subtask.tool_hints)}"
        if subtask.verification_criteria:
            prompt += f"\n\nSuccess criterion: {subtask.verification_criteria}"

        return prompt

    async def _execute_subtask(self, task: Task, subtask, idx: int, prior_results: list) -> str:
        """Execute a single subtask via agent.run() and return the result string.

        If execution_mode is 'delegate' and an AgentBroker is available, delegates
        to an external agent first. Falls back to self-execute on failure (CXO style).
        """
        # ── Delegation branch (Eisenhower Q3/Q4) ─────────────────────────────
        if getattr(subtask, 'execution_mode', 'self') == 'delegate' and self.agent_broker:
            delegate_to = getattr(subtask, 'delegate_to', '')
            strategy = getattr(subtask, 'orchestration_strategy', 'auto')
            try:
                logger.info(f"Task {task.id}: delegating step {idx+1} (agent='{delegate_to}', strategy='{strategy}')")
                result = await self.agent_broker.delegate_smart(
                    subtask_description=subtask.description,
                    tool_hints=subtask.tool_hints or [],
                    delegate_to=delegate_to,
                    context_id=task.id,
                    strategy=strategy,
                )
                return result or "Delegated step completed (no output)"
            except Exception as e:
                logger.warning(f"Delegation failed: {e} — trying self-execute")
                # CXO fallback: check if Nova has the required tools
                if subtask.tool_hints and not any(
                    t in self.agent.tools.tools for t in subtask.tool_hints
                ):
                    return f"ERROR: Delegation failed ({e}). Nova lacks tools for this step."
                logger.info(f"Nova has the tools — self-executing step {idx+1} instead")

        # ── Self-execute path ─────────────────────────────────────────────────
        task_prompt = self._build_subtask_prompt(task, subtask, idx, prior_results)
        model_tier = "sonnet"  # Use better model for all subtasks to reduce failures

        for attempt in range(self.MAX_SUBTASK_RETRIES):
            try:
                # Enable policy gate bypass — TaskRunner tasks are pre-approved by user
                self.agent.tools.policy_gate.set_bypass(True)
                try:
                    # #1: just-in-time tool access — scope tools to this subtask's hints
                    result = await self.agent.run(
                        task=task_prompt,
                        model_tier=model_tier,
                        max_iterations=8,  # generous for research tasks
                        allowed_tools=subtask.tool_hints if subtask.tool_hints else None,
                    )
                    return result or "Step completed (no output)"
                finally:
                    # Always re-disable bypass after subtask completes
                    self.agent.tools.policy_gate.set_bypass(False)
            except Exception as e:
                error_str = str(e)
                if attempt < self.MAX_SUBTASK_RETRIES - 1:
                    if "429" in error_str or "rate_limit" in error_str:
                        logger.warning(f"Rate limited on subtask {idx+1}, retrying in 30s...")
                        await asyncio.sleep(30)
                    else:
                        # Semantic retry: generate targeted fix hint before retrying
                        hint = await self._generate_retry_hint(subtask.description, error_str)
                        task_prompt = (
                            f"PREVIOUS ATTEMPT FAILED: {error_str[:200]}\n"
                            f"HINT FOR THIS RETRY: {hint}\n\n"
                            f"---\n{task_prompt}"
                        )
                        logger.warning(f"Subtask {idx+1} logic failure, retrying with hint: {hint[:80]}")
                    continue
                return f"ERROR: {error_str[:200]}"

        return "ERROR: Max retries exceeded"

    async def _generate_retry_hint(self, subtask_desc: str, error: str) -> str:
        """Ask LLM what went wrong and what different approach to try.

        Tries Gemini Flash first, falls back to Claude Haiku.
        Returns a 1-2 sentence hint string, or a generic fallback on any error.
        """
        hint_prompt = (
            f"An AI agent failed a task step. In 1-2 sentences only, suggest what it should "
            f"try differently on the next attempt.\n\n"
            f"Step: {subtask_desc[:200]}\n"
            f"Error: {error[:200]}"
        )
        result = await self._llm_call_with_fallback(hint_prompt, max_tokens=128)
        return result or "Try a different approach or use a different tool for this step."

    # ── Notification ──────────────────────────────────────────────────────────

    @staticmethod
    def _safe(text: str, limit: int = 200) -> str:
        """Strip Markdown special chars so Telegram plain-text mode never chokes."""
        return text[:limit].replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")

    # Dispatch symbols for plan display
    _DISPATCH_ICONS = {"green": "🟢", "yellow": "🟡", "red": "🔴", "gray": "⚪"}

    async def _notify_plan(self, task: Task, subtasks: list):
        """Send a compact numbered plan with dispatch classification and time estimate."""
        irreversible_count = sum(1 for st in subtasks if not st.reversible)
        total_minutes = sum(getattr(st, "estimated_minutes", 5) for st in subtasks)
        # Dispatch summary
        dispatch_counts = {}
        for st in subtasks:
            d = getattr(st, "dispatch", "green")
            dispatch_counts[d] = dispatch_counts.get(d, 0) + 1
        dispatch_summary = " ".join(
            f"{self._DISPATCH_ICONS.get(d, '⚪')}{c}" for d, c in sorted(dispatch_counts.items())
        )
        # 3B: Show approach reasoning — tools and strategy
        tools_used = set(t for st in subtasks for t in (st.tool_hints or []))
        approach = f"Approach: {len(subtasks)} steps (~{total_minutes} min)"
        if tools_used:
            approach += f" using {', '.join(sorted(tools_used))}"
        steps = " | ".join(
            f"{i}. {self._DISPATCH_ICONS.get(getattr(st, 'dispatch', 'green'), '⚪')}{'⚠️ ' if not st.reversible else ''}{self._safe(st.description, 40)}"
            for i, st in enumerate(subtasks, 1)
        )
        msg = f"📋 {approach} [{dispatch_summary}]\n{steps}"
        if irreversible_count:
            msg += f"\n⚠️ {irreversible_count} irreversible — reply 'stop task' to cancel."
        red_count = dispatch_counts.get("red", 0)
        if red_count:
            msg += f"\n🔴 {red_count} step(s) need your input — I'll flag them when reached."
        await self._notify_channel(task, msg)

    async def _notify_step_start(self, task: Task, step: int, total: int, description: str):
        """Notify that a step is starting."""
        msg = f"🔄 [{step}/{total}] {self._safe(description, 80)}"
        await self._notify_channel(task, msg)

    async def _notify_step_done(self, task: Task, step: int, total: int, result: str, failed: bool):
        """Notify step completion with a one-line outcome."""
        if failed:
            msg = f"❌ [{step}/{total}] {self._safe(result.removeprefix('ERROR:').strip(), 100)}"
        else:
            msg = f"✅ [{step}/{total}] {self._safe(result, 100)}"
        await self._notify_channel(task, msg)

    async def _notify_user(self, task: Task, summary: str):
        """Notify user on the SAME channel the task was initiated from.

        Intermediate step updates always go to Telegram (non-intrusive editing).
        Final completion notification goes ONLY to the originating channel:
        - task.channel == "telegram" → Telegram only
        - task.channel == "whatsapp" → WhatsApp only
        """
        file_path = Path(f"./data/tasks/{task.id}.txt")

        # Read full file content; fall back to the in-memory summary
        full_content = summary
        try:
            if file_path.exists():
                full_content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not read task file {file_path}: {e}")

        header = f"✅ Done: {self._safe(task.goal, 80)}\n\n"

        # Route final notification to originating channel only
        if task.channel == "whatsapp":
            # WhatsApp-initiated → send final result to WhatsApp only
            if self.whatsapp_channel:
                wa_to = task.user_id if (task.user_id or "").startswith(("whatsapp:", "+")) else self.owner_whatsapp_number
                if wa_to:
                    wa_msg = f"✅ Done!\n\n{full_content[:1200]}"
                    try:
                        await asyncio.to_thread(
                            self.whatsapp_channel.send_message, wa_to, wa_msg
                        )
                    except Exception as e:
                        logger.warning(f"WhatsApp notification failed: {e}")
                else:
                    logger.debug("WhatsApp channel configured but no owner number — falling back to Telegram")
                    await self._send_final_telegram(header, full_content)
            else:
                # WhatsApp not available — fall back to Telegram
                await self._send_final_telegram(header, full_content)
        else:
            # Telegram-initiated (or any other channel) → Telegram only
            await self._send_final_telegram(header, full_content)

    async def _send_final_telegram(self, header: str, full_content: str):
        """Send final task result to Telegram."""
        try:
            await self._send_chunked_telegram(header, full_content)
        except Exception as e:
            logger.warning(f"Telegram notification failed: {e}")

    async def _send_chunked_telegram(self, header: str, content: str):
        """Send a potentially long message to Telegram in 3800-char chunks.

        First chunk includes the header. Subsequent chunks are labeled
        (continued N) so the user can follow the sequence.
        """
        CHUNK = 3800
        first_body = content[: CHUNK - len(header)]
        await self.telegram.notify(header + first_body, level="info")

        remaining = content[CHUNK - len(header) :]
        part = 2
        while remaining:
            chunk_text = f"*(continued {part})*\n\n" + remaining[:CHUNK]
            await self.telegram.notify(chunk_text, level="info")
            remaining = remaining[CHUNK:]
            part += 1

    async def _notify_failure(self, task: Task, error: str):
        """Notify user when a task fails — on the originating channel only."""
        msg = f"❌ Task failed: {self._safe(task.goal, 60)}\nReason: {self._safe(error, 120)}"

        if task.channel == "whatsapp" and self.whatsapp_channel and task.user_id:
            try:
                await asyncio.to_thread(
                    self.whatsapp_channel.send_message,
                    task.user_id,
                    f"Sorry, I wasn't able to complete that task. Error: {error[:100]}"
                )
            except Exception as e:
                logger.warning(f"WhatsApp failure notification failed: {e}")
        else:
            try:
                await self.telegram.notify(msg, level="warning")
            except Exception as e:
                logger.warning(f"Telegram failure notification failed: {e}")

    async def _notify_budget_exceeded(
        self,
        task: Task,
        budget_type: str,
        total_tokens: Optional[int],
        elapsed: Optional[float],
    ):
        """Notify user when a task's token or wall-time budget is exceeded.

        Partial results collected so far are still delivered via the normal
        completion path — the caller sets budget_exceeded=True to skip
        remaining subtasks and proceed to summary + notification.
        """
        if budget_type == "token":
            detail = f"{total_tokens:,} tokens used (limit: {self.MAX_TASK_TOKENS:,})"
        else:
            detail = f"{elapsed/60:.1f} min elapsed (limit: {self.MAX_TASK_WALL_SECONDS//60} min)"

        msg = (
            f"⏱ Task budget reached for: {self._safe(task.goal, 60)}\n"
            f"{detail} — stopping here and delivering partial results."
        )
        await self._notify_channel(task, msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_summary(self, goal: str, results: list) -> str:
        """Build a compact summary from subtask results.

        The last subtask (synthesis) is expected to have written the file
        and produced a bullet-point summary. We extract that.
        """
        if not results:
            return "No results collected."

        # Use the last result (synthesis step) as the primary summary
        last = results[-1] if results else ""
        # Strip the "Step N:" prefix
        if ": " in last:
            last = last.split(": ", 1)[1]

        # Truncate for notification use (full content is in the file)
        if len(last) > 800:
            last = last[:800] + "..."

        return last

    def get_status(self) -> dict:
        """Return current runner status (for dashboard/health checks)."""
        return {
            "running": self._running,
            "current_task": self._current_task_id,
            "pending_tasks": self.task_queue.get_pending_count(),
        }

    # ── Delegation framework helpers (from Intelligent AI Delegation paper) ──

    async def _notify_irreversible_gate(self, task: Task, step: int, total: int, description: str):
        """#2: Warn user before an irreversible subtask executes.

        Sends a Telegram alert so the user can send 'stop task' to cancel
        if the action isn't what they intended. No artificial delay — the
        existing interrupt mechanism handles cancellation in real-time.
        """
        msg = (
            f"⚠️ [{step}/{total}] About to take an *irreversible* action:\n"
            f"{self._safe(description, 120)}\n\n"
            f"_Reply 'stop task' to cancel before this runs._"
        )
        try:
            await self._notify_channel(task, msg)
            # Brief pause to give user a chance to react
            await asyncio.sleep(10)
            # Re-check whether the task was cancelled during the wait
            fresh = self.task_queue.get_task(task.id)
            if fresh and fresh.status == "failed":
                logger.info(f"Task {task.id}: cancelled during irreversible gate wait at step {step}")
                raise asyncio.CancelledError(f"Task cancelled before irreversible step {step}")
        except asyncio.CancelledError:
            raise  # Don't swallow the cancellation
        except Exception as e:
            logger.warning(f"Irreversibility gate notification failed: {e}")

    async def _try_adaptive_redelegate(
        self,
        task: Task,
        failed_subtask: Subtask,
        idx: int,
        prior_results: list,
        error: str,
    ) -> Optional[str]:
        """#3: When a subtask exhausts all retries, ask Gemini for an alternative approach.

        Generates a single replacement subtask with a different strategy and
        attempts it once. Returns the result string, or None if re-delegation
        itself fails.
        """
        logger.info(f"Task {task.id}: attempting adaptive re-delegation for subtask {idx+1}")

        redelegate_prompt = (
            f"An AI agent failed a task step after multiple retries. "
            f"Propose ONE alternative approach as a short JSON object with keys: "
            f"description, tool_hints (list), verification_criteria.\n\n"
            f"Original step: {failed_subtask.description[:200]}\n"
            f"Error: {error[:200]}\n"
            f"Overall goal: {task.goal[:200]}\n\n"
            f"Respond ONLY with a JSON object. No explanation."
        )
        try:
            text = await self._llm_call_with_fallback(redelegate_prompt, max_tokens=256)
            if not text:
                return None
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            alt = json.loads(text)
            alt_subtask = Subtask(
                description=alt.get("description", failed_subtask.description),
                tool_hints=alt.get("tool_hints", failed_subtask.tool_hints),
                model_tier="sonnet",
                verification_criteria=alt.get("verification_criteria", ""),
                reversible=failed_subtask.reversible,
            )
            logger.info(f"Re-delegation plan: {alt_subtask.description[:80]}")
            return await self._execute_subtask(task, alt_subtask, idx, prior_results)
        except Exception as e:
            logger.warning(f"Adaptive re-delegation failed: {e}")
            return None

    async def _replan_between_waves(
        self,
        task: Task,
        completed_results: List[str],
        remaining_subtasks: List[Subtask],
        wave_had_failure: bool,
    ) -> Optional[List[Subtask]]:
        """Re-plan remaining subtasks if intermediate results suggest a pivot.

        Called between waves. Only triggers if:
        - A subtask in the current wave failed, OR
        - At least 3 results completed (enough data to judge)

        Returns revised subtask list or None (keep original plan).
        Max 1 replan per task. Fail-open: any error returns None.
        """
        if not wave_had_failure and len(completed_results) < 3:
            return None

        remaining_desc = "\n".join(
            f"  Step: {st.description[:100]}" for st in remaining_subtasks
        )
        completed_summary = "\n".join(r[:150] for r in completed_results[-3:])

        prompt = (
            f"You are re-evaluating a task plan mid-execution.\n\n"
            f"GOAL: {task.goal[:300]}\n\n"
            f"COMPLETED SO FAR:\n{completed_summary}\n\n"
            f"REMAINING PLAN:\n{remaining_desc}\n\n"
            f"Based on the results so far, should the remaining steps be revised?\n"
            f"- If the plan is still good, respond with exactly: KEEP_PLAN\n"
            f"- If the plan needs revision, respond with a JSON array of revised steps.\n"
            f"  Each step: {{\"description\": \"...\", \"tool_hints\": [...], "
            f"\"verification_criteria\": \"...\", \"reversible\": true}}\n\n"
            f"Respond with KEEP_PLAN or JSON array only. No explanation."
        )

        try:
            text = await self._llm_call_with_fallback(prompt, max_tokens=512)
            if not text:
                return None

            if "KEEP_PLAN" in text:
                logger.info(f"Task {task.id}: replan check — keeping original plan")
                return None

            # Strip markdown fences
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                text = text.strip()

            items = json.loads(text)
            if not isinstance(items, list) or not items:
                return None

            revised = []
            for item in items[:5]:  # cap at 5 remaining steps
                desc = item.get("description", "").strip()
                if not desc:
                    continue
                revised.append(Subtask(
                    description=desc[:500],
                    tool_hints=item.get("tool_hints", []),
                    model_tier=item.get("model_tier", "sonnet"),
                    verification_criteria=item.get("verification_criteria", "")[:200],
                    reversible=item.get("reversible", True),
                    depends_on=[],  # Fresh plan — no cross-dependencies
                ))

            if revised:
                logger.info(f"Task {task.id}: replanned — {len(revised)} revised steps")
                return revised
            return None

        except Exception as e:
            logger.debug(f"Task {task.id}: replan failed (keeping original): {e}")
            return None

    def _extract_text_from_response(self, response) -> str:
        """Extract plain text from an LLM response object."""
        if hasattr(response, "content"):
            parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts)
        if isinstance(response, str):
            return response
        return ""

    async def _record_subtask_episode(self, subtask: Subtask, result: str, failed: bool):
        """#4: Record each subtask execution as an episodic memory for tool performance tracking."""
        if not self.episodic_memory:
            return
        tool = subtask.tool_hints[0] if subtask.tool_hints else "unknown"
        try:
            await self.episodic_memory.record(
                action=subtask.description[:100],
                outcome=result[:200] if not failed else result.replace("ERROR:", "").strip()[:200],
                success=not failed,
                tool_used=tool,
                context=f"verification: {subtask.verification_criteria[:80]}" if subtask.verification_criteria else None,
            )
        except Exception as e:
            logger.debug(f"Episodic memory record failed: {e}")

    async def _save_delegation_audit(self, task: Task, delegation_log: List[Dict[str, Any]]):
        """#6: Save a structured JSON audit trail alongside the task result file."""
        audit = {
            "task_id": task.id,
            "goal": task.goal,
            "completed_at": datetime.utcnow().isoformat(),
            "total_steps": len(delegation_log),
            "successful_steps": sum(1 for s in delegation_log if s["success"]),
            "re_delegated_steps": sum(1 for s in delegation_log if s["re_delegated"]),
            "irreversible_steps": sum(1 for s in delegation_log if not s["reversible"]),
            "total_tokens": sum(s.get("tokens", 0) for s in delegation_log),
            "steps": delegation_log,
        }
        audit_path = Path(f"./data/tasks/{task.id}_audit.json")
        try:
            audit_path.write_text(json.dumps(audit, indent=2))
            logger.info(f"Delegation audit saved: {audit_path}")
        except Exception as e:
            logger.warning(f"Could not save delegation audit: {e}")
