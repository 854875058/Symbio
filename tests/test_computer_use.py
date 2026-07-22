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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
@pytest.mark.asyncio
async def test_computer_use_api_flow():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/computer-use/sessions", json={"start_url": "https://example.com"}
        )
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        resp = await client.get("/api/computer-use/sessions")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

        resp = await client.post(
            f"/api/computer-use/sessions/{sid}/act",
            json={"action": "navigate", "params": {"url": "https://example.com"}},
        )
        assert resp.status_code == 200
        _skip_if_transient_network_error(resp.json()["step"])
        assert resp.json()["step"]["success"] is True

        resp = await client.post(
            f"/api/computer-use/sessions/{sid}/plan",
            json={"goal": "read https://example.com", "auto_execute": True},
        )
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
        resp = await client.post(
            "/api/computer-use/sessions/nope/act", json={"action": "screenshot", "params": {}}
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# VLM 视觉规划
# ---------------------------------------------------------------------------

# 1x1 透明 PNG 的最小合法字节（用于让 latest_screenshot/_image_to_block 拿到真图）
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100"
    "05fe02fea7"
    "00000000"
    "49454e44ae426082"
)


def _make_session_with_screenshot(tmp_path) -> ComputerUseSession:
    """构造一个带真实截图文件的会话（绕过 Playwright，直接写审计步）。"""
    session = ComputerUseSession("cu-vision")
    png = tmp_path / "shot.png"
    png.write_bytes(_TINY_PNG)
    session._record(0, "screenshot", {}, True, result={"screenshot": str(png)})
    return session


def test_image_to_block_produces_base64(tmp_path):
    from symbio.tools.computer_use import _image_to_block

    png = tmp_path / "s.png"
    png.write_bytes(_TINY_PNG)
    block = _image_to_block(str(png))
    assert block is not None
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert len(block["source"]["data"]) > 0


def test_image_to_block_missing_file_returns_none():
    from symbio.tools.computer_use import _image_to_block

    assert _image_to_block("no/such/file.png") is None


def test_latest_screenshot_finds_recorded_shot(tmp_path):
    session = _make_session_with_screenshot(tmp_path)
    assert session.latest_screenshot() is not None
    assert Path(session.latest_screenshot()).is_file()


@pytest.mark.asyncio
async def test_vision_plan_feeds_screenshot_to_model(tmp_path):
    """核心：use_vision 时截图 base64 确实进了传给模型的 image_path，且回坐标动作。"""
    session = _make_session_with_screenshot(tmp_path)
    captured = {}

    async def fake_complete(system, user, image_path=None):
        captured["image_path"] = image_path
        captured["system"] = system
        return '{"action": "click", "params": {"x": 42, "y": 99}, "reason": "看到按钮"}'

    plan = await LLMActionPlanner(complete=fake_complete).plan("点击登录", session, use_vision=True)
    # 图确实喂进去了
    assert captured["image_path"] == session.latest_screenshot()
    # 视觉提示进了 system
    assert "视觉模式" in captured["system"]
    # 产出坐标动作，planner 标记为 vlm
    assert plan["planner"] == "vlm"
    assert plan["action"] == "click"
    assert plan["params"] == {"x": 42, "y": 99}


@pytest.mark.asyncio
async def test_vision_plan_without_screenshot_falls_back_to_text(tmp_path):
    """没有截图时 use_vision 也不带图（image_path=None），planner 退为 llm 文本模式。"""
    session = ComputerUseSession("cu-no-shot")
    seen = {}

    async def fake_complete(system, user, image_path=None):
        seen["image_path"] = image_path
        return '{"action": "screenshot", "params": {}, "reason": "先看看"}'

    plan = await LLMActionPlanner(complete=fake_complete).plan("打开页面", session, use_vision=True)
    assert seen["image_path"] is None
    assert plan["planner"] == "llm"


@pytest.mark.asyncio
async def test_vision_plan_tolerates_two_arg_backend(tmp_path):
    """旧的两参 complete 后端在视觉模式下自动降级为纯文本，不报错。"""
    session = _make_session_with_screenshot(tmp_path)

    async def old_complete(system, user):  # 不接受 image_path
        return '{"action": "wait", "params": {"ms": 0}, "reason": "ok"}'

    plan = await LLMActionPlanner(complete=old_complete).plan("x", session, use_vision=True)
    assert plan["action"] == "wait"


@pytest.mark.asyncio
async def test_plan_api_use_vision_flag(monkeypatch, tmp_path):
    """/plan use_vision=True：无截图时先自动截图，再带图规划。"""
    import symbio.tools.computer_use as cu

    got = {}

    async def fake_complete(system, user, image_path=None):
        got["image_path"] = image_path
        return '{"action": "click", "params": {"x": 5, "y": 6}, "reason": "mock"}'

    monkeypatch.setattr(cu.LLMActionPlanner, "_default_complete", lambda self: fake_complete)

    mgr = get_computer_use_manager()
    session = mgr.create_session()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/computer-use/sessions/{session.session_id}/plan",
            json={"goal": "点击", "auto_execute": False, "use_llm": True, "use_vision": True},
        )
    assert resp.status_code == 200
    body = resp.json()
    # dry-run 环境下 screenshot 无真实文件 → 视觉取不到图，planner 应为 llm（文本）
    # 若有真实截图（装了 Playwright）则为 vlm；两种都接受，关键是接口链路通
    assert body["plan"]["planner"] in ("vlm", "llm")
    assert body["plan"]["action"] == "click"
