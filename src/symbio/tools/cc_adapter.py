"""Claude Code 适配器 - 封装 Claude Code CLI 调用，解析输出，提取代码块。"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.tools.registry import BaseTool, ToolPermission, ToolSchema, ToolResult
from symbio.utils.logger import get_logger

logger = get_logger("tools.cc_adapter")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class CCOutputType(str, Enum):
    """Claude Code 输出类型"""
    TEXT = "text"
    CODE = "code"
    TOOL_USE = "tool_use"
    ERROR = "error"


class CodeBlock(BaseModel):
    """代码块"""
    language: str = ""
    code: str
    file_path: str = ""              # 目标文件路径（如有）
    line_start: int = 0
    line_end: int = 0


class CCResponse(BaseModel):
    """Claude Code 响应解析结果"""
    raw_output: str = ""
    text_content: str = ""           # 文本内容
    code_blocks: list[CodeBlock] = Field(default_factory=list)
    tool_uses: list[dict[str, Any]] = Field(default_factory=list)
    exit_code: int = 0
    duration_ms: int = 0
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.error

    @property
    def has_code(self) -> bool:
        return len(self.code_blocks) > 0

    def get_code_by_language(self, language: str) -> list[CodeBlock]:
        """按语言筛选代码块"""
        return [b for b in self.code_blocks if b.language == language]


class CCAdapterConfig(BaseModel):
    """Claude Code 适配器配置"""
    cli_path: str = "claude"         # CLI 路径
    default_model: str = ""          # 默认模型（空则用 CLI 默认）
    timeout: int = 300               # 超时秒数
    max_output_size: int = 1024 * 1024  # 最大输出大小（字节）
    allowed_tools: list[str] = Field(default_factory=list)  # 允许的工具
    blocked_tools: list[str] = Field(default_factory=list)  # 禁用的工具
    proxy: str = ""                  # 代理地址


# ---------------------------------------------------------------------------
# Claude Code 适配器
# ---------------------------------------------------------------------------

class ClaudeCodeAdapter:
    """Claude Code 适配器

    封装 Claude Code CLI 的调用，提供：
    1. 结构化调用 - 参数化调用 CLI
    2. 输出解析 - 解析响应，提取代码块和工具调用
    3. 会话管理 - 支持多轮对话上下文

    用法:
        adapter = ClaudeCodeAdapter()

        # 简单调用
        response = await adapter.run("帮我写一个 Python 函数计算斐波那契数列")

        # 提取代码
        if response.has_code:
            for block in response.code_blocks:
                print(f"Language: {block.language}")
                print(block.code)

        # 带上下文的多轮对话
        response1 = await adapter.run("创建一个 FastAPI 项目", session_id="s1")
        response2 = await adapter.run("添加用户认证功能", session_id="s1")
    """

    def __init__(self, config: CCAdapterConfig | None = None):
        self._config = config or CCAdapterConfig()
        self._sessions: dict[str, list[dict[str, str]]] = {}  # session_id -> history
        logger.info(
            f"ClaudeCodeAdapter 创建: cli={self._config.cli_path}, "
            f"timeout={self._config.timeout}"
        )

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        *,
        workdir: str | Path = ".",
        session_id: str = "",
        model: str = "",
        system_prompt: str = "",
        allowed_tools: list[str] | None = None,
        blocked_tools: list[str] | None = None,
        output_format: str = "text",    # text / json / stream-json
        max_turns: int = 10,
        verbose: bool = False,
    ) -> CCResponse:
        """运行 Claude Code

        Args:
            prompt: 发送给 Claude Code 的指令
            workdir: 工作目录
            session_id: 会话 ID（用于多轮对话）
            model: 模型名称
            system_prompt: 系统提示
            allowed_tools: 允许的工具列表
            blocked_tools: 禁用的工具列表
            output_format: 输出格式
            max_turns: 最大轮次
            verbose: 详细输出

        Returns:
            解析后的响应
        """
        import time
        start_time = time.monotonic()

        # 构建命令
        cmd = self._build_command(
            prompt=prompt,
            workdir=workdir,
            model=model or self._config.default_model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools or self._config.allowed_tools,
            blocked_tools=blocked_tools or self._config.blocked_tools,
            output_format=output_format,
            max_turns=max_turns,
        )

        logger.info(f"执行 Claude Code: workdir={workdir}, prompt={prompt[:100]}...")

        # 执行命令
        response = await self._execute(cmd, workdir=workdir)

        # 记录会话历史
        if session_id:
            if session_id not in self._sessions:
                self._sessions[session_id] = []
            self._sessions[session_id].append({
                "role": "user",
                "content": prompt,
            })
            self._sessions[session_id].append({
                "role": "assistant",
                "content": response.text_content,
            })

        duration_ms = int((time.monotonic() - start_time) * 1000)
        response.duration_ms = duration_ms

        logger.info(
            f"Claude Code 完成: success={response.success}, "
            f"code_blocks={len(response.code_blocks)}, duration={duration_ms}ms"
        )
        return response

    async def run_with_context(
        self,
        prompt: str,
        context_files: list[str | Path],
        **kwargs: Any,
    ) -> CCResponse:
        """带文件上下文运行

        Args:
            prompt: 指令
            context_files: 上下文文件路径列表
            **kwargs: 其他参数

        Returns:
            解析后的响应
        """
        # 读取文件内容并拼接到 prompt
        context_parts = []
        for file_path in context_files:
            path = Path(file_path)
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    context_parts.append(
                        f"--- {path.name} ---\n{content}\n--- end {path.name} ---"
                    )
                except Exception as e:
                    logger.warning(f"读取文件失败 {path}: {e}")

        full_prompt = "\n".join(context_parts) + "\n\n" + prompt
        return await self.run(full_prompt, **kwargs)

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def get_session_history(self, session_id: str) -> list[dict[str, str]]:
        """获取会话历史"""
        return self._sessions.get(session_id, [])

    def clear_session(self, session_id: str) -> bool:
        """清除会话"""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def list_sessions(self) -> list[str]:
        """列出所有会话"""
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_command(
        self,
        prompt: str,
        workdir: str | Path,
        model: str,
        system_prompt: str,
        allowed_tools: list[str],
        blocked_tools: list[str],
        output_format: str,
        max_turns: int,
    ) -> list[str]:
        """构建 CLI 命令"""
        cmd = [self._config.cli_path, "-p", prompt]

        if model:
            cmd.extend(["--model", model])

        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])

        if blocked_tools:
            cmd.extend(["--blockedTools", ",".join(blocked_tools)])

        if output_format != "text":
            cmd.extend(["--output-format", output_format])

        if max_turns != 10:
            cmd.extend(["--max-turns", str(max_turns)])

        return cmd

    async def _execute(
        self, cmd: list[str], workdir: str | Path
    ) -> CCResponse:
        """执行命令并解析输出"""
        env = None
        if self._config.proxy:
            import os
            env = os.environ.copy()
            env["HTTP_PROXY"] = self._config.proxy
            env["HTTPS_PROXY"] = self._config.proxy

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._config.timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return CCResponse(
                    exit_code=-1,
                    error=f"执行超时 ({self._config.timeout}s)",
                )

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            # 限制输出大小
            if len(stdout_str) > self._config.max_output_size:
                stdout_str = stdout_str[:self._config.max_output_size] + "\n... (truncated)"

            # 解析输出
            response = self._parse_output(stdout_str)
            response.exit_code = proc.returncode or 0
            response.raw_output = stdout_str

            if proc.returncode != 0 and not response.error:
                response.error = stderr_str

            return response

        except FileNotFoundError:
            return CCResponse(
                exit_code=-1,
                error=f"Claude Code CLI 未找到: {self._config.cli_path}",
            )
        except Exception as e:
            return CCResponse(
                exit_code=-1,
                error=f"执行异常: {str(e)}",
            )

    def _parse_output(self, output: str) -> CCResponse:
        """解析 Claude Code 输出"""
        response = CCResponse(raw_output=output)

        # 提取代码块（```language ... ```）
        code_pattern = re.compile(
            r"```(\w*)\n(.*?)```", re.DOTALL
        )
        for match in code_pattern.finditer(output):
            language = match.group(1) or "text"
            code = match.group(2).strip()
            response.code_blocks.append(CodeBlock(
                language=language,
                code=code,
            ))

        # 提取文本内容（排除代码块）
        text_content = code_pattern.sub("", output).strip()
        response.text_content = text_content

        # 提取文件路径信息
        file_pattern = re.compile(
            r"(?:写入|创建|修改|编辑|Write|Create|Edit)\s+(?:文件|file)?[:\s]*`?([^\s`]+\.\w+)`?",
            re.IGNORECASE,
        )
        for match in file_pattern.finditer(text_content):
            file_path = match.group(1)
            # 尝试关联到最近的代码块
            if response.code_blocks and not response.code_blocks[-1].file_path:
                response.code_blocks[-1].file_path = file_path

        return response


# ---------------------------------------------------------------------------
# Tool 注册
# ---------------------------------------------------------------------------

class ClaudeCodeTool(BaseTool):
    """Claude Code 工具（注册到 ToolRegistry）"""

    name = "claude_code"
    description = "调用 Claude Code CLI 执行编程任务"
    version = "1.0.0"
    tags = ["ai", "code", "claude"]
    permission = ToolPermission(level="execute", requires_approval=True)

    def __init__(self, adapter: ClaudeCodeAdapter | None = None):
        self._adapter = adapter or ClaudeCodeAdapter()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "发送给 Claude Code 的指令",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "工作目录路径",
                    },
                    "model": {
                        "type": "string",
                        "description": "模型名称",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "会话 ID（用于多轮对话）",
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        prompt = kwargs.get("prompt", "")
        workdir = kwargs.get("workdir", ".")
        model = kwargs.get("model", "")
        session_id = kwargs.get("session_id", "")

        response = await self._adapter.run(
            prompt=prompt,
            workdir=workdir,
            model=model,
            session_id=session_id,
        )

        return ToolResult(
            call_id="",
            tool_name=self.name,
            success=response.success,
            output=response.text_content,
            error=response.error if not response.success else None,
            duration_ms=response.duration_ms,
        )
