"""资源管理器：任务级预算控制、步数限制、超时熔断与实时成本观测。

本模块是 guardrail.py 的上层编排，负责：
1. 任务资源生命周期管理（签发 → 跟踪 → 挂起/恢复 → 释放）
2. 多级熔断（警告 / 临界 / 停机）
3. 实时成本仪表盘数据供给
4. 通过 EventBus 发布资源事件，通知外部系统
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from pydantic import BaseModel, Field

from symbio.core.event_bus import Event, EventBus, EventType
from symbio.core.guardrail import (
    BudgetExceededError,
    Guardrail,
)
from symbio.utils.logger import get_logger

logger = get_logger("resource_manager")


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------


class CircuitLevel(str, Enum):
    """熔断等级"""
    NORMAL = "normal"          # 正常运行
    WARNING = "warning"        # 接近阈值，发出预警
    CRITICAL = "critical"      # 高度危险，建议挂起
    HALTED = "halted"          # 已熔断，任务挂起


class ResourceBudget(BaseModel):
    """资源配置 — 用户提交任务时指定的资源上限"""
    task_id: str
    max_cost_usd: float = 10.0
    max_steps: int = 50
    max_tokens: int = 100000
    timeout_seconds: int = 3600

    # 熔断阈值（百分比，0.0 ~ 1.0）
    warning_threshold: float = 0.7    # 70% 时发出警告
    critical_threshold: float = 0.9   # 90% 时进入临界
    halt_threshold: float = 1.0       # 100% 时熔断停机


class ResourceStatusSnapshot(BaseModel):
    """实时资源状态快照 — 用于仪表盘展示"""
    task_id: str
    circuit_level: CircuitLevel

    # 已消耗
    consumed_cost_usd: float = 0.0
    consumed_steps: int = 0
    consumed_tokens: int = 0

    # 预算上限
    max_cost_usd: float = 10.0
    max_steps: int = 50
    max_tokens: int = 100000

    # 剩余
    remaining_cost_usd: float = 0.0
    remaining_steps: int = 0
    remaining_tokens: int = 0

    # 使用率（0.0 ~ 1.0）
    cost_usage_ratio: float = 0.0
    step_usage_ratio: float = 0.0
    token_usage_ratio: float = 0.0

    # 时间
    elapsed_seconds: float = 0.0
    timeout_seconds: int = 3600
    created_at: datetime = Field(default_factory=datetime.now)

    # 状态标记
    is_expired: bool = False
    is_exhausted: bool = False
    is_suspended: bool = False


class AggregateStats(BaseModel):
    """全局聚合统计 — 所有活跃任务的汇总数据"""
    active_task_count: int = 0
    total_consumed_cost_usd: float = 0.0
    total_consumed_tokens: int = 0
    total_consumed_steps: int = 0
    suspended_task_count: int = 0
    halted_task_count: int = 0


# 回调类型：当熔断等级变化时触发
CircuitBreakerCallback = Callable[
    [str, CircuitLevel, ResourceStatusSnapshot],
    Coroutine[Any, Any, None],
]


# ---------------------------------------------------------------------------
# 资源管理器
# ---------------------------------------------------------------------------


class ResourceManager:
    """任务级资源管理器

    职责：
    1. 签发资源支票并跟踪生命周期
    2. 每步扣减前执行多级熔断检测
    3. 资源耗尽时挂起任务并发出通知
    4. 提供实时成本仪表盘数据
    5. 通过 EventBus 发布资源事件
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        guardrail: Optional[Guardrail] = None,
    ) -> None:
        self._guardrail = guardrail or Guardrail()
        self._event_bus = event_bus

        # 每个 task_id 对应一份资源配置
        self._budgets: dict[str, ResourceBudget] = {}

        # 挂起中的任务集合
        self._suspended: set[str] = set()

        # 熔断回调列表
        self._callbacks: list[CircuitBreakerCallback] = []

        # 每个任务上一次的熔断等级（用于检测变化）
        self._last_circuit: dict[str, CircuitLevel] = {}

        logger.info("ResourceManager 初始化完成")

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def register_callback(self, callback: CircuitBreakerCallback) -> None:
        """注册熔断等级变化回调

        Args:
            callback: 异步回调函数，签名 (task_id, new_level, snapshot) -> None
        """
        self._callbacks.append(callback)
        logger.debug(f"注册熔断回调: {callback.__qualname__}")

    # ------------------------------------------------------------------
    # 任务生命周期
    # ------------------------------------------------------------------

    def issue_budget(
        self,
        task_id: str,
        max_cost_usd: float = 10.0,
        max_steps: int = 50,
        max_tokens: int = 100000,
        timeout_seconds: int = 3600,
        warning_threshold: float = 0.7,
        critical_threshold: float = 0.9,
    ) -> ResourceBudget:
        """为任务签发资源预算

        Args:
            task_id: 任务唯一标识
            max_cost_usd: 最大成本（美元）
            max_steps: 最大步数
            max_tokens: 最大 Token 数
            timeout_seconds: 超时时间（秒）
            warning_threshold: 警告阈值（0.0 ~ 1.0）
            critical_threshold: 临界阈值（0.0 ~ 1.0）

        Returns:
            资源预算对象
        """
        budget = ResourceBudget(
            task_id=task_id,
            max_cost_usd=max_cost_usd,
            max_steps=max_steps,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            warning_threshold=warning_threshold,
            critical_threshold=critical_threshold,
        )
        self._budgets[task_id] = budget

        # 同步签发底层 guardrail 支票
        self._guardrail.issue_ticket(
            task_id=task_id,
            max_cost_usd=max_cost_usd,
            max_steps=max_steps,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )

        self._last_circuit[task_id] = CircuitLevel.NORMAL

        logger.info(
            f"签发资源预算: task_id={task_id}, "
            f"max_cost=${max_cost_usd}, max_steps={max_steps}, "
            f"max_tokens={max_tokens}, timeout={timeout_seconds}s"
        )

        self._emit_event_sync(
            EventType.TASK_STARTED,
            source="resource_manager",
            data={
                "task_id": task_id,
                "budget": budget.model_dump(mode="json"),
            },
        )

        return budget

    def release_budget(self, task_id: str) -> None:
        """释放任务资源预算

        Args:
            task_id: 任务唯一标识
        """
        self._guardrail.release_ticket(task_id)
        self._budgets.pop(task_id, None)
        self._suspended.discard(task_id)
        self._last_circuit.pop(task_id, None)

        logger.info(f"释放资源预算: task_id={task_id}")

        self._emit_event_sync(
            EventType.TASK_COMPLETED,
            source="resource_manager",
            data={"task_id": task_id},
        )

    # ------------------------------------------------------------------
    # 资源扣减与熔断检测
    # ------------------------------------------------------------------

    def consume(
        self,
        task_id: str,
        tokens: int,
        cost_usd: float,
    ) -> ResourceStatusSnapshot:
        """扣减资源并执行熔断检测

        在每个 Agent 步骤结束时调用此方法。如果资源耗尽或超时，
        会抛出 BudgetExceededError 并挂起任务。

        Args:
            task_id: 任务唯一标识
            tokens: 本步消耗的 Token 数
            cost_usd: 本步消耗的成本（美元）

        Returns:
            当前资源状态快照

        Raises:
            BudgetExceededError: 预算超限或超时
        """
        # 调用底层 guardrail 扣减
        self._guardrail.check_and_deduct(task_id, tokens, cost_usd)

        # 获取扣减后的快照
        snapshot = self.get_status(task_id)
        if snapshot is None:
            logger.warning(f"扣减时未找到任务资源: task_id={task_id}")
            return self._empty_snapshot(task_id)

        # 计算熔断等级
        new_level = self._evaluate_circuit_level(task_id, snapshot)

        # 检测等级变化并通知
        old_level = self._last_circuit.get(task_id, CircuitLevel.NORMAL)
        if new_level != old_level:
            self._last_circuit[task_id] = new_level
            self._on_circuit_level_changed(task_id, old_level, new_level, snapshot)

        # 如果已熔断，挂起任务
        if new_level == CircuitLevel.HALTED:
            self._suspend_task(task_id, snapshot)

        return snapshot

    def check_before_step(self, task_id: str) -> ResourceStatusSnapshot:
        """在执行下一步之前检查资源状态

        Agent 在每步开始时调用，提前发现资源不足。

        Args:
            task_id: 任务唯一标识

        Returns:
            当前资源状态快照
        """
        snapshot = self.get_status(task_id)
        if snapshot is None:
            return self._empty_snapshot(task_id)

        # 检查超时
        if snapshot.is_expired:
            self._suspend_task(task_id, snapshot, reason="任务超时")
            raise BudgetExceededError(
                task_id=task_id,
                reason="任务超时",
                remaining={
                    "cost_usd": snapshot.remaining_cost_usd,
                    "steps": snapshot.remaining_steps,
                    "tokens": snapshot.remaining_tokens,
                },
            )

        # 检查是否已挂起
        if task_id in self._suspended:
            raise BudgetExceededError(
                task_id=task_id,
                reason="任务已挂起",
                remaining={
                    "cost_usd": snapshot.remaining_cost_usd,
                    "steps": snapshot.remaining_steps,
                    "tokens": snapshot.remaining_tokens,
                },
            )

        return snapshot

    # ------------------------------------------------------------------
    # 挂起 / 恢复
    # ------------------------------------------------------------------

    def suspend(self, task_id: str, reason: str = "手动挂起") -> None:
        """手动挂起任务

        Args:
            task_id: 任务唯一标识
            reason: 挂起原因
        """
        snapshot = self.get_status(task_id)
        self._suspend_task(task_id, snapshot, reason=reason)

    def resume(self, task_id: str) -> bool:
        """恢复已挂起的任务

        Args:
            task_id: 任务唯一标识

        Returns:
            是否恢复成功
        """
        if task_id not in self._suspended:
            logger.warning(f"任务不在挂起状态，无法恢复: task_id={task_id}")
            return False

        self._suspended.discard(task_id)
        self._last_circuit[task_id] = CircuitLevel.NORMAL

        logger.info(f"恢复任务: task_id={task_id}")

        self._emit_event_sync(
            EventType.HITL_APPROVED,
            source="resource_manager",
            data={"task_id": task_id, "action": "resumed"},
        )

        return True

    def is_suspended(self, task_id: str) -> bool:
        """检查任务是否处于挂起状态"""
        return task_id in self._suspended

    # ------------------------------------------------------------------
    # 实时状态查询
    # ------------------------------------------------------------------

    def get_status(self, task_id: str) -> Optional[ResourceStatusSnapshot]:
        """获取任务的实时资源状态快照

        Args:
            task_id: 任务唯一标识

        Returns:
            资源状态快照，任务不存在时返回 None
        """
        budget = self._budgets.get(task_id)
        if budget is None:
            return None

        # 从 guardrail 获取底层 ticket 状态
        guardrail_status = self._guardrail.get_status(task_id)

        if guardrail_status is None:
            return self._empty_snapshot(task_id)

        consumed = guardrail_status["consumed"]
        remaining = guardrail_status["remaining"]

        # 计算使用率
        cost_ratio = (
            consumed["cost_usd"] / budget.max_cost_usd
            if budget.max_cost_usd > 0
            else 0.0
        )
        step_ratio = (
            consumed["steps"] / budget.max_steps
            if budget.max_steps > 0
            else 0.0
        )
        token_ratio = (
            consumed["tokens"] / budget.max_tokens
            if budget.max_tokens > 0
            else 0.0
        )

        # 计算经过时间
        elapsed = 0.0
        guardrail_ticket = self._guardrail._tickets.get(task_id)
        if guardrail_ticket is not None:
            elapsed = (datetime.now() - guardrail_ticket.created_at).total_seconds()

        snapshot = ResourceStatusSnapshot(
            task_id=task_id,
            circuit_level=self._last_circuit.get(task_id, CircuitLevel.NORMAL),
            consumed_cost_usd=consumed["cost_usd"],
            consumed_steps=consumed["steps"],
            consumed_tokens=consumed["tokens"],
            max_cost_usd=budget.max_cost_usd,
            max_steps=budget.max_steps,
            max_tokens=budget.max_tokens,
            remaining_cost_usd=remaining["cost_usd"],
            remaining_steps=remaining["steps"],
            remaining_tokens=remaining["tokens"],
            cost_usage_ratio=min(cost_ratio, 1.0),
            step_usage_ratio=min(step_ratio, 1.0),
            token_usage_ratio=min(token_ratio, 1.0),
            elapsed_seconds=elapsed,
            timeout_seconds=budget.timeout_seconds,
            created_at=guardrail_ticket.created_at,
            is_expired=guardrail_status["expired"],
            is_exhausted=guardrail_status["expired"] or (
                consumed["cost_usd"] >= budget.max_cost_usd
                or consumed["steps"] >= budget.max_steps
                or consumed["tokens"] >= budget.max_tokens
            ),
            is_suspended=task_id in self._suspended,
        )

        return snapshot

    def get_all_statuses(self) -> list[ResourceStatusSnapshot]:
        """获取所有活跃任务的资源状态

        Returns:
            所有任务的状态快照列表
        """
        snapshots: list[ResourceStatusSnapshot] = []
        for task_id in self._budgets:
            snapshot = self.get_status(task_id)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def get_aggregate_stats(self) -> AggregateStats:
        """获取全局聚合统计

        Returns:
            所有活跃任务的汇总数据
        """
        snapshots = self.get_all_statuses()

        total_cost = sum(s.consumed_cost_usd for s in snapshots)
        total_tokens = sum(s.consumed_tokens for s in snapshots)
        total_steps = sum(s.consumed_steps for s in snapshots)
        suspended_count = sum(1 for s in snapshots if s.is_suspended)
        halted_count = sum(
            1 for s in snapshots
            if s.circuit_level == CircuitLevel.HALTED
        )

        return AggregateStats(
            active_task_count=len(snapshots),
            total_consumed_cost_usd=total_cost,
            total_consumed_tokens=total_tokens,
            total_consumed_steps=total_steps,
            suspended_task_count=suspended_count,
            halted_task_count=halted_count,
        )

    # ------------------------------------------------------------------
    # 成本预估
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_cost(
        input_tokens: int,
        output_tokens: int,
        input_price_per_1k: float = 0.003,
        output_price_per_1k: float = 0.015,
    ) -> float:
        """预估一次 LLM 调用的美元成本

        默认价格参考 Claude Sonnet 级别模型定价，实际使用时应根据
        具体模型传入正确价格。

        Args:
            input_tokens: 输入 Token 数
            output_tokens: 输出 Token 数
            input_price_per_1k: 每 1000 输入 Token 价格（美元）
            output_price_per_1k: 每 1000 输出 Token 价格（美元）

        Returns:
            预估成本（美元）
        """
        input_cost = (input_tokens / 1000.0) * input_price_per_1k
        output_cost = (output_tokens / 1000.0) * output_price_per_1k
        return round(input_cost + output_cost, 6)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _evaluate_circuit_level(
        self,
        task_id: str,
        snapshot: ResourceStatusSnapshot,
    ) -> CircuitLevel:
        """根据使用率评估熔断等级

        取三个维度（成本、步数、Token）中使用率最高的值来判定。

        Args:
            task_id: 任务唯一标识
            snapshot: 资源状态快照

        Returns:
            熔断等级
        """
        budget = self._budgets.get(task_id)
        if budget is None:
            return CircuitLevel.NORMAL

        # 如果已耗尽或超时，直接熔断
        if snapshot.is_exhausted or snapshot.is_expired:
            return CircuitLevel.HALTED

        # 取最大使用率
        max_ratio = max(
            snapshot.cost_usage_ratio,
            snapshot.step_usage_ratio,
            snapshot.token_usage_ratio,
        )

        # 时间使用率也要考虑
        if snapshot.timeout_seconds > 0:
            time_ratio = snapshot.elapsed_seconds / snapshot.timeout_seconds
            max_ratio = max(max_ratio, min(time_ratio, 1.0))

        if max_ratio >= budget.halt_threshold:
            return CircuitLevel.HALTED
        elif max_ratio >= budget.critical_threshold:
            return CircuitLevel.CRITICAL
        elif max_ratio >= budget.warning_threshold:
            return CircuitLevel.WARNING
        else:
            return CircuitLevel.NORMAL

    def _on_circuit_level_changed(
        self,
        task_id: str,
        old_level: CircuitLevel,
        new_level: CircuitLevel,
        snapshot: ResourceStatusSnapshot,
    ) -> None:
        """熔断等级变化时的处理

        发出日志、事件和回调。

        Args:
            task_id: 任务唯一标识
            old_level: 旧等级
            new_level: 新等级
            snapshot: 当前状态快照
        """
        logger.warning(
            f"熔断等级变化: task_id={task_id}, "
            f"{old_level.value} -> {new_level.value}, "
            f"cost_ratio={snapshot.cost_usage_ratio:.1%}, "
            f"step_ratio={snapshot.step_usage_ratio:.1%}, "
            f"token_ratio={snapshot.token_usage_ratio:.1%}"
        )

        # 通过 EventBus 发布事件
        self._emit_event_sync(
            EventType.HITL_SUSPENDED,
            source="resource_manager",
            data={
                "task_id": task_id,
                "old_level": old_level.value,
                "new_level": new_level.value,
                "snapshot": snapshot.model_dump(mode="json"),
            },
        )

        # 触发注册的回调
        for callback in self._callbacks:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(callback(task_id, new_level, snapshot))
            except RuntimeError:
                # 没有运行中的事件循环，同步调用
                logger.debug(
                    f"无事件循环，跳过异步回调: {callback.__qualname__}"
                )

    def _suspend_task(
        self,
        task_id: str,
        snapshot: Optional[ResourceStatusSnapshot],
        reason: str = "资源耗尽",
    ) -> None:
        """挂起任务

        Args:
            task_id: 任务唯一标识
            snapshot: 当前状态快照
            reason: 挂起原因
        """
        if task_id in self._suspended:
            return  # 已经挂起，避免重复处理

        self._suspended.add(task_id)
        self._last_circuit[task_id] = CircuitLevel.HALTED

        remaining = {}
        if snapshot is not None:
            remaining = {
                "cost_usd": snapshot.remaining_cost_usd,
                "steps": snapshot.remaining_steps,
                "tokens": snapshot.remaining_tokens,
            }

        logger.error(
            f"任务挂起: task_id={task_id}, reason={reason}, remaining={remaining}"
        )

        self._emit_event_sync(
            EventType.HITL_SUSPENDED,
            source="resource_manager",
            data={
                "task_id": task_id,
                "reason": reason,
                "remaining": remaining,
            },
        )

    def _empty_snapshot(self, task_id: str) -> ResourceStatusSnapshot:
        """构造空快照（任务不存在时使用）"""
        return ResourceStatusSnapshot(
            task_id=task_id,
            circuit_level=CircuitLevel.NORMAL,
        )

    def _emit_event_sync(
        self,
        event_type: EventType,
        source: str,
        data: dict,
    ) -> None:
        """同步方式发布事件（如果 EventBus 可用）

        尝试获取当前事件循环发布异步事件；如果无事件循环则跳过。

        Args:
            event_type: 事件类型
            source: 事件来源
            data: 事件数据
        """
        if self._event_bus is None:
            return

        event = Event(type=event_type, source=source, data=data)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._event_bus.emit(event))
        except RuntimeError:
            logger.debug(
                f"无事件循环，跳过事件发布: {event_type.value}"
            )
