"""Tests for StateManager-backed agent handoff records."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.state_manager import InstructionGenerator, StateManager


async def test_record_agent_handoff_appends_structured_state():
    manager = StateManager()
    await manager.initialize("task-1", "Build the feature")

    state = await manager.record_agent_handoff(
        from_agent="planner",
        to_agent="coder",
        artifact_type="implementation_plan",
        summary="Implement API endpoint with focused tests.",
        payload={"files": ["src/app.py"], "tests": ["tests/test_app.py"]},
    )

    assert len(state.agent_handoffs) == 1
    handoff = state.agent_handoffs[0]
    assert handoff["from_agent"] == "planner"
    assert handoff["to_agent"] == "coder"
    assert handoff["artifact_type"] == "implementation_plan"
    assert handoff["summary"] == "Implement API endpoint with focused tests."
    assert handoff["payload"]["files"] == ["src/app.py"]


async def test_agent_handoffs_persist_and_restore(tmp_path):
    db_path = tmp_path / "state.db"
    manager = StateManager(persist_path=str(db_path))
    await manager.initialize("task-1", "Build the feature")
    await manager.record_agent_handoff(
        from_agent="coder",
        to_agent="reviewer",
        artifact_type="code_changes",
        summary="Implemented endpoint.",
        payload={"commit": "abc123"},
    )
    await manager.close()

    restored_manager = StateManager(persist_path=str(db_path))
    restored = await restored_manager.restore("task-1")

    assert restored is not None
    assert len(restored.agent_handoffs) == 1
    assert restored.agent_handoffs[0]["to_agent"] == "reviewer"
    assert restored.agent_handoffs[0]["payload"]["commit"] == "abc123"
    await restored_manager.close()


async def test_minimal_context_includes_recent_agent_handoffs():
    manager = StateManager()
    await manager.initialize("task-1", "Build the feature")
    await manager.record_agent_handoff(
        from_agent="planner",
        to_agent="coder",
        artifact_type="plan",
        summary="Step 1",
    )
    await manager.record_agent_handoff(
        from_agent="coder",
        to_agent="reviewer",
        artifact_type="changes",
        summary="Step 2",
    )

    context = InstructionGenerator().get_minimal_context(await manager.read())

    assert context["agent_handoffs"] == [
        {
            "from_agent": "planner",
            "to_agent": "coder",
            "artifact_type": "plan",
            "summary": "Step 1",
        },
        {
            "from_agent": "coder",
            "to_agent": "reviewer",
            "artifact_type": "changes",
            "summary": "Step 2",
        },
    ]
