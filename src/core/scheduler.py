"""Reminder scheduler — background asyncio loop that fires due reminders via Telegram."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """Background scheduler that checks for due reminders and fires them.

    Runs as an asyncio task alongside dashboard and auto_updater.
    Reads the same data/reminders.json that ReminderTool writes to.
    Sends notifications via TelegramNotifier.
    """

    CHECK_INTERVAL = 30  # seconds between checks
    CLEANUP_AFTER_DAYS = 30  # remove fired/cancelled reminders older than this

    def __init__(self, telegram, data_dir: str = "./data"):
        """Initialize reminder scheduler.

        Args:
            telegram: TelegramNotifier instance for sending reminder notifications
            data_dir: Directory where reminders.json lives
        """
        self.telegram = telegram
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

        now = datetime.now()
        fired_any = False

        for reminder in reminders:
            if reminder.get("status") != "pending":
                continue

            try:
                remind_at = datetime.fromisoformat(reminder["remind_at"])
            except (ValueError, KeyError):
                continue

            if remind_at <= now:
                # Fire this reminder
                message = reminder.get("message", "Reminder")
                rid = reminder.get("id", "?")

                try:
                    await self.telegram.notify(
                        f"⏰ *Reminder:* {message}",
                        level="info"
                    )
                    logger.info(f"Fired reminder {rid}: {message}")
                except Exception as e:
                    logger.error(f"Failed to send reminder {rid}: {e}")
                    continue  # Don't mark as fired if notification failed

                reminder["status"] = "fired"
                reminder["fired_at"] = now.isoformat()
                fired_any = True

        if fired_any:
            self._save_reminders(reminders)

    def _cleanup_old(self):
        """Remove fired/cancelled reminders older than CLEANUP_AFTER_DAYS."""
        reminders = self._load_reminders()
        if not reminders:
            return

        cutoff = datetime.now() - timedelta(days=self.CLEANUP_AFTER_DAYS)
        original_count = len(reminders)

        reminders = [
            r for r in reminders
            if r.get("status") == "pending" or self._is_recent(r, cutoff)
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
