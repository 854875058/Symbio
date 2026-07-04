"""批次D8：A2A 协议做实。

覆盖：
- 入站任务真往返：POST /api/a2a/tasks -> 后台执行 -> 轮询到 COMPLETED 带真实回复
  （注入执行器，证明端到端而非黑箱）
- 无执行器 + 无 key 时优雅降级为 COMPLETED + [Symbio error...]，绝不挂起对端
- AgentCard 动态：真实包版本 + 能力快照 metadata + 请求来源 URL
- 出站会话：用假发送器证明会话追踪了发出的消息与远端卡片

全部走 ASGITransport + 注入内存态 A2ASessionManager（persist_path=None），
不落盘、不触网。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.a2a import A2ASessionManager


async def _poll_until_completed(client, task_id, tries=80, delay=0.02):
    for _ in range(tries):
        await asyncio.sleep(delay)
        g = await client.get(f"/api/a2a/tasks/{task_id}")
        data = g.json()
        if data["state"] == "completed":
            return data
    return data  # 返回最后一次状态，便于断言失败时定位


def _set_manager(api_mod):
    prev = getattr(api_mod.app.state, "a2a_manager", None)
    api_mod.app.state.a2a_manager = A2ASessionManager(persist_path=None)
    return prev


def _restore_executor(api_mod, prev):
    if prev is None:
        if hasattr(api_mod.app.state, "a2a_task_executor"):
            delattr(api_mod.app.state, "a2a_task_executor")
    else:
        api_mod.app.state.a2a_task_executor = prev


@pytest.mark.asyncio
async def test_inbound_task_roundtrip_with_injected_executor():
    from symbio.interfaces import api as api_mod

    async def fake_exec(prompt: str) -> str:
        return f"echo:{prompt}"

    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    api_mod.app.state.a2a_task_executor = fake_exec
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={"message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]}},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["state"] == "submitted"
            task_id = body["id"]

            data = await _poll_until_completed(client, task_id)
            assert data["state"] == "completed"
            assert data["result"]["message"]["parts"][0]["text"] == "echo:hello"
            assert data["result"]["message"]["role"] == "agent"
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        _restore_executor(api_mod, prev_exec)


@pytest.mark.asyncio
async def test_inbound_task_completes_gracefully_without_key(monkeypatch):
    from symbio.interfaces import api as api_mod
    from symbio.config.settings import Settings

    async def _fake_settings():
        s = Settings()
        s.model.anthropic_api_key = ""
        return s

    monkeypatch.setattr(api_mod, "_load_llm_settings", _fake_settings)

    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    # 确保未注入执行器 -> 走默认 LLM 执行器（无 key 抛错 -> 优雅降级）
    if hasattr(api_mod.app.state, "a2a_task_executor"):
        delattr(api_mod.app.state, "a2a_task_executor")
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
            )
            task_id = resp.json()["id"]
            data = await _poll_until_completed(client, task_id)

        assert data["state"] == "completed"  # 永远收敛，不挂起
        assert data["result"]["message"]["parts"][0]["text"].startswith("[Symbio error")
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        _restore_executor(api_mod, prev_exec)


@pytest.mark.asyncio
async def test_agent_card_is_dynamic():
    from symbio.interfaces import api as api_mod
    from symbio import __version__

    transport = ASGITransport(app=api_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/.well-known/agent.json")

    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "Symbio"
    # 真实包版本，而非硬编码 0.1.0
    assert card["version"] == __version__
    assert card["skills"]
    impl = card["metadata"]["implemented_capabilities"]
    assert isinstance(impl, list) and impl  # 能力快照非空
    assert "dynamic_dag" in impl
    assert card["metadata"]["capability_summary"]["total"] >= 10
    assert card["url"].startswith("http://test")


@pytest.mark.asyncio
async def test_outbound_session_tracks_sent_message(monkeypatch):
    from symbio.interfaces import api as api_mod

    async def fake_send(remote_url, message_text, session_id=None, timeout=30):
        return {"task_id": "t-123", "remote_url": remote_url, "state": "submitted"}

    async def fake_fetch(remote_url, timeout=10):
        return {"name": "RemoteBot"}

    monkeypatch.setattr(api_mod, "send_task_to_agent", fake_send)
    monkeypatch.setattr(api_mod, "fetch_remote_agent_card", fake_fetch)

    prev_mgr = _set_manager(api_mod)
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/sessions",
                json={"remote_url": "http://remote.example", "initial_message": "ping"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["remote_card"]["name"] == "RemoteBot"
            assert data["send_result"]["task_id"] == "t-123"
            sess_id = data["session"]["id"]

            g = await client.get(f"/api/a2a/sessions/{sess_id}")
            sdata = g.json()
        assert sdata["remote_name"] == "RemoteBot"
        assert sdata["state"] == "working"
        assert any("ping" in p["text"] for m in sdata["messages"] for p in m["parts"])
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
