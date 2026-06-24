"""Tests for Codex↔Claude Code relay orchestration (批次16)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.interfaces.api import app
from symbio.tools.external_relay import ExternalRelayOrchestrator, RelayConfig


class StubController:
    """Stand-in controller: echoes a numbered reply, can fail at a chosen turn."""

    def __init__(self, fail_at: int | None = None):
        self.created: dict[str, str] = {}
        self.runs: list[tuple[str, object]] = []
        self.fail_at = fail_at

    def create_session(self, request):
        sid = f"s-{request.provider}-{len(self.created)}"
        self.created[sid] = request.provider
        return SimpleNamespace(session_id=sid, external_session_id="", workspace=request.workspace)

    async def run_session(self, session_id, request):
        n = len(self.runs)
        self.runs.append((session_id, request))
        fail = self.fail_at is not None and n == self.fail_at
        return SimpleNamespace(
            success=not fail,
            dry_run=request.dry_run,
            run_id=f"r{n}",
            exit_code=2 if fail else 0,
            command=[],
            stdout="" if fail else f"<reply-{n}>",
            error="boom" if fail else "",
        )


async def test_relay_alternates_providers_and_pipes_output():
    stub = StubController()
    orch = ExternalRelayOrchestrator(controller=stub, workspace_root=".")
    result = await orch.run(
        RelayConfig(
            seed_prompt="实现快速排序并自测",
            provider_a="codex",
            provider_b="claude-code",
            rounds=3,
            role_a="你负责编写代码",
            role_b="你负责审查",
        )
    )

    assert result.success is True
    assert [t.provider for t in result.turns] == ["codex", "claude-code", "codex"]

    # first turn carries the seed; later turns are piped the previous output
    assert "实现快速排序并自测" in result.turns[0].prompt
    assert result.turns[0].output == "<reply-0>"
    assert "<reply-0>" in result.turns[1].prompt  # A's output -> B's prompt
    assert "<reply-1>" in result.turns[2].prompt  # B's output -> A's prompt
    # role hints land on the right side
    assert result.turns[0].prompt.startswith("你负责编写代码")
    assert result.turns[1].prompt.startswith("你负责审查")


async def test_relay_stops_on_failure():
    stub = StubController(fail_at=1)
    orch = ExternalRelayOrchestrator(controller=stub, workspace_root=".")
    result = await orch.run(
        RelayConfig(seed_prompt="任务", provider_a="codex", provider_b="claude-code", rounds=4)
    )
    assert result.success is False
    assert len(result.turns) == 2  # stopped right after the failing turn
    assert result.turns[1].success is False
    assert result.turns[1].error == "boom"


async def test_relay_dry_run_passes_flag_through():
    stub = StubController()
    orch = ExternalRelayOrchestrator(controller=stub, workspace_root=".")
    result = await orch.run(RelayConfig(seed_prompt="x", rounds=2, dry_run=True))
    assert all(req.dry_run for _, req in stub.runs)
    assert result.success is True


async def test_relay_api_endpoint_runs_relay():
    stub = StubController()
    prev = getattr(app.state, "external_agent_controller", None)
    app.state.external_agent_controller = stub
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/external-agents/relay",
                json={
                    "seed_prompt": "写个 hello world 再 review",
                    "provider_a": "codex",
                    "provider_b": "claude-code",
                    "rounds": 2,
                },
            )
    finally:
        if prev is not None:
            app.state.external_agent_controller = prev
        elif hasattr(app.state, "external_agent_controller"):
            delattr(app.state, "external_agent_controller")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    providers = [t["provider"] for t in body["result"]["turns"]]
    assert providers == ["codex", "claude-code"]
