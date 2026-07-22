import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app
from symbio.tools.sandbox import (
    ApprovalPolicy,
    PermissionLevel,
    SandboxAccessMode,
    SandboxExecutor,
    SandboxPolicy,
    normalize_approval_policy,
    normalize_sandbox_access_mode,
)


def test_codex_style_policy_names_are_canonicalized():
    assert normalize_sandbox_access_mode("workspace_write") == SandboxAccessMode.WORKSPACE_WRITE
    assert normalize_sandbox_access_mode("workspace-write") == SandboxAccessMode.WORKSPACE_WRITE
    assert normalize_sandbox_access_mode("unrestricted") == SandboxAccessMode.DANGER_FULL_ACCESS
    assert (
        normalize_sandbox_access_mode("danger-full-access") == SandboxAccessMode.DANGER_FULL_ACCESS
    )
    assert normalize_approval_policy("on_request") == ApprovalPolicy.ON_REQUEST
    assert normalize_approval_policy("on-request") == ApprovalPolicy.ON_REQUEST
    assert normalize_approval_policy("on_failure") == ApprovalPolicy.ON_FAILURE


@pytest.mark.asyncio
async def test_sandbox_policy_blocks_working_dir_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    executor = SandboxExecutor(default_working_dir=str(workspace))
    policy = SandboxPolicy(
        access_mode=SandboxAccessMode.WORKSPACE_WRITE,
        workspace_roots=[str(workspace)],
        writable_roots=[str(workspace)],
        approval_policy=ApprovalPolicy.NEVER,
    )

    result = await executor.execute_with_policy(
        "python -c \"print('nope')\"",
        policy=policy,
        permission_level=PermissionLevel.EXECUTE,
        working_dir=str(outside),
    )

    assert result.exit_code == -1
    assert result.error_message.startswith("WORKSPACE_VIOLATION")
    assert result.metadata["policy"]["access_mode"] == "workspace-write"


@pytest.mark.asyncio
async def test_sandbox_policy_requires_approval_for_execute_without_approval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = SandboxExecutor(default_working_dir=str(workspace))
    policy = SandboxPolicy(
        access_mode=SandboxAccessMode.WORKSPACE_WRITE,
        workspace_roots=[str(workspace)],
        writable_roots=[str(workspace)],
        approval_policy=ApprovalPolicy.ON_REQUEST,
    )

    result = await executor.execute_with_policy(
        "python -c \"print('blocked')\"",
        policy=policy,
        permission_level=PermissionLevel.EXECUTE,
        working_dir=str(workspace),
    )

    assert result.exit_code == -1
    assert result.error_message.startswith("APPROVAL_REQUIRED")
    assert result.metadata["approval_required"] is True


@pytest.mark.asyncio
async def test_sandbox_policy_blocks_network_commands_when_network_disabled(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = SandboxExecutor(default_working_dir=str(workspace))
    policy = SandboxPolicy(
        access_mode=SandboxAccessMode.WORKSPACE_WRITE,
        workspace_roots=[str(workspace)],
        writable_roots=[str(workspace)],
        approval_policy=ApprovalPolicy.NEVER,
        allow_network=False,
    )

    result = await executor.execute_with_policy(
        "curl https://example.com",
        policy=policy,
        permission_level=PermissionLevel.EXECUTE,
        working_dir=str(workspace),
        approved=True,
    )

    assert result.exit_code == -1
    assert result.error_message.startswith("NETWORK_BLOCKED")


@pytest.mark.asyncio
async def test_on_failure_runs_inside_workspace_without_preapproval(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = SandboxExecutor(default_working_dir=str(workspace))
    policy = SandboxPolicy(
        access_mode=SandboxAccessMode.WORKSPACE_WRITE,
        workspace_roots=[str(workspace)],
        writable_roots=[str(workspace)],
        approval_policy=ApprovalPolicy.ON_FAILURE,
    )

    result = await executor.execute_with_policy(
        "python -c \"print('inside')\"",
        policy=policy,
        permission_level=PermissionLevel.EXECUTE,
        working_dir=str(workspace),
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "inside"
    assert result.metadata["approval_required"] is False


@pytest.mark.asyncio
async def test_on_failure_requires_approval_when_command_crosses_sandbox_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    executor = SandboxExecutor(default_working_dir=str(workspace))
    policy = SandboxPolicy(
        access_mode=SandboxAccessMode.WORKSPACE_WRITE,
        workspace_roots=[str(workspace)],
        writable_roots=[str(workspace)],
        approval_policy=ApprovalPolicy.ON_FAILURE,
    )

    result = await executor.execute_with_policy(
        "python -c \"print('outside')\"",
        policy=policy,
        permission_level=PermissionLevel.EXECUTE,
        working_dir=str(outside),
    )

    assert result.exit_code == -1
    assert result.error_message.startswith("APPROVAL_REQUIRED")
    assert result.metadata["sandbox_violation"] is True


@pytest.mark.asyncio
async def test_danger_full_access_requires_approval_and_then_runs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    executor = SandboxExecutor(default_working_dir=str(tmp_path / "workspace"))
    policy = SandboxPolicy(
        access_mode=SandboxAccessMode.DANGER_FULL_ACCESS,
        approval_policy=ApprovalPolicy.ON_REQUEST,
    )

    blocked = await executor.execute_with_policy(
        "python -c \"print('no')\"",
        policy=policy,
        permission_level=PermissionLevel.READ_ONLY,
        working_dir=str(outside),
    )
    approved = await executor.execute_with_policy(
        "python -c \"print('yes')\"",
        policy=policy,
        permission_level=PermissionLevel.READ_ONLY,
        working_dir=str(outside),
        approved=True,
    )

    assert blocked.error_message.startswith("APPROVAL_REQUIRED")
    assert approved.exit_code == 0
    assert approved.stdout.strip() == "yes"
    assert approved.metadata["policy"]["access_mode"] == "danger-full-access"


@pytest.mark.asyncio
async def test_sandbox_policy_executes_approved_command_and_records_audit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = SandboxExecutor(default_working_dir=str(workspace))
    policy = SandboxPolicy(
        access_mode=SandboxAccessMode.WORKSPACE_WRITE,
        workspace_roots=[str(workspace)],
        writable_roots=[str(workspace)],
        approval_policy=ApprovalPolicy.ON_REQUEST,
    )

    result = await executor.execute_with_policy(
        "python -c \"print('ok')\"",
        policy=policy,
        permission_level=PermissionLevel.EXECUTE,
        working_dir=str(workspace),
        approved=True,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
    assert result.metadata["policy"]["access_mode"] == "workspace-write"
    assert executor.audit_records[-1].exit_code == 0
    assert executor.audit_records[-1].approved is True


@pytest.mark.asyncio
async def test_sandbox_api_blocks_unapproved_execute_and_exposes_audit(tmp_path):
    previous_executor = getattr(app.state, "sandbox_executor", None)
    previous_workspace = getattr(app.state, "sandbox_workspace_root", None)
    app.state.sandbox_executor = None
    app.state.sandbox_workspace_root = str(tmp_path)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            exec_resp = await client.post(
                "/api/sandbox/execute",
                json={
                    "command": "python -c \"print('blocked')\"",
                    "permission_level": "execute",
                    "working_dir": str(tmp_path),
                    "approval_policy": "on_request",
                    "approved": False,
                },
            )
            audit_resp = await client.get("/api/sandbox/audit")

        assert exec_resp.status_code == 200
        data = exec_resp.json()
        assert data["approval_required"] is True
        assert data["result"]["exit_code"] == -1
        assert audit_resp.status_code == 200
        assert audit_resp.json()["total"] >= 1
    finally:
        if previous_executor is not None:
            app.state.sandbox_executor = previous_executor
        elif hasattr(app.state, "sandbox_executor"):
            delattr(app.state, "sandbox_executor")
        if previous_workspace is not None:
            app.state.sandbox_workspace_root = previous_workspace
        elif hasattr(app.state, "sandbox_workspace_root"):
            delattr(app.state, "sandbox_workspace_root")
