"""A2AHandler — JSON-RPC 2.0 server for the A2A protocol.

Receives tasks from external agents, enqueues them in Nova's TaskQueue,
and maps task status back to A2A format.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .models import (
    Artifact,
    Part,
    Task,
    TaskState,
    TaskStatus,
    jsonrpc_error,
    jsonrpc_success,
)

logger = logging.getLogger(__name__)


class A2AHandler:
    """Handles incoming A2A JSON-RPC 2.0 requests.

    Tasks are enqueued in the existing TaskQueue and executed by TaskRunner.
    Status is mapped from Nova's internal format to A2A TaskState.
    """

    def __init__(self, task_queue, conversation_manager, api_key: str = ""):
        self._task_queue = task_queue
        self._conversation_manager = conversation_manager
        self._api_key = api_key
        # Maps a2a_task_id → nova_task_id for status lookups
        self._task_map: Dict[str, str] = {}

    async def handle_jsonrpc(self, body: dict) -> dict:
        """Route a JSON-RPC 2.0 request to the appropriate handler."""
        request_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {})

        if body.get("jsonrpc") != "2.0":
            return jsonrpc_error(request_id, -32600, "Invalid Request: must be JSON-RPC 2.0")

        handlers = {
            "message/send": self._handle_message_send,
            "tasks/get": self._handle_tasks_get,
            "tasks/cancel": self._handle_tasks_cancel,
        }

        handler = handlers.get(method)
        if not handler:
            return jsonrpc_error(request_id, -32601, f"Method not found: {method}")

        try:
            result = await handler(params)
            return jsonrpc_success(request_id, result)
        except Exception as e:
            logger.error(f"A2A handler error ({method}): {e}")
            return jsonrpc_error(request_id, -32603, f"Internal error: {e}")

    # ── Method handlers ──────────────────────────────────────────────────────

    async def _handle_message_send(self, params: dict) -> dict:
        """Handle message/send — enqueue a task from an external agent."""
        message = params.get("message", {})
        parts = message.get("parts", [])
        context_id = message.get("contextId")

        # Extract text from message parts
        text_parts = []
        for part in parts:
            if part.get("kind") == "text" and part.get("text"):
                text_parts.append(part["text"])

        goal = "\n".join(text_parts)
        if not goal.strip():
            raise ValueError("No text content in message parts")

        # Generate A2A task ID
        a2a_task_id = str(uuid.uuid4())

        # Enqueue in Nova's TaskQueue (channel="a2a" for identification)
        nova_task_id = self._task_queue.enqueue(
            goal=goal,
            channel="a2a",
            user_id=f"a2a:{a2a_task_id}",
            notify_on_complete=False,  # A2A tasks polled, not pushed
        )
        self._task_map[a2a_task_id] = nova_task_id
        logger.info(f"A2A task received: {a2a_task_id} → nova:{nova_task_id} | goal: {goal[:100]}")

        # Return task in A2A format
        return Task(
            id=a2a_task_id,
            contextId=context_id,
            status=TaskStatus(state=TaskState.SUBMITTED, message="Task enqueued"),
            messages=[{
                "role": "user",
                "parts": [{"kind": "text", "text": goal}],
            }],
        ).model_dump()

    async def _handle_tasks_get(self, params: dict) -> dict:
        """Handle tasks/get — return current task status."""
        a2a_task_id = params.get("id") or params.get("taskId")
        if not a2a_task_id:
            raise ValueError("Missing task ID")

        nova_task_id = self._task_map.get(a2a_task_id)
        if not nova_task_id:
            raise ValueError(f"Unknown task: {a2a_task_id}")

        nova_task = self._task_queue.get_task(nova_task_id)
        if not nova_task:
            raise ValueError(f"Task not found in queue: {nova_task_id}")

        # Map Nova status → A2A TaskState
        state = self._map_status(nova_task.status)

        # Build artifacts from completed task result
        artifacts = []
        if nova_task.status == "done" and nova_task.result:
            artifacts.append(Artifact(
                artifactId=str(uuid.uuid4()),
                name="result",
                parts=[Part(kind="text", text=nova_task.result)],
            ).model_dump())

        return Task(
            id=a2a_task_id,
            status=TaskStatus(state=state, message=nova_task.error or None),
            artifacts=artifacts,
        ).model_dump()

    async def _handle_tasks_cancel(self, params: dict) -> dict:
        """Handle tasks/cancel — cancel a running task."""
        a2a_task_id = params.get("id") or params.get("taskId")
        if not a2a_task_id:
            raise ValueError("Missing task ID")

        nova_task_id = self._task_map.get(a2a_task_id)
        if not nova_task_id:
            raise ValueError(f"Unknown task: {a2a_task_id}")

        self._task_queue.cancel(nova_task_id)
        logger.info(f"A2A task canceled: {a2a_task_id}")

        return Task(
            id=a2a_task_id,
            status=TaskStatus(state=TaskState.CANCELED, message="Canceled by client"),
        ).model_dump()

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _map_status(nova_status: str) -> TaskState:
        """Map Nova's internal task status to A2A TaskState."""
        mapping = {
            "pending": TaskState.SUBMITTED,
            "decomposing": TaskState.WORKING,
            "running": TaskState.WORKING,
            "done": TaskState.COMPLETED,
            "failed": TaskState.FAILED,
        }
        return mapping.get(nova_status, TaskState.FAILED)
