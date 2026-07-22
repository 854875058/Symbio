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
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
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
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state: A2ATaskState = A2ATaskState.SUBMITTED
    messages: list[A2AMessage] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# AgentCard
# ---------------------------------------------------------------------------


class A2AAgentCapabilities(BaseModel):
    streaming: bool = True
    pushNotifications: bool = True
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
    capabilities: A2AAgentCapabilities = Field(default_factory=A2AAgentCapabilities)
    authentication: dict[str, Any] = Field(default_factory=lambda: {"schemes": ["none"]})
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
    authentication: Optional[dict[str, Any]] = None,
) -> A2AAgentCard:
    """Build the current instance's AgentCard.

    version/skills/metadata/authentication 为 None 时用默认值；传入则覆盖，
    便于把真实的包版本、能力快照与鉴权方案写进卡片（动态自描述）。
    """
    card = A2AAgentCard(url=base_url)
    if version:
        card.version = version
    if skills is not None:
        card.skills = skills
    if metadata is not None:
        card.metadata = metadata
    if authentication is not None:
        card.authentication = authentication
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
        # SSE 订阅：task_id -> 订阅队列列表（每个订阅者一个）
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # 推送通知：task_id -> webhook URL
        self._push_configs: dict[str, str] = {}

        if persist_path and persist_path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Streaming subscriptions (SSE)
    # ------------------------------------------------------------------

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """订阅任务状态变更；每次 update_task_state 都会向队列投递任务快照。"""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(task_id, []).append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(task_id)
        if subs and queue in subs:
            subs.remove(queue)
        if subs is not None and not subs:
            self._subscribers.pop(task_id, None)

    def _notify_subscribers(self, task: A2ATask) -> None:
        for queue in self._subscribers.get(task.id, []):
            try:
                queue.put_nowait(task.model_dump(mode="json"))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Push notifications (webhook)
    # ------------------------------------------------------------------

    def set_push_config(self, task_id: str, webhook_url: str) -> None:
        """为任务注册推送 webhook；任务状态变更时 POST 任务快照过去。"""
        self._push_configs[task_id] = webhook_url

    def get_push_config(self, task_id: str) -> Optional[str]:
        return self._push_configs.get(task_id)

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
        # 锁外通知：SSE 订阅者 + 推送 webhook
        self._notify_subscribers(task)
        webhook = self._push_configs.get(task_id)
        if webhook:
            asyncio.ensure_future(_deliver_push_notification(webhook, task))
        return task

    async def get_task(self, task_id: str) -> Optional[A2ATask]:
        return self._tasks.get(task_id)

    async def list_tasks(self, origin: Optional[str] = None, limit: int = 50) -> list[A2ATask]:
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


async def _deliver_push_notification(webhook_url: str, task: "A2ATask") -> None:
    """POST 任务快照到注册的 webhook（fire-and-forget，失败静默）。"""
    payload = task.model_dump(mode="json")
    try:
        import aiohttp

        async with aiohttp.ClientSession() as http_session:
            await http_session.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Content-Type": "application/json"},
            )
    except ImportError:
        import urllib.request

        def _post():
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)

        try:
            await asyncio.to_thread(_post)
        except Exception:
            pass
    except Exception:
        pass


async def send_task_to_agent(
    remote_url: str,
    message_text: str,
    session_id: Optional[str] = None,
    timeout: int = 30,
    auth_token: Optional[str] = None,
    push_url: Optional[str] = None,
) -> dict[str, Any]:
    """Send a task to a remote A2A-compatible agent via HTTP POST.

    auth_token 非空时带 Bearer 头；push_url 非空时请求远端状态变更回调。
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
    if push_url:
        payload["pushNotification"] = {"url": push_url}

    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        import aiohttp  # optional dependency

        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers=headers,
            ) as resp:
                body = await resp.json()
                if resp.status >= 400:
                    raise ValueError(f"Remote agent returned HTTP {resp.status}: {body}")
                return {"task_id": task_id, "remote_url": remote_url, **body}
    except ImportError:
        # Fallback: stdlib urllib
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
                return {"task_id": task_id, "remote_url": remote_url, **body}
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Remote agent returned HTTP {exc.code}") from exc
        except Exception as exc:
            raise ConnectionError(f"Failed to reach remote agent at {remote_url}: {exc}") from exc
    except Exception as exc:
        raise ConnectionError(f"Failed to reach remote agent at {remote_url}: {exc}") from exc


async def fetch_remote_agent_card(remote_url: str, timeout: int = 10) -> Optional[dict[str, Any]]:
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


async def fetch_remote_task(
    remote_url: str, task_id: str, timeout: int = 10
) -> Optional[dict[str, Any]]:
    """Fetch the current state of a task we previously sent to a remote agent.

    Returns the remote task JSON（含 state/result）；不可达或 404 时返回 None。
    """
    endpoint = remote_url.rstrip("/") + f"/api/a2a/tasks/{task_id}"
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
