"""Tests for DAG execution runtime."""

from __future__ import annotations

import pytest

from symbio.agents.registry import AgentRegistry
from symbio.core.dag_runtime import DAGRuntime
from symbio.core.execution_models import (
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionPlan,
    ExecutionStatus,
)
from symbio.core.execution_planner import ExecutionPlanner
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.utils.types import Intent, Result, Task


class RecordingAgent:
    name = "worker"

    def __init__(self, result: Result | None = None, *, error: Exception | None = None):
        self.result = result
        self.error = error
        self.tasks = []

    async def execute(self, task):
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        return self.result or Result(
            task_id=task.task_id,
            success=True,
            content="done",
            data={"value": 42},
        )


def make_plan(*nodes: ExecutionNode, execution_id: str = "exec-1") -> ExecutionPlan:
    edges = [
        {"source": dependency, "target": node.node_id}
        for node in nodes
        for dependency in node.dependencies
    ]
    root_node_id = next(node.node_id for node in nodes if not node.dependencies)
    return ExecutionPlan(
        execution_id=execution_id,
        task_id="task-1",
        root_node_id=root_node_id,
        nodes=list(nodes),
        edges=edges,
    )


async def create_store(tmp_path, plan: ExecutionPlan) -> ExecutionStateStore:
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    await store.create_execution(plan, "user intent")
    return store


async def test_missing_agent_marks_node_and_execution_failed(tmp_path):
    node = ExecutionNode(node_id="node-1", name="Missing", executor="missing")
    store = await create_store(tmp_path, make_plan(node))

    await DAGRuntime(store, AgentRegistry()).run("exec-1")

    record = await store.get_execution("exec-1")
    nodes = await store.list_nodes("exec-1")
    events = await store.list_events("exec-1")

    assert record.status == ExecutionStatus.FAILED
    assert nodes[0].status == ExecutionNodeStatus.FAILED
    failed_events = [event for event in events if event.event_type == "node_failed"]
    assert failed_events[0].node_id == "node-1"
    assert failed_events[0].payload["reason"] == "agent_not_found"

    await store.close()


async def test_failed_agent_result_records_node_failed_event_and_no_node_result_artifact(
    tmp_path,
):
    node = ExecutionNode(node_id="node-1", name="Fails", executor="worker")
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    registry.register_instance(
        RecordingAgent(
            Result(
                task_id="ignored",
                success=False,
                content="could not finish",
                data={"error": "bad input"},
            )
        )
    )

    await DAGRuntime(store, registry).run("exec-1")

    record = await store.get_execution("exec-1")
    nodes = await store.list_nodes("exec-1")
    events = await store.list_events("exec-1")
    artifacts = await store.list_artifacts("exec-1")

    assert record.status == ExecutionStatus.FAILED
    assert nodes[0].status == ExecutionNodeStatus.FAILED
    assert [artifact.artifact_type for artifact in artifacts] == []
    failed_events = [event for event in events if event.event_type == "node_failed"]
    assert failed_events[0].payload["reason"] == "agent_result_failed"
    assert failed_events[0].payload["content"] == "could not finish"

    await store.close()


async def test_agent_receives_task_with_node_action_parameters_policy_and_metadata(
    tmp_path,
):
    node = ExecutionNode(
        node_id="node-1",
        name="Search",
        description="Search docs",
        action="search",
        executor="worker",
        workflow_policy={"max_rounds": 2},
        metadata={"parameters": {"query": "symbio", "limit": 3}},
    )
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    agent = RecordingAgent()
    registry.register_instance(agent)

    await DAGRuntime(store, registry).run("exec-1")

    task = agent.tasks[0]
    assert task.task_id == "node-1"
    assert task.intent.raw_text == "Search docs"
    assert task.intent.action == "search"
    assert task.intent.parameters == {"query": "symbio", "limit": 3}
    assert task.metadata["execution_id"] == "exec-1"
    assert task.metadata["node_id"] == "node-1"
    assert task.metadata["workflow_policy"] == {"max_rounds": 2}

    await store.close()


async def test_agent_receives_passthrough_task_metadata_from_execution_node(tmp_path):
    node = ExecutionNode(
        node_id="node-1",
        name="Answer",
        description="Answer with memory",
        executor="worker",
        metadata={
            "parameters": {},
            "task_metadata": {
                "memory_context": "=== 相关记忆 ===\n1. Python 优化",
                "available_tools": ["shell"],
            },
        },
    )
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    agent = RecordingAgent()
    registry.register_instance(agent)

    await DAGRuntime(store, registry).run("exec-1")

    assert agent.tasks[0].metadata["memory_context"].startswith("=== 相关记忆 ===")
    assert agent.tasks[0].metadata["available_tools"] == ["shell"]
    assert agent.tasks[0].metadata["node_metadata"] == node.metadata

    await store.close()


async def test_runtime_preserves_task_metadata_when_runtime_keys_collide(tmp_path):
    node = ExecutionNode(
        node_id="node-1",
        name="Answer",
        executor="worker",
        workflow_policy={"require_plan": True},
        metadata={
            "parameters": {},
            "task_metadata": {
                "execution_id": "caller-exec",
                "node_id": "caller-node",
                "workflow_policy": {"caller": True},
                "node_metadata": {"caller": True},
            },
        },
    )
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    agent = RecordingAgent()
    registry.register_instance(agent)

    await DAGRuntime(store, registry).run("exec-1")

    metadata = agent.tasks[0].metadata
    assert metadata["execution_id"] == "caller-exec"
    assert metadata["node_id"] == "caller-node"
    assert metadata["workflow_policy"] == {"caller": True}
    assert metadata["node_metadata"] == {"caller": True}
    assert metadata["dag_runtime"] == {
        "execution_id": "exec-1",
        "node_id": "node-1",
        "workflow_policy": {"require_plan": True},
        "node_metadata": node.metadata,
    }

    await store.close()


async def test_runtime_preserves_parameters_from_single_node_planner(tmp_path):
    task = Task(
        task_id="task-params",
        intent=Intent(
            raw_text="search docs",
            action="search",
            parameters={"query": "symbio", "limit": 5},
        ),
        metadata={"suggested_agent": "worker"},
    )
    plan = await ExecutionPlanner().plan(task, force_single_node=True)
    store = await create_store(tmp_path, plan)
    registry = AgentRegistry()
    agent = RecordingAgent()
    registry.register_instance(agent)

    await DAGRuntime(store, registry).run(plan.execution_id)

    assert agent.tasks[0].intent.parameters == {"query": "symbio", "limit": 5}

    await store.close()


async def test_runtime_auto_generates_verification_when_required(tmp_path):
    """When verification_required=True and agent succeeds, auto-generated
    verification artifact is created with pending status, and execution
    moves to NEEDS_VERIFICATION until VerificationStage completes it."""
    node = ExecutionNode(
        node_id="node-1",
        name="Write",
        executor="worker",
        verification_required=True,
    )
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    registry.register_instance(RecordingAgent())

    await DAGRuntime(store, registry).run("exec-1")

    record = await store.get_execution("exec-1")
    artifacts = await store.list_artifacts("exec-1")

    # Verification artifact is created with pending status
    verification_artifacts = [a for a in artifacts if a.artifact_type == "verification"]
    assert len(verification_artifacts) == 1
    assert verification_artifacts[0].content.get("verification_status") == "pending"
    assert verification_artifacts[0].content.get("passed") is None

    # Execution waits for verification to complete
    assert record.status == ExecutionStatus.NEEDS_VERIFICATION

    await store.close()


async def test_runtime_executes_ready_nodes_in_dependency_order(tmp_path):
    first = ExecutionNode(node_id="first", name="First", executor="worker")
    second = ExecutionNode(
        node_id="second",
        name="Second",
        executor="worker",
        dependencies=["first"],
    )
    store = await create_store(tmp_path, make_plan(first, second))
    registry = AgentRegistry()
    registry.register_instance(RecordingAgent())

    await DAGRuntime(store, registry).run("exec-1")

    record = await store.get_execution("exec-1")
    nodes = await store.list_nodes("exec-1")
    events = await store.list_events("exec-1")
    started = [event.node_id for event in events if event.event_type == "node_started"]

    assert record.status == ExecutionStatus.COMPLETED
    assert [node.status for node in nodes] == [
        ExecutionNodeStatus.COMPLETED,
        ExecutionNodeStatus.COMPLETED,
    ]
    assert started == ["first", "second"]

    await store.close()


async def test_agent_exception_marks_node_and_execution_failed(tmp_path):
    node = ExecutionNode(node_id="node-1", name="Raises", executor="worker")
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    registry.register_instance(RecordingAgent(error=RuntimeError("boom")))

    await DAGRuntime(store, registry).run("exec-1")

    record = await store.get_execution("exec-1")
    nodes = await store.list_nodes("exec-1")
    events = await store.list_events("exec-1")

    assert record.status == ExecutionStatus.FAILED
    assert nodes[0].status == ExecutionNodeStatus.FAILED
    failed_events = [event for event in events if event.event_type == "node_failed"]
    assert failed_events[0].payload["reason"] == "agent_exception"
    assert failed_events[0].payload["error"] == "boom"

    await store.close()


async def test_runtime_retries_transient_failure_and_completes(tmp_path):
    class FlakyAgent:
        name = "worker"

        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, task):
            self.calls += 1
            if self.calls == 1:
                return Result(
                    task_id=task.task_id,
                    success=False,
                    content="temporary outage",
                    data={"failure_type": "tool_transient_error"},
                )
            return Result(
                task_id=task.task_id,
                success=True,
                content="recovered",
                data={"value": 7},
            )

    node = ExecutionNode(
        node_id="node-1",
        name="Flaky",
        executor="worker",
        max_retries=2,
    )
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    agent = FlakyAgent()
    registry.register_instance(agent)

    await DAGRuntime(store, registry).run("exec-1")

    record = await store.get_execution("exec-1")
    nodes = await store.list_nodes("exec-1")
    events = await store.list_events("exec-1")
    graph_versions = await store.list_graph_versions("exec-1")

    assert record.status == ExecutionStatus.COMPLETED
    assert agent.calls == 2
    assert nodes[0].status == ExecutionNodeStatus.COMPLETED
    assert nodes[0].retry_count == 1
    node_events = [event.event_type for event in events if event.node_id == "node-1"]
    assert node_events.count("node_started") == 2
    assert node_events.count("node_failed") == 1
    assert node_events.count("node_completed") == 1
    replan_event = next(event for event in events if event.event_type == "node_replanned")
    assert replan_event.payload["decision"] == "retry"
    assert replan_event.payload["retry_count"] == 1
    mutation_event = next(event for event in events if event.event_type == "graph_mutated")
    assert mutation_event.payload["mutations"] == [
        {"action": "retry_node", "node_id": "node-1", "retry_count": 1}
    ]
    assert len(graph_versions) == 2
    assert graph_versions[-1].graph_version == 2
    assert graph_versions[-1].nodes[0]["retry_count"] == 1

    await store.close()


async def test_runtime_applies_local_patch_as_follow_up_node_and_replans_graph(tmp_path):
    class VerificationFailingAgent:
        name = "worker"

        async def execute(self, task):
            return Result(
                task_id=task.task_id,
                success=False,
                content="tests failed",
                data={"failure_type": "verification_failure"},
            )

    class RepairAgent:
        name = "general"

        def __init__(self) -> None:
            self.calls = []

        async def execute(self, task):
            self.calls.append(task.task_id)
            return Result(
                task_id=task.task_id,
                success=True,
                content="patched",
                data={"patched": True},
            )

    node = ExecutionNode(node_id="node-1", name="Verify", executor="worker")
    store = await create_store(tmp_path, make_plan(node))
    registry = AgentRegistry()
    registry.register_instance(VerificationFailingAgent())
    repair_agent = RepairAgent()
    registry.register_instance(repair_agent)

    await DAGRuntime(store, registry).run("exec-1")

    record = await store.get_execution("exec-1")
    nodes = await store.list_nodes("exec-1")
    events = await store.list_events("exec-1")
    graph_versions = await store.list_graph_versions("exec-1")

    assert record.status == ExecutionStatus.COMPLETED
    assert [node.node_id for node in nodes] == ["node-1", "node-1:repair"]
    assert nodes[0].status == ExecutionNodeStatus.FAILED
    assert nodes[1].status == ExecutionNodeStatus.COMPLETED
    assert nodes[1].dependencies == ["node-1"]
    assert repair_agent.calls == ["node-1:repair"]
    replan_event = next(event for event in events if event.event_type == "node_replanned")
    assert replan_event.payload["decision"] == "local_patch"
    assert replan_event.payload["mutation_count"] == 1
    mutation_event = next(event for event in events if event.event_type == "graph_mutated")
    assert mutation_event.payload["graph_version"] == 2
    assert mutation_event.payload["mutations"][0]["action"] == "add_node"
    assert len(graph_versions) == 2
    assert graph_versions[-1].nodes[-1]["node_id"] == "node-1:repair"

    await store.close()
