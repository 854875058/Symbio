"""HITL → 微信审批闭环测试。

验证两件事：
1. 出向：高危操作触发 HITL（/api/hitl/submit）后，审批卡（含短码）被推送到
   已登录个人微信（iLink bridge）的配置审批人。
2. 入向：微信回复"同意 <短码>"能把对应请求审批通过（已有路由）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.hitl_gateway import (
    ApprovalGateway,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
)
from symbio.core.hitl_notifier import HITLNotifier, approval_short_code
from symbio.interfaces import api as api_module
from symbio.interfaces.api import app, _wechat_dispatch, _resolve_hitl_request_id


class StubBridge:
    """已登录的微信 bridge 替身：记录 send，复用真实审批命令分类。"""

    def __init__(self, logged_in: bool = True):
        self._logged_in = logged_in
        self.sent: list[dict] = []

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    async def send(self, to_user, content, *, is_group=False, context_token=""):
        self.sent.append({"to": to_user, "content": content})
        return {"delivery_status": "sent", "via": "ilink"}

    def classify(self, content):
        from symbio.core.hitl_notifier import parse_im_approval_command

        cmd = parse_im_approval_command(content or "")
        return ("approval", cmd) if cmd else ("chat", None)

    def record_message(self, *args, **kwargs):
        pass


def _settings_with_approver(approver: str):
    wechat = SimpleNamespace(hitl_approver=approver, send_endpoint="", enabled=True)
    return SimpleNamespace(wechat=wechat)


def _install(monkeypatch, *, approver: str, logged_in: bool = True) -> StubBridge:
    bridge = StubBridge(logged_in=logged_in)
    monkeypatch.setattr(api_module, "get_wechat_bridge", lambda: bridge)
    import symbio.config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: _settings_with_approver(approver))
    # 隔离 HITL 状态，避免污染其它测试
    app.state.hitl_gateway = ApprovalGateway()
    app.state.hitl_notifier = HITLNotifier(targets=[])
    return bridge


async def _submit(client: AsyncClient, **overrides):
    body = {
        "task_id": "danger-1",
        "risk_level": "high",
        "action": "rm -rf /data",
        "impact_scope": "production",
        "reason": "高危删除操作",
        "timeout_seconds": 9999,
    }
    body.update(overrides)
    return await client.post("/api/hitl/submit", json=body)


# --------------------------------------------------------------------------
# 出向：提交即推送审批卡到微信
# --------------------------------------------------------------------------

async def test_submit_pushes_approval_card_to_wechat(monkeypatch):
    bridge = _install(monkeypatch, approver="wxid_admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _submit(client)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    code = data["request"]["code"]

    # 审批卡被推到配置的审批人，且包含短码
    assert len(bridge.sent) == 1
    assert bridge.sent[0]["to"] == "wxid_admin"
    assert code in bridge.sent[0]["content"]

    # 通知记录里出现微信投递条目
    platforms = [n.get("platform") for n in data["notifications"]]
    assert any("wechat" in (p or "") for p in platforms)
    assert data["request"]["notification_status"] == "sent"


async def test_no_push_when_approver_not_configured(monkeypatch):
    bridge = _install(monkeypatch, approver="")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _submit(client, risk_level="low")
    assert resp.status_code == 200
    assert bridge.sent == []  # 没配审批人就不推送


async def test_prepared_when_logged_out(monkeypatch):
    bridge = _install(monkeypatch, approver="wxid_admin", logged_in=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await _submit(client)
    assert resp.status_code == 200
    # 未登录且无 send_endpoint：不实际发送，但留下 prepared 记录
    assert bridge.sent == []
    platforms = [n.get("platform") for n in resp.json()["notifications"]]
    assert any("wechat" in (p or "") for p in platforms)


# --------------------------------------------------------------------------
# 入向：微信回复"同意 <短码>"完成审批
# --------------------------------------------------------------------------

async def test_wechat_reply_short_code_approves(monkeypatch):
    _install(monkeypatch, approver="wxid_admin")
    gw = app.state.hitl_gateway
    # MEDIUM 风险需 1 个审批人，单条"同意"即可终态（HIGH 需 2 人，属设计如此）
    req = ApprovalRequest(
        task_id="danger-2", risk_level=RiskLevel.MEDIUM, action="drop table users",
        timeout_seconds=9999,
    )
    rid = await gw.submit_request(req)
    code = approval_short_code(rid)

    reply, routed = await _wechat_dispatch("wxid_admin", f"同意 {code}")
    assert routed["kind"] == "approval"
    assert routed["action"] == "approve"
    assert "通过" in reply

    got = await gw.get_request(rid)
    assert got.status == ApprovalStatus.APPROVED


async def test_wechat_reply_reject_short_code(monkeypatch):
    _install(monkeypatch, approver="wxid_admin")
    gw = app.state.hitl_gateway
    rid = await gw.submit_request(
        ApprovalRequest(task_id="danger-3", risk_level=RiskLevel.HIGH, timeout_seconds=9999)
    )
    code = approval_short_code(rid)

    reply, routed = await _wechat_dispatch("wxid_admin", f"拒绝 {code} 风险太高")
    assert routed["action"] == "reject"
    assert "拒绝" in reply
    got = await gw.get_request(rid)
    assert got.status == ApprovalStatus.REJECTED


# --------------------------------------------------------------------------
# 短码：要短、好记（4 位数字），且按 pending 优先解析
# --------------------------------------------------------------------------

def test_short_code_is_short_and_numeric():
    code = approval_short_code("f483f8b7-53e4-4462-9494-bde0fa989be0")
    assert len(code) == 4
    assert code.isdigit()
    # 同一请求恒定映射到同一短码
    assert code == approval_short_code("f483f8b7-53e4-4462-9494-bde0fa989be0")
    # 微信审批命令能解析这个短码
    from symbio.core.hitl_notifier import parse_im_approval_command

    parsed = parse_im_approval_command(f"同意 {code}")
    assert parsed is not None and parsed.request_id == code


async def test_resolve_prefers_pending_when_short_code_collides(monkeypatch):
    gw = ApprovalGateway()
    app.state.hitl_gateway = gw
    # 强制两个请求短码相同，验证解析优先选 pending（历史里的同码不挡当前待审批）
    monkeypatch.setattr(api_module, "approval_short_code", lambda rid, length=4: "0001")

    old = await gw.submit_request(
        ApprovalRequest(task_id="old", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999)
    )
    await gw.approve(old, approver_id="someone")  # 进入历史（approved）
    new = await gw.submit_request(
        ApprovalRequest(task_id="new", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999)
    )

    resolved = await _resolve_hitl_request_id("0001")
    assert resolved == new  # 命中当前 pending，而不是历史里的同码


# --------------------------------------------------------------------------
# 审批命令：中文不带空格也要识别（"同意5754"）
# --------------------------------------------------------------------------

def test_parse_chinese_command_without_space():
    from symbio.core.hitl_notifier import parse_im_approval_command as parse

    c = parse("同意5754")
    assert c is not None and c.action == "approve" and c.request_id == "5754"
    r = parse("拒绝5754")
    assert r is not None and r.action == "reject" and r.request_id == "5754"
    # 带空格仍可
    assert parse("同意 5754").action == "approve"
    # 中文无空格动作 + 空格原因
    c2 = parse("同意5754 已确认")
    assert c2.request_id == "5754" and c2.comment == "已确认"
    # 英文不带空格不应误判为审批
    assert parse("ok1234") is None
    # 普通中文聊天不应误判
    assert parse("同意了这个方案吧") is None


async def test_wechat_dispatch_approves_without_space(monkeypatch):
    _install(monkeypatch, approver="wxid_admin")
    gw = app.state.hitl_gateway
    rid = await gw.submit_request(
        ApprovalRequest(task_id="nospace", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999)
    )
    code = approval_short_code(rid)

    reply, routed = await _wechat_dispatch("wxid_admin", f"同意{code}")  # 无空格
    assert routed["kind"] == "approval" and routed["action"] == "approve"
    got = await gw.get_request(rid)
    assert got.status == ApprovalStatus.APPROVED


# --------------------------------------------------------------------------
# 单条待审批时裸"同意/拒绝"（不用记码）
# --------------------------------------------------------------------------

def test_parse_bare_command():
    from symbio.core.hitl_notifier import parse_im_approval_command as parse

    assert parse("同意").action == "approve"
    assert parse("同意").request_id == ""
    r = parse("拒绝 太危险")
    assert r.action == "reject" and r.request_id == "" and r.comment == "太危险"
    assert parse("approve").action == "approve" and parse("approve").request_id == ""
    # 正常中文句子不误判
    assert parse("同意了这个方案吧") is None
    assert parse("通过这个路口") is None


async def test_bare_approve_single_pending(monkeypatch):
    _install(monkeypatch, approver="wxid_admin")
    gw = app.state.hitl_gateway
    rid = await gw.submit_request(
        ApprovalRequest(task_id="only-one", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999)
    )
    reply, routed = await _wechat_dispatch("wxid_admin", "同意")  # 裸，无短码
    assert routed.get("ok") is True and routed["action"] == "approve"
    assert (await gw.get_request(rid)).status == ApprovalStatus.APPROVED


async def test_bare_no_pending_gives_friendly_reply(monkeypatch):
    _install(monkeypatch, approver="wxid_admin")
    reply, routed = await _wechat_dispatch("wxid_admin", "同意")
    assert routed.get("ok") is False
    assert "没有待审批" in reply


async def test_bare_multiple_pending_asks_for_code(monkeypatch):
    _install(monkeypatch, approver="wxid_admin")
    gw = app.state.hitl_gateway
    a = await gw.submit_request(ApprovalRequest(task_id="a", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999))
    b = await gw.submit_request(ApprovalRequest(task_id="b", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999))
    reply, routed = await _wechat_dispatch("wxid_admin", "同意")
    assert routed.get("ok") is False
    assert "待审批" in reply  # 提示有多条、要带短码
    # 两条都还在 pending
    assert (await gw.get_request(a)).status == ApprovalStatus.PENDING
    assert (await gw.get_request(b)).status == ApprovalStatus.PENDING


# --------------------------------------------------------------------------
# 失败重推：/api/hitl/{id}/repush-wechat
# --------------------------------------------------------------------------

async def test_repush_wechat_endpoint(monkeypatch):
    bridge = _install(monkeypatch, approver="wxid_admin")
    gw = app.state.hitl_gateway
    rid = await gw.submit_request(
        ApprovalRequest(task_id="repush", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999)
    )
    code = approval_short_code(rid)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/hitl/{code}/repush-wechat")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["note"]["delivery_status"] == "sent"
    assert len(bridge.sent) == 1 and bridge.sent[0]["to"] == "wxid_admin"


async def test_repush_wechat_without_approver_400(monkeypatch):
    _install(monkeypatch, approver="")  # 未配置审批人
    gw = app.state.hitl_gateway
    rid = await gw.submit_request(
        ApprovalRequest(task_id="repush2", risk_level=RiskLevel.MEDIUM, timeout_seconds=9999)
    )
    code = approval_short_code(rid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/hitl/{code}/repush-wechat")
    assert resp.status_code == 400
