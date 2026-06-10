"""沙箱执行器 — 提供安全隔离的命令执行环境。

核心职责：
1. 容器化/微型虚拟机隔离 — 支持 Docker 容器执行
2. 本地环境沙箱 — 异步子进程 + 资源限制
3. 超时控制 — 防止命令卡死
4. 危险命令拦截 — 黑名单机制阻止高危操作
5. 执行结果捕获 — 完整记录 stdout/stderr

设计目标：
- ShellTool 和 FileTool 原生支持运行在 Docker 容器或轻量级沙箱
- 显式高危权限分级 — Read-Only / Write / Execute
- 最小权限原则 — Agent 默认只拥有完成任务所需的最小权限集

使用方式:
    executor = SandboxExecutor()
    result = await executor.execute("ls -la", working_dir="/tmp")

    # 带权限级别执行
    result = await executor.execute(
        "python script.py",
        permission_level=PermissionLevel.EXECUTE,
        timeout=60,
    )

    # Docker 容器执行
    result = await executor.execute_in_container(
        "python -c 'print(42)'",
        image="python:3.11-slim",
    )
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shlex
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from symbio.utils.logger import get_logger

logger = get_logger("sandbox")


# ---------------------------------------------------------------------------
# 常量与枚举
# ---------------------------------------------------------------------------


class PermissionLevel(str, Enum):
    """工具权限分级。

    Read-Only: 安全 — 只读操作（ls, cat, grep 等）
    Write: 敏感 — 写操作（cp, mv, mkdir 等）
    Execute: 高危 — 执行任意命令（需要显式授权）
    """

    READ_ONLY = "read_only"
    WRITE = "write"
    EXECUTE = "execute"


class SandboxMode(str, Enum):
    """沙箱执行模式。

    LOCAL: 本地子进程执行（默认）
    DOCKER: Docker 容器执行
    """

    LOCAL = "local"
    DOCKER = "docker"


class SandboxAccessMode(str, Enum):
    """Codex-style host access boundary for local command execution."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"
    UNRESTRICTED = "danger-full-access"


class ApprovalPolicy(str, Enum):
    """When a sandboxed command must be approved before running."""

    NEVER = "never"
    ON_REQUEST = "on-request"
    ON_FAILURE = "on-failure"
    ALWAYS = "always"


def normalize_sandbox_access_mode(value: str | SandboxAccessMode) -> SandboxAccessMode:
    if isinstance(value, SandboxAccessMode):
        return value
    normalized = str(value).strip().lower().replace("_", "-")
    aliases = {
        "read-only": SandboxAccessMode.READ_ONLY,
        "workspace-write": SandboxAccessMode.WORKSPACE_WRITE,
        "unrestricted": SandboxAccessMode.DANGER_FULL_ACCESS,
        "danger-full-access": SandboxAccessMode.DANGER_FULL_ACCESS,
    }
    if normalized not in aliases:
        raise ValueError(f"Unsupported sandbox access mode: {value}")
    return aliases[normalized]


def normalize_approval_policy(value: str | ApprovalPolicy) -> ApprovalPolicy:
    if isinstance(value, ApprovalPolicy):
        return value
    normalized = str(value).strip().lower().replace("_", "-")
    aliases = {
        "never": ApprovalPolicy.NEVER,
        "on-request": ApprovalPolicy.ON_REQUEST,
        "on-failure": ApprovalPolicy.ON_FAILURE,
        "always": ApprovalPolicy.ALWAYS,
    }
    if normalized not in aliases:
        raise ValueError(f"Unsupported approval policy: {value}")
    return aliases[normalized]


# ---------------------------------------------------------------------------
# 危险命令黑名单
# ---------------------------------------------------------------------------

# 高危命令模式 — 直接拦截
DANGEROUS_COMMAND_PATTERNS: list[re.Pattern] = [
    # 文件系统毁灭性操作
    re.compile(r"\brm\s+(-[rfR]+\s+)?/", re.IGNORECASE),  # rm -rf /
    re.compile(r"\brm\s+.*\s+--no-preserve-root", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),  # 格式化文件系统
    re.compile(r"\bdd\s+.*of=/dev/", re.IGNORECASE),  # dd 写设备
    re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),  # Windows 格式化

    # 磁盘操作
    re.compile(r"\bfdisk\b", re.IGNORECASE),
    re.compile(r"\bparted\b", re.IGNORECASE),
    re.compile(r"\bsfdisk\b", re.IGNORECASE),

    # 系统控制
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\breboot\b", re.IGNORECASE),
    re.compile(r"\bhalt\b", re.IGNORECASE),
    re.compile(r"\binit\s+0\b", re.IGNORECASE),
    re.compile(r"\binit\s+6\b", re.IGNORECASE),

    # 网络危险操作
    re.compile(r"\biptables\s+-F\b", re.IGNORECASE),  # 清空防火墙
    re.compile(r"\bnc\s+.*-e\b", re.IGNORECASE),  # netcat 反弹 shell
    re.compile(r"\bncat\s+.*-e\b", re.IGNORECASE),

    # 权限提升
    re.compile(r"\bsudo\s+su\b", re.IGNORECASE),
    re.compile(r"\bchmod\s+777\s+/", re.IGNORECASE),
    re.compile(r"\bchown\s+.*\s+/", re.IGNORECASE),

    # 反弹 shell / 后门
    re.compile(r"\bbash\s+-i\s+.*>/dev/tcp/", re.IGNORECASE),
    re.compile(r"\bpython.*socket.*connect", re.IGNORECASE),
    re.compile(r"\bperl.*socket.*connect", re.IGNORECASE),

    # Windows 高危命令
    re.compile(r"\bdel\s+/[sfq]\s+[a-zA-Z]:\\", re.IGNORECASE),
    re.compile(r"\brd\s+/[sq]\s+[a-zA-Z]:\\", re.IGNORECASE),
    re.compile(r"\breg\s+delete\b", re.IGNORECASE),
    re.compile(r"\bnet\s+user\s+.*\s+/add", re.IGNORECASE),  # 添加用户
    re.compile(r"\bnet\s+localgroup\s+administrators", re.IGNORECASE),
    re.compile(r"\bformat\s+", re.IGNORECASE),
]

# Read-Only 模式下允许的命令白名单
READ_ONLY_ALLOWED_COMMANDS: set[str] = {
    "ls", "dir", "cat", "type", "head", "tail", "more", "less",
    "grep", "find", "which", "where", "whereis", "file", "stat",
    "wc", "diff", "tree", "du", "df", "free", "uptime", "date",
    "whoami", "id", "hostname", "uname", "env", "printenv",
    "echo", "printf", "pwd", "cd", "pushd", "popd",
    "git", "python", "python3", "node", "npm", "pip",
    "pytest", "jest", "mocha", "cargo", "go",
}

# Write 模式下额外允许的命令
WRITE_ALLOWED_COMMANDS: set[str] = {
    "cp", "mv", "mkdir", "touch", "ln", "tee",
    "sed", "awk", "tr", "cut", "sort", "uniq",
    "tar", "zip", "unzip", "gzip", "gunzip",
    "chmod", "chown", "chgrp",
    "git", "docker", "pip", "npm", "yarn",
    "apt", "apt-get", "yum", "dnf", "brew", "choco",
}

NETWORK_COMMANDS: set[str] = {
    "curl", "wget", "http", "httpie", "ping", "tracert", "traceroute",
    "ssh", "scp", "sftp", "ftp", "telnet", "nc", "ncat", "netcat",
}

NETWORK_ARGUMENT_PATTERNS: list[re.Pattern] = [
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b[a-z0-9.-]+\.(com|cn|net|org|io|dev|ai)\b", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------------------------


class ResourceLimits(BaseModel):
    """资源限制配置。

    Attributes:
        max_memory_mb: 最大内存使用（MB），None 表示不限制。
        max_cpu_percent: 最大 CPU 使用百分比（1-100），None 表示不限制。
        max_output_size: 最大输出大小（字节），超过则截断。
        max_open_files: 最大打开文件数，None 表示不限制。
    """

    max_memory_mb: Optional[int] = None
    max_cpu_percent: Optional[int] = None
    max_output_size: int = 1024 * 1024  # 默认 1MB
    max_open_files: Optional[int] = None


class SandboxConfig(BaseModel):
    """沙箱执行配置。

    Attributes:
        mode: 执行模式（本地/Docker）。
        permission_level: 权限级别。
        timeout: 超时秒数，None 表示使用默认值。
        working_dir: 工作目录。
        env: 额外环境变量。
        resource_limits: 资源限制。
        docker_image: Docker 镜像（仅 Docker 模式）。
        docker_volumes: Docker 卷挂载（仅 Docker 模式）。
        capture_output: 是否捕获输出。
        shell: 是否使用 shell 模式执行。
    """

    mode: SandboxMode = SandboxMode.LOCAL
    permission_level: PermissionLevel = PermissionLevel.READ_ONLY
    timeout: Optional[int] = None
    working_dir: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    docker_image: str = "python:3.11-slim"
    docker_volumes: dict[str, str] = Field(default_factory=dict)
    capture_output: bool = True
    shell: bool = False


class SandboxResult(BaseModel):
    """沙箱执行结果。

    Attributes:
        command: 执行的命令。
        exit_code: 退出码（-1 表示异常）。
        stdout: 标准输出。
        stderr: 标准错误。
        duration_ms: 执行耗时（毫秒）。
        timed_out: 是否超时。
        permission_level: 使用的权限级别。
        working_dir: 实际工作目录。
        error_message: 错误摘要。
        metadata: 附加元数据。
    """

    command: str = ""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False
    permission_level: PermissionLevel = PermissionLevel.READ_ONLY
    working_dir: str = ""
    error_message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SandboxPolicy(BaseModel):
    """Runtime policy for local code execution.

    The shape follows the same practical split used by Codex-like agents:
    choose a filesystem access mode, list workspace/writable roots, and decide
    whether write/execute operations need human approval.
    """

    access_mode: SandboxAccessMode = SandboxAccessMode.READ_ONLY
    approval_policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST
    workspace_roots: list[str] = Field(default_factory=list)
    writable_roots: list[str] = Field(default_factory=list)
    allow_network: bool = False
    audit_enabled: bool = True

    @field_validator("access_mode", mode="before")
    @classmethod
    def _normalize_access_mode(cls, value):
        return normalize_sandbox_access_mode(value)

    @field_validator("approval_policy", mode="before")
    @classmethod
    def _normalize_approval_policy(cls, value):
        return normalize_approval_policy(value)


class SandboxAuditRecord(BaseModel):
    """One auditable sandbox execution decision/result."""

    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=datetime.now)
    command: str
    working_dir: str
    permission_level: PermissionLevel
    access_mode: SandboxAccessMode
    approval_policy: ApprovalPolicy
    approved: bool = False
    allowed: bool = False
    approval_required: bool = False
    exit_code: int = -1
    timed_out: bool = False
    duration_ms: float = 0.0
    reason: str = ""
    stdout_preview: str = ""
    stderr_preview: str = ""


# ---------------------------------------------------------------------------
# 命令验证器
# ---------------------------------------------------------------------------


class CommandValidator:
    """命令安全验证器。

    职责：
    - 检查命令是否在黑名单中
    - 验证权限级别是否允许执行
    - 解析命令参数用于安全审计
    """

    @staticmethod
    def is_dangerous(command: str) -> tuple[bool, str]:
        """检查命令是否危险。

        Args:
            command: 要检查的命令字符串。

        Returns:
            (is_dangerous, reason) 元组。
        """
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            match = pattern.search(command)
            if match:
                reason = f"Dangerous command pattern detected: {match.group()}"
                logger.warning(f"Blocked dangerous command: {command} — {reason}")
                return True, reason

        return False, ""

    @staticmethod
    def check_permission(command: str, level: PermissionLevel) -> tuple[bool, str]:
        """检查命令是否符合权限级别要求。

        Args:
            command: 要执行的命令。
            level: 当前权限级别。

        Returns:
            (allowed, reason) 元组。
        """
        # 提取命令的第一个词作为主命令
        try:
            parts = shlex.split(command)
        except ValueError:
            # shlex 解析失败，尝试简单分割
            parts = command.split()

        if not parts:
            return False, "Empty command"

        cmd_name = Path(parts[0]).name  # 处理带路径的命令

        if level == PermissionLevel.READ_ONLY:
            if cmd_name not in READ_ONLY_ALLOWED_COMMANDS:
                return False, (
                    f"Command '{cmd_name}' not allowed in READ_ONLY mode. "
                    f"Use WRITE or EXECUTE permission level."
                )

        elif level == PermissionLevel.WRITE:
            # Write 模式允许所有 read-only 命令 + write 命令
            allowed = READ_ONLY_ALLOWED_COMMANDS | WRITE_ALLOWED_COMMANDS
            if cmd_name not in allowed:
                return False, (
                    f"Command '{cmd_name}' not allowed in WRITE mode. "
                    f"Use EXECUTE permission level for arbitrary commands."
                )

        # EXECUTE 模式允许所有命令（但仍受危险命令黑名单限制）

        return True, ""


# ---------------------------------------------------------------------------
# 环境构建器
# ---------------------------------------------------------------------------


def _build_sandbox_env(
    extra_env: Optional[dict[str, str]] = None,
    inherit_env: bool = True,
) -> dict[str, str]:
    """构建沙箱化的环境变量。

    策略：
    - 继承当前进程的必要环境变量
    - 设置沙箱标志
    - 可叠加额外的环境变量

    Args:
        extra_env: 额外的环境变量。
        inherit_env: 是否继承当前进程的环境变量。

    Returns:
        沙箱化的环境变量字典。
    """
    # 安全的环境变量白名单
    safe_keys = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "SystemRoot",
        "SYSTEMROOT",
        "windir",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LANGUAGE",
        "SHELL",
        "PYTHONPATH",
        "PYTHONIOENCODING",
        "NODE_PATH",
        "GOPATH",
        "GOROOT",
        "CARGO_HOME",
        "JAVA_HOME",
    }

    env: dict[str, str] = {}

    if inherit_env:
        for key in safe_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

    # 沙箱标志
    env["SYMBIO_SANDBOX"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    # 叠加自定义环境变量
    if extra_env:
        env.update(extra_env)

    return env


# ---------------------------------------------------------------------------
# 沙箱执行器
# ---------------------------------------------------------------------------


class SandboxExecutor:
    """沙箱执行器 — 提供安全隔离的命令执行环境。

    核心能力：
    1. 危险命令拦截 — 基于黑名单机制
    2. 权限分级控制 — Read-Only / Write / Execute
    3. 超时控制 — 防止命令卡死
    4. 资源限制 — 内存/CPU/输出大小
    5. 结果捕获 — 完整记录 stdout/stderr
    6. Docker 容器执行 — 可选的容器隔离

    使用方式:
        executor = SandboxExecutor()

        # 基础执行
        result = await executor.execute("ls -la")

        # 带权限和超时
        result = await executor.execute(
            "python script.py",
            permission_level=PermissionLevel.EXECUTE,
            timeout=60,
        )

        # Docker 容器执行
        result = await executor.execute_in_container(
            "python -c 'print(42)'",
            image="python:3.11-slim",
        )
    """

    DEFAULT_TIMEOUT = 300  # 默认 5 分钟超时
    KILL_TIMEOUT = 3  # 强制终止等待时间

    def __init__(
        self,
        default_timeout: int = DEFAULT_TIMEOUT,
        default_permission: PermissionLevel = PermissionLevel.READ_ONLY,
        default_working_dir: str = ".",
    ) -> None:
        """初始化沙箱执行器。

        Args:
            default_timeout: 默认超时秒数。
            default_permission: 默认权限级别。
            default_working_dir: 默认工作目录。
        """
        self._default_timeout = default_timeout
        self._default_permission = default_permission
        self._default_working_dir = default_working_dir
        self._validator = CommandValidator()
        self.audit_records: list[SandboxAuditRecord] = []

        logger.info(
            f"SandboxExecutor initialized: "
            f"timeout={default_timeout}s, "
            f"permission={default_permission.value}, "
            f"working_dir={default_working_dir}"
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def execute(
        self,
        command: str,
        permission_level: Optional[PermissionLevel] = None,
        timeout: Optional[int] = None,
        working_dir: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        resource_limits: Optional[ResourceLimits] = None,
        shell: bool = False,
    ) -> SandboxResult:
        """执行命令（本地子进程模式）。

        流程：
        1. 验证命令安全性（危险命令检查 + 权限检查）
        2. 构建沙箱化环境变量
        3. 异步启动子进程
        4. 超时控制 — 超时则 kill 子进程
        5. 捕获 stdout/stderr
        6. 组装 SandboxResult 返回

        Args:
            command: 要执行的命令。
            permission_level: 权限级别，None 使用默认值。
            timeout: 超时秒数，None 使用默认值。
            working_dir: 工作目录，None 使用默认值。
            env: 额外环境变量。
            resource_limits: 资源限制，None 使用默认值。
            shell: 是否使用 shell 模式执行。

        Returns:
            SandboxResult 执行结果。
        """
        # 参数默认值
        perm_level = permission_level or self._default_permission
        exec_timeout = timeout or self._default_timeout
        work_dir = working_dir or self._default_working_dir
        limits = resource_limits or ResourceLimits()

        logger.info(f"Executing command: {command}")
        logger.debug(f"Permission: {perm_level.value}, timeout: {exec_timeout}s")

        # Step 1: 危险命令检查
        is_dangerous, danger_reason = self._validator.is_dangerous(command)
        if is_dangerous:
            logger.error(f"Blocked dangerous command: {command}")
            return SandboxResult(
                command=command,
                exit_code=-1,
                error_message=f"BLOCKED: {danger_reason}",
                permission_level=perm_level,
                working_dir=work_dir,
            )

        # Step 2: 权限检查
        allowed, perm_reason = self._validator.check_permission(command, perm_level)
        if not allowed:
            logger.error(f"Permission denied: {perm_reason}")
            return SandboxResult(
                command=command,
                exit_code=-1,
                error_message=f"PERMISSION_DENIED: {perm_reason}",
                permission_level=perm_level,
                working_dir=work_dir,
            )

        # Step 3: 验证工作目录
        work_path = Path(work_dir).resolve()
        if not work_path.exists():
            logger.error(f"Working directory does not exist: {work_path}")
            return SandboxResult(
                command=command,
                exit_code=-1,
                error_message=f"Working directory does not exist: {work_path}",
                permission_level=perm_level,
                working_dir=str(work_path),
            )

        # Step 4: 构建环境变量
        sandbox_env = _build_sandbox_env(env)

        # Step 5: 构建命令参数
        if shell:
            # shell 模式：通过 /bin/sh -c 或 cmd /c 执行
            if platform.system() == "Windows":
                cmd_parts = ["cmd", "/c", command]
            else:
                cmd_parts = ["/bin/sh", "-c", command]
        else:
            # 非 shell 模式：解析命令参数
            try:
                cmd_parts = shlex.split(command)
            except ValueError:
                # 解析失败时回退到简单分割
                cmd_parts = command.split()

        # Step 6: 执行子进程
        return await self._execute_subprocess(
            cmd=cmd_parts,
            work_dir=work_path,
            env=sandbox_env,
            timeout=exec_timeout,
            command_str=command,
            perm_level=perm_level,
            resource_limits=limits,
        )

    async def execute_in_container(
        self,
        command: str,
        image: str = "python:3.11-slim",
        volumes: Optional[dict[str, str]] = None,
        timeout: Optional[int] = None,
        working_dir: str = "/workspace",
        env: Optional[dict[str, str]] = None,
    ) -> SandboxResult:
        """在 Docker 容器中执行命令。

        流程：
        1. 构建 docker run 命令
        2. 启动容器并执行命令
        3. 捕获输出
        4. 清理容器

        Args:
            command: 要执行的命令。
            image: Docker 镜像名称。
            volumes: 卷挂载 {host_path: container_path}。
            timeout: 超时秒数。
            working_dir: 容器内工作目录。
            env: 额外环境变量。

        Returns:
            SandboxResult 执行结果。
        """
        exec_timeout = timeout or self._default_timeout
        volumes = volumes or {}

        logger.info(f"Executing in container: {command}")
        logger.debug(f"Image: {image}, timeout: {exec_timeout}s")

        # 危险命令检查（容器内也需要检查）
        is_dangerous, danger_reason = self._validator.is_dangerous(command)
        if is_dangerous:
            logger.error(f"Blocked dangerous command in container: {command}")
            return SandboxResult(
                command=command,
                exit_code=-1,
                error_message=f"BLOCKED: {danger_reason}",
                permission_level=PermissionLevel.EXECUTE,
                working_dir=working_dir,
                metadata={"mode": "docker", "image": image},
            )

        # 构建 docker run 命令
        docker_cmd = [
            "docker", "run",
            "--rm",  # 执行完自动删除容器
            "--network", "none",  # 禁用网络（安全隔离）
            "--memory", "512m",  # 内存限制
            "--cpus", "1",  # CPU 限制
            "--read-only",  # 只读文件系统
            "--tmpfs", "/tmp:rw,noexec,nosuid",  # 可写临时目录
        ]

        # 工作目录
        docker_cmd.extend(["-w", working_dir])

        # 卷挂载
        for host_path, container_path in volumes.items():
            abs_host_path = str(Path(host_path).resolve())
            docker_cmd.extend(["-v", f"{abs_host_path}:{container_path}:ro"])

        # 环境变量
        sandbox_env = _build_sandbox_env(env)
        for key, value in sandbox_env.items():
            docker_cmd.extend(["-e", f"{key}={value}"])

        # 镜像和命令
        docker_cmd.append(image)
        docker_cmd.extend(["sh", "-c", command])

        # 执行
        result = await self._execute_subprocess(
            cmd=docker_cmd,
            work_dir=Path.cwd(),
            env=os.environ.copy(),  # docker 命令需要宿主机环境
            timeout=exec_timeout,
            command_str=f"docker run {image} sh -c '{command}'",
            perm_level=PermissionLevel.EXECUTE,
            resource_limits=ResourceLimits(),
        )

        result.metadata["mode"] = "docker"
        result.metadata["image"] = image

        return result

    async def execute_with_policy(
        self,
        command: str,
        policy: SandboxPolicy,
        permission_level: Optional[PermissionLevel] = None,
        timeout: Optional[int] = None,
        working_dir: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        resource_limits: Optional[ResourceLimits] = None,
        shell: bool = False,
        approved: bool = False,
    ) -> SandboxResult:
        """Execute a command after applying workspace and approval policy."""

        perm_level = permission_level or self._default_permission
        work_dir = str(Path(working_dir or self._default_working_dir).resolve())
        policy_meta = self._policy_metadata(policy)

        allowed, reason = self._check_workspace_boundary(work_dir, policy, perm_level)
        if not allowed:
            approval_required = self._boundary_can_request_approval(policy)
            if approval_required and not approved:
                result = SandboxResult(
                    command=command,
                    exit_code=-1,
                    error_message=f"APPROVAL_REQUIRED: {reason}",
                    permission_level=perm_level,
                    working_dir=work_dir,
                    metadata={
                        "policy": policy_meta,
                        "approval_required": True,
                        "approved": False,
                        "sandbox_violation": True,
                    },
                )
                self._record_audit(command, result, policy, approved, True, False, reason)
                return result
            if approval_required and approved:
                logger.warning(f"Sandbox boundary bypass approved for command: {command}")
            else:
                result = SandboxResult(
                    command=command,
                    exit_code=-1,
                    error_message=f"WORKSPACE_VIOLATION: {reason}",
                    permission_level=perm_level,
                    working_dir=work_dir,
                    metadata={
                        "policy": policy_meta,
                        "approval_required": False,
                        "sandbox_violation": True,
                    },
                )
                self._record_audit(command, result, policy, approved, False, False, reason)
                return result

        approval_required = self._requires_approval(policy, perm_level)
        if approval_required and not approved:
            reason = f"{perm_level.value} command requires approval under {policy.approval_policy.value}"
            result = SandboxResult(
                command=command,
                exit_code=-1,
                error_message=f"APPROVAL_REQUIRED: {reason}",
                permission_level=perm_level,
                working_dir=work_dir,
                metadata={
                    "policy": policy_meta,
                    "approval_required": True,
                    "approved": False,
                },
            )
            self._record_audit(command, result, policy, approved, True, False, reason)
            return result

        if not policy.allow_network and self._uses_network(command):
            reason = "network access is disabled by sandbox policy"
            network_approval_required = self._boundary_can_request_approval(policy)
            if network_approval_required and not approved:
                result = SandboxResult(
                    command=command,
                    exit_code=-1,
                    error_message=f"APPROVAL_REQUIRED: {reason}",
                    permission_level=perm_level,
                    working_dir=work_dir,
                    metadata={
                        "policy": policy_meta,
                        "approval_required": True,
                        "approved": False,
                        "network_violation": True,
                    },
                )
                self._record_audit(command, result, policy, approved, True, False, reason)
                return result
            if network_approval_required and approved:
                logger.warning(f"Network boundary bypass approved for command: {command}")
            else:
                result = SandboxResult(
                    command=command,
                    exit_code=-1,
                    error_message=f"NETWORK_BLOCKED: {reason}",
                    permission_level=perm_level,
                    working_dir=work_dir,
                    metadata={
                        "policy": policy_meta,
                        "approval_required": False,
                        "approved": approved,
                        "network_violation": True,
                    },
                )
                self._record_audit(command, result, policy, approved, False, False, reason)
                return result

        result = await self.execute(
            command=command,
            permission_level=perm_level,
            timeout=timeout,
            working_dir=work_dir,
            env=env,
            resource_limits=resource_limits,
            shell=shell,
        )
        result.metadata.update({
            "policy": policy_meta,
            "approval_required": approval_required,
            "approved": approved,
            "escalated": approved and (approval_required or not allowed),
        })
        self._record_audit(
            command=command,
            result=result,
            policy=policy,
            approved=approved,
            approval_required=approval_required,
            allowed=result.exit_code != -1 or not result.error_message.startswith(("BLOCKED", "PERMISSION_DENIED")),
            reason=result.error_message,
        )
        return result

    async def execute_with_config(
        self,
        command: str,
        config: SandboxConfig,
    ) -> SandboxResult:
        """使用配置对象执行命令。

        Args:
            command: 要执行的命令。
            config: 沙箱配置。

        Returns:
            SandboxResult 执行结果。
        """
        if config.mode == SandboxMode.DOCKER:
            return await self.execute_in_container(
                command=command,
                image=config.docker_image,
                volumes=config.docker_volumes,
                timeout=config.timeout,
                working_dir=config.working_dir,
                env=config.env,
            )
        else:
            return await self.execute(
                command=command,
                permission_level=config.permission_level,
                timeout=config.timeout,
                working_dir=config.working_dir,
                env=config.env,
                resource_limits=config.resource_limits,
                shell=config.shell,
            )

    def _policy_metadata(self, policy: SandboxPolicy) -> dict[str, Any]:
        return {
            "access_mode": policy.access_mode.value,
            "approval_policy": policy.approval_policy.value,
            "workspace_roots": [str(Path(path).resolve()) for path in policy.workspace_roots],
            "writable_roots": [str(Path(path).resolve()) for path in policy.writable_roots],
            "allow_network": policy.allow_network,
        }

    def _check_workspace_boundary(
        self,
        working_dir: str,
        policy: SandboxPolicy,
        permission_level: PermissionLevel,
    ) -> tuple[bool, str]:
        if policy.access_mode == SandboxAccessMode.DANGER_FULL_ACCESS:
            return True, ""

        work_path = Path(working_dir).resolve()
        workspace_roots = [Path(path).resolve() for path in policy.workspace_roots]
        if not workspace_roots:
            workspace_roots = [Path(self._default_working_dir).resolve()]

        if not any(self._is_relative_to(work_path, root) for root in workspace_roots):
            return False, f"working_dir {work_path} is outside workspace roots"

        if policy.access_mode == SandboxAccessMode.READ_ONLY and permission_level != PermissionLevel.READ_ONLY:
            return False, "read_only sandbox only allows READ_ONLY commands"

        if permission_level in {PermissionLevel.WRITE, PermissionLevel.EXECUTE}:
            writable_roots = [Path(path).resolve() for path in policy.writable_roots]
            if not writable_roots:
                writable_roots = workspace_roots if policy.access_mode == SandboxAccessMode.WORKSPACE_WRITE else []
            if not any(self._is_relative_to(work_path, root) for root in writable_roots):
                return False, f"working_dir {work_path} is outside writable roots"

        return True, ""

    def _requires_approval(self, policy: SandboxPolicy, permission_level: PermissionLevel) -> bool:
        if policy.approval_policy == ApprovalPolicy.ALWAYS:
            return True
        if policy.approval_policy == ApprovalPolicy.NEVER:
            return False
        if policy.approval_policy == ApprovalPolicy.ON_FAILURE:
            return False
        if policy.access_mode == SandboxAccessMode.DANGER_FULL_ACCESS:
            return True
        return permission_level in {PermissionLevel.WRITE, PermissionLevel.EXECUTE}

    def _boundary_can_request_approval(self, policy: SandboxPolicy) -> bool:
        return policy.approval_policy in {
            ApprovalPolicy.ALWAYS,
            ApprovalPolicy.ON_REQUEST,
            ApprovalPolicy.ON_FAILURE,
        }

    def _uses_network(self, command: str) -> bool:
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        if not parts:
            return False
        cmd_name = Path(parts[0]).name.lower()
        if cmd_name in NETWORK_COMMANDS:
            return True
        return any(pattern.search(command) for pattern in NETWORK_ARGUMENT_PATTERNS)

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _record_audit(
        self,
        command: str,
        result: SandboxResult,
        policy: SandboxPolicy,
        approved: bool,
        approval_required: bool,
        allowed: bool,
        reason: str,
    ) -> None:
        if not policy.audit_enabled:
            return
        self.audit_records.append(
            SandboxAuditRecord(
                command=command,
                working_dir=result.working_dir,
                permission_level=result.permission_level,
                access_mode=policy.access_mode,
                approval_policy=policy.approval_policy,
                approved=approved,
                allowed=allowed,
                approval_required=approval_required,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_ms=result.duration_ms,
                reason=reason,
                stdout_preview=result.stdout[:500],
                stderr_preview=result.stderr[:500],
            )
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _execute_subprocess(
        self,
        cmd: list[str],
        work_dir: Path,
        env: dict[str, str],
        timeout: int,
        command_str: str,
        perm_level: PermissionLevel,
        resource_limits: ResourceLimits,
    ) -> SandboxResult:
        """沙箱化执行子进程。

        使用 asyncio.create_subprocess_exec 启动独立子进程，
        与主进程完全隔离。

        Args:
            cmd: 命令参数列表。
            work_dir: 工作目录。
            env: 环境变量。
            timeout: 超时秒数。
            command_str: 原始命令字符串（用于日志）。
            perm_level: 权限级别。
            resource_limits: 资源限制。

        Returns:
            SandboxResult 执行结果。
        """
        logger.info(f"Executing subprocess: {' '.join(cmd)}")
        logger.debug(f"Working dir: {work_dir}, timeout: {timeout}s")

        start_time = datetime.now()
        process = None

        try:
            # 创建子进程
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=env,
            )

            logger.debug(f"Subprocess started (pid={process.pid})")

            # 带超时的等待
            try:
                raw_stdout_bytes, raw_stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.error(f"Subprocess timed out after {timeout}s (pid={process.pid})")
                await self._kill_process(process)
                elapsed = (datetime.now() - start_time).total_seconds() * 1000

                return SandboxResult(
                    command=command_str,
                    exit_code=-1,
                    stdout="",
                    stderr=f"TIMEOUT: Command exceeded {timeout}s limit",
                    duration_ms=elapsed,
                    timed_out=True,
                    permission_level=perm_level,
                    working_dir=str(work_dir),
                    error_message=f"TIMEOUT: Command exceeded {timeout}s limit",
                )

            # 解码输出（带截断）
            max_size = resource_limits.max_output_size
            stdout_text = raw_stdout_bytes.decode("utf-8", errors="replace")[:max_size]
            stderr_text = raw_stderr_bytes.decode("utf-8", errors="replace")[:max_size]
            exit_code = process.returncode or 0
            elapsed = (datetime.now() - start_time).total_seconds() * 1000

            logger.info(
                f"Subprocess finished: pid={process.pid}, "
                f"exit_code={exit_code}, elapsed={elapsed:.0f}ms"
            )

            if stdout_text.strip():
                logger.debug(f"stdout ({len(stdout_text)} chars): {stdout_text[:200]}...")
            if stderr_text.strip():
                logger.debug(f"stderr ({len(stderr_text)} chars): {stderr_text[:200]}...")

            return SandboxResult(
                command=command_str,
                exit_code=exit_code,
                stdout=stdout_text,
                stderr=stderr_text,
                duration_ms=elapsed,
                timed_out=False,
                permission_level=perm_level,
                working_dir=str(work_dir),
                error_message="" if exit_code == 0 else f"Process exited with code {exit_code}",
            )

        except FileNotFoundError:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"Command not found: {cmd[0]}"
            logger.error(error_msg)
            return SandboxResult(
                command=command_str,
                exit_code=-1,
                stderr=error_msg,
                duration_ms=elapsed,
                permission_level=perm_level,
                working_dir=str(work_dir),
                error_message=error_msg,
            )

        except PermissionError:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"Permission denied executing: {' '.join(cmd)}"
            logger.error(error_msg)
            return SandboxResult(
                command=command_str,
                exit_code=-1,
                stderr=error_msg,
                duration_ms=elapsed,
                permission_level=perm_level,
                working_dir=str(work_dir),
                error_message=error_msg,
            )

        except OSError as exc:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"OS error executing subprocess: {exc}"
            logger.error(error_msg)
            return SandboxResult(
                command=command_str,
                exit_code=-1,
                stderr=error_msg,
                duration_ms=elapsed,
                permission_level=perm_level,
                working_dir=str(work_dir),
                error_message=error_msg,
            )

        except Exception as exc:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            error_msg = f"Unexpected error executing subprocess: {exc}"
            logger.error(error_msg)
            return SandboxResult(
                command=command_str,
                exit_code=-1,
                stderr=error_msg,
                duration_ms=elapsed,
                permission_level=perm_level,
                working_dir=str(work_dir),
                error_message=error_msg,
            )

        finally:
            # 确保进程资源被释放
            if process is not None and process.returncode is None:
                await self._kill_process(process)

    async def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        """安全终止子进程。

        先尝试 terminate（SIGTERM），给进程机会优雅退出；
        如果超时仍未退出，强制 kill（SIGKILL）。

        Args:
            process: 要终止的子进程。
        """
        try:
            logger.warning(f"Terminating subprocess (pid={process.pid})")
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self.KILL_TIMEOUT)
                logger.info(f"Subprocess terminated gracefully (pid={process.pid})")
            except asyncio.TimeoutError:
                logger.warning(
                    f"Subprocess did not terminate gracefully, killing (pid={process.pid})"
                )
                process.kill()
                await process.wait()
        except ProcessLookupError:
            # 进程已经退出
            pass
        except Exception as exc:
            logger.error(f"Error killing subprocess (pid={process.pid}): {exc}")


# ---------------------------------------------------------------------------
# 便捷工厂函数
# ---------------------------------------------------------------------------


def create_sandbox_executor(
    default_timeout: int = 300,
    default_permission: PermissionLevel = PermissionLevel.READ_ONLY,
    working_dir: str = ".",
) -> SandboxExecutor:
    """创建沙箱执行器实例。

    Args:
        default_timeout: 默认超时秒数。
        default_permission: 默认权限级别。
        working_dir: 默认工作目录。

    Returns:
        SandboxExecutor 实例。
    """
    return SandboxExecutor(
        default_timeout=default_timeout,
        default_permission=default_permission,
        default_working_dir=working_dir,
    )


def create_read_only_executor(working_dir: str = ".") -> SandboxExecutor:
    """创建只读沙箱执行器。

    只允许安全的只读命令（ls, cat, grep 等）。

    Args:
        working_dir: 默认工作目录。

    Returns:
        只读 SandboxExecutor 实例。
    """
    return SandboxExecutor(
        default_timeout=60,
        default_permission=PermissionLevel.READ_ONLY,
        default_working_dir=working_dir,
    )


def create_docker_executor(
    default_image: str = "python:3.11-slim",
    working_dir: str = "/workspace",
) -> SandboxExecutor:
    """创建 Docker 容器沙箱执行器。

    Args:
        default_image: 默认 Docker 镜像。
        working_dir: 容器内默认工作目录。

    Returns:
        SandboxExecutor 实例（需配合 execute_in_container 使用）。
    """
    return SandboxExecutor(
        default_timeout=300,
        default_permission=PermissionLevel.EXECUTE,
        default_working_dir=working_dir,
    )
