"""Computer Use 最小闭环测试：会话、动作、规划、审计、回放、API。"""

from pathlib import Path
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.tools.computer_use import (
    ActionPlanner,
    ComputerUseManager,
    ComputerUseSession,
    LLMActionPlanner,
    get_computer_use_manager,
    reset_computer_use_manager,
)
from symbio.interfaces.api import app


@pytest.fixture(autouse=True)
def _fresh_manager():
    reset_computer_use_manager()
    yield
    reset_computer_use_manager()


# ---------------------------------------------------------------------------
# 会话与动作
# ---------------------------------------------------------------------------

_NETWORK_ERR_MARKERS = ("net::ERR", "Timeout", "timeout", "ECONNRESET")


def _skip_if_transient_network_error(step: dict):
    """真实 Playwright 模式下，外网抖动（代理切换等）不是被测逻辑的失败。"""
    err = step.get("error") or ""
    if not step["success"] and any(m in err for m in _NETWORK_ERR_MARKERS):
        pytest.skip(f"transient network error in real-browser mode: {err[:120]}")


@pytest.mark.asyncio
async def test_session_records_audit_steps():
    session = ComputerUseSession("cu-test")
    step = await session.act("navigate", {"url": "https://example.com"})
    _skip_if_transient_network_error(step)
    assert step["success"] is True
    assert step["action"] == "navigate"
    assert len(session.steps) == 1
    assert session.steps[0]["url"]


@pytest.mark.asyncio
async def test_unknown_action_fails_gracefully():
    session = ComputerUseSession("cu-test")
    step = await session.act("teleport", {})
    assert step["success"] is False
    assert "未知动作" in step["error"]


@pytest.mark.asyncio
async def test_navigate_updates_current_url_in_dry_run():
    session = ComputerUseSession("cu-test")
    assert session.dry_run in (True, False)
    await session.act("navigate", {"url": "https://symbio.ai"})
    if session.dry_run:
        assert session.current_url == "https://symbio.ai"


# ---------------------------------------------------------------------------
# 规划器闭环
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_drives_recon_sequence():
    session = ComputerUseSession("cu-test")
    goal = "open https://example.com and read it"
    actions = []
    for _ in range(4):
        plan = ActionPlanner.plan(goal, session)
        actions.append(plan["action"])
        await session.act(plan["action"], plan["params"])
    # 标准侦察序列：navigate -> screenshot -> extract_text
    assert actions[0] == "navigate"
    assert "screenshot" in actions
    assert "extract_text" in actions


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_reexecutes_recorded_steps():
    session = ComputerUseSession("cu-test")
    await session.act("navigate", {"url": "https://example.com"})
    await session.act("screenshot", {})
    report = await session.replay()
    assert report["replayed_steps"] == 2
    # dry-run 下两步都应成功
    if session.dry_run:
        assert report["succeeded"] == 2


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manager_lifecycle(tmp_path):
    mgr = ComputerUseManager(audit_dir=tmp_path)
    session = mgr.create_session(start_url="https://example.com")
    sid = session.session_id
    assert mgr.get(sid) is not None
    assert len(mgr.list_sessions()) == 1
    await session.act("screenshot", {})
    ok = await mgr.close_session(sid)
    assert ok is True
    assert mgr.get(sid) is None
    # 审计被持久化
    assert (tmp_path / f"{sid}_audit.json").exists()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_computer_use_api_flow():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/computer-use/sessions", json={"start_url": "https://example.com"})
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        resp = await client.get("/api/computer-use/sessions")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

        resp = await client.post(f"/api/computer-use/sessions/{sid}/act",
                                 json={"action": "navigate", "params": {"url": "https://example.com"}})
        assert resp.status_code == 200
        _skip_if_transient_network_error(resp.json()["step"])
        assert resp.json()["step"]["success"] is True

        resp = await client.post(f"/api/computer-use/sessions/{sid}/plan",
                                 json={"goal": "read https://example.com", "auto_execute": True})
        assert resp.status_code == 200
        assert resp.json()["plan"]["action"]

        resp = await client.get(f"/api/computer-use/sessions/{sid}")
        assert resp.status_code == 200
        assert "steps" in resp.json()

        resp = await client.post(f"/api/computer-use/sessions/{sid}/replay")
        assert resp.status_code == 200
        assert "replayed_steps" in resp.json()

        resp = await client.delete(f"/api/computer-use/sessions/{sid}")
        assert resp.status_code == 200

        resp = await client.get(f"/api/computer-use/sessions/{sid}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# LLM 动作规划器
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_planner_parses_structured_action():
    async def fake_complete(system, user):
        return '{"action": "navigate", "params": {"url": "https://symbio.ai"}, "reason": "打开目标站点"}'
    planner = LLMActionPlanner(complete=fake_complete)
    session = ComputerUseSession("cu-test")
    plan = await planner.plan("打开 symbio.ai 看看", session)
    assert plan["action"] == "navigate"
    assert plan["params"]["url"] == "https://symbio.ai"
    assert plan["planner"] == "llm"


@pytest.mark.asyncio
async def test_llm_planner_tolerates_code_fence():
    async def fake_complete(system, user):
        return '```json\n{"action": "screenshot", "params": {}, "reason": "看页面"}\n```'
    planner = LLMActionPlanner(complete=fake_complete)
    plan = await planner.plan("理解页面", ComputerUseSession("cu-test"))
    assert plan["action"] == "screenshot"
    assert plan["planner"] == "llm"


@pytest.mark.asyncio
async def test_llm_planner_done_maps_to_wait_done():
    async def fake_complete(system, user):
        return '{"action": "done", "reason": "目标已完成"}'
    plan = await LLMActionPlanner(complete=fake_complete).plan("x", ComputerUseSession("cu-test"))
    assert plan["action"] == "wait"
    assert plan.get("done") is True


@pytest.mark.asyncio
async def test_llm_planner_falls_back_on_garbage():
    async def fake_complete(system, user):
        return "对不起我不知道怎么办"
    session = ComputerUseSession("cu-test")
    plan = await LLMActionPlanner(complete=fake_complete).plan("open https://x.com", session)
    # 回退启发式：目标含 URL -> navigate
    assert plan["action"] == "navigate"
    assert plan["planner"] == "heuristic-fallback"


@pytest.mark.asyncio
async def test_llm_planner_falls_back_when_no_llm():
    # complete=None 且默认无 anthropic key -> 回退启发式
    from symbio.config.settings import get_settings
    if get_settings().model.anthropic_api_key:
        pytest.skip("环境配置了真实 anthropic key")
    plan = await LLMActionPlanner().plan("open https://x.com", ComputerUseSession("cu-test"))
    assert plan["action"] == "navigate"
    assert plan["planner"] == "heuristic"


@pytest.mark.asyncio
async def test_plan_api_use_llm_flag(monkeypatch):
    # 注入假的 LLM 补全，验证 /plan?use_llm 走 LLM 路径
    import symbio.tools.computer_use as cu

    async def fake_complete(system, user):
        return '{"action": "screenshot", "params": {}, "reason": "mock"}'
    monkeypatch.setattr(cu.LLMActionPlanner, "_default_complete", lambda self: fake_complete)

    mgr = get_computer_use_manager()
    session = mgr.create_session()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/computer-use/sessions/{session.session_id}/plan",
            json={"goal": "理解页面", "auto_execute": False, "use_llm": True},
        )
        assert resp.status_code == 200
        assert resp.json()["plan"]["planner"] == "llm"
        assert resp.json()["plan"]["action"] == "screenshot"


@pytest.mark.asyncio
async def test_act_on_missing_session_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/computer-use/sessions/nope/act",
                                 json={"action": "screenshot", "params": {}})
        assert resp.status_code == 404
