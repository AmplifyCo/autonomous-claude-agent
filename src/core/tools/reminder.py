"""Reminder tool — set, list, and cancel reminders with persistent JSON storage.

Supports one-time and recurring reminders (daily, weekly, weekdays, custom interval).
Recurring reminders auto-reschedule after firing via ReminderScheduler.
"""

import json
import logging
import random
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from .base import BaseTool
from ..timezone import USER_TZ
from ..types import ToolResult

logger = logging.getLogger(__name__)


class ReminderTool(BaseTool):
    """Tool for setting, listing, and cancelling reminders.

    Reminders are stored in data/reminders.json and fired by the
    ReminderScheduler background task via Telegram notifications.
    No external dependencies — always available.
    """

    name = "reminder"
    description = (
        "Set, list, and cancel reminders. Two modes:\n"
        "1. PASSIVE (notify only) — use when no tool action is needed: 'Remind me about John's birthday', "
        "'Remind me to call mom'. Just sends a notification at the specified time.\n"
        "2. ACTIVE (execute action) — use when a task must be performed at a future time: "
        "'Post on LinkedIn at 9 AM', 'Send email tomorrow morning', 'Book restaurant on Mar 28'. "
        "Set action_goal to the full task description. When the reminder fires, Nova will execute "
        "that goal using the right tools automatically — do NOT just set a passive reminder for these.\n\n"
        "Supports RECURRING reminders: set recurrence to 'daily', 'weekdays', 'weekly', or 'Nd' (every N days). "
        "For random timing within a window (e.g. 'between 6-8 PM'), set random_window_minutes."
    )
    parameters = {
        "operation": {
            "type": "string",
            "description": "Operation: 'set_reminder', 'list_reminders', 'cancel_reminder'",
            "enum": ["set_reminder", "list_reminders", "cancel_reminder"]
        },
        "message": {
            "type": "string",
            "description": "Human-readable reminder label shown in notification (for set_reminder)"
        },
        "remind_at": {
            "type": "string",
            "description": "When to fire. Accepts absolute 'YYYY-MM-DD HH:MM' or relative like '30m', '2h', '1d', '90s', '1h30m' (for set_reminder)"
        },
        "action_goal": {
            "type": "string",
            "description": (
                "REQUIRED when the reminder must execute a task (post, send, book, call, etc.). "
                "Write the full goal as you would pass it to nova_task. "
                "Example: 'Post the LinkedIn post from linkedin_post.txt using the linkedin tool'. "
                "Leave empty for passive notify-only reminders."
            )
        },
        "recurrence": {
            "type": "string",
            "description": (
                "For recurring reminders. Options: 'daily' (every day), 'weekdays' (Mon-Fri), "
                "'weekly' (same day each week), or 'Nd' for every N days (e.g. '3d' = every 3 days). "
                "Leave empty for one-time reminders."
            )
        },
        "random_window_minutes": {
            "type": "integer",
            "description": (
                "If set, the reminder fires at a random time within this many minutes after remind_at. "
                "E.g. remind_at='2025-03-05 18:00' + random_window_minutes=120 → fires randomly between 6-8 PM. "
                "Default: 0 (fire at exact time)."
            )
        },
        "channel": {
            "type": "string",
            "description": "Channel to notify and run the action on when it fires: 'telegram' or 'whatsapp'. Default: 'telegram'.",
            "enum": ["telegram", "whatsapp"]
        },
        "reminder_id": {
            "type": "string",
            "description": "Reminder ID to cancel (for cancel_reminder)"
        }
    }

    def __init__(self, data_dir: str = "./data"):
        """Initialize reminder tool.

        Args:
            data_dir: Directory for persistent storage
        """
        self.data_dir = Path(data_dir)
        self.reminders_file = self.data_dir / "reminders.json"

    def to_anthropic_tool(self) -> Dict[str, Any]:
        """Override to make only 'operation' required."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": ["operation"]
            }
        }

    async def execute(
        self,
        operation: str,
        message: Optional[str] = None,
        remind_at: Optional[str] = None,
        action_goal: Optional[str] = None,
        recurrence: Optional[str] = None,
        random_window_minutes: Optional[int] = None,
        channel: str = "telegram",
        reminder_id: Optional[str] = None,
        **kwargs
    ) -> ToolResult:
        """Execute reminder operation."""
        try:
            if operation == "set_reminder":
                return self._set_reminder(message, remind_at, action_goal, channel,
                                          recurrence, random_window_minutes)
            elif operation == "list_reminders":
                return self._list_reminders()
            elif operation == "cancel_reminder":
                return self._cancel_reminder(reminder_id)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            logger.error(f"Reminder operation error: {e}", exc_info=True)
            return ToolResult(success=False, error=f"Reminder operation failed: {str(e)}")

    # Valid recurrence values
    _VALID_RECURRENCE = {"daily", "weekdays", "weekly"}

    def _set_reminder(
        self,
        message: Optional[str],
        remind_at: Optional[str],
        action_goal: Optional[str] = None,
        channel: str = "telegram",
        recurrence: Optional[str] = None,
        random_window_minutes: Optional[int] = None,
    ) -> ToolResult:
        """Set a new reminder (passive notify or active action, one-time or recurring)."""
        if not message:
            return ToolResult(success=False, error="Reminder message is required")
        if not remind_at:
            return ToolResult(success=False, error="remind_at is required. Use 'YYYY-MM-DD HH:MM' or relative like '30m', '2h', '1d'")

        now = datetime.now(USER_TZ)

        # Try relative time first (e.g. "30m", "2h", "1d", "90s", "1h30m")
        remind_dt = self._parse_relative_time(remind_at.strip(), now)

        if not remind_dt:
            # Try absolute datetime
            try:
                remind_dt = datetime.strptime(remind_at.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=USER_TZ)
            except ValueError:
                try:
                    remind_dt = datetime.fromisoformat(remind_at.strip())
                    if remind_dt.tzinfo is None:
                        remind_dt = remind_dt.replace(tzinfo=USER_TZ)
                except ValueError:
                    return ToolResult(
                        success=False,
                        error=f"Invalid time format: '{remind_at}'. Use 'YYYY-MM-DD HH:MM' or relative like '30m', '2h', '1d'"
                    )

        # For recurring reminders with past base time, advance to next occurrence
        if remind_dt < now and recurrence:
            remind_dt = self._advance_to_next(remind_dt, recurrence, now)

        # Reject past reminders (non-recurring)
        if remind_dt < now:
            return ToolResult(
                success=False,
                error=f"Cannot set reminder in the past. It's currently {now.strftime('%Y-%m-%d %H:%M')}."
            )

        # Apply random window offset for this first occurrence
        actual_fire_dt = remind_dt
        if random_window_minutes and random_window_minutes > 0:
            offset = random.randint(0, random_window_minutes)
            actual_fire_dt = remind_dt + timedelta(minutes=offset)

        # Validate recurrence
        recurrence_str = None
        if recurrence and recurrence.strip():
            rec = recurrence.strip().lower()
            # Accept 'daily', 'weekdays', 'weekly', or 'Nd' (e.g. '3d')
            if rec in self._VALID_RECURRENCE:
                recurrence_str = rec
            elif re.match(r'^\d+d$', rec):
                recurrence_str = rec
            else:
                return ToolResult(
                    success=False,
                    error=f"Invalid recurrence: '{recurrence}'. Use 'daily', 'weekdays', 'weekly', or 'Nd' (e.g. '3d')."
                )

        # Generate unique ID
        rid = uuid.uuid4().hex[:8]

        reminder = {
            "id": rid,
            "message": message,
            "remind_at": actual_fire_dt.isoformat(),
            "base_time": remind_dt.strftime("%H:%M"),  # preserve the base hour:minute for rescheduling
            "created_at": now.isoformat(),
            "status": "pending",
            "channel": channel or "telegram",
        }
        if action_goal and action_goal.strip():
            reminder["action_goal"] = action_goal.strip()
        if recurrence_str:
            reminder["recurrence"] = recurrence_str
        if random_window_minutes and random_window_minutes > 0:
            reminder["random_window_minutes"] = int(random_window_minutes)

        # Save
        reminders = self._load_reminders()
        reminders.append(reminder)
        self._save_reminders(reminders)

        formatted_time = actual_fire_dt.strftime("%Y-%m-%d %H:%M")
        kind = "action" if reminder.get("action_goal") else "notification"
        recur_label = f" (recurring: {recurrence_str})" if recurrence_str else ""
        window_label = f" (±{random_window_minutes}min window)" if random_window_minutes else ""
        logger.info(f"Reminder set ({kind}): {rid} — '{message}' at {formatted_time}{recur_label}")

        return ToolResult(
            success=True,
            output=f"{'Action reminder' if reminder.get('action_goal') else 'Reminder'} set for {formatted_time}: {message}{recur_label}{window_label} (ID: {rid})",
            metadata={"reminder_id": rid, "remind_at": actual_fire_dt.isoformat(),
                       "has_action": bool(reminder.get("action_goal")),
                       "recurrence": recurrence_str}
        )

    @staticmethod
    def _advance_to_next(base_dt: datetime, recurrence: str, now: datetime) -> datetime:
        """Advance a past base_dt to the next future occurrence based on recurrence."""
        rec = recurrence.lower()
        dt = base_dt

        if rec == "daily":
            while dt < now:
                dt += timedelta(days=1)
        elif rec == "weekdays":
            while dt < now or dt.weekday() >= 5:  # skip weekends
                dt += timedelta(days=1)
        elif rec == "weekly":
            while dt < now:
                dt += timedelta(weeks=1)
        elif re.match(r'^\d+d$', rec):
            days = int(rec[:-1])
            while dt < now:
                dt += timedelta(days=days)
        else:
            # Fallback: daily
            while dt < now:
                dt += timedelta(days=1)
        return dt

    def _list_reminders(self) -> ToolResult:
        """List all pending reminders."""
        reminders = self._load_reminders()

        # Filter pending only
        pending = [r for r in reminders if r.get("status") == "pending"]

        if not pending:
            return ToolResult(success=True, output="No active reminders.")

        # Sort by remind_at
        pending.sort(key=lambda r: r.get("remind_at", ""))

        lines = []
        for i, r in enumerate(pending, 1):
            try:
                dt = datetime.fromisoformat(r["remind_at"])
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                time_str = r.get("remind_at", "unknown")
            kind = " [ACTION]" if r.get("action_goal") else ""
            recur = f" 🔄{r['recurrence']}" if r.get("recurrence") else ""
            lines.append(f"{i}. [{r['id']}]{kind}{recur} {time_str} — {r.get('message', 'No message')}")

        return ToolResult(
            success=True,
            output=f"Active reminders ({len(pending)}):\n" + "\n".join(lines)
        )

    def _cancel_reminder(self, reminder_id: Optional[str]) -> ToolResult:
        """Cancel a reminder by ID."""
        if not reminder_id:
            return ToolResult(success=False, error="reminder_id is required")

        reminders = self._load_reminders()

        found = False
        for r in reminders:
            if r.get("id") == reminder_id and r.get("status") == "pending":
                r["status"] = "cancelled"
                r["cancelled_at"] = datetime.now(USER_TZ).isoformat()
                found = True
                break

        if not found:
            return ToolResult(
                success=False,
                error=f"Reminder '{reminder_id}' not found or already fired/cancelled"
            )

        self._save_reminders(reminders)
        logger.info(f"Reminder cancelled: {reminder_id}")

        return ToolResult(success=True, output=f"Reminder {reminder_id} cancelled.")

    def _parse_relative_time(self, value: str, now: datetime) -> Optional[datetime]:
        """Parse relative time strings like '30m', '2h', '1d', '90s', '1h30m'.

        Returns datetime or None if not a relative format.
        """
        # Match patterns like: 30m, 2h, 1d, 90s, 1h30m, 2h15m
        pattern = r'^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$'
        match = re.match(pattern, value.lower().strip())
        if not match:
            return None

        days, hours, minutes, seconds = match.groups()
        if not any([days, hours, minutes, seconds]):
            return None

        delta = timedelta(
            days=int(days or 0),
            hours=int(hours or 0),
            minutes=int(minutes or 0),
            seconds=int(seconds or 0)
        )

        if delta.total_seconds() <= 0:
            return None

        return now + delta

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
