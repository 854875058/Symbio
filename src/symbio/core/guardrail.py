"""预算熔断与安全拦截网关"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger

logger = get_logger("guardrail")


class ResourceTicket(BaseModel):
    """资源支票：任务启动时签发的总预算"""
    task_id: str
    max_cost_usd: float = 10.0
    max_steps: int = 50
    max_tokens: int = 100000
    timeout_seconds: int = 3600

    # 实时消耗
    consumed_cost: float = 0.0
    consumed_steps: int = 0
    consumed_tokens: int = 0
    created_at: datetime = Field(default_factory=datetime.now)

    def deduct(self, tokens: int, cost_usd: float) -> None:
        """扣减资源额度"""
        self.consumed_tokens += tokens
        self.consumed_cost += cost_usd
        self.consumed_steps += 1

    def is_exhausted(self) -> bool:
        """检查是否耗尽"""
        return (
            self.consumed_cost >= self.max_cost_usd or
            self.consumed_steps >= self.max_steps or
            self.consumed_tokens >= self.max_tokens
        )

    def is_expired(self) -> bool:
        """检查是否超时"""
        elapsed = (datetime.now() - self.created_at).total_seconds()
        return elapsed > self.timeout_seconds

    def remaining(self) -> dict:
        """返回剩余配额"""
        return {
            "cost_usd": max(0, self.max_cost_usd - self.consumed_cost),
            "steps": max(0, self.max_steps - self.consumed_steps),
            "tokens": max(0, self.max_tokens - self.consumed_tokens),
        }


class BudgetExceededError(Exception):
    """预算超限错误"""
    def __init__(self, task_id: str, reason: str, remaining: dict):
        self.task_id = task_id
        self.reason = reason
        self.remaining = remaining
        super().__init__(f"任务 {task_id} 预算超限: {reason}")


class Guardrail:
    """预算熔断与安全拦截网关

    职责：
    1. 任务级预算控制（Token 成本、步数、超时）
    2. 高危命令拦截
    3. 安全等级分级
    """

    # 危险命令黑名单
    BLOCKED_COMMANDS = [
        "rm -rf /",
        "mkfs",
        "dd if=",
        ":(){ :|:& };:",
        "chmod -R 777",
        "wget | sh",
        "curl | sh",
    ]

    def __init__(self):
        self._tickets: dict[str, ResourceTicket] = {}

    def issue_ticket(
        self,
        task_id: str,
        max_cost_usd: float = 10.0,
        max_steps: int = 50,
        max_tokens: int = 100000,
        timeout_seconds: int = 3600,
    ) -> ResourceTicket:
        """签发资源支票

        Args:
            task_id: 任务 ID
            max_cost_usd: 最大成本（美元）
            max_steps: 最大步数
            max_tokens: 最大 Token 数
            timeout_seconds: 超时时间（秒）

        Returns:
            资源支票
        """
        ticket = ResourceTicket(
            task_id=task_id,
            max_cost_usd=max_cost_usd,
            max_steps=max_steps,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
        )
        self._tickets[task_id] = ticket
        logger.info(f"签发资源支票: {task_id}, 最大成本=${max_cost_usd}")
        return ticket

    def check_and_deduct(
        self,
        task_id: str,
        tokens: int,
        cost_usd: float,
    ) -> bool:
        """检查资源并扣减额度

        Args:
            task_id: 任务 ID
            tokens: 消耗的 Token 数
            cost_usd: 消耗的成本

        Returns:
            是否允许继续

        Raises:
            BudgetExceededError: 预算超限
        """
        ticket = self._tickets.get(task_id)
        if not ticket:
            return True  # 无票证任务不限制

        # 扣减
        ticket.deduct(tokens, cost_usd)

        # 检查是否耗尽
        if ticket.is_exhausted():
            remaining = ticket.remaining()
            logger.warning(f"资源耗尽: {task_id}, 剩余={remaining}")
            raise BudgetExceededError(
                task_id=task_id,
                reason="预算耗尽",
                remaining=remaining,
            )

        # 检查是否超时
        if ticket.is_expired():
            logger.warning(f"任务超时: {task_id}")
            raise BudgetExceededError(
                task_id=task_id,
                reason="任务超时",
                remaining=ticket.remaining(),
            )

        return True

    def release_ticket(self, task_id: str) -> None:
        """释放资源支票"""
        self._tickets.pop(task_id, None)
        logger.debug(f"释放资源支票: {task_id}")

    def get_status(self, task_id: str) -> Optional[dict]:
        """获取任务资源消耗状态"""
        ticket = self._tickets.get(task_id)
        if not ticket:
            return None

        return {
            "task_id": task_id,
            "consumed": {
                "cost_usd": ticket.consumed_cost,
                "steps": ticket.consumed_steps,
                "tokens": ticket.consumed_tokens,
            },
            "remaining": ticket.remaining(),
            "expired": ticket.is_expired(),
        }

    def check_command(self, command: str) -> bool:
        """检查命令是否安全

        Args:
            command: Shell 命令

        Returns:
            是否安全

        Raises:
            ValueError: 命令被拦截
        """
        command_lower = command.lower().strip()

        for blocked in self.BLOCKED_COMMANDS:
            if blocked.lower() in command_lower:
                logger.warning(f"拦截危险命令: {command}")
                raise ValueError(f"命令被安全策略拦截: 包含危险模式 '{blocked}'")

        return True

    def check_permission(self, tool_name: str, action: str) -> str:
        """检查工具权限等级

        Args:
            tool_name: 工具名称
            action: 操作类型

        Returns:
            权限等级 (read_only / write / execute / admin)
        """
        # 高危工具列表
        high_risk_tools = {"shell", "sandbox", "git"}
        sensitive_actions = {"delete", "remove", "drop", "truncate", "format"}

        if tool_name in high_risk_tools:
            if any(sa in action.lower() for sa in sensitive_actions):
                return "execute"  # 高危，需要 HITL
            return "write"

        return "read_only"
