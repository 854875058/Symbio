from pathlib import Path
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app
from symbio.tools.external_agents import (
    ExternalAgentController,
    ExternalAgentProvider,
    ExternalAgentRunRequest,
    ExternalAgentRunResult,
    ExternalAgentSessionCreate,
    _clean_subprocess_env,
    _extract_claude_json,
)


def test_extract_claude_json_parses_result_and_session_id():
    stdout = 'Active code page: 65001\n{"type":"result","result":"HI","session_id":"abc-123"}'
    text, sid = _extract_claude_json(stdout)
    assert text == "HI"
    assert sid == "abc-123"
    # 非 JSON 原样返回、session 为空
    assert _extract_claude_json("just text no json") == ("just text no json", "")
    assert _extract_claude_json("") == ("", "")


@pytest.mark.asyncio
async def test_run_session_captures_claude_session_id_then_resumes(tmp_path):
    controller = ExternalAgentController(
        state_path=tmp_path / "ext.json",
        workspace_root=tmp_path,
        providers=[
            ExternalAgentProvider(
                provider_id="claude-code",
                display_name="Claude Code",
                executable="claude",
                path="claude",
                installed=True,
            )
        ],
    )
    session = controller.create_session(
        ExternalAgentSessionCreate(provider="claude-code", workspace=str(tmp_path))
    )

    async def fake_execute(command, cwd, timeout):
        return ExternalAgentRunResult(
            session_id="",
            provider="",
            success=True,
            exit_code=0,
            stdout='Active code page: 65001\n{"result":"done-1","session_id":"sess-xyz"}',
        )

    controller._execute = fake_execute

    r1 = await controller.run_session(session.session_id, ExternalAgentRunRequest(prompt="第一步"))
    assert r1.stdout == "done-1"  # 解包出干净文本
    assert controller.get_session(session.session_id).external_session_id == "sess-xyz"  # 回填

    captured = {}

    async def fake_execute2(command, cwd, timeout):
        captured["cmd"] = command
        return ExternalAgentRunResult(
            session_id="",
            provider="",
            success=True,
            exit_code=0,
            stdout='{"result":"done-2","session_id":"sess-xyz"}',
        )

    controller._execute = fake_execute2

    r2 = await controller.run_session(session.session_id, ExternalAgentRunRequest(prompt="第二步"))
    assert "--resume" in captured["cmd"] and "sess-xyz" in captured["cmd"]  # 后续轮续接
    assert r2.stdout == "done-2"


def test_clean_subprocess_env_strips_host_auth(monkeypatch):
    # 模拟宿主（Claude Code 运行时）注入的会破坏子 CLI 的变量
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "host-session-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-6")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-4-6")
    monkeypatch.setenv("PATH_KEEP_ME", "yes")

    env = _clean_subprocess_env()

    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
    # 其它环境变量保留，外部 CLI 仍能正常运行
    assert env.get("PATH_KEEP_ME") == "yes"


def test_external_agent_controller_registers_existing_codex_session(tmp_path):
    controller = ExternalAgentController(
        state_path=tmp_path / "external-agents.json",
        workspace_root=tmp_path,
        providers=[
            ExternalAgentProvider(
                provider_id="codex",
                display_name="Codex",
                executable="codex",
                installed=False,
            )
        ],
    )

    session = controller.create_session(
        ExternalAgentSessionCreate(
            provider="codex",
            label="existing codex",
            workspace=str(tmp_path),
            external_session_id="codex-thread-1",
            sandbox_mode="workspace-write",
            approval_policy="on-request",
        )
    )

    assert session.provider == "codex"
    assert session.external_session_id == "codex-thread-1"
    assert session.status == "registered"
    assert controller.list_sessions()[0].session_id == session.session_id


@pytest.mark.asyncio
async def test_external_agent_codex_command_preview_uses_session_and_policy(tmp_path):
    controller = ExternalAgentController(
        state_path=tmp_path / "external-agents.json",
        workspace_root=tmp_path,
        providers=[
            ExternalAgentProvider(
                provider_id="codex",
                display_name="Codex",
                executable="codex",
                installed=True,
            )
        ],
    )
    # 无历史会话：走全新 `codex exec`
    fresh = controller.create_session(
        ExternalAgentSessionCreate(
            provider="codex",
            workspace=str(tmp_path),
            sandbox_mode="workspace-write",
            approval_policy="on-failure",
        )
    )
    fresh_result = await controller.run_session(
        fresh.session_id,
        ExternalAgentRunRequest(prompt="全新任务", dry_run=True),
    )
    assert fresh_result.success is True
    assert fresh_result.dry_run is True
    assert fresh_result.command[:3] == ["codex", "exec", "--cd"]
    assert "--sandbox" in fresh_result.command
    assert "workspace-write" in fresh_result.command
    assert "--skip-git-repo-check" in fresh_result.command
    # codex-cli 新版 exec 不再接受 --approval-policy（会退出码 2）
    assert "--approval-policy" not in fresh_result.command
    assert fresh_result.command[-1] == "全新任务"

    # 有历史会话：走 `codex exec resume <id>` 子命令（不再是 --resume 选项）
    resumed = controller.create_session(
        ExternalAgentSessionCreate(
            provider="codex",
            workspace=str(tmp_path),
            external_session_id="thread-123",
            sandbox_mode="workspace-write",
            approval_policy="on-failure",
        )
    )
    result = await controller.run_session(
        resumed.session_id,
        ExternalAgentRunRequest(prompt="继续修复测试", dry_run=True),
    )

    assert result.success is True
    assert result.dry_run is True
    assert result.command[:4] == ["codex", "exec", "resume", "thread-123"]
    assert "--approval-policy" not in result.command
    assert "--resume" not in result.command
    assert result.command[-1] == "继续修复测试"


@pytest.mark.asyncio
async def test_external_agent_claude_dry_run_command_uses_registered_session(tmp_path):
    controller = ExternalAgentController(
        state_path=tmp_path / "external-agents.json",
        workspace_root=tmp_path,
        providers=[
            ExternalAgentProvider(
                provider_id="claude-code",
                display_name="Claude Code",
                executable="claude",
                installed=True,
            )
        ],
    )
    session = controller.create_session(
        ExternalAgentSessionCreate(
            provider="claude-code",
            workspace=str(tmp_path),
            external_session_id="claude-session-1",
            permission_mode="default",
        )
    )

    result = await controller.run_session(
        session.session_id,
        ExternalAgentRunRequest(prompt="review this project", dry_run=True),
    )

    assert result.success is True
    assert result.command[:2] == ["claude", "-p"]
    assert "--resume" in result.command
    assert "claude-session-1" in result.command
    assert result.session_id == session.session_id
    assert controller.list_audit()[0].provider == "claude-code"


@pytest.mark.asyncio
async def test_command_uses_resolved_full_path_when_available(tmp_path):
    # 发现到的 CLI 带完整路径（Windows 上是 claude.CMD）→ 命令用完整路径，
    # 否则 CreateProcess 找不到裸名 "claude"
    controller = ExternalAgentController(
        state_path=tmp_path / "external-agents.json",
        workspace_root=tmp_path,
        providers=[
            ExternalAgentProvider(
                provider_id="claude-code",
                display_name="Claude Code",
                executable="claude",
                path=r"C:\Users\x\AppData\Roaming\npm\claude.CMD",
                installed=True,
            )
        ],
    )
    session = controller.create_session(
        ExternalAgentSessionCreate(provider="claude-code", workspace=str(tmp_path))
    )
    result = await controller.run_session(
        session.session_id, ExternalAgentRunRequest(prompt="hi", dry_run=True)
    )
    assert result.command[0] == r"C:\Users\x\AppData\Roaming\npm\claude.CMD"
    assert result.command[1] == "-p"


@pytest.mark.asyncio
async def test_external_agent_api_lists_providers_and_runs_dry_run(tmp_path):
    previous_controller = getattr(app.state, "external_agent_controller", None)
    app.state.external_agent_controller = ExternalAgentController(
        state_path=tmp_path / "external-agents.json",
        workspace_root=tmp_path,
        providers=[
            ExternalAgentProvider(
                provider_id="codex",
                display_name="Codex",
                executable="codex",
                installed=True,
            )
        ],
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            providers = await client.get("/api/external-agents/providers")
            created = await client.post(
                "/api/external-agents/sessions",
                json={
                    "provider": "codex",
                    "workspace": str(tmp_path),
                    "external_session_id": "existing",
                },
            )
            session_id = created.json()["session"]["session_id"]
            run = await client.post(
                f"/api/external-agents/sessions/{session_id}/run",
                json={"prompt": "hello", "dry_run": True},
            )

        assert providers.status_code == 200
        assert providers.json()["providers"][0]["provider_id"] == "codex"
        assert created.status_code == 200
        assert run.status_code == 200
        assert run.json()["result"]["dry_run"] is True
    finally:
        if previous_controller is not None:
            app.state.external_agent_controller = previous_controller
        else:
            delattr(app.state, "external_agent_controller")
