"""调度中枢 - 接收任务，评估复杂度，选择模型，派发给 Agent"""

from __future__ import annotations

from typing import Optional

from symbio.agents.registry import get_registry
from symbio.core.evaluator import ComplexityEvaluator
from symbio.core.event_bus import Event, EventBus, EventType
from symbio.core.guardrail import Guardrail
from symbio.core.rate_limiter import RateLimiter
from symbio.core.router import ModelRouter
from symbio.utils.logger import get_logger
from symbio.utils.types import (
    Intent,
    Message,
    MessageSource,
    Result,
    Task,
    TaskComplexity,
)

logger = get_logger("orchestrator")


class Orchestrator:
    """调度中枢

    职责：
    1. 接收用户消息
    2. 解析意图
    3. 评估复杂度
    4. 选择模型
    5. 派发给 Agent
    6. 返回结果
    """

    def __init__(self):
        self.router = ModelRouter()
        self.evaluator = ComplexityEvaluator()
        self.guardrail = Guardrail()
        self.rate_limiter = RateLimiter()
        self.event_bus = EventBus()
        self.registry = get_registry()

    async def process(self, message: Message) -> Result:
        """处理用户消息

        Args:
            message: 用户消息

        Returns:
            执行结果
        """
        logger.info(f"收到消息: {message.content[:50]}...")

        # 1. 解析意图
        intent = await self._parse_intent(message)
        logger.debug(f"解析意图: action={intent.action}, complexity={intent.estimated_complexity}")

        # 2. 评估复杂度
        complexity = await self.evaluator.evaluate(intent)
        intent.estimated_complexity = complexity
        logger.debug(f"评估复杂度: {complexity.value}")

        # 3. 选择模型
        model_id = self.router.select(complexity)
        logger.debug(f"选择模型: {model_id}")

        # 4. 创建任务
        task = Task(
            intent=intent,
            model=model_id,
        )

        # 5. 签发资源支票
        ticket = self.guardrail.issue_ticket(task.task_id)

        # 6. 触发任务创建事件
        await self.event_bus.emit(Event(
            type=EventType.TASK_CREATED,
            data={"task_id": task.task_id, "model": model_id},
            source="orchestrator",
        ))

        # 7. 查找并执行 Agent
        result = await self._execute_task(task)

        # 8. 释放资源支票
        self.guardrail.release_ticket(task.task_id)

        return result

    async def _parse_intent(self, message: Message) -> Intent:
        """解析用户意图

        Args:
            message: 用户消息

        Returns:
            用户意图
        """
        # 简单实现：直接使用消息内容作为意图
        # TODO: 使用 LLM 进行更精确的意图解析
        return Intent(
            raw_text=message.content,
            action="chat",
        )

    async def _execute_task(self, task: Task) -> Result:
        """执行任务

        Args:
            task: 任务对象

        Returns:
            执行结果
        """
        # 查找合适的 Agent
        agent = self.registry.find_best(task.intent)

        if not agent:
            logger.warning("未找到合适的 Agent，使用默认 GeneralAgent")
            agent = self.registry.get("general")

        if not agent:
            return Result(
                task_id=task.task_id,
                success=False,
                content="没有可用的 Agent",
            )

        # 触发任务开始事件
        await self.event_bus.emit(Event(
            type=EventType.TASK_STARTED,
            data={"task_id": task.task_id, "agent": agent.name},
            source="orchestrator",
        ))

        try:
            # 速率限制
            await self.rate_limiter.acquire(task.model)

            # 执行任务
            result = await agent.execute(task)

            # 触发任务完成事件
            await self.event_bus.emit(Event(
                type=EventType.TASK_COMPLETED,
                data={"task_id": task.task_id, "success": result.success},
                source="orchestrator",
            ))

            return result

        except Exception as e:
            logger.error(f"任务执行失败: {e}")

            # 触发任务失败事件
            await self.event_bus.emit(Event(
                type=EventType.TASK_FAILED,
                data={"task_id": task.task_id, "error": str(e)},
                source="orchestrator",
            ))

            return Result(
                task_id=task.task_id,
                success=False,
                content=f"任务执行失败: {str(e)}",
            )

    def get_status(self) -> dict:
        """获取调度中枢状态"""
        return {
            "agents": self.registry.list_models(),
            "guardrail": {
                "active_tickets": len(self.guardrail._tickets),
            },
        }
