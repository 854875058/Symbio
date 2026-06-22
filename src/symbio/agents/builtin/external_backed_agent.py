"""External-backed Agent —— 单个 Agent 以 Claude Code / Codex CLI 为执行后端。

普通 Agent 直接调 LLM；ExternalBackedAgent 则把任务委托给本地的外部编码 Agent
（Codex / Claude Code）CLI 会话执行，复用 ExternalAgentController 的发现/会话/审计能力。
这样一个 Symbio Agent 就能"接入" Claude Code 或 Codex，让它们在工作区里真正动手干活，
而 Symbio 仍掌握编排、审批、沙箱边界和审计。

设计要点：
- provider 可配（claude-code / codex），两个便捷子类 ClaudeCodeAgent / CodexAgent 已注册
- controller 可注入，便于测试；CLI 未安装时返回 success=False 的明确结果，不抛异常
- 外部 CLI 不通过此路径回报 token，故 token_usage 记为 0（诚实）
"""

from __future__ import annotations

from typing import Any, Optional

from symbio.agents.base import AgentCapability, BaseAgent
from symbio.agents.registry import register_agent
from symbio.utils.logger import get_logger
from symbio.utils.types import Result, Task, TaskComplexity, TokenUsage

logger = get_logger("external_backed_agent")


class ExternalBackedAgent(BaseAgent):
    """以外部编码 Agent CLI 为后端的 Agent。"""

    name = "external"
    description = "由外部编码 Agent（Claude Code / Codex）驱动的 Agent"
    version = "1.0.0"
    provider = "claude-code"  # 子类可覆盖
    capabilities = [
        AgentCapability(
            name="coding",
            description="在工作区内由外部编码 Agent 执行代码/工程任务",
            complexity_range=[TaskComplexity.MEDIUM, TaskComplexity.HIGH],
        ),
    ]

    def __init__(
        self,
        *,
        provider: Optional[str] = None,
        workspace: str = ".",
        controller: Any = None,
        model: str = "",
        timeout: int = 300,
        session_id: Optional[str] = None,
    ) -> None:
        super().__init__()
        if provider:
            self.provider = provider
        self._workspace = workspace
        self._controller = controller
        self._model = model
        self._timeout = timeout
        self._session_id = session_id

    def _get_controller(self) -> Any:
        if self._controller is None:
            from symbio.tools.external_agents import ExternalAgentController
            self._controller = ExternalAgentController()
        return self._controller

    def _ensure_session(self) -> str:
        from symbio.tools.external_agents import ExternalAgentSessionCreate
        if self._session_id:
            return self._session_id
        controller = self._get_controller()
        session = controller.create_session(ExternalAgentSessionCreate(
            provider=self.provider,
            workspace=self._workspace,
            model=self._model,
            label=f"{self.provider} agent",
        ))
        self._session_id = session.session_id
        return self._session_id

    @staticmethod
    def _build_prompt(task: Task) -> str:
        prompt = task.intent.raw_text
        if task.metadata.get("workflow_guidance"):
            prompt = f"{task.metadata['workflow_guidance']}\n\nUser task:\n{prompt}"
        if task.metadata.get("memory_context"):
            prompt = f"相关背景知识:\n{task.metadata['memory_context']}\n\n用户问题:\n{prompt}"
        return prompt

    async def execute(self, task: Task) -> Result:
        self.start(task)
        try:
            from symbio.tools.external_agents import ExternalAgentRunRequest

            controller = self._get_controller()
            session_id = self._ensure_session()
            run = await controller.run_session(session_id, ExternalAgentRunRequest(
                prompt=self._build_prompt(task),
                approved=True,
                dry_run=bool(task.metadata.get("dry_run", False)),
                model=self._model,
                timeout=self._timeout,
            ))

            content = run.stdout or run.error or ""
            result = Result(
                task_id=task.task_id,
                success=run.success,
                content=content,
                token_usage=TokenUsage(),
                data={
                    "provider": self.provider,
                    "backend": "external-agent-cli",
                    "session_id": session_id,
                    "run_id": run.run_id,
                    "exit_code": run.exit_code,
                    "command": run.command,
                    "dry_run": run.dry_run,
                },
            )
            if run.success:
                self.complete(result)
            else:
                self.fail(run.error or "external agent run failed")
            return result
        except Exception as e:
            logger.error(f"ExternalBackedAgent 执行失败: {e}")
            self.fail(str(e))
            return Result(
                task_id=task.task_id,
                success=False,
                content=f"执行失败: {str(e)}",
            )


@register_agent("claude-code")
class ClaudeCodeAgent(ExternalBackedAgent):
    """由 Claude Code CLI 驱动的 Agent。"""
    description = "由 Claude Code CLI 驱动的 Agent"
    provider = "claude-code"


@register_agent("codex")
class CodexAgent(ExternalBackedAgent):
    """由 Codex CLI 驱动的 Agent。"""
    description = "由 Codex CLI 驱动的 Agent"
    provider = "codex"
