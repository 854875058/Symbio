"""Symbio A2A (Agent-to-Agent) Protocol Implementation.

Implements a minimal A2A-compatible interface so Symbio can:
  1. Advertise its own capabilities via an AgentCard at /.well-known/agent.json
  2. Receive tasks from external A2A-compatible agents (inbound)
  3. Send tasks to remote A2A-compatible agents (outbound) and track sessions

The protocol is modelled after the Google A2A open spec:
  https://google.github.io/A2A/

Minimal surface implemented:
  - AgentCard schema
  - Task / TaskResult models
  - A2ASession (multi-turn session tracking)
  - A2ASessionManager (in-memory + optional JSON persistence)
  - Outbound HTTP task sender (aiohttp, with graceful fallback)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class A2ATaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class A2AMessageRole(str, Enum):
    USER = "user"
    AGENT = "agent"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class A2ATextPart(BaseModel):
    type: str = "text"
    text: str


class A2AMessage(BaseModel):
    role: A2AMessageRole
    parts: list[A2ATextPart]
    messageId: str = Field(default_factory=lambda: f"msg-{uuid.uuid4().hex[:12]}")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @classmethod
    def text(cls, role: A2AMessageRole, content: str) -> "A2AMessage":
        return cls(role=role, parts=[A2ATextPart(text=content)])

    @property
    def text_content(self) -> str:
        return " ".join(p.text for p in self.parts if hasattr(p, "text"))


class A2ATaskResult(BaseModel):
    state: A2ATaskState
    message: Optional[A2AMessage] = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATask(BaseModel):
    """Represents a task exchanged between A2A agents."""

    id: str = Field(default_factory=lambda: f"a2a-task-{uuid.uuid4().hex[:16]}")
    sessionId: Optional[str] = None
    message: A2AMessage
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state: A2ATaskState = A2ATaskState.SUBMITTED
    result: Optional[A2ATaskResult] = None
    # origin: "inbound" = received from external agent; "outbound" = we sent it
    origin: str = "inbound"
    remote_agent_url: Optional[str] = None
    remote_agent_name: Optional[str] = None


class A2ASession(BaseModel):
    """A multi-turn conversation context between two A2A agents."""

    id: str = Field(default_factory=lambda: f"a2a-session-{uuid.uuid4().hex[:16]}")
    remote_url: str
    remote_name: str = "remote-agent"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_active: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state: A2ATaskState = A2ATaskState.SUBMITTED
    messages: list[A2AMessage] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# AgentCard
# ---------------------------------------------------------------------------


class A2AAgentCapabilities(BaseModel):
    streaming: bool = True
    pushNotifications: bool = False
    stateTransitionHistory: bool = True


class A2AAgentCard(BaseModel):
    """The self-description document an A2A agent publishes at /.well-known/agent.json"""

    name: str = "Symbio"
    description: str = (
        "AI Infra-grade multi-agent orchestration framework with "
        "dynamic DAG scheduling, HITL approval, ontology memory, "
        "MCP tool bridge, and external agent session control."
    )
    version: str = "0.1.0"
    url: str = "http://localhost:9090"
    provider: dict[str, str] = Field(
        default_factory=lambda: {
            "organization": "Symbio Project",
            "url": "https://github.com/854875058/Symbio",
        }
    )
    capabilities: A2AAgentCapabilities = Field(
        default_factory=A2AAgentCapabilities
    )
    authentication: dict[str, Any] = Field(
        default_factory=lambda: {"schemes": ["none"]}
    )
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[dict[str, Any]] = Field(
        default_factory=lambda: [
            {
                "id": "general_chat",
                "name": "General Chat",
                "description": "Multi-turn conversation with Symbio's LLM backend.",
                "tags": ["chat", "general"],
                "examples": ["Analyze this codebase.", "Help me write a Python script."],
            },
            {
                "id": "task_orchestration",
                "name": "Task Orchestration",
                "description": "Submit complex tasks with DAG-based planning and execution.",
                "tags": ["orchestration", "dag", "planning"],
                "examples": [
                    "Break down and execute: refactor module X.",
                    "Run eval pipeline on dataset Y.",
                ],
            },
        ]
    )
    # A2A 扩展位：放本实例真实能力快照，让 AgentCard 不再是死数据
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_agent_card(
    base_url: str = "http://localhost:9090",
    *,
    version: Optional[str] = None,
    skills: Optional[list[dict[str, Any]]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> A2AAgentCard:
    """Build the current instance's AgentCard.

    version/skills/metadata 为 None 时用默认值；传入则覆盖，便于把真实的
    包版本与能力快照写进卡片（动态自描述）。
    """
    card = A2AAgentCard(url=base_url)
    if version:
        card.version = version
    if skills is not None:
        card.skills = skills
    if metadata is not None:
        card.metadata = metadata
    return card


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class A2ASessionManager:
    """In-memory store for inbound tasks and outbound A2A sessions.

    Both are keyed by id; optional JSON persistence survives restarts.
    """

    def __init__(self, persist_path: Optional[Path] = None) -> None:
        self._tasks: dict[str, A2ATask] = {}
        self._sessions: dict[str, A2ASession] = {}
        self._persist_path = persist_path
        self._lock = asyncio.Lock()

        if persist_path and persist_path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def receive_task(self, task: A2ATask) -> A2ATask:
        async with self._lock:
            self._tasks[task.id] = task
            self._save()
        return task

    async def update_task_state(
        self,
        task_id: str,
        state: A2ATaskState,
        result: Optional[A2ATaskResult] = None,
    ) -> Optional[A2ATask]:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.state = state
            task.updated_at = datetime.now(timezone.utc).isoformat()
            if result is not None:
                task.result = result
            self._save()
        return task

    async def get_task(self, task_id: str) -> Optional[A2ATask]:
        return self._tasks.get(task_id)

    async def list_tasks(
        self, origin: Optional[str] = None, limit: int = 50
    ) -> list[A2ATask]:
        tasks = list(self._tasks.values())
        if origin:
            tasks = [t for t in tasks if t.origin == origin]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    # ------------------------------------------------------------------
    # Outbound sessions
    # ------------------------------------------------------------------

    async def create_session(
        self,
        remote_url: str,
        remote_name: str = "remote-agent",
        metadata: Optional[dict[str, Any]] = None,
    ) -> A2ASession:
        session = A2ASession(
            remote_url=remote_url,
            remote_name=remote_name,
            metadata=metadata or {},
        )
        async with self._lock:
            self._sessions[session.id] = session
            self._save()
        return session

    async def get_session(self, session_id: str) -> Optional[A2ASession]:
        return self._sessions.get(session_id)

    async def list_sessions(self, limit: int = 50) -> list[A2ASession]:
        sessions = list(self._sessions.values())
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        return sessions[:limit]

    async def append_session_message(
        self, session_id: str, message: A2AMessage, task_id: Optional[str] = None
    ) -> Optional[A2ASession]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.messages.append(message)
            session.last_active = datetime.now(timezone.utc).isoformat()
            if task_id and task_id not in session.task_ids:
                session.task_ids.append(task_id)
            self._save()
        return session

    async def update_session_state(
        self, session_id: str, state: A2ATaskState
    ) -> Optional[A2ASession]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            session.state = state
            session.last_active = datetime.now(timezone.utc).isoformat()
            self._save()
        return session

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "tasks": {k: v.model_dump(mode="json") for k, v in self._tasks.items()},
                "sessions": {k: v.model_dump(mode="json") for k, v in self._sessions.items()},
            }
            self._persist_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load(self) -> None:
        try:
            data = json.loads(self._persist_path.read_text())
            for v in data.get("tasks", {}).values():
                t = A2ATask(**v)
                self._tasks[t.id] = t
            for v in data.get("sessions", {}).values():
                s = A2ASession(**v)
                self._sessions[s.id] = s
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Outbound HTTP sender
# ---------------------------------------------------------------------------


async def send_task_to_agent(
    remote_url: str,
    message_text: str,
    session_id: Optional[str] = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Send a task to a remote A2A-compatible agent via HTTP POST.

    Returns the raw JSON response body.  Raises on HTTP/connection errors.
    """
    endpoint = remote_url.rstrip("/") + "/api/a2a/tasks"
    task_id = f"a2a-task-{uuid.uuid4().hex[:16]}"
    payload: dict[str, Any] = {
        "id": task_id,
        "message": {
            "role": "user",
            "parts": [{"type": "text", "text": message_text}],
        },
    }
    if session_id:
        payload["sessionId"] = session_id

    try:
        import aiohttp  # optional dependency

        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"Content-Type": "application/json"},
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    raise ValueError(
                        f"Remote agent returned HTTP {resp.status}: {body}"
                    )
                return {"task_id": task_id, "remote_url": remote_url, **body}
    except ImportError:
        # Fallback: stdlib urllib
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
                return {"task_id": task_id, "remote_url": remote_url, **body}
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Remote agent returned HTTP {exc.code}") from exc
        except Exception as exc:
            raise ConnectionError(
                f"Failed to reach remote agent at {remote_url}: {exc}"
            ) from exc
    except Exception as exc:
        raise ConnectionError(
            f"Failed to reach remote agent at {remote_url}: {exc}"
        ) from exc


async def fetch_remote_agent_card(
    remote_url: str, timeout: int = 10
) -> Optional[dict[str, Any]]:
    """Fetch the AgentCard from /.well-known/agent.json of a remote agent."""
    endpoint = remote_url.rstrip("/") + "/.well-known/agent.json"
    try:
        import aiohttp

        async with aiohttp.ClientSession() as http_session:
            async with http_session.get(
                endpoint, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except ImportError:
        import urllib.request

        try:
            with urllib.request.urlopen(endpoint, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception:
            pass
    except Exception:
        pass
    return None
