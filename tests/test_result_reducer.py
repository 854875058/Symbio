"""Tests for reducing DAG execution state into a user result."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionRecord,
    ExecutionStatus,
)
from symbio.core.result_reducer import ResultReducer


def _record(status: ExecutionStatus = ExecutionStatus.COMPLETED) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id="exec-1",
        task_id="task-1",
        intent_text="answer",
        status=status,
    )


def test_reducer_uses_node_result_content_and_execution_metadata():
    result = ResultReducer().reduce(
        _record(),
        nodes=[ExecutionNode(node_id="node-1", name="Answer")],
        artifacts=[
            ExecutionArtifact(
                execution_id="exec-1",
                node_id="node-1",
                artifact_type="observation",
                content={"content": "ignored"},
            ),
            ExecutionArtifact(
                execution_id="exec-1",
                node_id="node-1",
                artifact_type="node_result",
                content={"content": "final answer"},
            ),
        ],
        events=[
            ExecutionEvent(execution_id="exec-1", event_type="node_started"),
            ExecutionEvent(execution_id="exec-1", event_type="node_completed"),
        ],
    )

    assert result.task_id == "task-1"
    assert result.success is True
    assert result.content == "final answer"
    assert result.data == {
        "execution_id": "exec-1",
        "status": "completed",
        "node_count": 1,
        "event_count": 2,
        "artifact_count": 2,
    }


def test_reducer_success_depends_on_completed_status():
    result = ResultReducer().reduce(
        _record(ExecutionStatus.FAILED),
        nodes=[],
        artifacts=[],
        events=[],
    )

    assert result.success is False
    assert result.data["status"] == "failed"


def test_reducer_requires_verification_artifact_for_verification_nodes():
    result = ResultReducer().reduce(
        _record(),
        nodes=[
            ExecutionNode(
                node_id="node-1",
                name="Write file",
                verification_required=True,
            )
        ],
        artifacts=[
            ExecutionArtifact(
                execution_id="exec-1",
                node_id="node-1",
                artifact_type="node_result",
                content={"content": "candidate answer"},
            )
        ],
        events=[],
    )

    assert result.success is False
    assert "verification" in result.content.lower()
    assert result.data["status"] == "needs_verification"
    assert result.data["missing_verification"] == ["node-1"]


def test_reducer_accepts_verification_result_artifact():
    result = ResultReducer().reduce(
        _record(),
        nodes=[
            ExecutionNode(
                node_id="node-1",
                name="Write file",
                verification_required=True,
            )
        ],
        artifacts=[
            ExecutionArtifact(
                execution_id="exec-1",
                node_id="node-1",
                artifact_type="verification_result",
                content={"passed": True},
            )
        ],
        events=[],
    )

    assert result.success is True
    assert result.data["status"] == "completed"


def test_reducer_rejects_failed_verification_result_artifact():
    result = ResultReducer().reduce(
        _record(),
        nodes=[
            ExecutionNode(
                node_id="node-1",
                name="Write file",
                verification_required=True,
            )
        ],
        artifacts=[
            ExecutionArtifact(
                execution_id="exec-1",
                node_id="node-1",
                artifact_type="verification_result",
                content={"passed": False},
            )
        ],
        events=[],
    )

    assert result.success is False
    assert result.data["status"] == "needs_verification"
    assert result.data["missing_verification"] == ["node-1"]


def test_reducer_requires_explicit_passed_true_verification_artifact():
    result = ResultReducer().reduce(
        _record(),
        nodes=[
            ExecutionNode(
                node_id="node-1",
                name="Write file",
                verification_required=True,
            )
        ],
        artifacts=[
            ExecutionArtifact(
                execution_id="exec-1",
                node_id="node-1",
                artifact_type="verification",
                content={"summary": "checked"},
            )
        ],
        events=[],
    )

    assert result.success is False
    assert result.data["missing_verification"] == ["node-1"]


def test_reducer_uses_latest_node_result_artifact_when_multiple_exist():
    result = ResultReducer().reduce(
        _record(),
        nodes=[],
        artifacts=[
            ExecutionArtifact(
                execution_id="exec-1",
                artifact_type="node_result",
                content={"content": "old answer"},
            ),
            ExecutionArtifact(
                execution_id="exec-1",
                artifact_type="node_result",
                content={"content": "latest answer"},
            ),
        ],
        events=[],
    )

    assert result.content == "latest answer"


def test_reducer_skips_empty_node_result_and_uses_default_when_needed():
    result = ResultReducer().reduce(
        _record(),
        nodes=[],
        artifacts=[
            ExecutionArtifact(
                execution_id="exec-1",
                artifact_type="node_result",
                content={"content": ""},
            )
        ],
        events=[],
    )

    assert result.content == "DAG execution completed."


def test_reducer_empty_content_fallback_is_status_aware():
    result = ResultReducer().reduce(
        _record(ExecutionStatus.FAILED),
        nodes=[],
        artifacts=[],
        events=[],
    )

    assert result.success is False
    assert result.content == "DAG execution failed."
