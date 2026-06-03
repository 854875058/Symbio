"""Tests for DAG orchestrator composition and execution flow."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core import DAGOrchestrator
from symbio.agents.registry import AgentRegistry
from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionStatus,
)
from symbio.utils.types import Intent, Result, Task


class FakePlanner:
    def __init__(self, plan: ExecutionPlan) -> None:
        self.planned_result = plan
        self.calls = []

    async def plan(self, task: Task) -> ExecutionPlan:
        self.calls.append(task)
        return self.planned_result


class FailingPlanner:
    async def plan(self, task: Task) -> ExecutionPlan:
        raise RuntimeError("planner boom")


class FakeStore:
    def __init__(
        self,
        record: ExecutionRecord,
        nodes: list[ExecutionNode],
        artifacts: list[ExecutionArtifact],
        events: list[ExecutionEvent],
    ) -> None:
        self.record = record
        self.nodes = nodes
        self.artifacts = artifacts
        self.events = events
        self.create_calls = []
        self.get_calls = []
        self.node_calls = []
        self.artifact_calls = []
        self.event_calls = []

    async def create_execution(
        self, plan: ExecutionPlan, intent_text: str
    ) -> ExecutionRecord:
        self.create_calls.append((plan, intent_text))
        return self.record

    async def get_execution(self, execution_id: str) -> ExecutionRecord:
        self.get_calls.append(execution_id)
        return self.record

    async def list_nodes(self, execution_id: str) -> list[ExecutionNode]:
        self.node_calls.append(execution_id)
        return self.nodes

    async def list_artifacts(self, execution_id: str) -> list[ExecutionArtifact]:
        self.artifact_calls.append(execution_id)
        return self.artifacts

    async def list_events(self, execution_id: str) -> list[ExecutionEvent]:
        self.event_calls.append(execution_id)
        return self.events


class FakeRuntime:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, execution_id: str) -> None:
        self.calls.append(execution_id)


class FakeReducer:
    def __init__(self, result: Result) -> None:
        self.result = result
        self.calls = []

    def reduce(self, record, nodes, artifacts, events) -> Result:
        self.calls.append((record, nodes, artifacts, events))
        return self.result


def _task() -> Task:
    return Task(
        task_id="task-1",
        intent=Intent(raw_text="Write summary", action="chat"),
    )


def _plan() -> ExecutionPlan:
    node = ExecutionNode(node_id="node-1", name="Write summary")
    return ExecutionPlan(
        execution_id="exec-1",
        task_id="task-1",
        root_node_id="node-1",
        nodes=[node],
        edges=[],
    )


async def test_execute_plans_persists_runs_and_reduces_state():
    plan = _plan()
    record = ExecutionRecord(
        execution_id="exec-1",
        task_id="task-1",
        intent_text="Write summary",
        status=ExecutionStatus.COMPLETED,
    )
    nodes = [ExecutionNode(node_id="node-1", name="Write summary")]
    artifacts = [
        ExecutionArtifact(
            execution_id="exec-1",
            node_id="node-1",
            artifact_type="node_result",
            content={"content": "final summary"},
        )
    ]
    events = [ExecutionEvent(execution_id="exec-1", event_type="node_completed")]
    expected = Result(
        task_id="task-1",
        success=True,
        content="final summary",
        data={"status": "completed"},
    )
    planner = FakePlanner(plan)
    store = FakeStore(record, nodes, artifacts, events)
    runtime = FakeRuntime()
    reducer = FakeReducer(expected)

    result = await DAGOrchestrator(
        planner=planner,
        store=store,
        runtime=runtime,
        reducer=reducer,
        registry=object(),
    ).execute(_task())

    assert result is expected
    assert planner.calls[0].task_id == "task-1"
    assert store.create_calls == [(plan, "Write summary")]
    assert runtime.calls == ["exec-1"]
    assert store.get_calls == ["exec-1"]
    assert store.node_calls == ["exec-1"]
    assert store.artifact_calls == ["exec-1"]
    assert store.event_calls == ["exec-1"]
    assert reducer.calls == [(record, nodes, artifacts, events)]


def test_default_constructor_shares_store_between_orchestrator_and_runtime():
    orchestrator = DAGOrchestrator(registry=object())

    assert orchestrator.runtime.store is orchestrator.store


def test_default_constructor_registers_general_agent_for_standard_registry():
    registry = AgentRegistry()

    DAGOrchestrator(registry=registry)

    assert registry.get("general") is not None


async def test_execute_returns_needs_verification_result_from_reducer():
    plan = _plan()
    record = ExecutionRecord(
        execution_id="exec-1",
        task_id="task-1",
        intent_text="Write summary",
        status=ExecutionStatus.NEEDS_VERIFICATION,
    )
    nodes = [
        ExecutionNode(
            node_id="node-1",
            name="Write summary",
            verification_required=True,
        )
    ]
    artifacts = [
        ExecutionArtifact(
            execution_id="exec-1",
            node_id="node-1",
            artifact_type="node_result",
            content={"content": "draft summary"},
        )
    ]
    events = [
        ExecutionEvent(
            execution_id="exec-1",
            event_type="execution_needs_verification",
            payload={"missing_verification": ["node-1"]},
        )
    ]
    expected = Result(
        task_id="task-1",
        success=False,
        content="Verification required before returning the final result: node-1",
        data={
            "execution_id": "exec-1",
            "status": "needs_verification",
            "missing_verification": ["node-1"],
        },
    )
    runtime = FakeRuntime()
    reducer = FakeReducer(expected)

    result = await DAGOrchestrator(
        planner=FakePlanner(plan),
        store=FakeStore(record, nodes, artifacts, events),
        runtime=runtime,
        reducer=reducer,
        registry=object(),
    ).execute(_task())

    assert runtime.calls == ["exec-1"]
    assert result is expected
    assert result.success is False
    assert result.data["status"] == "needs_verification"


async def test_execute_converts_pipeline_exception_to_failed_result():
    task = _task()

    result = await DAGOrchestrator(
        planner=FailingPlanner(),
        store=object(),
        runtime=object(),
        reducer=object(),
        registry=object(),
    ).execute(task)

    assert result.task_id == task.task_id
    assert result.success is False
    assert result.data["status"] == "failed"
    assert result.data["error_type"] == "RuntimeError"
    assert "planner boom" in result.content
