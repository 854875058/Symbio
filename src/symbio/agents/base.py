"""Agent 基类 - 所有 Agent 的抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger
from symbio.utils.types import (
    AgentState,
    Intent,
    Message,
    Result,
    Task,
    TaskComplexity,
    TokenUsage,
)

logger = get_logger("agent")


class AgentCapability(BaseModel):
    """Agent 能力声明"""
    name: str
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    complexity_range: list[TaskComplexity] = Field(
        default=[TaskComplexity.LOW, TaskComplexity.MEDIUM, TaskComplexity.HIGH]
    )


class BaseAgent(ABC):
    """Agent 抽象基类

    所有 Agent 必须实现 execute 方法。
    """

    # Agent 元信息（子类覆盖）
    name: str = "base_agent"
    description: str = "基础 Agent"
    version: str = "1.0.0"
    capabilities: list[AgentCapability] = []

    def __init__(self):
        self.state: AgentState = AgentState.IDLE
        self._current_task: Optional[Task] = None
        self._step_count: int = 0
        self._total_tokens: TokenUsage = TokenUsage()

    @abstractmethod
    async def execute(self, task: Task) -> Result:
        """执行任务

        Args:
            task: 任务对象

        Returns:
            执行结果
        """
        pass

    async def can_handle(self, intent: Intent) -> bool:
        """检查是否能处理该意图

        Args:
            intent: 用户意图

        Returns:
            是否能处理
        """
        # 默认实现：检查复杂度是否在能力范围内
        for cap in self.capabilities:
            if intent.estimated_complexity in cap.complexity_range:
                return True
        return False

    def start(self, task: Task) -> None:
        """开始执行任务"""
        self.state = AgentState.RUNNING
        self._current_task = task
        self._step_count = 0
        logger.info(f"Agent {self.name} 开始执行任务: {task.task_id}")

    def complete(self, result: Result) -> None:
        """完成任务"""
        self.state = AgentState.COMPLETED
        logger.info(
            f"Agent {self.name} 完成任务: {self._current_task.task_id if self._current_task else 'unknown'}, "
            f"步数={self._step_count}, tokens={self._total_tokens.total_tokens}"
        )

    def fail(self, error: str) -> None:
        """任务失败"""
        self.state = AgentState.FAILED
        logger.error(f"Agent {self.name} 任务失败: {error}")

    def pause(self) -> None:
        """暂停任务"""
        self.state = AgentState.PAUSED
        logger.info(f"Agent {self.name} 暂停")

    def resume(self) -> None:
        """恢复任务"""
        self.state = AgentState.RUNNING
        logger.info(f"Agent {self.name} 恢复")

    def cancel(self) -> None:
        """取消任务"""
        self.state = AgentState.IDLE
        self._current_task = None
        logger.info(f"Agent {self.name} 取消任务")

    def record_step(self, tokens: TokenUsage) -> None:
        """记录执行步数和 Token 消耗"""
        self._step_count += 1
        self._total_tokens.input_tokens += tokens.input_tokens
        self._total_tokens.output_tokens += tokens.output_tokens
        self._total_tokens.total_tokens += tokens.total_tokens

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self.state == AgentState.RUNNING

    @property
    def is_completed(self) -> bool:
        """是否已完成"""
        return self.state == AgentState.COMPLETED

    @property
    def step_count(self) -> int:
        """当前步数"""
        return self._step_count

    @property
    def total_tokens(self) -> TokenUsage:
        """总 Token 消耗"""
        return self._total_tokens

    def get_info(self) -> dict:
        """获取 Agent 信息"""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "state": self.state.value,
            "capabilities": [cap.model_dump() for cap in self.capabilities],
            "step_count": self._step_count,
            "total_tokens": self._total_tokens.model_dump(),
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} state={self.state.value}>"
