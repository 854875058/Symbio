"""调度中枢 - 接收任务，评估复杂度，选择模型，派发给 Agent"""

from __future__ import annotations

import json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import httpx

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]

from symbio.agents.debate import DebateEngine
from symbio.core.hitl_gateway import ApprovalGateway, ApprovalRequest, ApprovalStatus, RiskLevel
from symbio.core.hitl_notifier import HITLNotifier
from symbio.agents.registry import get_registry
from symbio.agents.subagent import SubAgentManager
from symbio.config.settings import get_settings
from symbio.core.dag_orchestrator import DAGOrchestrator
from symbio.core.decomposer import TaskDecomposer
from symbio.core.evaluator import ComplexityEvaluator
from symbio.core.event_bus import Event, EventBus, EventType
from symbio.core.guardrail import Guardrail
from symbio.core.memory_bridge import MemoryBridge
from symbio.core.rate_limiter import RateLimiter
from symbio.core.router import ModelRouter
from symbio.core.state_manager import InstructionGenerator, StateManager, TaskPhase
from symbio.core.tracer import get_tracer
from symbio.core.workflow_policy import workflow_policy_for_intent
from symbio.tools.lazy_loader import ToolLazyLoader
from symbio.utils.logger import get_logger
from symbio.utils.types import (
    AgentState,
    Intent,
    Message,
    MessageSource,
    Result,
    Task,
    TaskComplexity,
    TokenUsage,
)

logger = get_logger("orchestrator")

DEFAULT_HITL_DB_PATH = str(Path("data") / "hitl.db")
DEFAULT_STATE_DB_PATH = str(Path("data") / "state.db")


@asynccontextmanager
async def _nullcontext() -> AsyncIterator[None]:
    """Async no-op context manager used when tracer is unavailable."""
    yield None


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
        self.decomposer = TaskDecomposer()
        self.subagent_manager = SubAgentManager(self.registry, self.event_bus, self.rate_limiter)
        self.debate_engine = DebateEngine(use_llm=True)
        self.memory_bridge = MemoryBridge()
        self.state_manager = StateManager(persist_path=DEFAULT_STATE_DB_PATH)
        self.instruction_generator = InstructionGenerator()
        self.hitl_gateway = ApprovalGateway(persist_path=DEFAULT_HITL_DB_PATH)
        self.hitl_notifier = HITLNotifier.from_settings()
        self.tool_loader = ToolLazyLoader()
        self.dag_orchestrator = DAGOrchestrator(registry=self.registry)
        self._pending_hitl_tasks: dict[str, Task] = {}  # request_id -> Task

    async def initialize_memory(self) -> None:
        """初始化记忆系统（在首次处理消息时调用）"""
        if not self.memory_bridge._initialized:
            await self.memory_bridge.initialize()

    async def _close_runtime_resources(self) -> None:
        """Close ephemeral persistence handles opened during task execution."""
        await self.state_manager.close()
        await self.hitl_gateway.close()

    async def process(self, message: Message) -> Result:
        """处理用户消息

        Args:
            message: 用户消息

        Returns:
            执行结果
        """
        tracer = get_tracer()

        # If tracer is unavailable, run without tracing
        if tracer is None:
            try:
                return await self._process_inner(message)
            finally:
                await self._close_runtime_resources()

        async with tracer.span("orchestrator.process") as root_span:
            try:
                return await self._process_inner(message, root_span)
            finally:
                await self._close_runtime_resources()

    async def _process_inner(self, message: Message, root_span=None) -> Result:
        """内部处理逻辑，可选地被 Span 包裹。"""
        logger.info(f"收到消息: {message.content[:50]}...")
        tracer = get_tracer()

        # 1. 解析意图
        async with tracer.span("orchestrator.intent_parsing") if tracer else _nullcontext():
            intent = await self._parse_intent(message)
            logger.debug(f"解析意图: action={intent.action}, complexity={intent.estimated_complexity}")

        # 2. 评估复杂度
        async with tracer.span("orchestrator.complexity_evaluation") if tracer else _nullcontext():
            complexity = await self.evaluator.evaluate(intent)
            intent.estimated_complexity = complexity
            logger.debug(f"评估复杂度: {complexity.value}")

        # 3. 选择模型
        async with tracer.span(
            "orchestrator.model_selection",
            attributes={"complexity": complexity.value},
        ) if tracer else _nullcontext() as model_span:
            model_id = self.router.select(complexity)
            logger.debug(f"选择模型: {model_id}")
            if model_span is not None:
                model_span.set_attribute("model_id", model_id)

        # 4. 创建任务
        async with tracer.span(
            "orchestrator.task_creation",
            attributes={"model_id": model_id},
        ) if tracer else _nullcontext() as task_span:
            task = Task(
                intent=intent,
                model=model_id,
            )
            if message.metadata:
                task.metadata.update(message.metadata)
            workflow_policy = workflow_policy_for_intent(intent)
            task.metadata["workflow_policy"] = workflow_policy.model_dump()
            task.metadata["workflow_guidance"] = workflow_policy.to_prompt()
            if task_span is not None:
                task_span.set_attribute("task_id", task.task_id)

        # Set task_id on root span now that we have it
        if root_span is not None:
            root_span.set_attribute("task_id", task.task_id)
            root_span.set_attribute("model_id", model_id)

        # 4.1. 初始化全局状态（StateManager 驱动的状态通信）
        try:
            await self.state_manager.initialize(
                task_id=task.task_id,
                requirements=message.content,
            )
            await self.state_manager.update(
                lambda s: s.model_copy(update={
                    "phase": TaskPhase.PLANNING,
                    "metadata": {
                        **s.metadata,
                        "workflow_policy": task.metadata.get("workflow_policy"),
                        "workflow_guidance": task.metadata.get("workflow_guidance"),
                        "message_metadata": message.metadata,
                    },
                })
            )
            logger.debug(f"全局状态初始化完成: task_id={task.task_id}")
        except Exception as exc:
            logger.warning(f"状态初始化失败（不影响主流程）: {exc}")

        # 4.5. 记忆上下文增强（初始化记忆系统并注入相关记忆）
        try:
            await self.initialize_memory()
            memory_context = await self.memory_bridge.enhance_context(
                query=message.content,
                session_id=message.session_id,
            )
            if memory_context:
                task.metadata["memory_context"] = memory_context
                logger.debug(f"记忆上下文已注入: {len(memory_context)} 字符")
        except Exception as exc:
            logger.debug(f"记忆上下文注入失败（不影响主流程）: {exc}")

        # 5. 签发资源支票
        ticket = self.guardrail.issue_ticket(task.task_id)

        # 6. 触发任务创建事件
        await self.event_bus.emit(Event(
            type=EventType.TASK_CREATED,
            data={"task_id": task.task_id, "model": model_id},
            source="orchestrator",
        ))

        # 7. 分解任务（Phase 2 集成）
        hitl_request_id = await self._check_hitl_required(task)
        if hitl_request_id:
            task.state = AgentState.WAITING
            await self.event_bus.emit(Event(
                type=EventType.HITL_SUSPENDED,
                data={
                    "task_id": task.task_id,
                    "request_id": hitl_request_id,
                    "risk_level": task.metadata.get("risk_level", "low"),
                },
                source="orchestrator",
            ))
            try:
                await self.state_manager.update(
                    lambda s: s.model_copy(update={
                        "status": "waiting_approval",
                        "metadata": {
                            **s.metadata,
                            "hitl_pending": True,
                            "hitl_request_id": hitl_request_id,
                        },
                    })
                )
            except Exception as exc:
                logger.warning(f"HITL 状态更新失败（不影响审批等待）: {exc}")

            self.guardrail.release_ticket(task.task_id)
            return Result(
                task_id=task.task_id,
                success=True,
                content="任务已暂停，等待人类审批后继续执行。",
                data={
                    "hitl_pending": True,
                    "hitl_request_id": hitl_request_id,
                    "task_id": task.task_id,
                    "risk_level": task.metadata.get("risk_level", "low"),
                },
            )

        return await self._execute_via_dag(task, root_span=root_span, release_ticket=True)

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

    async def _execute_task(self, task: Task, root_span=None) -> Result:
        """执行任务

        Args:
            task: 任务对象
            root_span: 父 Span，用于设置 agent 属性

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

        # Record agent info on root span
        if root_span is not None:
            root_span.set_attribute("agent_name", agent.name)

        # 触发任务开始事件
        await self.event_bus.emit(Event(
            type=EventType.TASK_STARTED,
            data={"task_id": task.task_id, "agent": agent.name},
            source="orchestrator",
        ))

        try:
            # 速率限制
            await self.rate_limiter.acquire(task.model)

            # 执行任务 -- wrapped in a child span
            tracer = get_tracer()
            async with tracer.span(
                "orchestrator.agent_dispatch",
                attributes={
                    "agent_name": agent.name,
                    "model_id": task.model,
                    "task_id": task.task_id,
                },
            ) if tracer else _nullcontext():
                result = await agent.execute(task)

            # Record token usage on root span
            if root_span is not None and result.token_usage.total_tokens > 0:
                root_span.add_event("agent_token_usage", attributes={
                    "agent_name": agent.name,
                    "input_tokens": result.token_usage.input_tokens,
                    "output_tokens": result.token_usage.output_tokens,
                    "total_tokens": result.token_usage.total_tokens,
                })

            # 触发任务完成事件
            await self.event_bus.emit(Event(
                type=EventType.TASK_COMPLETED,
                data={"task_id": task.task_id, "success": result.success},
                source="orchestrator",
            ))

            return result

        except Exception as e:
            logger.error(f"任务执行失败: {e}")

            # Record exception on root span
            if root_span is not None:
                root_span.set_status("ERROR", str(e))
                root_span.record_exception(e)

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

    async def _execute_with_subagents(
        self, task: Task, decomposition, root_span=None
    ) -> Result:
        """通过 SubAgentManager 并行执行多个子任务。

        Args:
            task: 父任务对象。
            decomposition: DecompositionResult 分解结果。
            root_span: 父 Span。

        Returns:
            聚合后的执行结果。
        """
        tracer = get_tracer()
        try:
            async with tracer.span(
                "orchestrator.subagent_execution",
                attributes={
                    "task_id": task.task_id,
                    "subtask_count": len(decomposition.subtasks),
                },
            ) if tracer else _nullcontext():
                aggregated = await self.subagent_manager.execute_subtasks(
                    subtasks=decomposition.subtasks,
                    parent_task=task,
                    execution_order=decomposition.execution_order,
                )

            # 将聚合 token 用量转换为 TokenUsage
            token_usage = self._token_usage_from_dict(aggregated.total_token_usage)

            result = Result(
                task_id=task.task_id,
                success=aggregated.success,
                content=aggregated.combined_content,
                token_usage=token_usage,
                data={
                    "total_subtasks": aggregated.total_subtasks,
                    "completed_subtasks": aggregated.completed_subtasks,
                    "failed_subtasks": aggregated.failed_subtasks,
                    "execution_mode": "subagent",
                },
            )

            await self.event_bus.emit(Event(
                type=EventType.TASK_COMPLETED,
                data={
                    "task_id": task.task_id,
                    "success": result.success,
                    "subtask_count": aggregated.total_subtasks,
                },
                source="orchestrator",
            ))

            return result

        except Exception as e:
            logger.error(f"子任务执行失败: {e}")
            if root_span is not None:
                root_span.set_status("ERROR", str(e))
                root_span.record_exception(e)

            await self.event_bus.emit(Event(
                type=EventType.TASK_FAILED,
                data={"task_id": task.task_id, "error": str(e)},
                source="orchestrator",
            ))

            return Result(
                task_id=task.task_id,
                success=False,
                content=f"子任务执行失败: {str(e)}",
            )

    async def _execute_with_debate(
        self, task: Task, decomposition, root_span=None
    ) -> Result:
        """通过 DebateEngine 运行多智能体辩论后执行子任务。

        先运行辩论获取最佳提案，再用子任务执行器落实。

        Args:
            task: 父任务对象。
            decomposition: DecompositionResult 分解结果。
            root_span: 父 Span。

        Returns:
            辩论与执行的综合结果。
        """
        tracer = get_tracer()
        debate_content = ""

        # Phase A: 运行辩论
        try:
            async with tracer.span(
                "orchestrator.debate",
                attributes={"task_id": task.task_id},
            ) if tracer else _nullcontext():
                topic = decomposition.original_intent or task.intent.raw_text
                initial_proposal = (
                    decomposition.subtasks[0].description
                    if decomposition.subtasks
                    else ""
                )
                session = await self.debate_engine.run_debate(
                    topic=topic,
                    initial_proposal=initial_proposal,
                )
                debate_content = session.final_proposal

            logger.info(
                f"辩论完成: session={session.session_id}, "
                f"status={session.status.value}, rounds={len(session.rounds)}"
            )
        except Exception as exc:
            logger.warning(f"辩论执行失败，降级到最后提案: {exc}")
            debate_content = decomposition.subtasks[0].description if decomposition.subtasks else ""

        # Phase B: 用子任务执行器落实辩论结果
        try:
            async with tracer.span(
                "orchestrator.subagent_execution",
                attributes={
                    "task_id": task.task_id,
                    "subtask_count": len(decomposition.subtasks),
                },
            ) if tracer else _nullcontext():
                aggregated = await self.subagent_manager.execute_subtasks(
                    subtasks=decomposition.subtasks,
                    parent_task=task,
                    execution_order=decomposition.execution_order,
                )

            token_usage = self._token_usage_from_dict(aggregated.total_token_usage)

            # 合并辩论提案与子任务执行结果
            combined = (
                f"## 辩论提案\n{debate_content}\n\n"
                f"## 执行结果\n{aggregated.combined_content}"
                if debate_content
                else aggregated.combined_content
            )

            result = Result(
                task_id=task.task_id,
                success=aggregated.success,
                content=combined,
                token_usage=token_usage,
                data={
                    "total_subtasks": aggregated.total_subtasks,
                    "completed_subtasks": aggregated.completed_subtasks,
                    "failed_subtasks": aggregated.failed_subtasks,
                    "execution_mode": "debate",
                    "debate_proposal": debate_content,
                },
            )

        except Exception as exc:
            logger.error(f"辩论后子任务执行失败: {exc}")
            # 降级：仅返回辩论提案作为结果
            result = Result(
                task_id=task.task_id,
                success=True,
                content=debate_content or "辩论已完成但执行失败",
                data={"execution_mode": "debate_fallback"},
            )

        await self.event_bus.emit(Event(
            type=EventType.TASK_COMPLETED,
            data={
                "task_id": task.task_id,
                "success": result.success,
                "execution_mode": result.data.get("execution_mode", "debate"),
            },
            source="orchestrator",
        ))

        return result

    @staticmethod
    def _token_usage_from_dict(usage_dict: dict) -> TokenUsage:
        """将字典格式的 token 用量转换为 TokenUsage 对象。"""
        return TokenUsage(
            input_tokens=usage_dict.get("input_tokens", 0),
            output_tokens=usage_dict.get("output_tokens", 0),
            total_tokens=usage_dict.get("total_tokens", 0),
            model=", ".join(usage_dict.get("models", [])) if "models" in usage_dict else usage_dict.get("model", ""),
            cost_usd=usage_dict.get("cost_usd", 0.0),
        )

    def get_status(self) -> dict:
        """获取调度中枢状态"""
        return {
            "agents": self.registry.list_agents(),
            "guardrail": {
                "active_tickets": len(self.guardrail._tickets),
            },
            "hitl_pending": len(self.hitl_gateway._pending),
        }

    # ------------------------------------------------------------------
    # HITL (Human-in-the-Loop) 集成
    # ------------------------------------------------------------------

    async def _check_hitl_required(self, task: Task) -> Optional[str]:
        """检查任务是否需要 HITL 审批，返回 request_id 或 None

        当任务 metadata 中 risk_level 为 medium/high/critical 时，
        提交审批请求并返回 request_id；low 风险直接返回 None。

        Args:
            task: 任务对象

        Returns:
            审批请求 ID（需要审批时），或 None（无需审批）
        """
        risk_level_str = task.metadata.get("risk_level", "low")
        try:
            risk_level = RiskLevel(risk_level_str)
        except ValueError:
            risk_level = RiskLevel.LOW

        if risk_level == RiskLevel.LOW:
            return None

        request = ApprovalRequest(
            task_id=task.task_id,
            action=f"执行任务: {task.intent.raw_text[:100]}",
            impact_scope="单次任务执行",
            reason="任务复杂度较高，需要人工确认",
            risk_level=risk_level,
            metadata={
                "intent": task.intent.raw_text,
                "workflow_policy": task.metadata.get("workflow_policy"),
                "workflow_guidance": task.metadata.get("workflow_guidance"),
                "risk_level": risk_level.value,
            },
        )
        request_id = await self.hitl_gateway.submit_request(request)
        if request.status == ApprovalStatus.PENDING:
            await self.hitl_notifier.notify(request)

        # 暂存任务，等待审批后恢复
        self._pending_hitl_tasks[request_id] = task
        await self.hitl_gateway.attach_task_context(
            request_id,
            task.model_dump(mode="json"),
        )

        logger.info(
            f"HITL 审批已提交: request_id={request_id}, "
            f"task_id={task.task_id}, risk_level={risk_level.value}"
        )
        return request_id

    async def resume_after_approval(self, request_id: str) -> Optional[Result]:
        """在人工审批通过后继续执行任务

        Args:
            request_id: 审批请求 ID

        Returns:
            执行结果，如果审批未通过或任务不存在则返回 None
        """
        request = await self.hitl_gateway.get_request(request_id)
        if request is None:
            logger.warning(f"HITL 恢复失败: 请求 {request_id} 不存在")
            return None

        if request.status != ApprovalStatus.APPROVED:
            logger.warning(
                f"HITL 恢复失败: 请求 {request_id} 状态为 {request.status.value}，需要 APPROVED"
            )
            return None

        task = self._pending_hitl_tasks.pop(request_id, None)
        if task is None:
            task_context = await self.hitl_gateway.get_task_context(request_id)
            if task_context is not None:
                task = Task.model_validate(task_context)
        if task is None:
            logger.warning(f"HITL 恢复失败: 任务 {request.task_id} 未在暂存中找到")
            return None

        logger.info(f"HITL 审批通过，恢复执行任务: task_id={task.task_id}")
        try:
            await self.state_manager.restore(task.task_id)
        except Exception as exc:
            logger.warning(f"HITL 状态恢复失败（不影响恢复执行）: {exc}")
        result = await self._execute_via_dag(task, release_ticket=False)
        await self.hitl_gateway.clear_task_context(request_id)

        # 将审批信息附加到结果中
        result.data["hitl_approved"] = True
        result.data["hitl_request_id"] = request_id

        await self.event_bus.emit(Event(
            type=EventType.HITL_APPROVED,
            data={
                "task_id": task.task_id,
                "request_id": request_id,
                "success": result.success,
            },
            source="orchestrator",
        ))

        return result

    def _load_task_tools(self, task: Task) -> None:
        try:
            tool_schemas = self.tool_loader.load_for_node({
                "node_id": task.task_id,
                "tools": task.intent.requires_tools,
            })
            task.metadata["available_tools"] = [s.name for s in tool_schemas]
            if tool_schemas:
                logger.debug(
                    f"工具懒加载完成: task_id={task.task_id}, "
                    f"tools={[s.name for s in tool_schemas]}"
                )
        except Exception as exc:
            logger.warning(f"工具懒加载失败（不影响主流程）: {exc}")

    async def _execute_via_dag(
        self,
        task: Task,
        *,
        root_span=None,
        release_ticket: bool,
    ) -> Result:
        self._load_task_tools(task)

        try:
            try:
                await self.state_manager.update(
                    lambda s: s.model_copy(update={"phase": TaskPhase.EXECUTING})
                )
            except Exception as exc:
                logger.warning(f"状态更新失败（不影响主流程）: {exc}")

            result = await self.dag_orchestrator.execute(task)
            task.result = result
            task.state = AgentState.COMPLETED if result.success else AgentState.FAILED

            try:
                final_phase = TaskPhase.COMPLETED if result.success else TaskPhase.FAILED
                await self.state_manager.update(
                    lambda s: s.model_copy(update={
                        "phase": final_phase,
                        "status": "completed" if result.success else "failed",
                        "metadata": {
                            **s.metadata,
                            "result_summary": result.content[:500] if result.content else "",
                            "model": task.model,
                        },
                    })
                )
                logger.debug(f"状态更新为 {final_phase.value}: success={result.success}")
            except Exception as exc:
                logger.warning(f"状态更新失败（不影响主流程）: {exc}")

            try:
                await self.memory_bridge.store_execution_result(
                    task_id=task.task_id,
                    result_content=result.content,
                    metadata={"success": result.success, "model": task.model},
                )
            except Exception as exc:
                logger.debug(f"执行结果存储到记忆失败（不影响主流程）: {exc}")

            if root_span is not None:
                root_span.set_attribute("success", result.success)
                root_span.add_event("task_completed", attributes={
                    "task_id": task.task_id,
                    "success": result.success,
                })
                if result.token_usage.total_tokens > 0:
                    root_span.add_event("token_usage", attributes={
                        "input_tokens": result.token_usage.input_tokens,
                        "output_tokens": result.token_usage.output_tokens,
                        "total_tokens": result.token_usage.total_tokens,
                        "model": result.token_usage.model,
                    })

            return result
        finally:
            try:
                unloaded = self.tool_loader.unload_node_tools(task.task_id)
                if unloaded:
                    logger.debug(f"工具懒卸载完成: task_id={task.task_id}, unloaded={unloaded}")
            except Exception as exc:
                logger.warning(f"工具懒卸载失败（不影响主流程）: {exc}")

            if release_ticket:
                self.guardrail.release_ticket(task.task_id)

    async def store_conversation(self, session_id: str, role: str, content: str) -> None:
        """存储对话到记忆系统

        Args:
            session_id: 会话 ID
            role: 角色 (user / assistant / system)
            content: 对话内容
        """
        try:
            await self.initialize_memory()
            await self.memory_bridge.store_conversation(session_id, role, content)
        except Exception as exc:
            logger.debug(f"对话存储到记忆失败（不影响主流程）: {exc}")

    def get_memory_stats(self) -> dict:
        """获取记忆系统统计"""
        try:
            return self.memory_bridge.get_stats()
        except Exception as exc:
            logger.warning(f"获取记忆统计失败: {exc}")
            return {"error": str(exc)}

    def get_tool_stats(self) -> dict:
        """获取工具懒加载统计信息"""
        try:
            stats = self.tool_loader.get_stats()
            return stats.model_dump()
        except Exception as exc:
            logger.warning(f"获取工具统计失败: {exc}")
            return {"error": str(exc)}

    async def get_current_instruction(self) -> str:
        """获取当前任务指令

        从全局状态的 checklist 中生成面向 Agent 的任务指令，
        实现状态驱动的零对话通信。

        Returns:
            当前任务指令文本
        """
        state = await self.state_manager.read()
        return self.instruction_generator.generate_instruction(state)

    async def clear_agent_session(self, agent_name: str) -> None:
        """清空 Agent 会话历史（每轮任务完成后）

        通过事件总线发出信号，由 Agent 自行清理上下文，
        避免跨任务的上下文污染。

        Args:
            agent_name: 要清空会话的 Agent 名称
        """
        await self.event_bus.emit(Event(
            type=EventType.AGENT_COMPLETED,
            data={"agent": agent_name, "clear_session": True},
            source="orchestrator",
        ))
        logger.debug(f"已发送会话清理信号: agent={agent_name}")
