"""A2A protocol data models — minimal set for Nova's server implementation."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Message parts ────────────────────────────────────────────────────────────

class Part(BaseModel):
    """A single content part within a message or artifact."""
    kind: str = "text"          # text | data | file
    text: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    mimeType: Optional[str] = None


class Message(BaseModel):
    """A2A message: one turn in a conversation."""
    role: str                   # user | agent
    parts: List[Part]
    messageId: Optional[str] = None
    contextId: Optional[str] = None
    taskId: Optional[str] = None


# ── Task lifecycle ───────────────────────────────────────────────────────────

class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input-required"


class TaskStatus(BaseModel):
    state: TaskState
    message: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Artifact(BaseModel):
    artifactId: str
    name: str = "result"
    parts: List[Part] = []


class Task(BaseModel):
    """A2A task: represents work sent to an agent."""
    id: str
    contextId: Optional[str] = None
    status: TaskStatus
    messages: List[Message] = []
    artifacts: List[Artifact] = []


# ── JSON-RPC 2.0 ────────────────────────────────────────────────────────────

class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Optional[Any] = None


def jsonrpc_success(request_id: Any, result: Any) -> dict:
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    """Build a JSON-RPC 2.0 error response."""
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}
