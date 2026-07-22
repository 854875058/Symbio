"""通用 Agent - 处理一般对话和问答，支持工具调用循环"""

from __future__ import annotations

from typing import Any

from symbio.agents.base import AgentCapability, BaseAgent
from symbio.agents.registry import register_agent
from symbio.config.settings import get_settings
from symbio.utils.logger import get_logger
from symbio.utils.types import Result, Task, TaskComplexity, TokenUsage

logger = get_logger("general_agent")

# Maximum tool-call iterations per LLM turn
MAX_TOOL_ITERATIONS = 10


@register_agent("general")
class GeneralAgent(BaseAgent):
    """通用 Agent - 处理一般对话和问答，支持工具调用循环"""

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
            # 调用 LLM（含工具循环）
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
        """调用 LLM（支持多 provider 和工具调用循环）

        Args:
            task: 任务对象

        Returns:
            包含 content 和 token_usage 的字典
        """
        # 从 task 获取路由选择的模型，优先级：task.model > metadata.selected_model > 默认
        model_id = (
            task.model or task.metadata.get("selected_model") or self.settings.model.model_medium
        )

        # 获取可用工具 schemas
        tool_schemas = self._get_tool_schemas(task)

        # 根据模型 ID 判断 provider
        if self._is_openai_model(model_id):
            return await self._call_openai(task, model_id, tool_schemas)
        return await self._call_anthropic(task, model_id, tool_schemas)

    def _get_tool_schemas(self, task: Task) -> list[dict[str, Any]]:
        """从 task metadata 获取工具 schema 定义"""
        tool_defs = task.metadata.get("tool_definitions", [])
        if tool_defs:
            return tool_defs

        # Fallback: 从 ToolRegistry 获取
        available_names = task.metadata.get("available_tools", [])
        if not available_names:
            return []

        try:
            from symbio.tools.registry import get_tool_registry

            registry = get_tool_registry()
            schemas = []
            for name in available_names:
                tool = registry.get(name)
                if tool and tool.enabled:
                    schemas.append(tool.schema().model_dump())
            return schemas
        except Exception as exc:
            logger.debug(f"获取工具 schema 失败: {exc}")
            return []

    @staticmethod
    def _is_openai_model(model_id: str) -> bool:
        """判断是否为 OpenAI 兼容模型"""
        openai_prefixes = ("gpt-", "o1-", "o3-", "o4-", "deepseek", "qwen", "glm")
        return any(model_id.startswith(p) for p in openai_prefixes)

    async def _call_anthropic(self, task: Task, model_id: str, tool_schemas: list[dict]) -> dict:
        """调用 Anthropic API（含工具调用循环）"""
        import anthropic

        from symbio.config.settings import get_settings

        settings = get_settings()

        client = anthropic.AsyncAnthropic(
            api_key=settings.model.anthropic_api_key,
            base_url=settings.model.anthropic_base_url,
        )

        user_content = self._build_user_content(task)
        messages = [{"role": "user", "content": user_content}]

        # Convert tool schemas to Anthropic format
        tools_param = None
        if tool_schemas:
            tools_param = [
                {
                    "name": s.get("name", ""),
                    "description": s.get("description", ""),
                    "input_schema": s.get("parameters", s.get("input_schema", {})),
                }
                for s in tool_schemas
            ]

        total_input = 0
        total_output = 0
        final_content = ""

        # Tool-call loop
        for iteration in range(MAX_TOOL_ITERATIONS):
            kwargs: dict[str, Any] = {
                "model": model_id,
                "max_tokens": 4096,
                "messages": messages,
            }
            if tools_param:
                kwargs["tools"] = tools_param

            response = await client.messages.create(**kwargs)

            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            # Check if model wants to call tools
            tool_use_blocks = [
                block
                for block in response.content
                if hasattr(block, "type") and block.type == "tool_use"
            ]

            if not tool_use_blocks:
                # No tool calls - extract text and return
                for block in response.content:
                    if hasattr(block, "text"):
                        final_content += block.text
                break

            # Extract text blocks (assistant message part)
            text_parts = []
            for block in response.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                elif hasattr(block, "type") and block.type == "text":
                    text_parts.append(block.text)

            # Add assistant message with tool_use to conversation
            assistant_content = []
            for block in response.content:
                if hasattr(block, "model_dump"):
                    assistant_content.append(block.model_dump())
                elif hasattr(block, "type"):
                    assistant_content.append(
                        {
                            "type": block.type,
                            "text": getattr(block, "text", ""),
                        }
                    )
            messages.append({"role": "assistant", "content": assistant_content})

            # Execute each tool and collect results
            tool_results = []
            for tool_block in tool_use_blocks:
                tool_name = tool_block.name
                tool_input = tool_block.input
                tool_result = await self._execute_tool(tool_name, tool_input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": tool_result,
                    }
                )

            # Add tool results as user message
            messages.append({"role": "user", "content": tool_results})

            if text_parts:
                final_content += "\n".join(text_parts)

        token_usage = TokenUsage(
            input_tokens=total_input,
            output_tokens=total_output,
            total_tokens=total_input + total_output,
            model=model_id,
        )
        return {"content": final_content, "token_usage": token_usage}

    async def _call_openai(self, task: Task, model_id: str, tool_schemas: list[dict]) -> dict:
        """调用 OpenAI 兼容 API（含工具调用循环）"""
        from openai import AsyncOpenAI
        from symbio.config.settings import get_settings

        settings = get_settings()

        client = AsyncOpenAI(
            api_key=settings.model.openai_api_key or settings.model.anthropic_api_key,
            base_url=settings.model.openai_base_url,
        )

        user_content = self._build_user_content(task)
        messages = [{"role": "user", "content": user_content}]

        # Convert to OpenAI tool format
        tools_param = None
        if tool_schemas:
            tools_param = [
                {
                    "type": "function",
                    "function": {
                        "name": s.get("name", ""),
                        "description": s.get("description", ""),
                        "parameters": s.get("parameters", {}),
                    },
                }
                for s in tool_schemas
            ]

        total_input = 0
        total_output = 0
        final_content = ""

        for iteration in range(MAX_TOOL_ITERATIONS):
            kwargs: dict[str, Any] = {
                "model": model_id,
                "max_tokens": 4096,
                "messages": messages,
            }
            if tools_param:
                kwargs["tools"] = tools_param

            response = await client.chat.completions.create(**kwargs)
            choice = response.choices[0]

            if response.usage:
                total_input += response.usage.prompt_tokens
                total_output += response.usage.completion_tokens

            # Check for tool calls
            tool_calls = choice.message.tool_calls
            if not tool_calls:
                final_content = choice.message.content or ""
                break

            # Add assistant message
            messages.append(choice.message.model_dump())

            # Execute tools
            for tc in tool_calls:
                import json

                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                result_text = await self._execute_tool(tc.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )

            if choice.message.content:
                final_content += choice.message.content

        token_usage = TokenUsage(
            input_tokens=total_input,
            output_tokens=total_output,
            total_tokens=total_input + total_output,
            model=model_id,
        )
        return {"content": final_content, "token_usage": token_usage}

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """通过 ToolRegistry 执行工具并返回结果文本"""
        try:
            from symbio.tools.registry import get_tool_registry

            registry = get_tool_registry()
            tool = registry.get(tool_name)
            if tool is None:
                return f"Error: tool '{tool_name}' not found"

            result = await tool.execute(**tool_input)
            if hasattr(result, "content"):
                return str(result.content)
            if hasattr(result, "output"):
                return str(result.output)
            return str(result)
        except Exception as exc:
            logger.error(f"工具执行失败: {tool_name}: {exc}")
            return f"Error executing {tool_name}: {exc}"

    @staticmethod
    def _build_user_content(task: Task) -> str:
        """构建用户消息内容"""
        user_content = task.intent.raw_text

        # Inject predecessor node results (DAG data dependencies)
        predecessor_results = task.metadata.get("predecessor_results", {})
        if predecessor_results:
            dep_context = "\n\n--- 前序任务结果 ---\n"
            for dep_id, result_text in predecessor_results.items():
                dep_context += f"\n[{dep_id}]:\n{result_text}\n"
            user_content = dep_context + "\n--- 当前任务 ---\n" + user_content

        if task.metadata.get("workflow_guidance"):
            user_content = f"{task.metadata['workflow_guidance']}\n\nUser task:\n{user_content}"
        if task.metadata.get("memory_context"):
            user_content = (
                f"相关背景知识:\n{task.metadata['memory_context']}\n\n用户问题:\n{user_content}"
            )
        return user_content
