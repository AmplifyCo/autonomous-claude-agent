"""Type definitions for the autonomous agent system."""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Literal, Union
from enum import Enum


class TaskStatus(Enum):
    """Status of a task or feature."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class BrainType(Enum):
    """Type of brain (core or digital clone)."""
    CORE = "core"  # For self-building
    DIGITAL_CLONE = "digital_clone"  # For production


@dataclass
class ToolResult:
    """Result from a tool execution."""
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentConfig:
    """Configuration for the autonomous agent."""
    # API
    api_key: str
    default_model: str = "claude-opus-4-6"  # Architect - complex planning
    subagent_model: str = "claude-sonnet-4-5"  # Workers - implementation
    chat_model: str = "claude-haiku-4-5"  # Chat - simple queries
    intent_model: str = "claude-haiku-4-5"  # Intent parsing

    # Local Models (optional, for CPU inference)
    local_model_enabled: bool = False
    local_model_name: str = "HuggingFaceTB/SmolLM2-1.7B-Instruct"  # Status, reports, monitoring
    local_model_endpoint: Optional[str] = None  # e.g., "http://localhost:8000"
    local_model_for: str = "trivial,simple"  # Comma-separated: trivial, simple, chat, intent

    # Specialized local models for specific tasks
    local_coder_enabled: bool = False
    local_coder_name: str = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"  # Quick code changes
    local_coder_endpoint: Optional[str] = None  # Separate endpoint for coder model

    # Execution
    max_iterations: int = 50
    timeout_seconds: int = 300
    retry_attempts: int = 3
    self_build_mode: bool = True

    # Brain
    vector_db_path: str = "./data/chroma"
    core_brain_path: str = "./data/core_brain"
    digital_clone_brain_path: str = "./data/digital_clone_brain"
    memory_path: str = "./data/memory"

    # Git
    auto_commit: bool = True
    git_user_name: str = "Autonomous Agent"
    git_user_email: str = "agent@autonomous.ai"

    # Monitoring
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    dashboard_enabled: bool = True
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 18789

    # Logging
    log_level: str = "INFO"
    log_file: str = "./data/logs/agent.log"


@dataclass
class Feature:
    """Represents a feature to be built."""
    name: str
    description: str
    requirements: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    complexity: Literal["low", "medium", "high"] = "medium"
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class BuildPhase:
    """Represents a phase in the build plan."""
    name: str
    features: List[Feature] = field(default_factory=list)

    def add_task(self, feature: Feature):
        """Add a feature to this phase."""
        self.features.append(feature)


@dataclass
class DependencyNode:
    """Node in the dependency graph."""
    name: str
    dependencies: List[str] = field(default_factory=list)


@dataclass
class DependencyGraph:
    """Dependency graph for build planning."""
    nodes: List[DependencyNode] = field(default_factory=list)

    def get_dependencies(self, node_name: str) -> List[str]:
        """Get dependencies for a node."""
        for node in self.nodes:
            if node.name == node_name:
                return node.dependencies
        return []

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DependencyGraph':
        """Create from dictionary."""
        nodes = [DependencyNode(**node) for node in data.get("nodes", [])]
        return cls(nodes=nodes)


@dataclass
class SubAgentResult:
    """Result from a sub-agent execution."""
    success: bool
    summary: str
    files_created: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    error: Optional[str] = None
    suggestions: List[str] = field(default_factory=list)


@dataclass
class Message:
    """Chat message for API calls."""
    role: Literal["user", "assistant"]
    content: Union[str, List[Dict[str, Any]]]
