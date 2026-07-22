"""单 Agent 接入 Claude Code / Codex 测试。"""

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.agents.builtin.external_backed_agent import (
    ClaudeCodeAgent,
    CodexAgent,
)
from symbio.agents.registry import get_registry
from symbio.utils.types import Intent, Task


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def test_agents_registered():
    reg = get_registry()
    assert reg.get("claude-code") is not None
    assert reg.get("codex") is not None
    assert ClaudeCodeAgent().provider == "claude-code"
    assert CodexAgent().provider == "codex"


# ---------------------------------------------------------------------------
# 注入 controller 的执行
# ---------------------------------------------------------------------------


class _FakeRunResult:
    def __init__(self, *, success=True, stdout="done", error="", exit_code=0, dry_run=False):
        self.run_id = "run-1"
        self.success = success
        self.stdout = stdout
        self.error = error
        self.exit_code = exit_code
        self.dry_run = dry_run
        self.command = ["claude", "-p", "x"]


class _FakeController:
    def __init__(self, result):
        self._result = result
        self.created = []
        self.ran = []

    def create_session(self, request):
        self.created.append(request)

        class _S:
            session_id = "sess-1"

        return _S()

    async def run_session(self, session_id, request):
        self.ran.append((session_id, request))
        return self._result


@pytest.mark.asyncio
async def test_execute_success_via_injected_controller():
    ctrl = _FakeController(_FakeRunResult(success=True, stdout="已新增 hello 函数"))
    agent = ClaudeCodeAgent(controller=ctrl, workspace=".")
    result = await agent.execute(Task(intent=Intent(raw_text="加个 hello 函数")))
    assert result.success
    assert result.content == "已新增 hello 函数"
    assert result.data["provider"] == "claude-code"
    assert result.data["backend"] == "external-agent-cli"
    assert ctrl.created and ctrl.ran


@pytest.mark.asyncio
async def test_execute_failure_maps_to_unsuccessful_result():
    ctrl = _FakeController(_FakeRunResult(success=False, stdout="", error="CLI 未安装"))
    agent = CodexAgent(controller=ctrl)
    result = await agent.execute(Task(intent=Intent(raw_text="do x")))
    assert result.success is False
    assert "CLI 未安装" in result.content


@pytest.mark.asyncio
async def test_session_reused_across_runs():
    ctrl = _FakeController(_FakeRunResult())
    agent = ClaudeCodeAgent(controller=ctrl)
    await agent.execute(Task(intent=Intent(raw_text="a")))
    await agent.execute(Task(intent=Intent(raw_text="b")))
    # 只创建一次会话，第二次复用
    assert len(ctrl.created) == 1
    assert len(ctrl.ran) == 2


@pytest.mark.asyncio
async def test_prompt_includes_workflow_and_memory_context():
    captured = {}

    class _CapCtrl(_FakeController):
        async def run_session(self, session_id, request):
            captured["prompt"] = request.prompt
            return self._result

    ctrl = _CapCtrl(_FakeRunResult())
    agent = ClaudeCodeAgent(controller=ctrl)
    task = Task(
        intent=Intent(raw_text="核心问题"),
        metadata={
            "workflow_guidance": "先规划再执行",
            "memory_context": "生产需审批",
        },
    )
    await agent.execute(task)
    assert "先规划再执行" in captured["prompt"]
    assert "生产需审批" in captured["prompt"]
    assert "核心问题" in captured["prompt"]


# ---------------------------------------------------------------------------
# dry-run（真实 controller，不需要 CLI 安装）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_with_real_controller(tmp_path, monkeypatch):
    # 用真实 ExternalAgentController，工作区设为项目根的子目录以通过边界校验
    from symbio.tools.external_agents import ExternalAgentController

    work = PROJECT_ROOT  # 项目根本身在 workspace_root 内
    ctrl = ExternalAgentController(workspace_root=str(work))
    agent = ClaudeCodeAgent(controller=ctrl, workspace=".")
    result = await agent.execute(
        Task(intent=Intent(raw_text="加个函数"), metadata={"dry_run": True})
    )
    assert result.success
    assert "Dry run" in result.content
    assert result.data["dry_run"] is True
