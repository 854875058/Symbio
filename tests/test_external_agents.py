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
    ExternalAgentSessionCreate,
)


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
    session = controller.create_session(
        ExternalAgentSessionCreate(
            provider="codex",
            workspace=str(tmp_path),
            external_session_id="thread-123",
            sandbox_mode="workspace-write",
            approval_policy="on-failure",
        )
    )

    result = await controller.run_session(
        session.session_id,
        ExternalAgentRunRequest(prompt="继续修复测试", dry_run=True),
    )

    assert result.success is True
    assert result.dry_run is True
    assert result.command[:3] == ["codex", "exec", "--cd"]
    assert "--sandbox" in result.command
    assert "workspace-write" in result.command
    assert "--approval-policy" in result.command
    assert "on-failure" in result.command
    assert "--resume" in result.command
    assert "thread-123" in result.command


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
