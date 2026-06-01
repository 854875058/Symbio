"""调度中枢 - 接收任务，评估复杂度，选择模型，派发给 Agent"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]

from symbio.agents.registry import get_registry
from symbio.config.settings import get_settings
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

    _VALID_ACTIONS: set[str] = {
        "chat", "code_review", "write_code", "analyze_data",
        "search", "file_operation", "git_operation",
    }

    async def _parse_intent(self, message: Message) -> Intent:
        """使用 LLM 解析用户意图

        三层降级策略：
        1. Anthropic API（httpx）── 优先
        2. OpenAI SDK ── 备选
        3. 安全回退（验证 + 默认）── 兜底

        Args:
            message: 用户消息

        Returns:
            解析后的用户意图
        """
        settings = get_settings()
        mc = settings.model

        # 第 1/2 层：尝试 LLM 解析
        raw_result: Optional[dict] = None
        if mc.anthropic_api_key:
            raw_result = await self._call_llm_anthropic(mc, message.content)
        elif mc.openai_api_key and openai is not None:
            raw_result = await self._call_llm_openai(mc, message.content)

        # 第 3 层：安全回退 ── 验证 LLM 输出，无效则降级
        return self._safety_fallback(message.content, raw_result)

    @classmethod
    def _safety_fallback(cls, raw_text: str, llm_result: Optional[dict]) -> Intent:
        """验证 LLM 返回结果，不合法时安全降级到默认意图。"""
        # 无 LLM 结果 → 直接降级
        if not llm_result or not isinstance(llm_result, dict):
            logger.debug("LLM 返回为空或非 dict，降级到 chat")
            return Intent(raw_text=raw_text, action="chat")

        # 验证 action 必须是合法枚举值
        action = llm_result.get("action", "chat")
        if action not in cls._VALID_ACTIONS:
            logger.warning(f"LLM 返回非法 action='{action}'，降级到 chat")
            action = "chat"

        # 验证参数类型，防止 LLM 返回畸形数据
        parameters = llm_result.get("parameters", {})
        if not isinstance(parameters, dict):
            parameters = {}

        requires_tools = llm_result.get("requires_tools", [])
        if not isinstance(requires_tools, list):
            requires_tools = []

        requires_memory = llm_result.get("requires_memory", False)
        if not isinstance(requires_memory, bool):
            requires_memory = False

        return Intent(
            raw_text=raw_text,
            action=action,
            parameters=parameters,
            requires_tools=requires_tools,
            requires_memory=requires_memory,
        )

    # ------------------------------------------------------------------
    # LLM 调用辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _build_intent_system_prompt() -> str:
        """构造意图解析的 system prompt。"""
        return (
            "You are an intent parser. Analyze the user's message and extract a structured intent.\n"
            "Respond with ONLY a JSON object, no markdown fences.\n\n"
            "Supported action categories:\n"
            "- chat: general conversation, questions, greetings\n"
            "- code_review: reviewing, auditing, or analyzing existing code\n"
            "- write_code: creating or modifying code\n"
            "- analyze_data: data analysis, statistics, visualization\n"
            "- search: searching for information, files, or content\n"
            "- file_operation: reading, writing, or managing files\n"
            "- git_operation: git commands, version control tasks\n\n"
            'JSON schema: {"action": str, "parameters": {"file_paths": [], "tool_names": [], "language": str, "description": str}, '
            '"requires_tools": [str], "requires_memory": bool}'
        )

    async def _call_llm_anthropic(self, mc: Any, user_text: str) -> Optional[dict]:
        """使用 httpx 调用 Anthropic Messages API。"""
        try:
            url = mc.anthropic_base_url.rstrip("/") + "/v1/messages"
            headers = {
                "x-api-key": mc.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": mc.model_low,
                "max_tokens": 512,
                "system": self._build_intent_system_prompt(),
                "messages": [{"role": "user", "content": user_text}],
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()

            data = resp.json()
            text = data["content"][0]["text"].strip()
            return json.loads(text)

        except Exception as exc:
            logger.warning(f"Anthropic 意图解析失败: {exc}")
            return None

    async def _call_llm_openai(self, mc: Any, user_text: str) -> Optional[dict]:
        """使用 OpenAI SDK 调用兼容端点。"""
        try:
            client = openai.AsyncOpenAI(
                api_key=mc.openai_api_key,
                base_url=mc.openai_base_url,
            )
            response = await client.chat.completions.create(
                model=mc.model_low,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": self._build_intent_system_prompt()},
                    {"role": "user", "content": user_text},
                ],
            )
            text = response.choices[0].message.content.strip()
            return json.loads(text)

        except Exception as exc:
            logger.warning(f"OpenAI 意图解析失败: {exc}")
            return None

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
            "agents": self.registry.list_agents(),
            "guardrail": {
                "active_tickets": len(self.guardrail._tickets),
            },
        }
