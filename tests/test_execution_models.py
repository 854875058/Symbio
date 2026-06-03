"""Tests for DAG-first execution models."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionGraphVersion,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionPlan,
    ReplanDecision,
    ReplanDecisionType,
    ExecutionRecord,
    ExecutionStatus,
)


def test_execution_plan_defaults_to_generation_zero():
    plan = ExecutionPlan(
        task_id="task-1",
        root_node_id="node-1",
        nodes=[
            ExecutionNode(
                node_id="node-1",
                name="Answer",
                executor="general",
            )
        ],
        edges=[],
    )

    assert plan.execution_id
    assert plan.plan_version == 1
    assert plan.replan_generation == 0
    assert plan.nodes[0].status == ExecutionNodeStatus.PENDING


def test_execution_record_is_created_state():
    record = ExecutionRecord(task_id="task-1", intent_text="hello")

    assert record.status == ExecutionStatus.CREATED
    assert record.plan_version == 1
    assert record.replan_generation == 0


def test_event_artifact_and_graph_version_are_json_ready():
    event = ExecutionEvent(
        execution_id="exec-1",
        event_type="node_started",
        node_id="node-1",
        payload={"ok": True},
    )
    artifact = ExecutionArtifact(
        execution_id="exec-1",
        node_id="node-1",
        artifact_type="observation",
        content={"text": "done"},
    )
    version = ExecutionGraphVersion(
        execution_id="exec-1",
        graph_version=1,
        nodes=[{"id": "node-1"}],
        edges=[],
    )

    assert event.model_dump(mode="json")["payload"]["ok"] is True
    assert artifact.model_dump(mode="json")["content"]["text"] == "done"
    assert version.model_dump(mode="json")["nodes"][0]["id"] == "node-1"
    assert event.model_dump(mode="json")["timestamp"].endswith("Z")
    assert artifact.model_dump(mode="json")["created_at"].endswith("Z")
    assert version.model_dump(mode="json")["created_at"].endswith("Z")


def test_execution_model_contract_defaults_and_enums():
    node = ExecutionNode(name="Answer")
    decision = ReplanDecision()

    assert node.executor == "general"
    assert ExecutionNodeStatus.COMPLETED.value == "completed"
    assert ExecutionStatus.FAILED_POLICY.value == "failed_policy"
    assert decision.decision == ReplanDecisionType.NONE


def test_execution_record_uses_utc_timestamp():
    record = ExecutionRecord(task_id="task-1", intent_text="hello")

    assert record.model_dump(mode="json")["created_at"].endswith("Z")


def test_non_json_ready_values_fail_fast():
    with pytest.raises(ValueError):
        ExecutionEvent(
            execution_id="exec-1",
            event_type="node_started",
            payload={"bad": {1, 2, 3}},
        )

    with pytest.raises(ValueError):
        ExecutionPlan(
            task_id="task-1",
            root_node_id="node-1",
            nodes=[ExecutionNode(node_id="node-1", name="Answer")],
            edges=[],
            metadata={"bad": {1, 2, 3}},
        )


def test_invalid_root_or_edge_references_fail_fast():
    with pytest.raises(ValueError):
        ExecutionPlan(
            task_id="task-1",
            root_node_id="missing",
            nodes=[ExecutionNode(node_id="node-1", name="Answer")],
            edges=[],
        )

    with pytest.raises(ValueError):
        ExecutionPlan(
            task_id="task-1",
            root_node_id="node-1",
            nodes=[ExecutionNode(node_id="node-1", name="Answer")],
            edges=[{"source": "node-1", "target": "missing"}],
        )


def test_root_and_edges_must_match_dependencies():
    with pytest.raises(ValueError):
        ExecutionPlan(
            task_id="task-1",
            root_node_id="write",
            nodes=[
                ExecutionNode(node_id="collect", name="Collect"),
                ExecutionNode(node_id="write", name="Write", dependencies=["collect"]),
            ],
            edges=[{"source": "collect", "target": "write"}],
        )

    with pytest.raises(ValueError):
        ExecutionPlan(
            task_id="task-1",
            root_node_id="collect",
            nodes=[
                ExecutionNode(node_id="collect", name="Collect"),
                ExecutionNode(node_id="write", name="Write"),
            ],
            edges=[{"source": "collect", "target": "write"}],
        )
