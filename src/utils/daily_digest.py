"""Daily digest — scheduled report + on-demand /report command.

Generates a summary of Nova's activity: messages handled, tasks completed,
capability backlog status, errors detected/fixed, and uptime.

Runs as a background asyncio task (like ReminderScheduler) and sends a
Telegram digest at a configurable time each day (default 9 AM PST).
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class DailyDigest:
    """Generates and sends daily activity reports via Telegram."""

    def __init__(
        self,
        telegram,
        self_healing_monitor=None,
        log_file: str = "./data/logs/agent.log",
        data_dir: str = "./data",
        digest_hour: int = 9,   # 9 AM
        digest_minute: int = 0
    ):
        """Initialize daily digest.

        Args:
            telegram: TelegramNotifier instance
            self_healing_monitor: SelfHealingMonitor for capability backlog + error stats
            log_file: Path to agent log for activity counting
            data_dir: Data directory for backlog files
            digest_hour: Hour to send daily digest (in local/USER_TZ)
            digest_minute: Minute to send daily digest
        """
        self.telegram = telegram
        self.monitor = self_healing_monitor
        self.log_file = Path(log_file)
        self.data_dir = Path(data_dir)
        self.digest_hour = digest_hour
        self.digest_minute = digest_minute
        self._last_digest_date = None

        logger.info(f"DailyDigest initialized (scheduled at {digest_hour:02d}:{digest_minute:02d})")

    async def start(self):
        """Background loop — check every 60s if it's time to send the digest."""
        logger.info("📊 Daily digest scheduler started")
        while True:
            try:
                await self._check_and_send()
            except Exception as e:
                logger.error(f"Daily digest error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _check_and_send(self):
        """Check if it's time to send the daily digest."""
        try:
            from ..core.timezone import USER_TZ
            now = datetime.now(USER_TZ)
        except ImportError:
            now = datetime.now()

        today = now.date()

        # Already sent today?
        if self._last_digest_date == today:
            return

        # Is it time?
        if now.hour == self.digest_hour and now.minute >= self.digest_minute:
            logger.info("📊 Sending daily digest...")
            report = await self.generate_report(hours=24)
            await self.telegram.notify(report, level="info")
            self._last_digest_date = today
            logger.info("📊 Daily digest sent")

    async def generate_report(self, hours: int = 24) -> str:
        """Generate the activity report.

        Args:
            hours: How many hours back to report on

        Returns:
            Formatted markdown report string
        """
        try:
            from ..core.timezone import USER_TZ
            now = datetime.now(USER_TZ)
        except ImportError:
            now = datetime.now()

        cutoff = now - timedelta(hours=hours)
        period = f"last {hours}h" if hours != 24 else "today"

        # ── Gather stats ──
        log_stats = self._count_log_activity(cutoff)
        capability_summary = self._get_capability_summary()
        error_summary = self._get_error_summary()
        uptime = self._get_uptime()

        # ── Build report ──
        from src.core.config import get_bot_name
        bot_name = get_bot_name()
        lines = [f"📊 **{bot_name} Daily Report** — {now.strftime('%b %d, %Y')}"]
        lines.append("")

        # Activity
        lines.append(f"💬 **Messages handled:** {log_stats['messages']}")
        lines.append(f"✅ **Tasks completed:** {log_stats['tasks_completed']}")
        lines.append(f"🔧 **Tool calls:** {log_stats['tool_calls']}")

        if log_stats['errors_in_tasks'] > 0:
            lines.append(f"⚠️ **Task errors:** {log_stats['errors_in_tasks']}")

        # Capability Backlog
        if capability_summary:
            lines.append("")
            lines.append(capability_summary)

        # Errors & Self-Healing
        if error_summary:
            lines.append("")
            lines.append(error_summary)

        # Uptime
        if uptime:
            lines.append("")
            lines.append(f"⏱️ **Uptime:** {uptime}")

        return "\n".join(lines)

    def _count_log_activity(self, cutoff: datetime) -> Dict[str, int]:
        """Count activity from agent log since cutoff.

        Counts:
        - Messages handled (Starting autonomous execution)
        - Tasks completed (Task completed)
        - Tool calls (Executing tool:)
        - Errors in tasks

        Args:
            cutoff: Count activity after this time

        Returns:
            Dict of counts
        """
        stats = {
            "messages": 0,
            "tasks_completed": 0,
            "tool_calls": 0,
            "errors_in_tasks": 0
        }

        if not self.log_file.exists():
            return stats

        try:
            with open(self.log_file, 'r') as f:
                for line in f:
                    # Check timestamp
                    ts_match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
                    if ts_match:
                        try:
                            line_time = datetime.fromisoformat(ts_match.group(1))
                            # Make naive comparison work
                            cutoff_naive = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff
                            if line_time < cutoff_naive:
                                continue
                        except ValueError:
                            continue

                    # Count patterns
                    if "Starting autonomous execution" in line:
                        stats["messages"] += 1
                    elif "Task completed (end_turn)" in line:
                        stats["tasks_completed"] += 1
                    elif "Executing tool:" in line:
                        stats["tool_calls"] += 1
                    elif "Error in iteration" in line:
                        stats["errors_in_tasks"] += 1

        except Exception as e:
            logger.error(f"Error reading log for digest: {e}")

        return stats

    def _get_capability_summary(self) -> Optional[str]:
        """Get capability backlog summary from the interceptor's backlog file.

        Auto-purges entries older than 3 days (completed/failed) to keep
        the backlog relevant and the daily report clean.
        """
        backlog_file = self.data_dir / "capability_backlog.json"
        if not backlog_file.exists():
            return None

        try:
            with open(backlog_file, 'r') as f:
                backlog = json.load(f)

            if not backlog:
                return None

            # ── Auto-purge stale entries (older than 3 days) ──────────
            cutoff = datetime.now() - timedelta(days=3)
            original_len = len(backlog)
            fresh = []
            for item in backlog:
                detected = item.get("detected_at", "")
                status = item.get("status", "")
                # Keep pending/fixing items regardless of age
                if status in ("pending", "fixing", "fix_pending"):
                    fresh.append(item)
                    continue
                # Purge old fixed/failed entries
                if detected:
                    try:
                        item_time = datetime.fromisoformat(detected)
                        if item_time < cutoff:
                            continue  # skip — stale
                    except (ValueError, TypeError):
                        pass
                fresh.append(item)

            if len(fresh) < original_len:
                purged = original_len - len(fresh)
                with open(backlog_file, 'w') as f:
                    json.dump(fresh, f, indent=2)
                logger.info(f"Purged {purged} stale capability backlog entries (older than 3 days)")

            backlog = fresh
            if not backlog:
                return None

            pending = [i for i in backlog if i.get("status") == "pending"]
            fixed = [i for i in backlog if i.get("status") == "fixed"]
            failed = [i for i in backlog if i.get("status") == "failed"]

            parts = ["🧠 **Capability Backlog:**"]
            if fixed:
                parts.append(f"  ✅ {len(fixed)} learned")
                # Show most recent
                latest = fixed[-1]
                parts.append(f"  ✨ New: _{latest.get('gap_description', '?')}_")
            if pending:
                parts.append(f"  ⏳ {len(pending)} pending")
                for item in pending[:3]:
                    parts.append(f"  • {item.get('gap_description', '?')}")
            if failed:
                parts.append(f"  ❌ {len(failed)} failed")

            return "\n".join(parts) if len(parts) > 1 else None

        except Exception as e:
            logger.error(f"Error reading capability backlog: {e}")
            return None

    def _get_error_summary(self) -> Optional[str]:
        """Get error/fix summary from the self-healing monitor."""
        if not self.monitor:
            return None

        try:
            total_errors = self.monitor.total_errors_detected
            total_fixes = self.monitor.total_fixes_attempted

            if total_errors == 0 and total_fixes == 0:
                return None

            parts = ["🩺 **Self-Healing:**"]
            parts.append(f"  Errors detected: {total_errors}")
            if total_fixes > 0:
                fix_summary = self.monitor.fixer.get_fix_summary()
                successful = fix_summary.get("successful_count", 0)
                parts.append(f"  Auto-fixed: {successful}/{total_fixes}")

            return "\n".join(parts)

        except Exception as e:
            logger.error(f"Error getting monitor summary: {e}")
            return None

    def _get_uptime(self) -> Optional[str]:
        """Get uptime string from monitor startup time."""
        if not self.monitor or not self.monitor.startup_time:
            return None

        try:
            delta = datetime.now() - self.monitor.startup_time
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)

            if hours >= 24:
                days = hours // 24
                hours = hours % 24
                return f"{days}d {hours}h {minutes}m"
            return f"{hours}h {minutes}m"

        except Exception:
            return None
