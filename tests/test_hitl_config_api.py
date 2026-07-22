from pathlib import Path
import sys

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.config.settings import get_settings
from symbio.core.hitl_gateway import ApprovalRequest, RiskLevel, generate_approval_token
from symbio.interfaces.api import app


@pytest.mark.asyncio
async def test_config_api_persists_hitl_targets_and_refreshes_channels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    if hasattr(app.state, "hitl_notifier"):
        delattr(app.state, "hitl_notifier")

    payload = {
        "hitl": {
            "enabled": True,
            "high_risk_auto_suspend": True,
            "approval_timeout": 600,
            "callback_base_url": "https://symbio.example.com",
            "im_webhook_token": "shared-token",
            "notify_timeout": 8.0,
            "notify_targets": [
                {
                    "platform": "feishu",
                    "endpoint": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                    "chat_id": "ops",
                    "chat_type": "group",
                    "secret": "sign-secret",
                    "enabled": True,
                }
            ],
        }
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        update_resp = await client.post("/api/config", json=payload)
        config_resp = await client.get("/api/config")
        channels_resp = await client.get("/api/hitl/channels")

    assert update_resp.status_code == 200
    assert config_resp.status_code == 200
    assert channels_resp.status_code == 200

    config = config_resp.json()
    assert config["hitl"]["callback_base_url"] == "https://symbio.example.com"
    assert config["hitl"]["im_webhook_token"] == "****oken"
    assert config["hitl"]["notify_targets"][0]["platform"] == "feishu"

    channels = channels_resp.json()
    assert channels["enabled"] == ["feishu"]
    assert channels["channels"][0]["platform"] == "feishu"
    assert "access_token" not in channels["channels"][0]


@pytest.mark.asyncio
async def test_hitl_action_endpoint_approves_signed_card_button(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    for attr in ("hitl_gateway", "hitl_notifier"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)

    request = ApprovalRequest(
        task_id="task-card-action",
        action="Deploy",
        risk_level=RiskLevel.MEDIUM,
        timeout_seconds=0,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        submit_resp = await client.post("/api/hitl/submit", json=request.model_dump(mode="json"))
        request_id = submit_resp.json()["request_id"]
        action_resp = await client.get(
            "/api/hitl/action",
            params={
                "request_id": request_id,
                "action": "approve",
                "token": generate_approval_token(request_id),
                "approver_id": "card-user",
            },
        )

    gateway = getattr(app.state, "hitl_gateway", None)
    if gateway is not None:
        await gateway.close()
        delattr(app.state, "hitl_gateway")

    assert submit_resp.status_code == 200
    assert action_resp.status_code == 200
    assert action_resp.json()["request"]["status"] == "approved"
