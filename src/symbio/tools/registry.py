"""工具注册中心 - 管理所有工具的注册、发现、执行、权限与统计。

内置工具: cc, shell, file, git, browser
支持 LLM function calling schema 导出。
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from symbio.utils.logger import get_logger
from symbio.utils.types import ToolCall, ToolResult

logger = get_logger("tool.registry")


# ---------------------------------------------------------------------------
# 权限模型
# ---------------------------------------------------------------------------


class PermissionLevel(str, Enum):
    """工具权限等级，与 architecture.md 保持一致。"""

    READ_ONLY = "read_only"  # 安全：只读操作
    WRITE = "write"  # 敏感：写操作
    EXECUTE = "execute"  # 高危：执行操作，强制绑定 HITL
    ADMIN = "admin"  # 管理：系统配置操作


# 低等级权限隐含包含高等级权限的子集关系
# READ_ONLY < WRITE < EXECUTE < ADMIN
_PERMISSION_HIERARCHY: dict[PermissionLevel, int] = {
    PermissionLevel.READ_ONLY: 0,
    PermissionLevel.WRITE: 1,
    PermissionLevel.EXECUTE: 2,
    PermissionLevel.ADMIN: 3,
}


class ToolPermission(BaseModel):
    """单个工具的权限声明。"""

    level: PermissionLevel = PermissionLevel.READ_ONLY
    requires_approval: bool = False
    allowed_roles: list[str] = Field(
        default_factory=list,
        description="允许调用此工具的角色列表，空列表表示不限制。",
    )


# ---------------------------------------------------------------------------
# Schema 模型（用于 LLM function calling）
# ---------------------------------------------------------------------------


class ToolParameter(BaseModel):
    """工具单个参数的 Schema 描述。"""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list[Any] = Field(default_factory=list)
    default: Any = None


class ToolSchema(BaseModel):
    """符合 OpenAI function calling 规范的工具 Schema。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema 格式的参数定义。",
    )
    strict: bool = False


# ---------------------------------------------------------------------------
# 元数据与统计
# ---------------------------------------------------------------------------


class ToolMetadata(BaseModel):
    """工具元数据。"""

    name: str
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    tags: list[str] = Field(default_factory=list)
    permission: ToolPermission = Field(default_factory=ToolPermission)
    enabled: bool = True


class ToolStats(BaseModel):
    """工具调用统计。"""

    total_calls: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_duration_ms: float = 0.0
    last_called_at: Optional[datetime] = None
    last_error: Optional[str] = None

    @property
    def success_rate(self) -> float:
        """成功率（0.0 ~ 1.0）。"""
        if self.total_calls == 0:
            return 0.0
        return self.success_count / self.total_calls

    @property
    def avg_duration_ms(self) -> float:
        """平均执行耗时（毫秒）。"""
        if self.total_calls == 0:
            return 0.0
        return self.total_duration_ms / self.total_calls


# ---------------------------------------------------------------------------
# 工具基类
# ---------------------------------------------------------------------------


class BaseTool(ABC):
    """工具抽象基类。

    所有工具必须继承此类并实现 ``execute`` 方法以及 ``schema`` 属性。
    """

    # 子类应覆盖以下类属性
    name: str = "base_tool"
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    tags: list[str] = []
    permission: ToolPermission = ToolPermission()
    enabled: bool = True

    # ------------------------------------------------------------------
    # 抽象接口
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具逻辑，返回 ``ToolResult``。"""
        ...

    @abstractmethod
    def schema(self) -> ToolSchema:
        """返回该工具的 function-calling Schema。"""
        ...

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def get_metadata(self) -> ToolMetadata:
        """构造工具元数据。"""
        return ToolMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            author=self.author,
            tags=list(self.tags),
            permission=self.permission,
            enabled=self.enabled,
        )

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} version={self.version}>"


# ---------------------------------------------------------------------------
# 工具注册中心
# ---------------------------------------------------------------------------


class ToolRegistry:
    """工具注册中心。

    职责:
    - 工具注册 / 注销 / 发现
    - 权限校验后执行工具
    - 导出所有工具 Schema（供 LLM function calling）
    - 自动记录调用统计
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._stats: dict[str, ToolStats] = {}
        self._user_roles: dict[str, set[str]] = {}  # user_id -> roles

    # ------------------------------------------------------------------
    # 用户角色管理（供权限校验）
    # ------------------------------------------------------------------

    def set_user_roles(self, user_id: str, roles: set[str]) -> None:
        """设置用户角色。"""
        self._user_roles[user_id] = set(roles)
        logger.debug(f"用户 {user_id} 角色设置为: {roles}")

    def get_user_roles(self, user_id: str) -> set[str]:
        """获取用户角色。"""
        return self._user_roles.get(user_id, set())

    # ------------------------------------------------------------------
    # 注册 / 注销
    # ------------------------------------------------------------------

    def register(self, tool: BaseTool) -> None:
        """注册一个工具实例。

        Args:
            tool: 工具实例。

        Raises:
            ValueError: 工具名已存在。
        """
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已注册，请先注销或使用不同名称。")

        self._tools[tool.name] = tool
        self._stats[tool.name] = ToolStats()
        logger.info(f"注册工具: {tool.name} (v{tool.version})")

    def unregister(self, name: str) -> bool:
        """注销工具。

        Args:
            name: 工具名称。

        Returns:
            是否成功注销。
        """
        if name not in self._tools:
            logger.warning(f"注销失败，工具 '{name}' 不存在。")
            return False

        del self._tools[name]
        self._stats.pop(name, None)
        logger.info(f"注销工具: {name}")
        return True

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[BaseTool]:
        """按名称获取工具实例。"""
        return self._tools.get(name)

    def list_tools(self, enabled_only: bool = True) -> list[str]:
        """列出已注册的工具名称。

        Args:
            enabled_only: 是否仅返回启用的工具。
        """
        if enabled_only:
            return [t.name for t in self._tools.values() if t.enabled]
        return list(self._tools.keys())

    def list_metadata(self, enabled_only: bool = True) -> list[ToolMetadata]:
        """列出所有工具的元数据。"""
        tools = self._tools.values()
        if enabled_only:
            return [t.get_metadata() for t in tools if t.enabled]
        return [t.get_metadata() for t in tools]

    def get_by_tag(self, tag: str) -> list[BaseTool]:
        """按标签查找工具。"""
        return [t for t in self._tools.values() if tag in t.tags]

    # ------------------------------------------------------------------
    # 权限校验
    # ------------------------------------------------------------------

    def check_permission(
        self,
        tool: BaseTool,
        user_id: str = "",
    ) -> tuple[bool, str]:
        """校验用户是否有权调用指定工具。

        Args:
            tool: 工具实例。
            user_id: 调用者标识。

        Returns:
            (是否允许, 原因说明)。
        """
        perm = tool.permission

        # 角色校验
        if perm.allowed_roles and user_id:
            user_roles = self.get_user_roles(user_id)
            if not user_roles.intersection(perm.allowed_roles):
                return False, (
                    f"用户 {user_id} 的角色 {user_roles} "
                    f"不在工具 {tool.name} 的允许列表 {perm.allowed_roles} 中。"
                )

        # 高危操作需审批（此处标记，由上层 HITL 流程处理）
        if perm.requires_approval:
            logger.warning(
                f"工具 {tool.name} 需要人工审批，当前调用者: {user_id or 'anonymous'}"
            )

        return True, "权限校验通过。"

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def execute(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        user_id: str = "",
        timeout: float | None = None,
    ) -> ToolResult:
        """执行指定工具。

        流程: 查找工具 -> 权限校验 -> 执行 -> 记录统计。

        Args:
            name: 工具名称。
            params: 传给工具的参数。
            user_id: 调用者标识，用于权限校验。
            timeout: 超时秒数，None 表示不限。

        Returns:
            工具执行结果。

        Raises:
            KeyError: 工具不存在。
            PermissionError: 权限不足。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"工具 '{name}' 未注册。")

        # 权限校验
        allowed, reason = self.check_permission(tool, user_id)
        if not allowed:
            logger.error(f"权限拒绝: {reason}")
            raise PermissionError(reason)

        params = params or {}
        stats = self._stats[name]
        start_ms = time.monotonic() * 1000

        logger.info(f"执行工具: {name}, 参数: {list(params.keys())}")

        try:
            if timeout is not None:
                result = await asyncio.wait_for(tool.execute(**params), timeout=timeout)
            else:
                result = await tool.execute(**params)

            # 记录成功统计
            duration = time.monotonic() * 1000 - start_ms
            stats.total_calls += 1
            stats.success_count += 1
            stats.total_duration_ms += duration
            stats.last_called_at = datetime.now()

            logger.info(
                f"工具 {name} 执行成功, 耗时={duration:.1f}ms"
            )
            return result

        except asyncio.TimeoutError:
            duration = time.monotonic() * 1000 - start_ms
            stats.total_calls += 1
            stats.failure_count += 1
            stats.total_duration_ms += duration
            stats.last_called_at = datetime.now()
            stats.last_error = f"执行超时 ({timeout}s)"

            logger.error(f"工具 {name} 执行超时 ({timeout}s)")
            return ToolResult(
                call_id="",
                tool_name=name,
                success=False,
                error=f"工具执行超时 ({timeout}s)",
                duration_ms=int(duration),
            )

        except Exception as exc:
            duration = time.monotonic() * 1000 - start_ms
            stats.total_calls += 1
            stats.failure_count += 1
            stats.total_duration_ms += duration
            stats.last_called_at = datetime.now()
            stats.last_error = str(exc)

            logger.error(f"工具 {name} 执行异常: {exc}")
            return ToolResult(
                call_id="",
                tool_name=name,
                success=False,
                error=str(exc),
                duration_ms=int(duration),
            )

    async def execute_call(self, call: ToolCall, user_id: str = "") -> ToolResult:
        """通过 ``ToolCall`` 对象执行工具（兼容 types.py 中的定义）。

        Args:
            call: 工具调用请求。
            user_id: 调用者标识。

        Returns:
            工具执行结果。
        """
        result = await self.execute(
            name=call.tool_name,
            params=call.parameters,
            user_id=user_id,
        )
        result.call_id = call.call_id
        return result

    # ------------------------------------------------------------------
    # Schema 导出（LLM function calling）
    # ------------------------------------------------------------------

    def export_schemas(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        """导出所有工具的 function-calling Schema。

        导出格式兼容 OpenAI / Anthropic function calling 规范。

        Args:
            tags: 若指定，仅导出包含这些标签的工具。

        Returns:
            Schema 字典列表。
        """
        schemas: list[dict[str, Any]] = []
        for tool in self._tools.values():
            if tags and not set(tags).intersection(tool.tags):
                continue
            s = tool.schema()
            schemas.append({
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            })
        logger.debug(f"导出 {len(schemas)} 个工具 Schema。")
        return schemas

    def export_openai_tools(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        """导出 OpenAI function calling 格式的工具列表。

        返回格式::

            [
                {
                    "type": "function",
                    "function": {
                        "name": "...",
                        "description": "...",
                        "parameters": { ... }
                    }
                },
                ...
            ]

        Args:
            tags: 若指定，仅导出包含这些标签的工具。
        """
        return [
            {
                "type": "function",
                "function": schema,
            }
            for schema in self.export_schemas(tags=tags)
        ]

    def export_anthropic_tools(self, tags: list[str] | None = None) -> list[dict[str, Any]]:
        """导出 Anthropic tool_use 格式的工具列表。

        返回格式::

            [
                {
                    "name": "...",
                    "description": "...",
                    "input_schema": { ... }
                },
                ...
            ]

        Args:
            tags: 若指定，仅导出包含这些标签的工具。
        """
        return [
            {
                "name": schema["name"],
                "description": schema["description"],
                "input_schema": schema["parameters"],
            }
            for schema in self.export_schemas(tags=tags)
        ]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def get_stats(self, name: str) -> Optional[ToolStats]:
        """获取指定工具的调用统计。"""
        return self._stats.get(name)

    def get_all_stats(self) -> dict[str, ToolStats]:
        """获取所有工具的调用统计。"""
        return dict(self._stats)

    def reset_stats(self, name: str | None = None) -> None:
        """重置统计。

        Args:
            name: 工具名称。为 None 时重置全部。
        """
        if name is not None:
            if name in self._stats:
                self._stats[name] = ToolStats()
                logger.info(f"重置工具 {name} 的统计。")
        else:
            for key in self._stats:
                self._stats[key] = ToolStats()
            logger.info("重置所有工具统计。")

    # ------------------------------------------------------------------
    # 批量注册
    # ------------------------------------------------------------------

    def register_many(self, tools: list[BaseTool]) -> None:
        """批量注册工具。"""
        for tool in tools:
            self.register(tool)

    # ------------------------------------------------------------------
    # 魔术方法
    # ------------------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:
        return f"<ToolRegistry tools={len(self._tools)}>"


# ---------------------------------------------------------------------------
# 内置工具实现
# ---------------------------------------------------------------------------


class CCTool(BaseTool):
    """Claude Code 调用工具。"""

    name = "cc"
    description = "调用 Claude Code CLI 执行编程任务。"
    version = "1.0.0"
    tags = ["builtin", "ai", "code"]
    permission = ToolPermission(
        level=PermissionLevel.EXECUTE,
        requires_approval=True,
    )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "发送给 Claude Code 的指令。",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "工作目录路径。",
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        prompt = kwargs.get("prompt", "")
        workdir = kwargs.get("workdir", ".")
        logger.info(f"[cc] 执行 Claude Code, workdir={workdir}")
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        success = proc.returncode == 0
        return ToolResult(
            call_id="",
            tool_name=self.name,
            success=success,
            output=stdout.decode("utf-8", errors="replace"),
            error=stderr.decode("utf-8", errors="replace") if not success else None,
        )


class ShellTool(BaseTool):
    """Shell 命令执行工具。"""

    name = "shell"
    description = "执行 Shell 命令。"
    version = "1.0.0"
    tags = ["builtin", "system"]
    permission = ToolPermission(
        level=PermissionLevel.EXECUTE,
        requires_approval=True,
    )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 Shell 命令。",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "工作目录。",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，默认 60。",
                        "default": 60,
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command", "")
        cwd = kwargs.get("cwd", None)
        timeout = kwargs.get("timeout", 60)
        logger.info(f"[shell] 执行命令: {command}")
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                error=f"命令执行超时 ({timeout}s)",
            )

        success = proc.returncode == 0
        return ToolResult(
            call_id="",
            tool_name=self.name,
            success=success,
            output=stdout.decode("utf-8", errors="replace"),
            error=stderr.decode("utf-8", errors="replace") if not success else None,
        )


class FileTool(BaseTool):
    """文件读写工具。"""

    name = "file"
    description = "文件读写操作。"
    version = "1.0.0"
    tags = ["builtin", "filesystem"]
    permission = ToolPermission(
        level=PermissionLevel.WRITE,
        requires_approval=False,
    )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "append", "delete", "exists"],
                        "description": "操作类型。",
                    },
                    "path": {
                        "type": "string",
                        "description": "文件路径。",
                    },
                    "content": {
                        "type": "string",
                        "description": "写入内容（write/append 时需要）。",
                    },
                    "encoding": {
                        "type": "string",
                        "description": "文件编码，默认 utf-8。",
                        "default": "utf-8",
                    },
                },
                "required": ["action", "path"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        from pathlib import Path

        action = kwargs.get("action", "read")
        path = Path(kwargs.get("path", ""))
        content = kwargs.get("content", "")
        encoding = kwargs.get("encoding", "utf-8")

        logger.info(f"[file] {action}: {path}")

        try:
            if action == "read":
                if not path.exists():
                    return ToolResult(
                        call_id="",
                        tool_name=self.name,
                        success=False,
                        error=f"文件不存在: {path}",
                    )
                text = path.read_text(encoding=encoding)
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=True,
                    output=text,
                )

            elif action == "write":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding=encoding)
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=True,
                    output=f"已写入 {path}",
                )

            elif action == "append":
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding=encoding) as f:
                    f.write(content)
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=True,
                    output=f"已追加到 {path}",
                )

            elif action == "delete":
                if path.exists():
                    path.unlink()
                    return ToolResult(
                        call_id="",
                        tool_name=self.name,
                        success=True,
                        output=f"已删除 {path}",
                    )
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=False,
                    error=f"文件不存在: {path}",
                )

            elif action == "exists":
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=True,
                    output=str(path.exists()),
                )

            else:
                return ToolResult(
                    call_id="",
                    tool_name=self.name,
                    success=False,
                    error=f"不支持的操作: {action}",
                )

        except Exception as exc:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                error=str(exc),
            )


class GitTool(BaseTool):
    """Git 操作工具。"""

    name = "git"
    description = "Git 版本控制操作。"
    version = "1.0.0"
    tags = ["builtin", "vcs"]
    permission = ToolPermission(
        level=PermissionLevel.WRITE,
        requires_approval=False,
    )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "status",
                            "add",
                            "commit",
                            "push",
                            "pull",
                            "log",
                            "diff",
                            "branch",
                            "checkout",
                        ],
                        "description": "Git 操作类型。",
                    },
                    "args": {
                        "type": "string",
                        "description": "额外参数。",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Git 仓库目录。",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "status")
        args = kwargs.get("args", "")
        cwd = kwargs.get("cwd", None)

        cmd = f"git {action}"
        if args:
            cmd += f" {args}"

        logger.info(f"[git] 执行: {cmd}")

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        success = proc.returncode == 0
        return ToolResult(
            call_id="",
            tool_name=self.name,
            success=success,
            output=stdout.decode("utf-8", errors="replace"),
            error=stderr.decode("utf-8", errors="replace") if not success else None,
        )


class BrowserTool(BaseTool):
    """网页访问工具。"""

    name = "browser"
    description = "网页访问与内容提取。"
    version = "1.0.0"
    tags = ["builtin", "web"]
    permission = ToolPermission(
        level=PermissionLevel.READ_ONLY,
        requires_approval=False,
    )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "目标 URL。",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["fetch", "screenshot"],
                        "description": "操作类型：fetch 抓取内容，screenshot 截图。",
                        "default": "fetch",
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS 选择器，仅返回匹配元素的文本。",
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = kwargs.get("url", "")
        action = kwargs.get("action", "fetch")

        logger.info(f"[browser] {action}: {url}")

        try:
            import httpx

            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()

                if action == "fetch":
                    selector = kwargs.get("selector")
                    text = resp.text
                    if selector:
                        try:
                            from selectolax.parser import HTMLParser

                            tree = HTMLParser(text)
                            nodes = tree.css(selector)
                            text = "\n".join(node.text() for node in nodes)
                        except ImportError:
                            logger.warning(
                                "[browser] selectolax 未安装，返回完整 HTML。"
                            )
                    return ToolResult(
                        call_id="",
                        tool_name=self.name,
                        success=True,
                        output=text,
                    )

                elif action == "screenshot":
                    return ToolResult(
                        call_id="",
                        tool_name=self.name,
                        success=False,
                        error="截图功能需要 playwright，当前未实现。",
                    )

                else:
                    return ToolResult(
                        call_id="",
                        tool_name=self.name,
                        success=False,
                        error=f"不支持的操作: {action}",
                    )

        except Exception as exc:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# 全局注册中心
# ---------------------------------------------------------------------------

class PlaywrightBrowserTool(BrowserTool):
    """Browser tool with Playwright-backed screenshots."""

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = kwargs.get("action", "fetch")
        if action == "screenshot":
            return await self._screenshot(**kwargs)
        return await super().execute(**kwargs)

    async def _screenshot(self, **kwargs: Any) -> ToolResult:
        output_path = kwargs.get("output_path") or f"browser_screenshot_{int(time.time())}.png"
        full_page = bool(kwargs.get("full_page", True))
        viewport = kwargs.get("viewport") or {"width": 1280, "height": 720}

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                error="Playwright is required for screenshots. Install with: pip install playwright && playwright install chromium",
            )

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(viewport=viewport)
                await page.goto(kwargs.get("url", ""), wait_until="networkidle", timeout=30000)
                await page.screenshot(path=output_path, full_page=full_page)
                await browser.close()
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=True,
                output=output_path,
            )
        except Exception as exc:
            return ToolResult(
                call_id="",
                tool_name=self.name,
                success=False,
                error=str(exc),
            )


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """获取全局工具注册中心（懒初始化，含内置工具）。"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _register_builtin_tools(_registry)
    return _registry


def _register_builtin_tools(registry: ToolRegistry) -> None:
    """注册内置工具。"""
    from symbio.tools.submit_task import SubmitTaskTool

    builtins = [
        CCTool(),
        ShellTool(),
        FileTool(),
        GitTool(),
        PlaywrightBrowserTool(),
        SubmitTaskTool(),
    ]
    registry.register_many(builtins)
    logger.info(f"已注册 {len(builtins)} 个内置工具。")


def register_tool(tool: BaseTool) -> None:
    """向全局注册中心注册工具的便捷函数。"""
    get_tool_registry().register(tool)
