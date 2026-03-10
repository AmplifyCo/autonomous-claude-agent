"""Attention Engine — Nova proactively notices things and surfaces them.

Driven by NovaPurpose: different times of day trigger different observation
modes (morning briefing, evening summary, weekly look-ahead, curiosity scan).

Runs every 6 hours. Generates 1-3 observations using the LLM.
Sends via Telegram. Never sends more than once per topic per 24h.

Security:
- All LLM calls use the same security budget as regular Nova calls.
- Dedup log stored locally in data/attention_log.json — no PII.
- Never sends raw contact data or message content — only observations.
"""

import asyncio
import json
import logging
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..timezone import now as tz_now
from .nova_purpose import NovaPurpose, PurposeMode

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 6 * 3600   # 6 hours
MAX_ITEMS      = 3          # Max observations per cycle
MAX_OBS_LEN    = 280        # Max characters per observation sent to Telegram
_MD_LINK_RE    = re.compile(r'\[([^\]]*)\]\([^)]+\)')  # [text](url) → text
_RAW_URL_RE    = re.compile(r'https?://\S+')


class AttentionEngine:
    """Background loop that proactively surfaces relevant observations."""

    def __init__(
        self,
        digital_brain,
        llm_client,
        telegram_notifier,
        owner_name: str = "User",
        purpose: Optional[NovaPurpose] = None,
        pattern_detector=None,
        contact_intelligence=None,
        episodic_memory=None,
        task_queue=None,
    ):
        self.brain = digital_brain
        self.llm = llm_client
        self.telegram = telegram_notifier
        self.owner_name = owner_name
        self.purpose = purpose or NovaPurpose()
        self.pattern_detector = pattern_detector          # 2A: behavioral patterns
        self.contact_intelligence = contact_intelligence  # 2D: contact tracking
        self.episodic_memory = episodic_memory            # For periodic memory cleanup
        self.task_queue = task_queue                      # For morning sweep triage
        self._log_path = Path("data/attention_log.json")
        self._is_running = False
        self._last_forget_date = None                     # Run forget at most once/day
        self._last_sweep_date = None                      # Morning sweep at most once/day

    # ── Background loop ───────────────────────────────────────────────

    async def start(self):
        """Start the background attention loop."""
        self._is_running = True
        logger.info("🔍 Attention Engine started")

        while self._is_running:
            try:
                await self._scan_and_surface()
            except Exception as e:
                logger.error(f"AttentionEngine error: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL)

    async def stop(self):
        self._is_running = False

    # ── Core scan ─────────────────────────────────────────────────────

    async def _scan_and_surface(self):
        """Scan memory and surface purpose-driven observations."""
        now = tz_now()

        # Only send during waking hours (7am – 9pm) in user's timezone
        if not (7 <= now.hour <= 21):
            logger.debug("AttentionEngine: outside waking hours, skipping")
            return

        mode = self.purpose.get_mode(now)
        logger.info(f"🔍 Attention scan running (mode={mode.value})...")

        # Build context from memory
        snippets = await self._gather_memory_snippets()
        if not snippets:
            return

        # Build purpose-driven prompt and generate observations
        prompt = self.purpose.build_prompt(mode, snippets, self.owner_name, now)
        observations = await self._generate_observations_from_prompt(prompt)
        if not observations:
            return

        # Filter already-sent topics
        new_obs = [o for o in observations if not self._already_sent(o)]
        if not new_obs:
            return

        # Send via Telegram with purpose-appropriate header
        header = self.purpose.get_header(mode, self.owner_name, now)
        await self._notify_with_header(new_obs, header)

        # Log sent topics
        for o in new_obs:
            self._mark_sent(o)

        # Morning sweep — once per day during morning mode, triage pending tasks
        if mode == PurposeMode.MORNING:
            await self._morning_sweep()

        # Periodic memory cleanup — once per day, forget old low-importance memories
        await self._periodic_forget()

    async def _periodic_forget(self):
        """Run intentional forgetting at most once per day."""
        if not self.episodic_memory:
            return

        today = tz_now().date().isoformat()
        if self._last_forget_date == today:
            return

        try:
            forgotten = await self.episodic_memory.forget_old(
                max_age_days=90,
                min_importance=0.3,
            )
            self._last_forget_date = today
            if forgotten > 0:
                logger.info(f"Memory cleanup: forgot {forgotten} old low-importance episodes")
        except Exception as e:
            logger.debug(f"Memory cleanup failed: {e}")

    async def _morning_sweep(self):
        """Morning sweep — triage pending/active tasks and send a structured brief.

        Inspired by Jim Prosser's 'chief of staff' system: classify tasks by
        dispatch level (green/yellow/red/gray) and present an action-ready brief.
        Runs at most once per day during morning mode.
        """
        today = tz_now().date().isoformat()
        if self._last_sweep_date == today:
            return
        if not self.task_queue:
            self._last_sweep_date = today
            return

        try:
            # Pull active + recently completed tasks
            tasks = self.task_queue.get_active_and_recent_tasks(completed_hours=12)
            if not tasks:
                self._last_sweep_date = today
                return

            # Build sweep brief
            lines = [f"📊 **Morning Sweep** — {self.owner_name}'s task triage"]
            dispatch_icons = {"green": "🟢", "yellow": "🟡", "red": "🔴", "gray": "⚪"}

            active = [t for t in tasks if t.status in ("pending", "decomposing", "running")]
            completed = [t for t in tasks if t.status == "done"]
            failed = [t for t in tasks if t.status == "failed"]

            if active:
                lines.append(f"\n⏳ **Active ({len(active)}):**")
                for t in active[:5]:
                    # Summarize dispatch breakdown of subtasks
                    dispatch_counts = {}
                    total_mins = 0
                    for st in t.subtasks:
                        d = getattr(st, "dispatch", "green")
                        dispatch_counts[d] = dispatch_counts.get(d, 0) + 1
                        total_mins += getattr(st, "estimated_minutes", 5)
                    dispatch_str = " ".join(
                        f"{dispatch_icons.get(d, '⚪')}{c}" for d, c in sorted(dispatch_counts.items())
                    )
                    progress = sum(1 for st in t.subtasks if st.status == "done")
                    total = len(t.subtasks)
                    time_est = f"~{total_mins}min" if total_mins else ""
                    lines.append(
                        f"  • {t.goal[:60]} [{progress}/{total}] {dispatch_str} {time_est}"
                    )

            if completed:
                lines.append(f"\n✅ **Done overnight ({len(completed)}):**")
                for t in completed[:3]:
                    lines.append(f"  • {t.goal[:60]}")

            if failed:
                lines.append(f"\n❌ **Needs attention ({len(failed)}):**")
                for t in failed[:3]:
                    err = (t.error or "unknown")[:40]
                    lines.append(f"  • {t.goal[:50]} — {err}")

            # Only send if there's meaningful content
            if len(lines) > 1:
                await self.telegram.notify("\n".join(lines), level="info")
                logger.info(f"Morning sweep sent: {len(active)} active, {len(completed)} done, {len(failed)} failed")

            self._last_sweep_date = today

        except Exception as e:
            logger.debug(f"Morning sweep failed: {e}")
            self._last_sweep_date = today

    async def _gather_memory_snippets(self) -> str:
        """Pull relevant memory context for attention analysis."""
        parts = []

        try:
            # Recent conversations — use whichever API the brain supports
            query = "recent conversations tasks reminders follow-up"
            if hasattr(self.brain, 'get_relevant_context'):
                try:
                    recent = await self.brain.get_relevant_context(
                        query, max_results=5, channel="telegram"
                    )
                except TypeError:
                    recent = await self.brain.get_relevant_context(query, max_results=5)
                if recent:
                    parts.append(f"Recent activity:\n{recent[:800]}")
            elif hasattr(self.brain, 'search_context'):
                recent = await self.brain.search_context(query, channel="telegram", n_results=5)
                if recent:
                    parts.append(f"Recent activity:\n{recent[:800]}")
        except Exception as e:
            logger.debug(f"Memory snippet error: {e}")

        # ── Inject detected behavioral patterns (2A) ──
        if self.pattern_detector:
            try:
                patterns_ctx = self.pattern_detector.get_patterns_context()
                if patterns_ctx:
                    parts.append(patterns_ctx)
            except Exception as e:
                logger.debug(f"Pattern context error: {e}")

        # ── Inject contact intelligence (2D) — follow-ups + stale contacts ──
        if self.contact_intelligence:
            try:
                followups = self.contact_intelligence.get_followup_context()
                if followups:
                    parts.append(followups)
                stale = self.contact_intelligence.get_stale_contacts(days=14)
                if stale:
                    stale_lines = [f"  - {s['name']}: last contacted {s['last_date']}" for s in stale[:3]]
                    parts.append("People not contacted recently:\n" + "\n".join(stale_lines))
            except Exception as e:
                logger.debug(f"Contact intelligence attention error: {e}")

        return "\n\n".join(parts) if parts else ""

    _ATTENTION_MODELS = ["gemini/gemini-2.0-flash", "claude-haiku-4-5-20251001"]

    async def _generate_observations_from_prompt(self, prompt: str) -> list:
        """Use LLM with the given purpose-built prompt to generate observations.

        Tries Gemini Flash first, falls back to Claude Haiku.
        """
        if not self.llm:
            return []

        for model in self._ATTENTION_MODELS:
            try:
                resp = await self.llm.create_message(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=256,
                )
                text = resp.content[0].text.strip()
                # Strip markdown fences if present
                text = text.replace("```json", "").replace("```", "").strip()
                result = json.loads(text)
                if not isinstance(result, list):
                    continue
                # Sanitize each observation before returning
                prompt_names = self._extract_prompt_names(prompt)
                sanitized = []
                for obs in result:
                    if not isinstance(obs, str) or not obs.strip():
                        continue
                    sanitized.append(self._sanitize_observation(obs, prompt_names))
                return sanitized
            except Exception as e:
                logger.debug(f"Attention LLM ({model}) failed: {e}")

        return []

    @staticmethod
    def _extract_prompt_names(prompt: str) -> set:
        """Extract capitalized names present in the prompt for hallucination check."""
        # Grab capitalized words (2+ chars) that look like proper names
        words = set(re.findall(r'\b[A-Z][a-z]{1,}\b', prompt))
        # Exclude common English words that happen to be capitalized
        stop = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday", "January", "February", "March",
                "April", "May", "June", "July", "August", "September",
                "October", "November", "December", "Today", "Memory",
                "Reply", "JSON", "Be", "What", "Time", "Good", "One",
                "People", "Anything", "Scan", "Items", "If", "No"}
        return words - stop

    @staticmethod
    def _sanitize_observation(obs: str, prompt_names: set) -> str:
        """Sanitize a single LLM observation before sending to Telegram.

        - Strip markdown links [text](url) → text
        - Remove raw URLs
        - Cap length
        - Warn if observation mentions names not present in prompt
        """
        # Strip markdown links, keep anchor text
        clean = _MD_LINK_RE.sub(r'\1', obs)
        # Remove raw URLs
        clean = _RAW_URL_RE.sub('', clean).strip()
        # Cap length
        if len(clean) > MAX_OBS_LEN:
            clean = clean[:MAX_OBS_LEN - 1] + "\u2026"
        # Check for hallucinated names
        obs_names = set(re.findall(r'\b[A-Z][a-z]{1,}\b', clean))
        unknown = obs_names - prompt_names - {
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday", "January", "February", "March",
            "April", "May", "June", "July", "August", "September",
            "October", "November", "December", "Today", "Nova"}
        if unknown:
            logger.warning(f"Attention observation contains names not in prompt: {unknown}")
        return clean

    async def _notify_with_header(self, observations: list, header: str):
        """Send observations via Telegram with the purpose-appropriate header."""
        if not self.telegram or not observations:
            return

        lines = [header]
        for obs in observations:
            lines.append(f"  • {obs}")

        try:
            await self.telegram.notify("\n".join(lines), level="info")
            logger.info(f"Attention Engine sent {len(observations)} observation(s) [{header[:30]}]")
        except Exception as e:
            logger.error(f"Attention notify failed: {e}")

    # ── Dedup log ─────────────────────────────────────────────────────

    def _load_log(self) -> dict:
        if self._log_path.exists():
            try:
                return json.loads(self._log_path.read_text())
            except Exception:
                pass
        return {}

    def _save_log(self, log: dict):
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(log, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self._log_path.parent, suffix=".tmp")
        try:
            with open(fd, "w") as f:
                f.write(data)
            Path(tmp).rename(self._log_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _already_sent(self, observation: str) -> bool:
        """Return True if this observation was sent in the last 24 hours."""
        log = self._load_log()
        key = observation[:50].lower()
        if key in log:
            sent_at = datetime.fromisoformat(log[key])
            now = tz_now()
            # Ensure both are aware or both naive for comparison
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=now.tzinfo)
            if now - sent_at < timedelta(hours=24):
                return True
        return False

    def _mark_sent(self, observation: str):
        log = self._load_log()
        key = observation[:50].lower()
        log[key] = tz_now().isoformat()
        # Prune old entries (keep last 100)
        if len(log) > 100:
            oldest = sorted(log.items(), key=lambda x: x[1])[:20]
            for k, _ in oldest:
                del log[k]
        self._save_log(log)
