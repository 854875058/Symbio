"""API tests for the external-agent live two-way sync endpoints (批次15)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app
from symbio.tools.external_live_session import ExternalLiveSessionManager


def _claude_line(role: str, content: str) -> str:
    return json.dumps(
        {"type": role, "message": {"role": role, "content": content}},
        ensure_ascii=False,
    )


class StubController:
    def __init__(self, transcript_path: Path):
        self.transcript_path = Path(transcript_path)
        self.runs: list[tuple[str, object]] = []
        self._n = 0

    def create_session(self, request):
        self._n += 1
        return SimpleNamespace(
            session_id=f"ctrl-{self._n}",
            external_session_id=request.external_session_id,
            workspace=request.workspace,
        )

    async def run_session(self, session_id, request):
        self.runs.append((session_id, request))
        if not request.dry_run:
            with self.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(_claude_line("assistant", "已收到，继续") + "\n")
        return SimpleNamespace(
            success=True,
            dry_run=request.dry_run,
            run_id="run-1",
            exit_code=0,
            command=["claude", "-p", "--resume", "abc-123", request.prompt],
            stdout="ok",
            error="",
        )


async def _client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def live_env(tmp_path):
    """Inject a live manager backed by a stub controller and tmp transcript roots."""
    claude_file = tmp_path / ".claude" / "projects" / "demo" / "abc-123.jsonl"
    claude_file.parent.mkdir(parents=True, exist_ok=True)
    claude_file.write_text(_claude_line("user", "第一句") + "\n", encoding="utf-8")

    stub = StubController(claude_file)
    manager = ExternalLiveSessionManager(
        controller=stub,
        state_path=tmp_path / "live_state.json",
        workspace_root=tmp_path,
    )

    prev_roots = getattr(app.state, "external_transcript_roots", None)
    prev_mgr = getattr(app.state, "external_live_manager", None)
    app.state.external_transcript_roots = {
        "codex": str(tmp_path / ".codex"),
        "claude-code": str(tmp_path / ".claude"),
    }
    app.state.external_live_manager = manager
    try:
        yield SimpleNamespace(file=claude_file, stub=stub, manager=manager)
    finally:
        if prev_roots is not None:
            app.state.external_transcript_roots = prev_roots
        elif hasattr(app.state, "external_transcript_roots"):
            delattr(app.state, "external_transcript_roots")
        if prev_mgr is not None:
            app.state.external_live_manager = prev_mgr
        elif hasattr(app.state, "external_live_manager"):
            delattr(app.state, "external_live_manager")


async def test_attach_list_poll_send_detach_full_loop(live_env):
    async with await _client() as client:
        attach = await client.post(
            "/api/external-agents/live/attach",
            json={"provider": "claude-code", "transcript_path": str(live_env.file)},
        )
        assert attach.status_code == 200, attach.text
        session_id = attach.json()["session"]["session_id"]
        assert attach.json()["session"]["external_session_id"] == "abc-123"

        listed = await client.get("/api/external-agents/live")
        assert any(s["session_id"] == session_id for s in listed.json()["sessions"])

        # inbound: first poll returns the existing history (attached from_start)
        poll1 = await client.post(f"/api/external-agents/live/{session_id}/poll")
        assert [m["content"] for m in poll1.json()["messages"]] == ["第一句"]

        # outbound: send resumes the session and tails the appended reply
        sent = await client.post(
            f"/api/external-agents/live/{session_id}/send",
            json={"prompt": "继续干活"},
        )
        assert sent.status_code == 200, sent.text
        body = sent.json()
        assert body["success"] is True
        assert "--resume" in body["result"]["command"]
        assert [m["content"] for m in body["result"]["new_messages"]] == ["已收到，继续"]

        detach = await client.delete(f"/api/external-agents/live/{session_id}")
        assert detach.status_code == 200
        gone = await client.get("/api/external-agents/live")
        assert all(s["session_id"] != session_id for s in gone.json()["sessions"])


async def test_attach_rejects_path_outside_root(live_env, tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_claude_line("user", "x") + "\n", encoding="utf-8")
    async with await _client() as client:
        resp = await client.post(
            "/api/external-agents/live/attach",
            json={"provider": "claude-code", "transcript_path": str(outside)},
        )
    assert resp.status_code == 400


async def test_poll_unknown_session_returns_404(live_env):
    async with await _client() as client:
        resp = await client.post("/api/external-agents/live/does-not-exist/poll")
    assert resp.status_code == 404


async def test_send_dry_run_skips_tail(live_env):
    async with await _client() as client:
        attach = await client.post(
            "/api/external-agents/live/attach",
            json={"provider": "claude-code", "transcript_path": str(live_env.file)},
        )
        session_id = attach.json()["session"]["session_id"]
        sent = await client.post(
            f"/api/external-agents/live/{session_id}/send",
            json={"prompt": "试运行", "dry_run": True},
        )
    assert sent.status_code == 200
    assert sent.json()["result"]["dry_run"] is True
    assert sent.json()["result"]["new_messages"] == []
