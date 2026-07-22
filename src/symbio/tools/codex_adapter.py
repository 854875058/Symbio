"""Codex 适配器 - 封装 OpenAI Codex / 代码生成 API 调用。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from pydantic import BaseModel, Field

from symbio.tools.registry import BaseTool, ToolPermission, ToolSchema, ToolResult
from symbio.utils.logger import get_logger

logger = get_logger("tools.codex_adapter")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


class CodexRequest(BaseModel):
    """Codex 请求"""

    prompt: str
    model: str = ""
    language: str = ""  # 编程语言提示
    max_tokens: int = 4096
    temperature: float = 0.0
    stop_sequences: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)  # 上下文代码片段


class CodeSuggestion(BaseModel):
    """代码建议"""

    code: str
    language: str = ""
    description: str = ""
    confidence: float = 0.0
    file_path: str = ""
    line_start: int = 0
    line_end: int = 0


class CodexResponse(BaseModel):
    """Codex 响应"""

    suggestions: list[CodeSuggestion] = Field(default_factory=list)
    raw_output: str = ""
    model: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str = ""
    duration_ms: int = 0
    error: str = ""

    @property
    def success(self) -> bool:
        return not self.error

    @property
    def has_suggestions(self) -> bool:
        return len(self.suggestions) > 0

    def get_best_suggestion(self) -> CodeSuggestion | None:
        """获取最佳建议"""
        if not self.suggestions:
            return None
        return max(self.suggestions, key=lambda s: s.confidence)


class CodexAdapterConfig(BaseModel):
    """Codex 适配器配置"""

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4"  # 默认模型
    timeout: int = 120
    max_retries: int = 3
    proxy: str = ""


# ---------------------------------------------------------------------------
# Codex 适配器
# ---------------------------------------------------------------------------


class CodexAdapter:
    """Codex 适配器

    封装 OpenAI Codex / 代码生成 API，提供：
    1. 代码生成 - 根据描述生成代码
    2. 代码补全 - 补全不完整的代码
    3. 代码解释 - 解释代码逻辑
    4. 代码重构 - 重构建议

    用法:
        adapter = CodexAdapter()

        # 生成代码
        response = await adapter.generate(
            prompt="写一个 Python 函数实现快速排序",
            language="python",
        )

        # 补全代码
        response = await adapter.complete(
            prefix="def fibonacci(n):\n    if n <= 1:",
            suffix="return fibonacci(n-1) + fibonacci(n-2)",
            language="python",
        )

        # 解释代码
        explanation = await adapter.explain(
            code="def memoize(f):\n    cache = {}\n    def wrapper(*args):\n        if args not in cache:\n            cache[args] = f(*args)\n        return cache[args]\n    return wrapper"
        )
    """

    def __init__(self, config: CodexAdapterConfig | None = None):
        from symbio.config.settings import get_settings

        settings = get_settings()

        self._config = config or CodexAdapterConfig(
            api_key=settings.model.openai_api_key,
            base_url=settings.model.openai_base_url,
        )

        # 从全局配置补全
        if not self._config.api_key:
            self._config.api_key = settings.model.openai_api_key
        if not self._config.base_url or self._config.base_url == "https://api.openai.com/v1":
            self._config.base_url = settings.model.openai_base_url

        logger.info(
            f"CodexAdapter 创建: base_url={self._config.base_url}, model={self._config.model}"
        )

    # ------------------------------------------------------------------
    # 核心功能
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        *,
        language: str = "",
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        context: list[str] | None = None,
    ) -> CodexResponse:
        """生成代码

        Args:
            prompt: 代码描述或指令
            language: 目标编程语言
            model: 模型名称
            max_tokens: 最大 token 数
            temperature: 温度参数
            context: 上下文代码片段

        Returns:
            Codex 响应
        """
        # 构建系统提示
        system_prompt = "你是一个专业的编程助手。"
        if language:
            system_prompt += f"请用 {language} 编写代码。"
        system_prompt += "\n请直接输出代码，使用 ```language ... ``` 格式包裹。"

        # 构建用户消息
        user_message = prompt
        if context:
            user_message = "上下文代码:\n```\n" + "\n".join(context) + "\n```\n\n" + prompt

        return await self._chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
            model=model or self._config.model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def complete(
        self,
        prefix: str,
        suffix: str = "",
        *,
        language: str = "",
        model: str = "",
        max_tokens: int = 2048,
    ) -> CodexResponse:
        """补全代码

        Args:
            prefix: 代码前缀
            suffix: 代码后缀（可选）
            language: 编程语言
            model: 模型名称
            max_tokens: 最大 token 数

        Returns:
            Codex 响应
        """
        system_prompt = "你是一个代码补全助手。请根据提供的代码上下文进行补全。"
        if language:
            system_prompt += f" 编程语言: {language}。"
        system_prompt += "\n只输出需要补全的代码部分，不要重复已有代码。"

        user_message = f"前缀代码:\n```\n{prefix}\n```"
        if suffix:
            user_message += f"\n\n后缀代码:\n```\n{suffix}\n```"
        user_message += "\n\n请补全中间的代码。"

        return await self._chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
            model=model or self._config.model,
            max_tokens=max_tokens,
            temperature=0.0,
        )

    async def explain(
        self,
        code: str,
        *,
        language: str = "",
        detail_level: str = "normal",  # brief / normal / detailed
    ) -> CodexResponse:
        """解释代码

        Args:
            code: 要解释的代码
            language: 编程语言
            detail_level: 详细程度

        Returns:
            Codex 响应
        """
        detail_prompts = {
            "brief": "简要说明这段代码的功能，不超过 3 句话。",
            "normal": "详细解释这段代码的功能、参数和返回值。",
            "detailed": "详细解释这段代码，包括：1) 功能说明 2) 算法分析 3) 时间/空间复杂度 4) 潜在问题和改进建议。",
        }

        system_prompt = "你是一个代码分析助手。"
        if language:
            system_prompt += f" 编程语言: {language}。"

        user_message = (
            f"{detail_prompts.get(detail_level, detail_prompts['normal'])}\n\n```\n{code}\n```"
        )

        return await self._chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
        )

    async def refactor(
        self,
        code: str,
        *,
        language: str = "",
        goals: list[str] | None = None,
    ) -> CodexResponse:
        """重构建议

        Args:
            code: 要重构的代码
            language: 编程语言
            goals: 重构目标（如 "performance", "readability", "testability"）

        Returns:
            Codex 响应
        """
        system_prompt = "你是一个代码重构助手。请提供重构后的代码和解释。"
        if language:
            system_prompt += f" 编程语言: {language}。"
        system_prompt += (
            "\n输出格式：先输出重构后的代码（使用 ```language ... ``` 格式），然后解释改动。"
        )

        goal_str = ""
        if goals:
            goal_str = f"\n重构目标: {', '.join(goals)}"

        user_message = f"请重构以下代码{goal_str}:\n```\n{code}\n```"

        return await self._chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _chat_completion(
        self,
        system_prompt: str,
        user_message: str,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> CodexResponse:
        """调用 Chat Completion API"""
        import time

        start_time = time.monotonic()

        if not self._config.api_key:
            return CodexResponse(error="未配置 API Key")

        model = model or self._config.model
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

        proxy = self._config.proxy or None

        for attempt in range(self._config.max_retries):
            try:
                import httpx

                async with httpx.AsyncClient(
                    timeout=self._config.timeout,
                    proxy=proxy,
                ) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()

                duration_ms = int((time.monotonic() - start_time) * 1000)

                # 解析响应
                content = data["choices"][0]["message"]["content"]
                finish_reason = data["choices"][0].get("finish_reason", "")
                usage = data.get("usage", {})

                # 提取代码建议
                suggestions = self._extract_suggestions(content)

                return CodexResponse(
                    suggestions=suggestions,
                    raw_output=content,
                    model=model,
                    usage=usage,
                    finish_reason=finish_reason,
                    duration_ms=duration_ms,
                )

            except Exception as e:
                logger.warning(f"Codex API 调用失败 (attempt {attempt + 1}): {e}")
                if attempt == self._config.max_retries - 1:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    return CodexResponse(
                        error=f"API 调用失败: {str(e)}",
                        duration_ms=duration_ms,
                    )
                await asyncio.sleep(2**attempt)

        return CodexResponse(error="未知错误")

    def _extract_suggestions(self, content: str) -> list[CodeSuggestion]:
        """从响应中提取代码建议"""
        suggestions: list[CodeSuggestion] = []

        # 提取代码块
        code_pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
        for match in code_pattern.finditer(content):
            language = match.group(1) or "text"
            code = match.group(2).strip()

            if code:
                suggestions.append(
                    CodeSuggestion(
                        code=code,
                        language=language,
                        confidence=0.9,  # 默认置信度
                    )
                )

        # 如果没有代码块，将整个内容作为文本建议
        if not suggestions and content.strip():
            suggestions.append(
                CodeSuggestion(
                    code=content.strip(),
                    language="text",
                    confidence=0.5,
                )
            )

        return suggestions


# ---------------------------------------------------------------------------
# Tool 注册
# ---------------------------------------------------------------------------


class CodexTool(BaseTool):
    """Codex 工具（注册到 ToolRegistry）"""

    name = "codex"
    description = "调用 OpenAI Codex API 进行代码生成、补全和解释"
    version = "1.0.0"
    tags = ["ai", "code", "openai"]
    permission = ToolPermission(level="execute", requires_approval=False)

    def __init__(self, adapter: CodexAdapter | None = None):
        self._adapter = adapter or CodexAdapter()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["generate", "complete", "explain", "refactor"],
                        "description": "操作类型",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "代码描述或指令（generate/explain/refactor 时使用）",
                    },
                    "code": {
                        "type": "string",
                        "description": "代码内容（complete/explain/refactor 时使用）",
                    },
                    "prefix": {
                        "type": "string",
                        "description": "代码前缀（complete 时使用）",
                    },
                    "suffix": {
                        "type": "string",
                        "description": "代码后缀（complete 时使用）",
                    },
                    "language": {
                        "type": "string",
                        "description": "编程语言",
                    },
                    "model": {
                        "type": "string",
                        "description": "模型名称",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "generate")

        try:
            if action == "generate":
                response = await self._adapter.generate(
                    prompt=kwargs.get("prompt", ""),
                    language=kwargs.get("language", ""),
                    model=kwargs.get("model", ""),
                )

            elif action == "complete":
                response = await self._adapter.complete(
                    prefix=kwargs.get("prefix", kwargs.get("code", "")),
                    suffix=kwargs.get("suffix", ""),
                    language=kwargs.get("language", ""),
                    model=kwargs.get("model", ""),
                )

            elif action == "explain":
                response = await self._adapter.explain(
                    code=kwargs.get("code", ""),
                    language=kwargs.get("language", ""),
                )

            elif action == "refactor":
                response = await self._adapter.refactor(
                    code=kwargs.get("code", ""),
                    language=kwargs.get("language", ""),
                )

            else:
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=False,
                    error=f"不支持的操作: {action}",
                )

            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=response.success,
                output=response.raw_output,
                error=response.error if not response.success else None,
                duration_ms=response.duration_ms,
            )

        except Exception as e:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                error=str(e),
            )
