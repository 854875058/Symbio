"""Cost Tracker & Budget Manager - 成本追踪与预算管理

提供 API 调用成本记录、汇总、按 Agent 分解以及预算控制。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("tools.cost_tracker")


class UsageRecord(BaseModel):
    """使用记录"""
    record_id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    task_id: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class CostSummary(BaseModel):
    """成本汇总"""
    total_records: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    avg_cost_per_record: float = 0.0
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None


class AgentCostBreakdown(BaseModel):
    """按 Agent 的成本分解"""
    agent_id: str = ""
    record_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    avg_cost_per_call: float = 0.0
    models_used: list[str] = Field(default_factory=list)


class BudgetStatus(str, Enum):
    """预算状态"""
    NORMAL = "normal"
    WARNING = "warning"
    EXCEEDED = "exceeded"
    DISABLED = "disabled"


class BudgetConfig(BaseModel):
    """预算配置"""
    budget_id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str = ""
    max_cost_usd: float = 100.0
    warning_threshold: float = 0.8
    period_hours: int = 24
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.now)


class BudgetCheckResult(BaseModel):
    """预算检查结果"""
    budget_id: str = ""
    agent_id: str = ""
    status: BudgetStatus = BudgetStatus.NORMAL
    current_cost_usd: float = 0.0
    max_cost_usd: float = 0.0
    usage_ratio: float = 0.0
    remaining_usd: float = 0.0
    should_downgrade: bool = False


class CostTracker:
    """成本追踪器

    记录每次 API 调用的成本，提供汇总和按 Agent 分解。
    """

    def __init__(self):
        self._records: list[UsageRecord] = []
        logger.info("CostTracker 创建")

    def record(self, record: UsageRecord) -> None:
        """记录一次使用"""
        self._records.append(record)
        logger.debug(
            f"记录使用: agent={record.agent_id}, "
            f"tokens={record.total_tokens}, cost=${record.cost_usd:.6f}"
        )

    def record_usage(
        self,
        agent_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        task_id: str = "",
        session_id: str = "",
    ) -> UsageRecord:
        """记录使用（便捷方法）"""
        record = UsageRecord(
            agent_id=agent_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost_usd,
            task_id=task_id,
            session_id=session_id,
        )
        self.record(record)
        return record

    def get_summary(
        self,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> CostSummary:
        """获取成本汇总

        Args:
            agent_id: 按 Agent 过滤
            session_id: 按会话过滤

        Returns:
            成本汇总
        """
        filtered = self._records
        if agent_id:
            filtered = [r for r in filtered if r.agent_id == agent_id]
        if session_id:
            filtered = [r for r in filtered if r.session_id == session_id]

        if not filtered:
            return CostSummary()

        total_input = sum(r.input_tokens for r in filtered)
        total_output = sum(r.output_tokens for r in filtered)
        total_tokens = sum(r.total_tokens for r in filtered)
        total_cost = sum(r.cost_usd for r in filtered)

        return CostSummary(
            total_records=len(filtered),
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            avg_cost_per_record=total_cost / len(filtered) if filtered else 0.0,
            period_start=min(r.created_at for r in filtered),
            period_end=max(r.created_at for r in filtered),
        )

    def get_agent_breakdown(self) -> list[AgentCostBreakdown]:
        """按 Agent 分解成本"""
        agent_data: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "tokens": 0, "cost": 0.0, "models": set()}
        )

        for record in self._records:
            data = agent_data[record.agent_id]
            data["count"] += 1
            data["tokens"] += record.total_tokens
            data["cost"] += record.cost_usd
            if record.model:
                data["models"].add(record.model)

        breakdowns = []
        for agent_id, data in agent_data.items():
            breakdowns.append(AgentCostBreakdown(
                agent_id=agent_id,
                record_count=data["count"],
                total_tokens=data["tokens"],
                total_cost_usd=data["cost"],
                avg_cost_per_call=data["cost"] / data["count"] if data["count"] > 0 else 0.0,
                models_used=list(data["models"]),
            ))

        breakdowns.sort(key=lambda b: b.total_cost_usd, reverse=True)
        return breakdowns

    def get_records(
        self,
        agent_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[UsageRecord]:
        """获取使用记录"""
        filtered = self._records
        if agent_id:
            filtered = [r for r in filtered if r.agent_id == agent_id]
        return filtered[-limit:]

    def clear(self) -> int:
        """清空记录"""
        count = len(self._records)
        self._records.clear()
        return count

    @property
    def total_records(self) -> int:
        return len(self._records)


class BudgetManager:
    """预算管理器

    为每个 Agent 设置成本预算，监控使用情况，触发降级策略。
    """

    def __init__(self, cost_tracker: CostTracker):
        self._cost_tracker = cost_tracker
        self._budgets: dict[str, BudgetConfig] = {}
        logger.info("BudgetManager 创建")

    def set_budget(self, config: BudgetConfig) -> BudgetConfig:
        """设置预算

        Args:
            config: 预算配置

        Returns:
            预算配置
        """
        self._budgets[config.agent_id] = config
        logger.info(
            f"设置预算: agent={config.agent_id}, "
            f"max=${config.max_cost_usd}, warning={config.warning_threshold}"
        )
        return config

    def get_budget(self, agent_id: str) -> Optional[BudgetConfig]:
        """获取预算配置"""
        return self._budgets.get(agent_id)

    def check_status(self, agent_id: str) -> BudgetCheckResult:
        """检查预算状态

        Args:
            agent_id: Agent ID

        Returns:
            预算检查结果
        """
        budget = self._budgets.get(agent_id)
        if budget is None or not budget.enabled:
            return BudgetCheckResult(
                agent_id=agent_id,
                status=BudgetStatus.DISABLED,
            )

        # 获取当前成本
        summary = self._cost_tracker.get_summary(agent_id=agent_id)
        current_cost = summary.total_cost_usd
        usage_ratio = current_cost / budget.max_cost_usd if budget.max_cost_usd > 0 else 0.0
        remaining = max(0.0, budget.max_cost_usd - current_cost)

        # 判断状态
        if usage_ratio >= 1.0:
            status = BudgetStatus.EXCEEDED
        elif usage_ratio >= budget.warning_threshold:
            status = BudgetStatus.WARNING
        else:
            status = BudgetStatus.NORMAL

        # 判断是否需要降级
        should_downgrade = status in (BudgetStatus.WARNING, BudgetStatus.EXCEEDED)

        return BudgetCheckResult(
            budget_id=budget.budget_id,
            agent_id=agent_id,
            status=status,
            current_cost_usd=current_cost,
            max_cost_usd=budget.max_cost_usd,
            usage_ratio=usage_ratio,
            remaining_usd=remaining,
            should_downgrade=should_downgrade,
        )

    def should_downgrade(self, agent_id: str) -> bool:
        """检查是否应该降级（使用更便宜的模型）"""
        result = self.check_status(agent_id)
        return result.should_downgrade

    def get_all_statuses(self) -> list[BudgetCheckResult]:
        """获取所有预算状态"""
        return [self.check_status(agent_id) for agent_id in self._budgets]

    def remove_budget(self, agent_id: str) -> bool:
        """移除预算"""
        if agent_id in self._budgets:
            del self._budgets[agent_id]
            return True
        return False

    @property
    def total_budgets(self) -> int:
        return len(self._budgets)
