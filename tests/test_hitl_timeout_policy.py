"""HITL 审批超时策略测试：升级转交、超时自动处置、策略 API。"""

from pathlib import Path
import sys

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
from symbio.interfaces.api import app


# ---------------------------------------------------------------------------
# 升级转交逻辑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_keeps_pending_then_rejects_when_exhausted():
    gw = ApprovalGateway()
    req = ApprovalRequest(
        task_id="t1",
        risk_level=RiskLevel.HIGH,
        timeout_seconds=9999,
        timeout_policy="escalate",
        max_escalations=1,
    )
    rid = await gw.submit_request(req)

    r1 = await gw.escalate(rid, escalation_target="admin", comment="no response")
    assert r1.status == ApprovalStatus.PENDING
    assert r1.escalation_level == 1
    assert r1.escalation_target == "admin"
    assert r1.metadata.get("escalations")

    # 超过 max_escalations -> 落到自动拒绝
    r2 = await gw.escalate(rid, comment="still no response")
    assert r2.status == ApprovalStatus.REJECTED


@pytest.mark.asyncio
async def test_escalate_extends_deadline():
    gw = ApprovalGateway()
    req = ApprovalRequest(
        task_id="t1",
        risk_level=RiskLevel.HIGH,
        timeout_seconds=5,
        timeout_policy="escalate",
        max_escalations=3,
    )
    rid = await gw.submit_request(req)
    r = await gw.escalate(rid, extend_seconds=600)
    assert r.status == ApprovalStatus.PENDING
    assert r.timeout_seconds == 600


@pytest.mark.asyncio
async def test_escalate_unknown_request_raises():
    gw = ApprovalGateway()
    with pytest.raises(KeyError):
        await gw.escalate("nonexistent")


# ---------------------------------------------------------------------------
# 超时自动处置（短超时触发 handler）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_policy_reject():
    gw = ApprovalGateway()
    req = ApprovalRequest(
        task_id="t", risk_level=RiskLevel.HIGH, timeout_seconds=1, timeout_policy="reject"
    )
    rid = await gw.submit_request(req)
    import asyncio

    await asyncio.sleep(1.3)
    got = await gw.get_request(rid)
    assert got.status == ApprovalStatus.TIMEOUT
    # 留下了一条拒绝记录
    assert any(a.decision == ApprovalStatus.REJECTED for a in got.approvals)


@pytest.mark.asyncio
async def test_timeout_policy_approve():
    gw = ApprovalGateway()
    req = ApprovalRequest(
        task_id="t", risk_level=RiskLevel.HIGH, timeout_seconds=1, timeout_policy="approve"
    )
    rid = await gw.submit_request(req)
    import asyncio

    await asyncio.sleep(1.3)
    got = await gw.get_request(rid)
    assert got.status == ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_legacy_auto_approve_on_timeout_still_works():
    gw = ApprovalGateway()
    req = ApprovalRequest(
        task_id="t", risk_level=RiskLevel.HIGH, timeout_seconds=1, auto_approve_on_timeout=True
    )
    rid = await gw.submit_request(req)
    import asyncio

    await asyncio.sleep(1.3)
    got = await gw.get_request(rid)
    assert got.status == ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# 策略配置 API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_policy_api_roundtrip(tmp_path, monkeypatch):
    # 切到临时工作目录，避免污染真实 symbio.yaml
    monkeypatch.chdir(tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/hitl/timeout/policy",
            json={
                "timeout_action": "escalate",
                "escalation_target": "feishu-admin",
                "max_escalations": 2,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["timeout_action"] == "escalate"
        assert data["escalation_target"] == "feishu-admin"
        assert data["max_escalations"] == 2

        resp = await client.get("/api/hitl/timeout/policy")
        assert resp.status_code == 200
        assert resp.json()["timeout_action"] == "escalate"

        # 非法动作被拒
        resp = await client.post("/api/hitl/timeout/policy", json={"timeout_action": "nonsense"})
        assert resp.status_code == 400
