"""个人微信双向 bridge 测试：分类、出站发送、inbound 对话/审批路由、鉴权。"""

from pathlib import Path
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.config.settings import Settings
from symbio.interfaces.wechat_bridge import (
    WeChatBridge,
    WeChatInbound,
    get_wechat_bridge,
    reset_wechat_bridge,
)
import symbio.interfaces.api as api
from symbio.interfaces.api import ChatResponse, app


@pytest.fixture(autouse=True)
def _fresh_bridge():
    reset_wechat_bridge()
    yield
    reset_wechat_bridge()


def _enabled_settings(**wechat_kwargs):
    s = Settings()
    s.wechat.enabled = True
    for k, v in wechat_kwargs.items():
        setattr(s.wechat, k, v)
    return s


# ---------------------------------------------------------------------------
# 分类与出站
# ---------------------------------------------------------------------------

def test_classify_approval_command():
    bridge = WeChatBridge()
    kind, parsed = bridge.classify("同意 REQ-12345")
    assert kind == "approval"
    assert parsed.action == "approve"
    assert parsed.request_id == "REQ-12345"


def test_classify_plain_chat():
    bridge = WeChatBridge()
    kind, parsed = bridge.classify("帮我写个快排")
    assert kind == "chat"
    assert parsed is None


@pytest.mark.asyncio
async def test_send_prepared_without_endpoint(monkeypatch):
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(_enabled_settings()))
    # bridge.send 读取 get_settings()（非 _load_llm_settings），默认无 send_endpoint
    bridge = WeChatBridge()
    result = await bridge.send("user1", "hello")
    assert result["delivery_status"] == "prepared"
    assert result["payload"]["to_user"] == "user1"


# ---------------------------------------------------------------------------
# inbound 鉴权
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_disabled_returns_403(monkeypatch):
    s = Settings()
    s.wechat.enabled = False
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(s))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/inbound", json={"from_user": "u1", "content": "hi"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_inbound_bad_token_returns_401(monkeypatch):
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(_enabled_settings(inbound_token="secret")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/inbound",
                                 json={"from_user": "u1", "content": "hi", "token": "wrong"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# inbound 对话路由（mock 掉 chat 以免真实调用 LLM）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbound_chat_routes_to_pipeline(monkeypatch):
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(_enabled_settings()))

    async def fake_chat(request):
        assert request.session_id.startswith("wechat-")
        return ChatResponse(success=True, content="你好，我是 Symbio", session_id=request.session_id)

    monkeypatch.setattr(api, "chat", fake_chat)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/inbound",
                                 json={"from_user": "wxid_abc", "content": "你好"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["reply"] == "你好，我是 Symbio"
    assert data["result"]["kind"] == "chat"
    assert data["result"]["session_id"] == "wechat-wxid_abc"


@pytest.mark.asyncio
async def test_inbound_approval_routes_to_hitl(monkeypatch):
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(_enabled_settings()))

    # 先提交一个待审批请求到端点使用的同一个网关
    from symbio.core.hitl_gateway import ApprovalRequest, RiskLevel
    from symbio.core.hitl_notifier import approval_short_code
    gateway = api._get_hitl_gateway()
    req = ApprovalRequest(task_id="t-wx", risk_level=RiskLevel.HIGH, action="删库", timeout_seconds=9999)
    rid = await gateway.submit_request(req)
    code = approval_short_code(rid)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/inbound",
                                 json={"from_user": "wxid_admin", "content": f"同意 {code}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["kind"] == "approval"
    assert data["result"]["action"] == "approve"
    # 网关里该请求已不在 pending
    assert await gateway.get_request(rid) is not None


# ---------------------------------------------------------------------------
# /api/wechat/send 端点
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/send",
                                 json={"to_user": "u1", "content": "通知一下"})
    assert resp.status_code == 200
    assert resp.json()["delivery_status"] in ("prepared", "sent")


# ---------------------------------------------------------------------------
# 扫码绑定登录态
# ---------------------------------------------------------------------------

def test_login_state_transitions():
    bridge = WeChatBridge()
    assert bridge.login_state()["status"] == "logged_out"
    bridge.update_login("waiting_scan", qr="https://login.weixin.qq.com/abc")
    s = bridge.login_state()
    assert s["status"] == "waiting_scan"
    assert s["qr"].endswith("abc")
    bridge.update_login("logged_in", user="我的微信")
    assert bridge.login_state()["user"] == "我的微信"
    # 登出清空二维码与账号
    bridge.update_login("logged_out")
    s = bridge.login_state()
    assert s["qr"] == "" and s["user"] == ""


def test_update_login_ignores_unknown_status():
    bridge = WeChatBridge()
    bridge.update_login("nonsense_status", qr="x")
    # 状态不变，但 qr 仍更新
    assert bridge.login_state()["status"] == "logged_out"
    assert bridge.login_state()["qr"] == "x"


@pytest.mark.asyncio
async def test_login_event_and_status_endpoints(monkeypatch):
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(_enabled_settings()))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/login/event", json={
            "status": "waiting_scan", "qr": "https://login.weixin.qq.com/x",
        })
        assert resp.status_code == 200
        assert resp.json()["login"]["status"] == "waiting_scan"

        resp = await client.post("/api/wechat/login/event", json={
            "status": "logged_in", "user": "我的微信",
        })
        assert resp.status_code == 200

        resp = await client.get("/api/wechat/login/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "logged_in"
        assert data["user"] == "我的微信"
        assert data["enabled"] is True


@pytest.mark.asyncio
async def test_login_event_bad_token(monkeypatch):
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(_enabled_settings(inbound_token="secret")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/login/event",
                                 json={"status": "logged_in", "token": "wrong"})
    assert resp.status_code == 401


def _areturn(value):
    """构造一个返回固定值的 async 函数（替换 _load_llm_settings）。"""
    async def _fn(*args, **kwargs):
        return value
    return _fn
