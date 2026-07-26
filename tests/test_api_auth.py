"""全局 API 鉴权测试。

API 暴露了沙箱命令执行、PTY 终端和配置写入。这些测试锁定两条不变量：
1. 配置了 token 时，未鉴权请求必须被 401 拒绝（含 WebSocket）。
2. 没配 token 时保持开放，本机单用户场景不被挡住。
"""

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app  # noqa: E402

TOKEN = "test-token-abc123"


@pytest_asyncio.fixture
async def client():
    """无鉴权客户端（默认状态）。"""
    app.state.api_token = ""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.state.api_token = ""


@pytest_asyncio.fixture
async def secured_client():
    """已配置 token 的客户端。"""
    app.state.api_token = TOKEN
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.state.api_token = ""


@pytest.mark.asyncio
async def test_no_token_configured_allows_access(client):
    """未配置 token 时不应拦截 —— 默认只绑 127.0.0.1，本机使用不该被挡。"""
    resp = await client.get("/api/capabilities")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_missing_token_is_rejected(secured_client):
    resp = await secured_client.get("/api/capabilities")
    assert resp.status_code == 401
    assert "token" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_wrong_token_is_rejected(secured_client):
    resp = await secured_client.get(
        "/api/capabilities", headers={"Authorization": "Bearer wrong-token"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_bearer_token_is_accepted(secured_client):
    resp = await secured_client.get(
        "/api/capabilities", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_x_api_token_header_is_accepted(secured_client):
    """WebSocket 客户端和简单脚本常用 X-API-Token。"""
    resp = await secured_client.get("/api/capabilities", headers={"X-API-Token": TOKEN})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_and_agent_card_stay_public(secured_client):
    """探活和 A2A 发现协议端点必须保持公开，否则容器健康检查会失败。"""
    for path in ("/health", "/api/health", "/", "/.well-known/agent.json"):
        resp = await secured_client.get(path)
        assert resp.status_code == 200, f"{path} should be exempt, got {resp.status_code}"


@pytest.mark.asyncio
async def test_sandbox_execute_is_protected(secured_client):
    """最危险的端点：未鉴权绝不能命令执行。"""
    resp = await secured_client.post(
        "/api/sandbox/execute", json={"command": "echo pwned", "policy": "read-only"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_config_write_is_protected(secured_client):
    """配置接口能写入 API key，必须受保护。"""
    resp = await secured_client.post("/api/config", json={"anthropic_api_key": "leaked"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_via_query_param_is_accepted(secured_client):
    """浏览器 WebSocket 无法设置自定义头，因此也支持 ?token=。"""
    resp = await secured_client.get(f"/api/capabilities?token={TOKEN}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_env_var_token_is_honoured(monkeypatch):
    """未显式设置 app.state 时应回落到 SYMBIO_API_TOKEN 环境变量。"""
    if hasattr(app.state, "api_token"):
        delattr(app.state, "api_token")
    monkeypatch.setenv("SYMBIO_API_TOKEN", "env-token-xyz")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/api/capabilities")).status_code == 401
            resp = await c.get(
                "/api/capabilities", headers={"Authorization": "Bearer env-token-xyz"}
            )
            assert resp.status_code == 200
    finally:
        app.state.api_token = ""


# ---- WebSocket ----
#
# HTTP 中间件不覆盖 WS 握手，所以这两条单独锁定。/ws/terminal 会起真 PTY 跑
# shell，是整个 API 里最危险的端点。


def test_websockets_reject_missing_token():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app.state.api_token = TOKEN
    try:
        client = TestClient(app)
        for path in ("/ws/chat", "/ws/terminal"):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(path) as ws:
                    ws.send_text('{"type":"start","kind":"shell"}')
                    ws.receive_text()
    finally:
        app.state.api_token = ""


def test_websocket_accepts_query_param_token():
    from fastapi.testclient import TestClient

    app.state.api_token = TOKEN
    try:
        client = TestClient(app)
        with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
            assert ws is not None
    finally:
        app.state.api_token = ""
