"""Reminder scheduler — background asyncio loop that fires due reminders via Telegram.

Supports recurring reminders: after firing, auto-reschedules based on recurrence
(daily, weekdays, weekly, Nd). Random window offsets applied on each reschedule.
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional
from .timezone import USER_TZ

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """Background scheduler that checks for due reminders and fires them.

    Two reminder modes:
    - Passive (no action_goal): sends a Telegram notification only.
    - Active (has action_goal): enqueues the goal as a background task via TaskQueue
      so Nova actually executes the action (post, send, call, etc.) at the right time.

    Runs as an asyncio task alongside dashboard and auto_updater.
    Reads the same data/reminders.json that ReminderTool writes to.
    """

    CHECK_INTERVAL = 30  # seconds between checks
    CLEANUP_AFTER_DAYS = 30  # remove fired/cancelled reminders older than this

    def __init__(self, telegram, data_dir: str = "./data", task_queue=None):
        """Initialize reminder scheduler.

        Args:
            telegram: TelegramNotifier instance for sending reminder notifications
            data_dir: Directory where reminders.json lives
            task_queue: TaskQueue instance for enqueuing action reminders (optional)
        """
        self.telegram = telegram
        self.task_queue = task_queue
        self.reminders_file = Path(data_dir) / "reminders.json"
        self._cleanup_counter = 0

    async def start(self):
        """Main loop — check every 30 seconds for due reminders."""
        logger.info("⏰ Reminder scheduler started")
        while True:
            try:
                await self._check_and_fire()

                # Cleanup old reminders once every ~100 loops (~50 min)
                self._cleanup_counter += 1
                if self._cleanup_counter >= 100:
                    self._cleanup_old()
                    self._cleanup_counter = 0

            except Exception as e:
                logger.error(f"Reminder scheduler error: {e}", exc_info=True)

            await asyncio.sleep(self.CHECK_INTERVAL)

    async def _check_and_fire(self):
        """Check for due reminders and fire them."""
        reminders = self._load_reminders()
        if not reminders:
            return

        now = datetime.now(USER_TZ)
        fired_any = False

        for reminder in reminders:
            if reminder.get("status") != "pending":
                continue

            try:
                remind_at = datetime.fromisoformat(reminder["remind_at"])
                if remind_at.tzinfo is None:
                    remind_at = remind_at.replace(tzinfo=USER_TZ)
            except (ValueError, KeyError):
                continue

            if remind_at <= now:
                message = reminder.get("message", "Reminder")
                action_goal = reminder.get("action_goal", "")
                channel = reminder.get("channel", "telegram")
                rid = reminder.get("id", "?")

                try:
                    if action_goal and self.task_queue:
                        # Active reminder — enqueue task and notify
                        task_id = self.task_queue.enqueue(
                            goal=action_goal,
                            channel=channel,
                        )
                        await self.telegram.notify(
                            f"⏰ *Scheduled action starting:* {message}\n_Nova is now executing this task._",
                            level="info"
                        )
                        logger.info(f"Fired action reminder {rid}: enqueued task {task_id} — {action_goal[:80]}")
                    elif action_goal and not self.task_queue:
                        # action_goal set but no task_queue wired — fall back to notification + warn
                        await self.telegram.notify(
                            f"⏰ *Reminder:* {message}\n⚠️ _Could not execute action automatically — task queue unavailable._",
                            level="warning"
                        )
                        logger.warning(f"Action reminder {rid} fired but task_queue not available — notified only")
                    else:
                        # Passive reminder — notify only
                        await self.telegram.notify(
                            f"⏰ *Reminder:* {message}",
                            level="info"
                        )
                        logger.info(f"Fired reminder {rid}: {message}")
                except Exception as e:
                    logger.error(f"Failed to fire reminder {rid}: {e}")
                    continue  # Don't mark as fired if it failed

                # Handle recurring vs one-time
                if reminder.get("recurrence"):
                    self._reschedule_recurring(reminder, now)
                    logger.info(f"Recurring reminder {rid} rescheduled: next at {reminder['remind_at']}")
                else:
                    reminder["status"] = "fired"
                    reminder["fired_at"] = now.isoformat()
                fired_any = True

        if fired_any:
            self._save_reminders(reminders)

    def _reschedule_recurring(self, reminder: Dict[str, Any], now: datetime):
        """Reschedule a recurring reminder for its next occurrence.

        Uses base_time (HH:MM) to maintain consistent scheduling,
        then applies random_window_minutes offset if configured.
        """
        recurrence = reminder.get("recurrence", "daily")
        base_time_str = reminder.get("base_time", "09:00")

        try:
            base_hour, base_minute = map(int, base_time_str.split(":"))
        except (ValueError, AttributeError):
            base_hour, base_minute = 9, 0

        # Calculate next base date
        next_date = now.date() + timedelta(days=1)  # start from tomorrow

        if recurrence == "daily":
            pass  # tomorrow is fine
        elif recurrence == "weekdays":
            while next_date.weekday() >= 5:  # skip Sat/Sun
                next_date += timedelta(days=1)
        elif recurrence == "weekly":
            next_date += timedelta(days=6)  # 7 days from now (tomorrow + 6)
        elif re.match(r'^\d+d$', recurrence):
            days = int(recurrence[:-1])
            next_date = now.date() + timedelta(days=days)
        # else: default to daily (tomorrow)

        next_dt = datetime(
            next_date.year, next_date.month, next_date.day,
            base_hour, base_minute, tzinfo=USER_TZ
        )

        # Apply random window offset
        window = reminder.get("random_window_minutes", 0)
        if window and window > 0:
            offset = random.randint(0, window)
            next_dt += timedelta(minutes=offset)

        reminder["remind_at"] = next_dt.isoformat()
        reminder["last_fired"] = now.isoformat()
        # status stays "pending" so it fires again

    def _cleanup_old(self):
        """Remove fired/cancelled reminders older than CLEANUP_AFTER_DAYS."""
        reminders = self._load_reminders()
        if not reminders:
            return

        cutoff = datetime.now(USER_TZ) - timedelta(days=self.CLEANUP_AFTER_DAYS)
        original_count = len(reminders)

        reminders = [
            r for r in reminders
            if r.get("status") == "pending" or r.get("recurrence") or self._is_recent(r, cutoff)
        ]

        removed = original_count - len(reminders)
        if removed > 0:
            self._save_reminders(reminders)
            logger.info(f"Cleaned up {removed} old reminders")

    def _is_recent(self, reminder: Dict[str, Any], cutoff: datetime) -> bool:
        """Check if a non-pending reminder is recent enough to keep."""
        timestamp_key = "fired_at" if reminder.get("status") == "fired" else "cancelled_at"
        ts = reminder.get(timestamp_key, reminder.get("created_at", ""))
        try:
            return datetime.fromisoformat(ts) > cutoff
        except (ValueError, TypeError):
            return False  # Can't parse → remove it

    def _load_reminders(self) -> List[Dict[str, Any]]:
        """Load reminders from JSON file."""
        if not self.reminders_file.exists():
            return []
        try:
            with open(self.reminders_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load reminders file: {e}")
            return []

    def _save_reminders(self, reminders: List[Dict[str, Any]]):
        """Save reminders to JSON file."""
        self.reminders_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.reminders_file, 'w') as f:
            json.dump(reminders, f, indent=2, default=str)
