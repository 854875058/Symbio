"""Core type definitions for Symbio."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskComplexity(str, Enum):
    """Task complexity levels for model routing."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentState(str, Enum):
    """Agent lifecycle states."""
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"


class MessageSource(str, Enum):
    """Message source platforms."""
    CLI = "cli"
    WEB = "web"
    QQ = "qq"
    WECHAT = "wechat"
    FEISHU = "feishu"
    DINGTALK = "dingtalk"


class Message(BaseModel):
    """Unified message format across all interfaces."""
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    source: MessageSource
    user_id: str
    content: str
    session_id: str
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Intent(BaseModel):
    """Parsed user intent."""
    intent_id: str = Field(default_factory=lambda: str(uuid4()))
    raw_text: str
    action: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    requires_tools: list[str] = Field(default_factory=list)
    requires_memory: bool = False
    estimated_complexity: TaskComplexity = TaskComplexity.MEDIUM


class Task(BaseModel):
    """Task to be executed by an Agent."""
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    intent: Intent
    model: str = ""
    parent_task_id: Optional[str] = None
    state: AgentState = AgentState.IDLE
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Result] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Result(BaseModel):
    """Task execution result."""
    result_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    success: bool
    content: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    token_usage: TokenUsage = Field(default_factory=lambda: TokenUsage())
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=datetime.now)


class TokenUsage(BaseModel):
    """Token consumption tracking."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    cost_usd: float = 0.0


class Memory(BaseModel):
    """Memory unit stored in vector database."""
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    content: str
    embedding: list[float] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    importance: float = 0.5
    created_at: datetime = Field(default_factory=datetime.now)
    last_accessed: datetime = Field(default_factory=datetime.now)
    access_count: int = 0


class ToolCall(BaseModel):
    """Tool invocation request."""
    tool_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(default_factory=lambda: str(uuid4()))


class ToolResult(BaseModel):
    """Tool execution result."""
    call_id: str
    tool_name: str
    success: bool
    output: str = ""
    error: Optional[str] = None
    duration_ms: int = 0


class Trajectory(BaseModel):
    """Execution trajectory for data flywheel."""
    trajectory_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    steps: list[TrajectoryStep] = Field(default_factory=list)
    final_result: Optional[Result] = None
    success: bool = False
    created_at: datetime = Field(default_factory=datetime.now)


class TrajectoryStep(BaseModel):
    """Single step in execution trajectory."""
    step_id: int
    thought: str = ""
    action: str = ""
    observation: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)


# Fix forward reference
Task.model_rebuild()
Result.model_rebuild()
