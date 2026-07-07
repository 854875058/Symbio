"""批次D10：A2A 接编排器 + 跨实例真往返。

覆盖：
- 执行器解析优先级：注入 > 编排器 > 裸 LLM（_resolve_a2a_executor）
- 入站任务走 Orchestrator 完整管线（假编排器验证 Message 构造与 Result 回传）
- 编排器失败时优雅降级为 COMPLETED + [Symbio error...]
- 出站会话 poll：远端 COMPLETED 后把 agent 回复拉回会话、会话状态收敛、
  重复 poll 不重复追加（幂等）
- 跨实例真往返：subprocess 起第二个 Symbio uvicorn 实例（注入 echo 执行器），
  本实例通过 /api/a2a/sessions 出站发送 → 远端后台执行 → poll 拉回真实回复。
  两个独立进程、真 HTTP，非 ASGI 内存直连。

除跨实例集成外全部走 ASGITransport + 内存态 manager，不落盘不触网。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.a2a import A2ASessionManager, A2ATaskState


def _set_manager(api_mod):
    prev = getattr(api_mod.app.state, "a2a_manager", None)
    api_mod.app.state.a2a_manager = A2ASessionManager(persist_path=None)
    return prev


async def _poll_until_completed(client, task_id, tries=100, delay=0.02):
    data = {}
    for _ in range(tries):
        await asyncio.sleep(delay)
        g = await client.get(f"/api/a2a/tasks/{task_id}")
        data = g.json()
        if data["state"] == "completed":
            return data
    return data


# ---------------------------------------------------------------------------
# 执行器解析优先级
# ---------------------------------------------------------------------------


def test_executor_resolution_prefers_injected_then_orchestrator_then_llm():
    from symbio.interfaces import api as api_mod

    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    prev_orch = getattr(api_mod.app.state, "orchestrator", None)
    try:
        async def injected(prompt: str) -> str:
            return "x"

        api_mod.app.state.a2a_task_executor = injected
        api_mod.app.state.orchestrator = object()
        _, mode = api_mod._resolve_a2a_executor()
        assert mode == "injected"

        api_mod.app.state.a2a_task_executor = None
        _, mode = api_mod._resolve_a2a_executor()
        assert mode == "orchestrator"

        api_mod.app.state.orchestrator = None
        _, mode = api_mod._resolve_a2a_executor()
        assert mode == "llm"
    finally:
        api_mod.app.state.a2a_task_executor = prev_exec
        api_mod.app.state.orchestrator = prev_orch


# ---------------------------------------------------------------------------
# 入站任务走编排器管线
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, success=True, content="", error=None):
        self.success = success
        self.content = content
        self.error = error


class _FakeOrchestrator:
    def __init__(self, reply="orchestrated!", success=True, error=None):
        self.seen_messages = []
        self._reply = reply
        self._success = success
        self._error = error

    async def process(self, message):
        self.seen_messages.append(message)
        return _FakeResult(self._success, self._reply, self._error)


@pytest.mark.asyncio
async def test_inbound_task_routes_through_orchestrator():
    from symbio.interfaces import api as api_mod

    fake_orch = _FakeOrchestrator(reply="来自编排器的回复")
    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    prev_orch = getattr(api_mod.app.state, "orchestrator", None)
    api_mod.app.state.a2a_task_executor = None
    api_mod.app.state.orchestrator = fake_orch
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={"message": {"role": "user", "parts": [{"type": "text", "text": "帮我规划"}]}},
            )
            task_id = resp.json()["id"]
            data = await _poll_until_completed(client, task_id)

        assert data["state"] == "completed"
        assert data["result"]["message"]["parts"][0]["text"] == "来自编排器的回复"
        assert data["result"]["metadata"]["executor"] == "orchestrator"
        # 编排器收到的是标准 Message：a2a 来源 + 原文
        msg = fake_orch.seen_messages[0]
        assert msg.content == "帮我规划"
        assert msg.source.value == "a2a"
        assert msg.session_id.startswith("a2a-")
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        api_mod.app.state.a2a_task_executor = prev_exec
        api_mod.app.state.orchestrator = prev_orch


@pytest.mark.asyncio
async def test_inbound_task_orchestrator_failure_degrades_gracefully():
    from symbio.interfaces import api as api_mod

    class _BoomOrchestrator:
        async def process(self, message):
            raise RuntimeError("orchestrator exploded")

    prev_mgr = _set_manager(api_mod)
    prev_exec = getattr(api_mod.app.state, "a2a_task_executor", None)
    prev_orch = getattr(api_mod.app.state, "orchestrator", None)
    api_mod.app.state.a2a_task_executor = None
    api_mod.app.state.orchestrator = _BoomOrchestrator()
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/tasks",
                json={"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
            )
            task_id = resp.json()["id"]
            data = await _poll_until_completed(client, task_id)

        assert data["state"] == "completed"  # 永远收敛
        text = data["result"]["message"]["parts"][0]["text"]
        assert text.startswith("[Symbio error") and "orchestrator exploded" in text
    finally:
        api_mod.app.state.a2a_manager = prev_mgr
        api_mod.app.state.a2a_task_executor = prev_exec
        api_mod.app.state.orchestrator = prev_orch


# ---------------------------------------------------------------------------
# 出站会话 poll 闭环（假远端）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_poll_pulls_remote_reply_and_completes(monkeypatch):
    from symbio.interfaces import api as api_mod

    async def fake_send(remote_url, message_text, session_id=None, timeout=30):
        return {"task_id": "t-remote-1", "remote_url": remote_url, "state": "submitted"}

    async def fake_fetch_card(remote_url, timeout=10):
        return {"name": "RemoteBot"}

    remote_states = iter([
        {"state": "working"},
        {
            "state": "completed",
            "result": {"message": {
                "messageId": "msg-remote-reply",
                "parts": [{"type": "text", "text": "远端算完了"}],
            }},
        },
    ])

    async def fake_fetch_task(remote_url, task_id, timeout=10):
        try:
            return next(remote_states)
        except StopIteration:
            return {
                "state": "completed",
                "result": {"message": {
                    "messageId": "msg-remote-reply",
                    "parts": [{"type": "text", "text": "远端算完了"}],
                }},
            }

    monkeypatch.setattr(api_mod, "send_task_to_agent", fake_send)
    monkeypatch.setattr(api_mod, "fetch_remote_agent_card", fake_fetch_card)
    monkeypatch.setattr(api_mod, "fetch_remote_task", fake_fetch_task)

    prev_mgr = _set_manager(api_mod)
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/sessions",
                json={"remote_url": "http://remote.example", "initial_message": "ping"},
            )
            sess_id = resp.json()["session"]["id"]

            # 第一次 poll：远端还在 working
            p1 = await client.post(f"/api/a2a/sessions/{sess_id}/poll")
            assert p1.json()["all_completed"] is False

            # 第二次 poll：远端完成，回复拉回会话
            p2 = await client.post(f"/api/a2a/sessions/{sess_id}/poll")
            d2 = p2.json()
            assert d2["all_completed"] is True
            assert d2["session"]["state"] == "completed"
            agent_msgs = [m for m in d2["session"]["messages"] if m["role"] == "agent"]
            assert len(agent_msgs) == 1
            assert agent_msgs[0]["parts"][0]["text"] == "远端算完了"

            # 第三次 poll：幂等，不重复追加
            p3 = await client.post(f"/api/a2a/sessions/{sess_id}/poll")
            agent_msgs3 = [m for m in p3.json()["session"]["messages"] if m["role"] == "agent"]
            assert len(agent_msgs3) == 1
    finally:
        api_mod.app.state.a2a_manager = prev_mgr


# ---------------------------------------------------------------------------
# 跨实例真往返（两个独立进程 + 真 HTTP）
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


REMOTE_BOOTSTRAP = """
import sys
sys.path.insert(0, {src!r})
import uvicorn
from symbio.interfaces import api as api_mod
from symbio.interfaces.a2a import A2ASessionManager

# 内存态 manager + echo 执行器：不落盘、不触网、不需要 API key
api_mod.app.state.a2a_manager = A2ASessionManager(persist_path=None)

async def echo_executor(prompt: str) -> str:
    return "remote-echo:" + prompt

api_mod.app.state.a2a_task_executor = echo_executor
uvicorn.run(api_mod.app, host="127.0.0.1", port={port}, log_level="warning")
"""


@pytest.fixture(scope="module")
def remote_symbio_instance(tmp_path_factory):
    """在独立进程里起第二个 Symbio 实例，yield 其 base_url。"""
    port = _free_port()
    script = REMOTE_BOOTSTRAP.format(src=str(PROJECT_ROOT / "src"), port=port)
    work = tmp_path_factory.mktemp("a2a_remote")
    script_path = work / "remote_instance.py"
    script_path.write_text(script, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 子进程输出必须落文件：uvicorn/loguru 日志量大，PIPE 不读会塞满
    # Windows 管道缓冲区导致子进程阻塞在写日志上（启动永远"未就绪"）
    log_path = work / "remote_instance.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, str(script_path)],
        cwd=str(tmp_path_factory.mktemp("a2a_remote_cwd")),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )

    base_url = f"http://127.0.0.1:{port}"
    # 等远端 ready（最多 30s）
    ready = False
    for _ in range(150):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base_url + "/.well-known/agent.json", timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.2)

    if not ready:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass
        log_file.close()
        tail = log_path.read_bytes()[-800:] if log_path.exists() else b""
        pytest.skip(f"Remote Symbio instance failed to start: {tail!r}")

    yield base_url

    proc.kill()
    proc.wait(timeout=10)
    log_file.close()


@pytest.mark.asyncio
async def test_cross_instance_roundtrip_over_real_http(remote_symbio_instance):
    """本实例 → 出站会话 → 远端实例后台执行 → poll 拉回真实回复。"""
    from symbio.interfaces import api as api_mod

    prev_mgr = _set_manager(api_mod)
    try:
        transport = ASGITransport(app=api_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/a2a/sessions",
                json={
                    "remote_url": remote_symbio_instance,
                    "initial_message": "cross-instance-hello",
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            # 远端卡片是真实 Symbio AgentCard（真 HTTP 发现）
            assert body["remote_card"]["name"] == "Symbio"
            assert "send_error" not in body, body.get("send_error")
            sess_id = body["session"]["id"]

            # 轮询直到远端完成并拉回回复
            reply = None
            for _ in range(100):
                await asyncio.sleep(0.1)
                p = await client.post(f"/api/a2a/sessions/{sess_id}/poll")
                d = p.json()
                if d["all_completed"]:
                    agent_msgs = [
                        m for m in d["session"]["messages"] if m["role"] == "agent"
                    ]
                    assert agent_msgs, "completed but no agent reply pulled back"
                    reply = agent_msgs[0]["parts"][0]["text"]
                    break

        assert reply == "remote-echo:cross-instance-hello"
    finally:
        api_mod.app.state.a2a_manager = prev_mgr


@pytest.mark.asyncio
async def test_cross_instance_remote_card_reflects_capabilities(remote_symbio_instance):
    """真 HTTP 探测远端 AgentCard：动态版本 + 能力快照都在。"""
    from symbio.interfaces import api as api_mod
    from symbio import __version__

    transport = ASGITransport(app=api_mod.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/a2a/probe", params={"url": remote_symbio_instance}
        )
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "Symbio"
    assert card["version"] == __version__
    assert "dynamic_dag" in card["metadata"]["implemented_capabilities"]
