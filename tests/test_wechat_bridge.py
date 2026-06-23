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


# ---------------------------------------------------------------------------
# 内置 iLink 扫码登录（clawbot）
# ---------------------------------------------------------------------------

class _FakeILinkClient:
    """假 iLink 客户端：可编排扫码状态序列与一批入站消息。"""

    def __init__(self, *, status_seq=None, updates=None):
        self.base_url = "https://fake.ilink"
        self.token = ""
        self.account_id = ""
        self._status_seq = list(status_seq or ["confirmed"])
        self._updates = updates or {}
        self.sent: list[tuple[str, str]] = []
        self._got_updates = False

    async def get_qr(self):
        return {"qrcode": "QR-123", "qr_content": "https://login.weixin.qq.com/QR-123"}

    async def poll_qr_status(self, qrcode):
        st = self._status_seq.pop(0) if self._status_seq else "confirmed"
        if st == "confirmed":
            return {"status": "confirmed", "account_id": "bot_me", "token": "tok-xyz", "base_url": self.base_url}
        return {"status": st}

    async def get_updates(self, sync_buf="", timeout_ms=0):
        if self._got_updates:
            # 第二次起阻塞，避免收消息循环空转刷屏
            import asyncio
            await asyncio.sleep(3600)
        self._got_updates = True
        return self._updates

    async def send_message(self, to_user, text, context_token=""):
        self.sent.append((to_user, text))
        return {"ok": True}


@pytest.mark.asyncio
async def test_ilink_login_confirmed_sets_logged_in(monkeypatch):
    import symbio.interfaces.ilink_client as ilink
    fake = _FakeILinkClient(status_seq=["scaned", "confirmed"])
    monkeypatch.setattr(ilink, "ILinkClient", lambda **kw: fake)

    bridge = WeChatBridge()
    state = await bridge.start_ilink_login()
    assert state["status"] == "waiting_scan"
    assert state["qr"].endswith("QR-123")

    # 等后台轮询确认登录
    import asyncio
    for _ in range(60):
        await asyncio.sleep(0.1)
        if bridge.is_logged_in:
            break
    assert bridge.is_logged_in
    assert bridge.login_state()["user"] == "bot_me"
    await bridge.logout()


@pytest.mark.asyncio
async def test_ilink_login_expired_fails(monkeypatch):
    import symbio.interfaces.ilink_client as ilink
    fake = _FakeILinkClient(status_seq=["expired"])
    monkeypatch.setattr(ilink, "ILinkClient", lambda **kw: fake)

    bridge = WeChatBridge()
    await bridge.start_ilink_login()
    import asyncio
    for _ in range(40):
        await asyncio.sleep(0.1)
        if bridge.login_state()["status"] == "failed":
            break
    assert bridge.login_state()["status"] == "failed"
    await bridge.logout()


@pytest.mark.asyncio
async def test_ilink_recv_loop_dispatches_and_replies(monkeypatch):
    import symbio.interfaces.ilink_client as ilink
    # iLink getupdates 真实字段是 "msgs"（不是 msg_list）
    updates = {"msgs": [
        {"from_user_id": "friend1", "context_token": "ctx1",
         "item_list": [{"type": 1, "text_item": {"text": "你好机器人"}}]},
    ]}
    fake = _FakeILinkClient(status_seq=["confirmed"], updates=updates)
    monkeypatch.setattr(ilink, "ILinkClient", lambda **kw: fake)

    bridge = WeChatBridge()
    captured = []

    async def handler(from_user, content, is_group):
        captured.append((from_user, content))
        return f"收到:{content}"

    bridge.set_message_handler(handler)
    await bridge.start_ilink_login()

    import asyncio
    for _ in range(60):
        await asyncio.sleep(0.1)
        if fake.sent:
            break
    assert captured == [("friend1", "你好机器人")]
    assert fake.sent == [("friend1", "收到:你好机器人")]
    await bridge.logout()


@pytest.mark.asyncio
async def test_send_uses_ilink_when_logged_in(monkeypatch):
    import symbio.interfaces.ilink_client as ilink
    fake = _FakeILinkClient(status_seq=["confirmed"])
    monkeypatch.setattr(ilink, "ILinkClient", lambda **kw: fake)

    bridge = WeChatBridge()
    await bridge.start_ilink_login()
    import asyncio
    for _ in range(60):
        await asyncio.sleep(0.1)
        if bridge.is_logged_in:
            break
    # 已登录 -> send 直接走 iLink，而不是返回 prepared
    result = await bridge.send("friend2", "你好")
    assert result["delivery_status"] == "sent"
    assert result["via"] == "ilink"
    assert ("friend2", "你好") in fake.sent
    await bridge.logout()


@pytest.mark.asyncio
async def test_ilink_login_start_endpoint(monkeypatch):
    import symbio.interfaces.ilink_client as ilink
    fake = _FakeILinkClient(status_seq=["confirmed"])
    monkeypatch.setattr(ilink, "ILinkClient", lambda **kw: fake)
    monkeypatch.setattr(api, "_load_llm_settings", _areturn(_enabled_settings()))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/wechat/login/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["login"]["status"] == "waiting_scan"
    assert body["login"]["qr"].endswith("QR-123")
    await get_wechat_bridge().logout()


@pytest.mark.asyncio
async def test_ilink_get_qr_none_marks_failed(monkeypatch):
    import symbio.interfaces.ilink_client as ilink

    class _NoQR(_FakeILinkClient):
        async def get_qr(self):
            return None

    monkeypatch.setattr(ilink, "ILinkClient", lambda **kw: _NoQR())
    bridge = WeChatBridge()
    state = await bridge.start_ilink_login()
    assert state["status"] == "failed"


@pytest.mark.asyncio
async def test_recv_loop_records_message_stream(monkeypatch):
    import symbio.interfaces.ilink_client as ilink
    updates = {"msgs": [
        {"from_user_id": "friendX", "item_list": [{"type": 1, "text_item": {"text": "在吗"}}]},
    ]}
    fake = _FakeILinkClient(status_seq=["confirmed"], updates=updates)
    monkeypatch.setattr(ilink, "ILinkClient", lambda **kw: fake)

    bridge = WeChatBridge()

    async def handler(u, c, g):
        return "在的"

    bridge.set_message_handler(handler)
    await bridge.start_ilink_login()
    import asyncio
    for _ in range(60):
        await asyncio.sleep(0.1)
        if fake.sent:
            break
    msgs = bridge.recent_messages()
    # 最近在前：先是 out(在的) 再是 in(在吗)
    assert len(msgs) == 2
    assert msgs[0]["direction"] == "out" and msgs[0]["text"] == "在的"
    assert msgs[1]["direction"] == "in" and msgs[1]["text"] == "在吗"
    await bridge.logout()


@pytest.mark.asyncio
async def test_messages_endpoint(monkeypatch):
    reset_wechat_bridge()
    b = get_wechat_bridge()
    b.record_message("in", "u1", "hi")
    b.record_message("out", "u1", "hello", kind="chat")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/wechat/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["messages"][0]["text"] == "hello"  # 最近在前


def test_record_message_ring_buffer_caps():
    b = WeChatBridge()
    b._messages_max = 5
    for i in range(12):
        b.record_message("in", "u", f"m{i}")
    msgs = b.recent_messages(100)
    assert len(msgs) == 5
    assert msgs[0]["text"] == "m11"  # 最新


def test_extract_text_prefers_text_then_voice():
    from symbio.interfaces.ilink_client import extract_text
    assert extract_text([{"type": 1, "text_item": {"text": "hi"}}]) == "hi"
    assert extract_text([{"type": 3, "voice_item": {"text": "语音转写"}}]) == "语音转写"
    assert extract_text([]) == ""
