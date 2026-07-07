"""批次D11：A2A SSE 流式 + 推送通知 + Bearer 鉴权。

覆盖：
- SSE：/api/a2a/tasks/{id}/stream 先发当前快照，随状态变更推事件，
  终态后流关闭；完整收到 working -> completed 序列与最终结果
- 推送通知：入站任务带 pushNotification.url，状态变更时 webhook 收到
  任务快照（捕获式假投递，不触网）
- 鉴权：配置 token 后无/错 Bearer -> 401，正确 -> 200；
  AgentCard 如实声明 schemes（配了 bearer / 没配 none）
- send_task_to_agent 组装 Authorization 头与 pushNotification 字段

全部走 ASGITransport + 内存态 manager，不落盘不触网。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces import a2a as a2a_mod
from symbio.interfaces.a2a import A2ASessionManager


def _set_manager(api_mod):
    prev = getattr(api_mod.app.state, "a2a_manager", None)
    api_mod.app.state.a2a_manager = A2ASessionManager(persist_path=None)
    return prev


# ---------------------------------------------------------------------------
# SSE 流式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_emits_states_until_completed():
    from symbio.interfaces import api as api_mod

    started = asyncio.Event()

    async def slow_echo(prompt: str) -> str:
        started.set()
        await asyncio.sleep(0.05)
        return f"echo:{prompt}"

    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    api_mod.app.state.a2a_task_executor = slow_echo
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={"message": {"role": "user", "parts": [{"type": "text", "text": "流式"}]}},
            )
            task_id = resp.json()["id"]

            events = []
            async with client.stream(
                "GET", f"/api/a2a/tasks/{task_id}/stream", params={"timeout": 10}
            ) as stream:
                assert stream.headers["content-type"].startswith("text/event-stream")
                async for line in stream.aiter_lines():
                    if line.startswith("data: ") and line != "data: {}":
                        events.append(json.loads(line[len("data: "):]))
                        if events[-1].get("state") == "completed":
                            break

        states = [e["state"] for e in events]
        assert states[-1] == "completed"
        assert len(events) >= 2  # 至少快照 + 终态
        final = events[-1]
        assert final["result"]["message"]["parts"][0]["text"] == "echo:流式"
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        api_mod.app.state.a2a_task_executor = prev_exec


@pytest.mark.asyncio
async def test_sse_stream_on_already_completed_task_sends_snapshot_and_closes():
    from symbio.interfaces import api as api_mod

    async def instant(prompt: str) -> str:
        return "done"

    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    api_mod.app.state.a2a_task_executor = instant
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={"message": {"role": "user", "parts": [{"type": "text", "text": "x"}]}},
            )
            task_id = resp.json()["id"]
            # 等任务先完成
            for _ in range(100):
                await asyncio.sleep(0.02)
                g = await client.get(f"/api/a2a/tasks/{task_id}")
                if g.json()["state"] == "completed":
                    break

            events = []
            async with client.stream("GET", f"/api/a2a/tasks/{task_id}/stream") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("data: ") and line != "data: {}":
                        events.append(json.loads(line[len("data: "):]))

        # 已终态：一条快照后立即关闭
        assert len(events) == 1
        assert events[0]["state"] == "completed"
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        api_mod.app.state.a2a_task_executor = prev_exec


@pytest.mark.asyncio
async def test_sse_stream_unknown_task_404():
    from symbio.interfaces import api as api_mod

    transport = ASGITransport(app=api_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/a2a/tasks/nope/stream")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 推送通知
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_notification_fires_webhook_on_completion(monkeypatch):
    from symbio.interfaces import api as api_mod

    delivered = []

    async def fake_deliver(webhook_url, task):
        delivered.append((webhook_url, task.model_dump(mode="json")))

    monkeypatch.setattr(a2a_mod, "_deliver_push_notification", fake_deliver)

    async def echo(prompt: str) -> str:
        return f"pushed:{prompt}"

    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    api_mod.app.state.a2a_task_executor = echo
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={
                    "message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
                    "pushNotification": {"url": "http://callback.example/hook"},
                },
            )
            task_id = resp.json()["id"]
            for _ in range(100):
                await asyncio.sleep(0.02)
                g = await client.get(f"/api/a2a/tasks/{task_id}")
                if g.json()["state"] == "completed":
                    break
            await asyncio.sleep(0.05)  # 等 fire-and-forget 投递跑完

        urls = {u for u, _ in delivered}
        assert urls == {"http://callback.example/hook"}
        states = [t["state"] for _, t in delivered]
        assert "completed" in states  # 至少终态被推送
        final = [t for _, t in delivered if t["state"] == "completed"][-1]
        assert final["result"]["message"]["parts"][0]["text"] == "pushed:hi"
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        api_mod.app.state.a2a_task_executor = prev_exec


@pytest.mark.asyncio
async def test_no_push_without_registration(monkeypatch):
    from symbio.interfaces import api as api_mod

    delivered = []

    async def fake_deliver(webhook_url, task):
        delivered.append(webhook_url)

    monkeypatch.setattr(a2a_mod, "_deliver_push_notification", fake_deliver)

    async def echo(prompt: str) -> str:
        return "x"

    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    api_mod.app.state.a2a_task_executor = echo
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
            )
            task_id = resp.json()["id"]
            for _ in range(100):
                await asyncio.sleep(0.02)
                g = await client.get(f"/api/a2a/tasks/{task_id}")
                if g.json()["state"] == "completed":
                    break

        assert delivered == []
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        api_mod.app.state.a2a_task_executor = prev_exec


# ---------------------------------------------------------------------------
# Bearer 鉴权
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbound_requires_bearer_when_token_configured():
    from symbio.interfaces import api as api_mod

    prev_mgr = _set_manager(api_mod)
    prev_token = getattr(api_mod.app.state, "a2a_auth_token", None)
    api_mod.app.state.a2a_auth_token = "sekrit-token"

    async def echo(prompt: str) -> str:
        return "ok"

    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    api_mod.app.state.a2a_task_executor = echo
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}}

            no_auth = await client.post("/api/a2a/tasks", json=body)
            assert no_auth.status_code == 401

            bad = await client.post(
                "/api/a2a/tasks", json=body,
                headers={"Authorization": "Bearer wrong"},
            )
            assert bad.status_code == 401

            ok = await client.post(
                "/api/a2a/tasks", json=body,
                headers={"Authorization": "Bearer sekrit-token"},
            )
            assert ok.status_code == 200
            assert ok.json()["id"]
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        api_mod.app.state.a2a_auth_token = prev_token
        api_mod.app.state.a2a_task_executor = prev_exec


@pytest.mark.asyncio
async def test_agent_card_advertises_auth_scheme():
    from symbio.interfaces import api as api_mod

    prev_token = getattr(api_mod.app.state, "a2a_auth_token", None)
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            api_mod.app.state.a2a_auth_token = "sekrit"
            with_auth = await client.get("/.well-known/agent.json")
            assert with_auth.json()["authentication"]["schemes"] == ["bearer"]
            assert with_auth.json()["capabilities"]["streaming"] is True
            assert with_auth.json()["capabilities"]["pushNotifications"] is True

            api_mod.app.state.a2a_auth_token = ""
            without = await client.get("/.well-known/agent.json")
            assert without.json()["authentication"]["schemes"] == ["none"]
    finally:
        api_mod.app.state.a2a_auth_token = prev_token


# ---------------------------------------------------------------------------
# 出站侧：Authorization 头与 pushNotification 组装
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_task_to_agent_includes_auth_and_push(monkeypatch):
    captured = {}

    class _FakeResp:
        status = 200

        async def json(self):
            return {"id": "t-1", "state": "submitted"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def post(self, url, json=None, timeout=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: _FakeSession())

    result = await a2a_mod.send_task_to_agent(
        "http://remote.example",
        "hello",
        auth_token="tok-123",
        push_url="http://me.example/hook",
    )
    assert result["task_id"]
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    assert captured["json"]["pushNotification"] == {"url": "http://me.example/hook"}
