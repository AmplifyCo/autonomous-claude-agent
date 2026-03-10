"""Persistent task queue for Nova's background autonomous execution.

Uses SQLite so tasks survive service restarts and are safe for concurrent access.
Tasks move through: pending → decomposing → running → done | failed

Design inspired by SagaLLM checkpointing and Voyager skill library patterns.
"""

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Subtask:
    """A single decomposed step within a larger task."""
    description: str
    tool_hints: List[str] = field(default_factory=list)
    model_tier: str = "flash"
    status: str = "pending"   # pending | running | done | failed | skipped
    result: str = ""
    error: str = ""
    verification_criteria: str = ""       # How to confirm this step actually succeeded
    reversible: bool = True               # False = cannot be undone (send email, post tweet, delete)
    depends_on: List[int] = field(default_factory=list)  # 0-indexed step indices this must wait for
    execution_mode: str = "self"          # "self" | "delegate" — Eisenhower Matrix decision
    delegate_to: str = ""                 # Agent name from known_agents.json (if execution_mode="delegate")
    priority: str = "q2"                  # Eisenhower quadrant: q1 (do), q2 (schedule), q3 (delegate), q4 (eliminate)
    orchestration_strategy: str = "auto"  # "auto" | "race" | "fan_out" — multi-agent strategy
    dispatch: str = "green"               # Prosser dispatch: green (AI fully), yellow (AI 80%), red (human needed), gray (skip today)
    estimated_minutes: int = 5            # Estimated execution time in minutes


@dataclass
class Task:
    """A persistent background task with decomposed subtasks."""
    id: str
    goal: str
    channel: str           # whatsapp | telegram | voice — where to notify on completion
    user_id: str           # user identifier for the notification
    status: str            # pending | decomposing | running | done | failed
    subtasks: List[Subtask] = field(default_factory=list)
    result: str = ""
    error: str = ""
    notify_on_complete: bool = True
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    def current_subtask_idx(self) -> int:
        """Return index of first non-done subtask, or -1 if all done."""
        for i, st in enumerate(self.subtasks):
            if st.status in ("pending", "running", "failed"):
                return i
        return -1

    def all_subtasks_done(self) -> bool:
        return all(st.status in ("done", "skipped") for st in self.subtasks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "channel": self.channel,
            "user_id": self.user_id,
            "status": self.status,
            "subtasks": [
                {
                    "description": st.description,
                    "tool_hints": st.tool_hints,
                    "model_tier": st.model_tier,
                    "status": st.status,
                    "result": st.result,
                    "error": st.error,
                    "verification_criteria": st.verification_criteria,
                    "reversible": st.reversible,
                    "depends_on": st.depends_on,
                    "execution_mode": st.execution_mode,
                    "delegate_to": st.delegate_to,
                    "priority": st.priority,
                    "orchestration_strategy": st.orchestration_strategy,
                    "dispatch": st.dispatch,
                    "estimated_minutes": st.estimated_minutes,
                }
                for st in self.subtasks
            ],
            "result": self.result,
            "error": self.error,
            "notify_on_complete": self.notify_on_complete,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class TaskQueue:
    """SQLite-backed persistent task queue.

    Thread/async-safe: each operation opens its own connection.
    Survives service restarts — tasks persist until explicitly completed.
    """

    def __init__(self, data_dir: str = "./data"):
        self.db_path = Path(data_dir) / "nova_tasks.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"TaskQueue initialized at {self.db_path}")

    @contextmanager
    def _conn(self):
        """Yield a short-lived SQLite connection with WAL mode for concurrency."""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'telegram',
                    user_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    subtasks_json TEXT NOT NULL DEFAULT '[]',
                    result TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    notify_on_complete INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
            """)

    # ── Write operations ─────────────────────────────────────────────────────

    def enqueue(
        self,
        goal: str,
        channel: str = "telegram",
        user_id: str = "",
        notify_on_complete: bool = True,
    ) -> str:
        """Add a new task to the queue. Returns the task ID."""
        task_id = uuid.uuid4().hex[:12]
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO tasks
                   (id, goal, channel, user_id, status, subtasks_json,
                    notify_on_complete, created_at)
                   VALUES (?, ?, ?, ?, 'pending', '[]', ?, ?)""",
                (task_id, goal, channel, user_id, int(notify_on_complete), now),
            )
        logger.info(f"Enqueued task {task_id}: {goal[:60]}")
        return task_id

    def set_subtasks(self, task_id: str, subtasks: List[Subtask]):
        """Store decomposed subtasks and mark task as 'running'."""
        subtasks_json = json.dumps([
            {
                "description": st.description,
                "tool_hints": st.tool_hints,
                "model_tier": st.model_tier,
                "status": st.status,
                "result": st.result,
                "error": st.error,
                "verification_criteria": st.verification_criteria,
                "reversible": st.reversible,
                "execution_mode": st.execution_mode,
                "delegate_to": st.delegate_to,
                "priority": st.priority,
                "orchestration_strategy": st.orchestration_strategy,
                "dispatch": st.dispatch,
                "estimated_minutes": st.estimated_minutes,
            }
            for st in subtasks
        ])
        with self._conn() as conn:
            conn.execute(
                """UPDATE tasks SET subtasks_json=?, status='running', started_at=?
                   WHERE id=?""",
                (subtasks_json, datetime.utcnow().isoformat(), task_id),
            )

    def update_subtask(self, task_id: str, subtask_idx: int, status: str, result: str = "", error: str = ""):
        """Update a single subtask's status and result."""
        task = self.get_task(task_id)
        if not task:
            return
        if subtask_idx >= len(task.subtasks):
            return
        task.subtasks[subtask_idx].status = status
        task.subtasks[subtask_idx].result = result
        task.subtasks[subtask_idx].error = error
        subtasks_json = json.dumps([
            {
                "description": st.description,
                "tool_hints": st.tool_hints,
                "model_tier": st.model_tier,
                "status": st.status,
                "result": st.result,
                "error": st.error,
                "verification_criteria": st.verification_criteria,
                "reversible": st.reversible,
                "execution_mode": st.execution_mode,
                "delegate_to": st.delegate_to,
                "priority": st.priority,
                "orchestration_strategy": st.orchestration_strategy,
                "dispatch": st.dispatch,
                "estimated_minutes": st.estimated_minutes,
            }
            for st in task.subtasks
        ])
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET subtasks_json=? WHERE id=?",
                (subtasks_json, task_id),
            )

    def mark_done(self, task_id: str, result: str = ""):
        """Mark task as completed with a result summary.

        Also marks any remaining 'pending' subtasks as 'skipped' so the
        dashboard never shows stale 'pending' subtasks on a completed task.
        """
        task = self.get_task(task_id)
        if task and task.subtasks:
            changed = False
            for st in task.subtasks:
                if st.status == "pending":
                    st.status = "skipped"
                    changed = True
            if changed:
                self._save_subtasks(task_id, task.subtasks)

        with self._conn() as conn:
            conn.execute(
                """UPDATE tasks SET status='done', result=?, completed_at=?
                   WHERE id=?""",
                (result, datetime.utcnow().isoformat(), task_id),
            )
        logger.info(f"Task {task_id} marked done")

    def mark_failed(self, task_id: str, error: str = ""):
        """Mark task as failed. Remaining pending subtasks are marked skipped."""
        task = self.get_task(task_id)
        if task and task.subtasks:
            changed = False
            for st in task.subtasks:
                if st.status in ("pending", "running"):
                    st.status = "skipped"
                    changed = True
            if changed:
                self._save_subtasks(task_id, task.subtasks)

        with self._conn() as conn:
            conn.execute(
                """UPDATE tasks SET status='failed', error=?, completed_at=?
                   WHERE id=?""",
                (error, datetime.utcnow().isoformat(), task_id),
            )
        logger.warning(f"Task {task_id} marked failed: {error[:80]}")

    def _save_subtasks(self, task_id: str, subtasks):
        """Write subtasks list back to DB."""
        subtasks_json = json.dumps([
            {
                "description": st.description,
                "tool_hints": st.tool_hints,
                "model_tier": st.model_tier,
                "status": st.status,
                "result": st.result,
                "error": st.error,
                "verification_criteria": st.verification_criteria,
                "reversible": st.reversible,
                "depends_on": st.depends_on,
                "execution_mode": st.execution_mode,
                "delegate_to": st.delegate_to,
                "priority": st.priority,
                "orchestration_strategy": st.orchestration_strategy,
                "dispatch": st.dispatch,
                "estimated_minutes": st.estimated_minutes,
            }
            for st in subtasks
        ])
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET subtasks_json=? WHERE id=?",
                (subtasks_json, task_id),
            )

    def cancel(self, task_id: str):
        """Cancel a pending or running task."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', error='Cancelled by user' WHERE id=? AND status IN ('pending', 'running', 'decomposing')",
                (task_id,),
            )

    # ── Read operations ───────────────────────────────────────────────────────

    def dequeue_next(self) -> Optional[Task]:
        """Return the oldest pending task (None if none available)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            # Mark as decomposing so concurrent processes don't double-pick
            conn.execute(
                "UPDATE tasks SET status='decomposing' WHERE id=?", (row["id"],)
            )
            return self._row_to_task(row)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Fetch a task by ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return self._row_to_task(row) if row else None

    def get_pending_count(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('pending', 'decomposing', 'running')"
            ).fetchone()[0]

    def get_recent_tasks(self, limit: int = 10) -> List[Task]:
        """Return recent tasks (newest first) for status display."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def get_active_tasks(self) -> List[Task]:
        """Return all tasks with status pending, decomposing, or running."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('pending', 'decomposing', 'running') ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def get_active_and_recent_tasks(self, completed_hours: int = 2) -> List[Task]:
        """Return all active tasks + completed/failed tasks from the last N hours.

        Active = pending, decomposing, running (always shown).
        Completed/failed = only those finished within the last completed_hours.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                   WHERE status IN ('pending', 'decomposing', 'running')
                      OR (status IN ('done', 'failed')
                          AND completed_at >= datetime('now', ?))
                   ORDER BY created_at DESC""",
                (f"-{completed_hours} hours",),
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    # ── Maintenance ─────────────────────────────────────────────────────────

    def purge_old(self, days: int = 3) -> int:
        """Delete completed/failed tasks older than `days` days.

        Returns number of rows deleted.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """DELETE FROM tasks
                   WHERE status IN ('done', 'failed')
                     AND completed_at < datetime('now', ?)""",
                (f"-{days} days",),
            )
            count = cursor.rowcount
            if count:
                logger.info(f"Purged {count} completed/failed tasks older than {days} days")
            return count

    def reset_stale_running(self, hours: int = 6) -> int:
        """Reset tasks stuck in 'running'/'decomposing' for more than `hours` hours to 'failed'.

        Prevents zombie tasks from surviving restarts and getting re-picked.
        Returns number of rows affected.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE tasks SET status='failed', error='Stale: timed out', completed_at=datetime('now')
                   WHERE status IN ('running', 'decomposing')
                     AND started_at < datetime('now', ?)""",
                (f"-{hours} hours",),
            )
            count = cursor.rowcount
            if count:
                logger.info(f"Reset {count} stale running/decomposing tasks (>{hours}h old)")
            return count

    # ── Internal ─────────────────────────────────────────────────────────────

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        subtask_dicts = json.loads(row["subtasks_json"] or "[]")
        subtasks = [
            Subtask(
                description=s["description"],
                tool_hints=s.get("tool_hints", []),
                model_tier=s.get("model_tier", "flash"),
                status=s.get("status", "pending"),
                result=s.get("result", ""),
                error=s.get("error", ""),
                verification_criteria=s.get("verification_criteria", ""),
                reversible=s.get("reversible", True),
                depends_on=s.get("depends_on", []),
                execution_mode=s.get("execution_mode", "self"),
                delegate_to=s.get("delegate_to", ""),
                priority=s.get("priority", "q2"),
                orchestration_strategy=s.get("orchestration_strategy", "auto"),
                dispatch=s.get("dispatch", "green"),
                estimated_minutes=s.get("estimated_minutes", 5),
            )
            for s in subtask_dicts
        ]
        return Task(
            id=row["id"],
            goal=row["goal"],
            channel=row["channel"],
            user_id=row["user_id"],
            status=row["status"],
            subtasks=subtasks,
            result=row["result"] or "",
            error=row["error"] or "",
            notify_on_complete=bool(row["notify_on_complete"]),
            created_at=row["created_at"] or "",
            started_at=row["started_at"] or "",
            completed_at=row["completed_at"] or "",
        )
