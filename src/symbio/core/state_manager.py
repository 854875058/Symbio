"""State-Driven Communication - 零对话的 Agent 间通信模块

基于全局状态的 Agent 协作架构，取代传统的对话式消息传递。
所有 Agent 通过读写共享的 GlobalState 对象进行协作，避免上下文爆炸。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("state_manager")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class TaskPhase(str, Enum):
    """任务阶段枚举"""

    INIT = "init"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class FileInfo(BaseModel):
    """文件追踪信息"""

    path: str
    lines: int = 0
    last_modified: str = ""
    hash: str = ""


class TestResults(BaseModel):
    """测试执行结果"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    failures: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


class WorkflowCheckpoint(BaseModel):
    """Workflow policy checkpoint persisted with task state."""

    name: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    details: dict[str, Any] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class AgentHandoff(BaseModel):
    """Structured artifact handoff between agents through GlobalState."""

    handoff_id: str = Field(default_factory=lambda: str(uuid4()))
    from_agent: str
    to_agent: str
    artifact_type: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class ErrorRecord(BaseModel):
    """错误记录"""

    error_id: str = Field(default_factory=lambda: str(uuid4()))
    message: str
    source: str = ""
    timestamp: str = ""
    resolved: bool = False


class GlobalState(BaseModel):
    """全局状态对象 - Agent 间通信的唯一媒介

    所有 Agent 通过读写此对象进行协作，无需直接对话。
    每次修改都会递增 version，支持 CAS 乐观锁。
    """

    task_id: str
    status: str = "active"
    phase: TaskPhase = TaskPhase.INIT
    requirements: str = ""  # 用户原始需求

    # Checklist（来自 checklist 模块的约定结构）
    checklist: dict = Field(default_factory=dict)

    # 文件追踪
    files: dict[str, FileInfo] = Field(default_factory=dict)

    # 测试结果
    test_results: TestResults = Field(default_factory=TestResults)

    # 错误记录
    errors: list[ErrorRecord] = Field(default_factory=list)

    # 元数据
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Workflow policy checkpoints
    workflow_checkpoints: list[WorkflowCheckpoint] = Field(default_factory=list)

    # Structured agent handoffs
    agent_handoffs: list[AgentHandoff] = Field(default_factory=list)

    # 版本号（用于 CAS 乐观锁）
    version: int = 0


# ---------------------------------------------------------------------------
# State Manager
# ---------------------------------------------------------------------------

class StateManager:
    """状态管理器 - 线程安全的全局状态管理

    提供原子读写、CAS 乐观锁、SQLite 持久化等能力。
    所有公开方法均为 async，通过 asyncio.Lock 保证并发安全。
    """

    def __init__(self, persist_path: str = "") -> None:
        self._state: Optional[GlobalState] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._persist_path = persist_path
        self._db: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, task_id: str, requirements: str) -> GlobalState:
        """初始化新的全局状态

        Args:
            task_id: 任务唯一标识
            requirements: 用户原始需求

        Returns:
            初始化后的全局状态快照
        """
        async with self._lock:
            self._state = GlobalState(
                task_id=task_id,
                requirements=requirements,
                version=1,
            )
            logger.info(f"全局状态初始化完成: task_id={task_id}")

            if self._persist_path:
                await self._init_db()
                await self._persist()

            return self._state.model_copy(deep=True)

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None
            logger.debug("状态管理器数据库连接已关闭")

    # ------------------------------------------------------------------
    # Read / Write
    # ------------------------------------------------------------------

    async def read(self) -> GlobalState:
        """读取当前状态（深拷贝快照）

        Returns:
            当前全局状态的深拷贝副本
        """
        async with self._lock:
            if self._state is None:
                raise RuntimeError("StateManager 尚未初始化，请先调用 initialize()")
            return self._state.model_copy(deep=True)

    async def update(
        self,
        updater: Callable[[GlobalState], GlobalState],
    ) -> GlobalState:
        """原子更新状态

        updater 函数接收当前状态，返回修改后的状态。
        整个操作在锁内完成，保证原子性。

        Args:
            updater: 状态更新函数

        Returns:
            更新后的全局状态快照
        """
        async with self._lock:
            if self._state is None:
                raise RuntimeError("StateManager 尚未初始化，请先调用 initialize()")

            self._state = updater(self._state)
            self._state.version += 1
            await self._persist()
            logger.debug(f"状态更新完成: version={self._state.version}")
            return self._state.model_copy(deep=True)

    async def record_workflow_checkpoint(
        self,
        name: str,
        details: Optional[dict[str, Any]] = None,
    ) -> GlobalState:
        """Append a workflow policy checkpoint to the current state."""

        checkpoint = WorkflowCheckpoint(name=name, details=details or {})
        return await self.update(
            lambda s: s.model_copy(update={
                "workflow_checkpoints": [
                    *s.workflow_checkpoints,
                    checkpoint,
                ],
            })
        )

    async def record_agent_handoff(
        self,
        *,
        from_agent: str,
        to_agent: str,
        artifact_type: str,
        summary: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> GlobalState:
        """Append a structured agent handoff artifact to the current state."""

        handoff = AgentHandoff(
            from_agent=from_agent,
            to_agent=to_agent,
            artifact_type=artifact_type,
            summary=summary,
            payload=payload or {},
        )
        return await self.update(
            lambda s: s.model_copy(update={
                "agent_handoffs": [
                    *s.agent_handoffs,
                    handoff,
                ],
            })
        )

    async def compare_and_swap(
        self,
        expected_version: int,
        updater: Callable[[GlobalState], GlobalState],
    ) -> bool:
        """CAS（Compare-And-Swap）乐观锁操作

        仅当当前版本号等于 expected_version 时才执行更新，
        否则返回 False，由调用方决定重试或放弃。

        Args:
            expected_version: 期望的当前版本号
            updater: 状态更新函数

        Returns:
            True 表示更新成功，False 表示版本冲突
        """
        async with self._lock:
            if self._state is None:
                raise RuntimeError("StateManager 尚未初始化，请先调用 initialize()")

            if self._state.version != expected_version:
                logger.warning(
                    f"CAS 版本冲突: expected={expected_version}, "
                    f"actual={self._state.version}"
                )
                return False

            self._state = updater(self._state)
            self._state.version += 1
            await self._persist()
            logger.debug(f"CAS 更新成功: version={self._state.version}")
            return True

    # ------------------------------------------------------------------
    # Persistence (SQLite)
    # ------------------------------------------------------------------

    async def _init_db(self) -> None:
        """初始化 SQLite 持久化数据库"""
        if self._db is not None:
            return

        db_path = Path(self._persist_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(str(db_path))
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS state_snapshots (
                task_id    TEXT NOT NULL,
                version    INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, version)
            )
        """)
        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_snapshots_task_id
            ON state_snapshots(task_id)
        """)
        await self._db.commit()
        logger.info(f"状态持久化数据库初始化完成: {db_path}")

    async def _persist(self) -> None:
        """将当前状态快照持久化到 SQLite"""
        if self._db is None or self._state is None:
            return

        state_json = self._state.model_dump_json()
        created_at = datetime.now().isoformat()

        await self._db.execute(
            "INSERT OR REPLACE INTO state_snapshots "
            "(task_id, version, state_json, created_at) VALUES (?, ?, ?, ?)",
            (
                self._state.task_id,
                self._state.version,
                state_json,
                created_at,
            ),
        )
        await self._db.commit()
        logger.debug(
            f"状态已持久化: task_id={self._state.task_id}, "
            f"version={self._state.version}"
        )

    async def restore(self, task_id: str) -> Optional[GlobalState]:
        """从 SQLite 恢复最新版本的状态

        Args:
            task_id: 任务唯一标识

        Returns:
            恢复的全局状态，如果不存在返回 None
        """
        if self._db is None:
            await self._init_db()

        cursor = await self._db.execute(
            "SELECT state_json FROM state_snapshots "
            "WHERE task_id = ? ORDER BY version DESC LIMIT 1",
            (task_id,),
        )
        row = await cursor.fetchone()

        if not row:
            logger.warning(f"未找到可恢复的状态: task_id={task_id}")
            return None

        self._state = GlobalState.model_validate_json(row[0])
        logger.info(
            f"状态恢复成功: task_id={task_id}, version={self._state.version}"
        )
        return self._state.model_copy(deep=True)

    async def list_snapshots(
        self,
        task_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """列出指定任务的状态快照版本

        Args:
            task_id: 任务唯一标识
            limit: 最大返回数量

        Returns:
            快照元信息列表 [{version, created_at}, ...]
        """
        if self._db is None:
            await self._init_db()

        cursor = await self._db.execute(
            "SELECT version, created_at FROM state_snapshots "
            "WHERE task_id = ? ORDER BY version DESC LIMIT ?",
            (task_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {"version": row[0], "created_at": row[1]}
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Instruction Generator
# ---------------------------------------------------------------------------

class InstructionGenerator:
    """从全局状态生成 Agent 任务指令

    避免 Agent 间直接对话，通过状态驱动的任务指令实现协作。
    每个 Agent 只需读取自己被分配的指令即可工作。
    """

    def generate_instruction(self, state: GlobalState) -> str:
        """从 pending checklist 生成精确任务指令

        Args:
            state: 当前全局状态

        Returns:
            面向单个 Agent 的任务指令文本
        """
        items = state.checklist.get("items", [])
        pending = [item for item in items if item.get("status") == "pending"]

        if not pending:
            return "All tasks completed."

        next_item = pending[0]

        instruction = f"## Current Task: {next_item['name']}\n\n"
        instruction += f"{next_item.get('description', '')}\n\n"

        if next_item.get("files"):
            instruction += (
                f"Expected output files: {', '.join(next_item['files'])}\n"
            )

        if next_item.get("test"):
            instruction += f"Verification: {next_item['test']}\n"

        # 注入错误上下文（如有未解决的错误）
        unresolved = [e for e in state.errors if not e.resolved]
        if unresolved:
            instruction += "\n### Unresolved Errors\n\n"
            for err in unresolved[-3:]:  # 最近 3 条
                instruction += f"- [{err.source}] {err.message}\n"

        return instruction

    def get_minimal_context(self, state: GlobalState) -> dict[str, Any]:
        """最小上下文 - 只提供 Agent 执行所需的必要信息

        避免将完整状态暴露给 Agent，减少上下文消耗。

        Args:
            state: 当前全局状态

        Returns:
            精简的上下文字典
        """
        items = state.checklist.get("items", [])
        current_item = items[0] if items else {}

        return {
            "task_id": state.task_id,
            "phase": state.phase.value,
            "current_item": current_item,
            "completed_count": sum(
                1 for i in items if i.get("status") == "completed"
            ),
            "total_count": len(items),
            "test_results": state.test_results.model_dump(),
            "agent_handoffs": [
                {
                    "from_agent": handoff.from_agent,
                    "to_agent": handoff.to_agent,
                    "artifact_type": handoff.artifact_type,
                    "summary": handoff.summary,
                }
                for handoff in state.agent_handoffs[-3:]
            ],
            "errors": [
                e.model_dump() for e in state.errors[-3:]
            ],  # 最近 3 条错误
        }


# ---------------------------------------------------------------------------
# Module-level re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "AgentHandoff",
    "ErrorRecord",
    "FileInfo",
    "GlobalState",
    "InstructionGenerator",
    "StateManager",
    "TaskPhase",
    "TestResults",
    "WorkflowCheckpoint",
]
