"""通用 Agent - 处理一般对话和问答"""

from __future__ import annotations

from symbio.agents.base import AgentCapability, BaseAgent
from symbio.agents.registry import register_agent
from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger
from symbio.utils.types import Result, Task, TaskComplexity, TokenUsage

logger = get_logger("general_agent")


@register_agent("general")
class GeneralAgent(BaseAgent):
    """通用 Agent - 处理一般对话和问答"""

    name = "general"
    description = "通用对话与问答 Agent"
    version = "1.0.0"
    capabilities = [
        AgentCapability(
            name="chat",
            description="一般对话",
            complexity_range=[TaskComplexity.LOW, TaskComplexity.MEDIUM],
        ),
        AgentCapability(
            name="qa",
            description="问答",
            complexity_range=[TaskComplexity.LOW, TaskComplexity.MEDIUM],
        ),
    ]

    def __init__(self):
        super().__init__()
        self.settings = get_settings()

    async def execute(self, task: Task) -> Result:
        """执行任务

        Args:
            task: 任务对象

        Returns:
            执行结果
        """
        self.start(task)

        try:
            # 调用 LLM
            response = await self._call_llm(task)

            result = Result(
                task_id=task.task_id,
                success=True,
                content=response["content"],
                token_usage=response["token_usage"],
            )

            self.record_step(response["token_usage"])
            self.complete(result)
            return result

        except Exception as e:
            logger.error(f"GeneralAgent 执行失败: {e}")
            self.fail(str(e))
            return Result(
                task_id=task.task_id,
                success=False,
                content=f"执行失败: {str(e)}",
            )

    async def _call_llm(self, task: Task) -> dict:
        """调用 LLM

        Args:
            task: 任务对象

        Returns:
            包含 content 和 token_usage 的字典
        """
        import anthropic

        client = anthropic.AsyncAnthropic(
            api_key=self.settings.model.anthropic_api_key,
            base_url=self.settings.model.anthropic_base_url,
        )

        # 构建消息
        user_content = task.intent.raw_text
        if task.metadata.get("workflow_guidance"):
            user_content = f"{task.metadata['workflow_guidance']}\n\nUser task:\n{user_content}"
        if task.metadata.get("memory_context"):
            user_content = f"相关背景知识:\n{task.metadata['memory_context']}\n\n用户问题:\n{user_content}"
        messages = [
            {"role": "user", "content": user_content}
        ]

        # 调用 API
        response = await client.messages.create(
            model=self.settings.model.model_medium,
            max_tokens=4096,
            messages=messages,
        )

        # 提取内容
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        # 构建 Token 使用信息
        token_usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            total_tokens=response.usage.input_tokens + response.usage.output_tokens,
            model=response.model,
        )

        return {
            "content": content,
            "token_usage": token_usage,
        }
