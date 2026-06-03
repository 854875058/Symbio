"""Tests for compiling tasks into DAG execution plans."""

from __future__ import annotations

import pytest

from symbio.core.decomposer import DecompositionResult, SubTask
from symbio.core.execution_planner import ExecutionPlanner
from symbio.core.workflow_policy import WorkflowPolicy
from symbio.utils.types import Intent, Task, TaskComplexity


class FakeDecomposer:
    def __init__(self, result: DecompositionResult) -> None:
        self.result = result
        self.calls: list[tuple[Intent, str]] = []

    async def decompose(self, intent: Intent, task_id: str) -> DecompositionResult:
        self.calls.append((intent, task_id))
        return self.result


def make_task(
    raw_text: str = "answer the question",
    *,
    task_id: str = "task-1",
    action: str = "chat",
    workflow_policy: WorkflowPolicy | None = None,
) -> Task:
    metadata = {"suggested_agent": "researcher"}
    if workflow_policy is not None:
        metadata["workflow_policy"] = workflow_policy
    return Task(
        task_id=task_id,
        intent=Intent(
            raw_text=raw_text,
            action=action,
            parameters={"topic": "symbio"},
            estimated_complexity=TaskComplexity.LOW,
        ),
        metadata=metadata,
    )


async def test_force_single_node_compiles_plan_without_calling_decomposer():
    decomposition = DecompositionResult(
        task_id="task-1",
        original_intent="ignored",
        subtasks=[
            SubTask(
                subtask_id="ignored",
                name="Ignored",
                description="Should not be used",
                action="write_code",
            )
        ],
    )
    decomposer = FakeDecomposer(decomposition)
    policy = WorkflowPolicy(require_verification_before_completion=False)
    task = make_task("say hello", action="", workflow_policy=policy)

    plan = await ExecutionPlanner(decomposer).plan(task, force_single_node=True)

    assert decomposer.calls == []
    assert plan.task_id == "task-1"
    assert plan.root_node_id == "task-1:root"
    assert plan.edges == []
    assert [node.node_id for node in plan.nodes] == ["task-1:root"]
    node = plan.nodes[0]
    assert node.name == "say hello"
    assert node.description == "say hello"
    assert node.action == "chat"
    assert node.executor == "researcher"
    assert node.workflow_policy["require_verification_before_completion"] is False
    assert node.verification_required is False


async def test_decomposition_compiles_dependencies_to_edges():
    decomposition = DecompositionResult(
        task_id="task-2",
        original_intent="write report",
        reasoning="collect first, then write",
        needs_debate=True,
        subtasks=[
            SubTask(
                subtask_id="collect",
                name="Collect",
                description="Collect source material",
                action="search",
                suggested_agent="researcher",
            ),
            SubTask(
                subtask_id="write",
                name="Write",
                description="Write the report",
                action="write_code",
                suggested_agent="writer",
                dependencies=["collect"],
                parameters={"format": "brief"},
            ),
        ],
    )
    decomposer = FakeDecomposer(decomposition)
    task = make_task("write report", task_id="task-2")

    plan = await ExecutionPlanner(decomposer).plan(task)

    assert [node.node_id for node in plan.nodes] == ["collect", "write"]
    assert plan.edges == [{"source": "collect", "target": "write"}]
    assert plan.root_node_id == "collect"
    assert plan.metadata["decomposition_reasoning"] == "collect first, then write"
    assert plan.metadata["needs_debate"] is True


async def test_single_subtask_decomposition_falls_back_to_consistent_single_node_plan():
    decomposition = DecompositionResult(
        task_id="task-3",
        original_intent="single",
        subtasks=[
            SubTask(
                subtask_id="only",
                name="Only",
                description="Only subtask",
                action="search",
                dependencies=["missing"],
            )
        ],
    )
    task = make_task("single", task_id="task-3")

    plan = await ExecutionPlanner(FakeDecomposer(decomposition)).plan(task)

    assert [node.node_id for node in plan.nodes] == ["task-3:root"]
    assert plan.nodes[0].dependencies == []
    assert plan.edges == []


async def test_workflow_policy_verification_flag_propagates_to_decomposition_nodes():
    policy = WorkflowPolicy(require_verification_before_completion=False)
    decomposition = DecompositionResult(
        task_id="task-4",
        original_intent="multi",
        subtasks=[
            SubTask(subtask_id="collect", name="Collect", description="Collect", action="search"),
            SubTask(
                subtask_id="write",
                name="Write",
                description="Write",
                action="write_code",
                dependencies=["collect"],
            ),
        ],
    )
    task = make_task("multi", task_id="task-4", workflow_policy=policy)

    plan = await ExecutionPlanner(FakeDecomposer(decomposition)).plan(task)

    assert [node.verification_required for node in plan.nodes] == [False, False]
    assert [
        node.workflow_policy["require_verification_before_completion"]
        for node in plan.nodes
    ] == [False, False]


async def test_invalid_decomposition_graph_falls_back_to_single_node_plan():
    invalid_cases = [
        [
            SubTask(subtask_id="root", name="Root", description="Root", action="chat"),
            SubTask(
                subtask_id="a",
                name="A",
                description="A",
                action="chat",
                dependencies=["b"],
            ),
            SubTask(
                subtask_id="b",
                name="B",
                description="B",
                action="chat",
                dependencies=["a"],
            ),
        ],
        [
            SubTask(
                subtask_id="a",
                name="A",
                description="A",
                action="chat",
                dependencies=["missing"],
            ),
            SubTask(subtask_id="b", name="B", description="B", action="chat"),
        ],
        [
            SubTask(subtask_id="dup", name="A", description="A", action="chat"),
            SubTask(subtask_id="dup", name="B", description="B", action="chat"),
        ],
    ]

    for subtasks in invalid_cases:
        decomposition = DecompositionResult(
            task_id="task-invalid",
            original_intent="invalid graph",
            subtasks=subtasks,
        )

        plan = await ExecutionPlanner(FakeDecomposer(decomposition)).plan(
            make_task("invalid graph", task_id="task-invalid")
        )

        assert [node.node_id for node in plan.nodes] == ["task-invalid:root"]
        assert plan.edges == []
        assert plan.metadata["decomposition_rejected"] is True


async def test_non_json_ready_subtask_parameters_fail_fast():
    decomposition = DecompositionResult(
        task_id="task-json",
        original_intent="bad json",
        subtasks=[
            SubTask(
                subtask_id="collect",
                name="Collect",
                description="Collect",
                action="search",
                parameters={"bad": {1, 2, 3}},
            ),
            SubTask(
                subtask_id="write",
                name="Write",
                description="Write",
                action="write_code",
                dependencies=["collect"],
            ),
        ],
    )

    with pytest.raises(ValueError):
        await ExecutionPlanner(FakeDecomposer(decomposition)).plan(
            make_task("bad json", task_id="task-json")
        )
