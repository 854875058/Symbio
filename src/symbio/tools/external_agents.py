"""Control plane for external coding agents such as Codex and Claude Code.

The controller treats local coding-agent CLIs as managed runtimes: discover the
binary, register or create a session in a workspace, send prompts, and keep an
audit trail that the API/UI can expose.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


ExternalAgentProviderId = Literal["codex", "claude-code"]


# 宿主（如 Symbio 自身运行在 Claude Code / 某 Agent 运行时下）会向环境注入这些
# Anthropic 凭据/端点/模型变量。直接传给被 Symbio 拉起的 `claude` 子进程会让它
# 误用宿主的会话 token（401 Invalid bearer token）或错误端点/模型。剥离它们，
# 让外部编码 Agent CLI 用自己的登录态运行。
_HOST_AUTH_CONFLICT_ENV = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


def _clean_subprocess_env() -> dict[str, str]:
    """复制当前环境，剥离会破坏被拉起的外部 CLI 的宿主注入凭据/端点/模型变量。"""
    env = dict(os.environ)
    for key in _HOST_AUTH_CONFLICT_ENV:
        env.pop(key, None)
    return env


def _extract_claude_json(stdout: str) -> tuple[str, str]:
    """解析 claude -p --output-format json 输出，取出 (result 文本, session_id)。

    Windows 上 .CMD 会在前面打一行 "Active code page: ..."，故从第一个 '{' 起解析。
    解析失败（非 JSON / 报错输出）则原样返回文本、session_id 为空，不影响调用方。
    """
    if not stdout:
        return stdout, ""
    start = stdout.find("{")
    if start == -1:
        return stdout, ""
    try:
        data = json.loads(stdout[start:])
    except Exception:
        return stdout, ""
    if not isinstance(data, dict):
        return stdout, ""
    text = data.get("result")
    if not isinstance(text, str):
        text = stdout
    return text, str(data.get("session_id") or "")


def _strip_cmd_shim_noise(stdout: str) -> str:
    """剥掉 Windows .CMD 包装器打印的 "Active code page: N" 噪声行。

    codex.CMD 启动时会 `chcp 65001` 切到 UTF-8，stdout 里因此多出一行
    "Active code page: 65001"。只删这类已知 shim 行，其余原样保留。
    """
    if not stdout or "Active code page" not in stdout:
        return stdout
    lines = stdout.splitlines()
    kept = [ln for ln in lines if not ln.strip().startswith("Active code page")]
    cleaned = "\n".join(kept)
    # 保留原本是否以换行结尾的观感
    if stdout.endswith("\n") and not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned.lstrip("\n")


class ExternalAgentProvider(BaseModel):
    """Runtime definition for an external coding-agent CLI."""

    provider_id: str
    display_name: str
    executable: str
    installed: bool = False
    path: str = ""
    version: str = ""
    capabilities: list[str] = Field(default_factory=list)
    session_resume: bool = True
    notes: str = ""


class ExternalAgentSessionCreate(BaseModel):
    """Request to register an existing CLI session or create a managed handle."""

    provider: str
    label: str = ""
    workspace: str = "."
    external_session_id: str = ""
    model: str = ""
    sandbox_mode: str = "workspace-write"
    approval_policy: str = "on-request"
    permission_mode: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"claude", "claude-code", "cc"}:
            return "claude-code"
        if normalized in {"codex", "openai-codex"}:
            return "codex"
        return normalized


class ExternalAgentRunRequest(BaseModel):
    """Prompt execution request for a registered external session."""

    prompt: str
    dry_run: bool = False
    approved: bool = False
    timeout: int = 300
    model: str = ""
    sandbox_mode: str = ""
    approval_policy: str = ""
    permission_mode: str = ""


class ExternalAgentSession(BaseModel):
    """Symbio-managed handle for a local external-agent session."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider: str
    label: str = ""
    workspace: str
    external_session_id: str = ""
    model: str = ""
    sandbox_mode: str = "workspace-write"
    approval_policy: str = "on-request"
    permission_mode: str = ""
    status: str = "registered"
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_run_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalAgentRunResult(BaseModel):
    """Result and audit payload for a prompt sent to an external agent."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    provider: str
    command: list[str] = Field(default_factory=list)
    dry_run: bool = False
    success: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_ms: float = 0.0
    created_at: datetime = Field(default_factory=datetime.now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalAgentController:
    """Discover and operate Codex/Claude Code CLI sessions."""

    def __init__(
        self,
        *,
        state_path: str | Path = Path("data") / "external_agents.json",
        workspace_root: str | Path = ".",
        providers: list[ExternalAgentProvider] | None = None,
        allow_any_workspace: bool = False,
    ) -> None:
        self.state_path = Path(state_path)
        self.workspace_root = Path(workspace_root).resolve()
        # 开启后允许工作区指向整机任意绝对目录（工作台派活到真实项目用）；
        # 关闭时（默认）仍限制在 workspace_root 内，保持既有安全边界与测试行为。
        self.allow_any_workspace = allow_any_workspace
        self.providers: dict[str, ExternalAgentProvider] = {
            provider.provider_id: provider
            for provider in (providers if providers is not None else discover_external_agent_providers())
        }
        self.sessions: dict[str, ExternalAgentSession] = {}
        self.audit: list[ExternalAgentRunResult] = []
        self._load_state()

    def list_providers(self) -> list[ExternalAgentProvider]:
        """Return supported local CLI runtimes."""
        return list(self.providers.values())

    def list_sessions(self) -> list[ExternalAgentSession]:
        """Return registered external-agent sessions."""
        return sorted(self.sessions.values(), key=lambda item: item.updated_at, reverse=True)

    def get_session(self, session_id: str) -> ExternalAgentSession | None:
        return self.sessions.get(session_id)

    def create_session(self, request: ExternalAgentSessionCreate) -> ExternalAgentSession:
        provider = self._require_provider(request.provider)
        workspace = self._resolve_workspace(request.workspace)
        label = request.label or f"{provider.display_name} session"
        session = ExternalAgentSession(
            provider=provider.provider_id,
            label=label,
            workspace=str(workspace),
            external_session_id=request.external_session_id,
            model=request.model,
            sandbox_mode=request.sandbox_mode,
            approval_policy=request.approval_policy,
            permission_mode=request.permission_mode,
            metadata=request.metadata,
        )
        self.sessions[session.session_id] = session
        self._save_state()
        return session

    async def run_session(
        self,
        session_id: str,
        request: ExternalAgentRunRequest,
    ) -> ExternalAgentRunResult:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"External agent session not found: {session_id}")
        provider = self._require_provider(session.provider)
        command = build_external_agent_command(provider, session, request)

        if request.dry_run:
            result = ExternalAgentRunResult(
                session_id=session.session_id,
                provider=session.provider,
                command=command,
                dry_run=True,
                success=True,
                exit_code=0,
                stdout="Dry run only. Command was not executed.",
            )
            self._record_result(session, result)
            return result

        if not provider.installed:
            result = ExternalAgentRunResult(
                session_id=session.session_id,
                provider=session.provider,
                command=command,
                success=False,
                error=f"{provider.display_name} CLI is not installed or not on PATH",
            )
            self._record_result(session, result)
            return result

        result = await self._execute(command, Path(session.workspace), request.timeout)
        result.session_id = session.session_id
        result.provider = session.provider
        result.command = command
        # claude-code：从 --output-format json 输出里解包干净文本 + 回填 session_id，
        # 后续轮自动 --resume 拿到原生多轮记忆。
        if session.provider == "claude-code" and result.success:
            text, captured_id = _extract_claude_json(result.stdout)
            result.stdout = text
            if captured_id and not session.external_session_id:
                session.external_session_id = captured_id
                result.metadata["captured_session_id"] = captured_id
        elif session.provider == "codex":
            # Windows 上 codex.CMD 会先 `chcp 65001` 打印一行 "Active code page:
            # 65001"，混进纯文本输出里。剥掉这类 shim 噪声再回显。
            result.stdout = _strip_cmd_shim_noise(result.stdout)
        self._record_result(session, result)
        return result

    def list_audit(self, limit: int = 50) -> list[ExternalAgentRunResult]:
        return list(reversed(self.audit[-max(limit, 1):]))

    def _require_provider(self, provider_id: str) -> ExternalAgentProvider:
        normalized = ExternalAgentSessionCreate(provider=provider_id).provider
        provider = self.providers.get(normalized)
        if provider is None:
            raise ValueError(f"Unsupported external agent provider: {provider_id}")
        return provider

    def _resolve_workspace(self, workspace: str) -> Path:
        path = Path(workspace)
        if not path.is_absolute():
            path = self.workspace_root / path
        resolved = path.resolve()
        if not self.allow_any_workspace and not _is_relative_to(resolved, self.workspace_root):
            raise ValueError(f"Workspace is outside Symbio workspace root: {resolved}")
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    async def _execute(
        self,
        command: list[str],
        cwd: Path,
        timeout: int,
    ) -> ExternalAgentRunResult:
        started = datetime.now()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_clean_subprocess_env(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                elapsed = (datetime.now() - started).total_seconds() * 1000
                return ExternalAgentRunResult(
                    session_id="",
                    provider="",
                    success=False,
                    exit_code=-1,
                    stderr=f"TIMEOUT: external agent exceeded {timeout}s",
                    error=f"TIMEOUT: external agent exceeded {timeout}s",
                    duration_ms=elapsed,
                )
            elapsed = (datetime.now() - started).total_seconds() * 1000
            exit_code = proc.returncode or 0
            return ExternalAgentRunResult(
                session_id="",
                provider="",
                success=exit_code == 0,
                exit_code=exit_code,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                error="" if exit_code == 0 else stderr.decode("utf-8", errors="replace"),
                duration_ms=elapsed,
            )
        except FileNotFoundError:
            elapsed = (datetime.now() - started).total_seconds() * 1000
            return ExternalAgentRunResult(
                session_id="",
                provider="",
                success=False,
                exit_code=-1,
                error=f"Command not found: {command[0]}",
                stderr=f"Command not found: {command[0]}",
                duration_ms=elapsed,
            )

    def _record_result(
        self,
        session: ExternalAgentSession,
        result: ExternalAgentRunResult,
    ) -> None:
        now = datetime.now()
        session.updated_at = now
        session.last_run_at = now
        self.audit.append(result)
        self._save_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.sessions = {
                item["session_id"]: ExternalAgentSession(**item)
                for item in data.get("sessions", [])
            }
            self.audit = [ExternalAgentRunResult(**item) for item in data.get("audit", [])]
        except Exception:
            self.sessions = {}
            self.audit = []

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": [session.model_dump(mode="json") for session in self.sessions.values()],
            "audit": [record.model_dump(mode="json") for record in self.audit[-200:]],
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_external_agent_providers() -> list[ExternalAgentProvider]:
    """Discover supported CLIs without failing when they are absent."""
    return [
        _provider_from_path(
            provider_id="codex",
            display_name="Codex",
            executable="codex",
            capabilities=[
                "local_repo_control",
                "session_resume",
                "sandbox_mode",
                "approval_policy",
                "mcp_bridge",
            ],
            notes="OpenAI Codex CLI runtime; Symbio controls prompts, policy, and audit.",
        ),
        _provider_from_path(
            provider_id="claude-code",
            display_name="Claude Code",
            executable="claude",
            capabilities=[
                "local_repo_control",
                "session_resume",
                "allowed_tools",
                "permission_mode",
                "mcp_bridge",
            ],
            notes="Anthropic Claude Code CLI runtime; Symbio registers and resumes sessions.",
        ),
    ]


def build_external_agent_command(
    provider: ExternalAgentProvider,
    session: ExternalAgentSession,
    request: ExternalAgentRunRequest,
) -> list[str]:
    prompt = request.prompt.strip()
    if not prompt:
        raise ValueError("Prompt is required")
    # 用 shutil.which 解析出的完整路径（含扩展名）作为可执行文件：Windows 上 npm
    # 装的 CLI 是 claude.CMD/codex.CMD，裸名 "claude" 经 CreateProcess 找不到。
    executable = provider.path or provider.executable
    if provider.provider_id == "codex":
        model = request.model or session.model
        # codex-cli ≥0.40 起：`codex exec` 去掉了 --approval-policy（exec 非交互
        # 本就不弹审批），且续接从 `--resume <id>` 改成了子命令 `exec resume <id>`。
        # 旧的拼法会触发 "unexpected argument '--approval-policy'" 退出码 2。
        if session.external_session_id:
            command = [
                executable,
                "exec",
                "resume",
                session.external_session_id,
                "--skip-git-repo-check",
            ]
            if model:
                command.extend(["--model", model])
            command.append(prompt)
            return command

        command = [
            executable,
            "exec",
            "--cd",
            session.workspace,
            "--sandbox",
            request.sandbox_mode or session.sandbox_mode,
            "--skip-git-repo-check",
        ]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    if provider.provider_id == "claude-code":
        # --output-format json：拿到结构化输出，含 session_id（用于后续 --resume
        # 原生多轮记忆）与干净的 result 文本（run_session 里解包回 stdout）。
        command = [executable, "-p", prompt, "--output-format", "json"]
        model = request.model or session.model
        if model:
            command.extend(["--model", model])
        permission_mode = request.permission_mode or session.permission_mode
        if permission_mode:
            command.extend(["--permission-mode", permission_mode])
        if session.external_session_id:
            command.extend(["--resume", session.external_session_id])
        return command

    raise ValueError(f"Unsupported external agent provider: {provider.provider_id}")


def _provider_from_path(
    *,
    provider_id: str,
    display_name: str,
    executable: str,
    capabilities: list[str],
    notes: str,
) -> ExternalAgentProvider:
    path = shutil.which(executable) or ""
    return ExternalAgentProvider(
        provider_id=provider_id,
        display_name=display_name,
        executable=executable,
        installed=bool(path),
        path=path,
        capabilities=capabilities,
        notes=notes,
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
