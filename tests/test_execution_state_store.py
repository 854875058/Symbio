"""Tests for async SQLite-backed execution state storage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlite3

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionPlan,
    ExecutionStatus,
)
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.core import ExecutionStateStore as CoreExecutionStateStore


def _single_node_plan(execution_id: str = "exec-1") -> ExecutionPlan:
    return ExecutionPlan(
        execution_id=execution_id,
        task_id="task-1",
        root_node_id="node-1",
        nodes=[
            ExecutionNode(
                node_id="node-1",
                name="Answer",
                description="Answer the user",
                metadata={"priority": 1},
            )
        ],
        edges=[],
        metadata={"source": "test"},
    )


async def test_create_and_restore_execution(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    plan = _single_node_plan()

    record = await store.create_execution(plan, "hello")

    assert record.execution_id == "exec-1"
    assert record.task_id == "task-1"
    assert record.intent_text == "hello"
    assert record.status == ExecutionStatus.PLANNED
    assert record.created_at.tzinfo is not None

    restored = await store.get_execution("exec-1")
    nodes = await store.list_nodes("exec-1")
    versions = await store.list_graph_versions("exec-1")
    events = await store.list_events("exec-1")

    assert restored == record
    assert nodes == plan.nodes
    assert versions[0].graph_version == 1
    assert versions[0].nodes[0]["node_id"] == "node-1"
    assert versions[0].edges == []
    assert events[0].event_type == "execution_planned"
    assert events[0].payload["intent_text"] == "hello"

    await store.close()


async def test_events_artifacts_and_graph_versions_are_append_only(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    plan = _single_node_plan()
    await store.create_execution(plan, "hello")

    first_event = await store.append_event(
        ExecutionEvent(
            execution_id="exec-1",
            event_type="node_started",
            node_id="node-1",
            payload={"attempt": 1},
        )
    )
    second_event = await store.append_event(
        ExecutionEvent(
            execution_id="exec-1",
            event_type="node_completed",
            node_id="node-1",
            payload={"ok": True},
        )
    )
    artifact = await store.append_artifact(
        ExecutionArtifact(
            execution_id="exec-1",
            node_id="node-1",
            artifact_type="answer",
            content={"text": "done"},
            metadata={"format": "text"},
        )
    )
    await store.save_graph_version(
        "exec-1",
        2,
        [{"node_id": "node-1", "status": "completed"}],
        [],
    )

    events = await store.list_events("exec-1")
    artifacts = await store.list_artifacts("exec-1")
    versions = await store.list_graph_versions("exec-1")

    assert [event.event_type for event in events] == [
        "execution_planned",
        "node_started",
        "node_completed",
    ]
    assert events[1] == first_event
    assert events[2] == second_event
    assert events[1].timestamp.tzinfo is not None
    assert artifacts == [artifact]
    assert artifacts[0].content == {"text": "done"}
    assert artifacts[0].metadata == {"format": "text"}
    assert [version.graph_version for version in versions] == [1, 2]
    assert versions[1].nodes == [{"node_id": "node-1", "status": "completed"}]

    await store.close()


async def test_update_node_and_execution_status(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    await store.create_execution(_single_node_plan(), "hello")

    updated_node = await store.update_node_status(
        "exec-1",
        "node-1",
        ExecutionNodeStatus.COMPLETED,
    )
    updated_record = await store.update_execution_status(
        "exec-1",
        ExecutionStatus.COMPLETED,
    )

    assert updated_node.status == ExecutionNodeStatus.COMPLETED
    assert (await store.list_nodes("exec-1"))[0].status == ExecutionNodeStatus.COMPLETED
    assert updated_record.status == ExecutionStatus.COMPLETED
    assert updated_record.completed_at is not None
    assert updated_record.completed_at.tzinfo is not None
    assert (await store.get_execution("exec-1")).completed_at == updated_record.completed_at

    await store.close()


async def test_close_then_reopen_reads_same_sqlite_file(tmp_path):
    db_path = tmp_path / "executions.db"
    store = ExecutionStateStore(str(db_path))
    await store.create_execution(_single_node_plan(), "persistent intent")
    await store.update_node_status("exec-1", "node-1", ExecutionNodeStatus.RUNNING)
    await store.close()

    reopened = ExecutionStateStore(str(db_path))

    assert (await reopened.get_execution("exec-1")).intent_text == "persistent intent"
    assert (await reopened.list_nodes("exec-1"))[0].status == ExecutionNodeStatus.RUNNING
    assert (await reopened.list_events("exec-1"))[0].event_type == "execution_planned"

    await reopened.close()


async def test_saving_duplicate_graph_version_fails(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    await store.create_execution(_single_node_plan(), "hello")

    with pytest.raises(sqlite3.IntegrityError):
        await store.save_graph_version("exec-1", 1, [{"node_id": "changed"}], [])

    await store.append_event(
        ExecutionEvent(
            execution_id="exec-1",
            event_type="after_failed_duplicate_version",
        )
    )
    versions = await store.list_graph_versions("exec-1")
    events = await store.list_events("exec-1")
    assert len(versions) == 1
    assert versions[0].nodes[0]["node_id"] == "node-1"
    assert events[-1].event_type == "after_failed_duplicate_version"

    await store.close()


async def test_default_constructor_and_core_export_work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    store = CoreExecutionStateStore()
    await store.create_execution(_single_node_plan(), "default path")

    assert (tmp_path / "data" / "executions.db").exists()
    assert (await store.get_execution("exec-1")).intent_text == "default path"

    await store.close()


async def test_status_updates_are_append_only_events(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    await store.create_execution(_single_node_plan(), "hello")

    await store.update_node_status("exec-1", "node-1", ExecutionNodeStatus.RUNNING)
    await store.update_execution_status("exec-1", ExecutionStatus.RUNNING)

    events = await store.list_events("exec-1")

    assert [event.event_type for event in events][-2:] == [
        "node_status_updated",
        "execution_status_updated",
    ]
    assert events[-2].node_id == "node-1"
    assert events[-2].payload["status"] == "running"
    assert events[-1].payload["status"] == "running"

    await store.close()


async def test_node_scoped_events_and_artifacts_require_existing_nodes(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    await store.create_execution(_single_node_plan(), "hello")

    with pytest.raises(ValueError):
        await store.append_event(
            ExecutionEvent(
                execution_id="exec-1",
                event_type="node_started",
                node_id="missing",
            )
        )

    with pytest.raises(ValueError):
        await store.append_artifact(
            ExecutionArtifact(
                execution_id="exec-1",
                node_id="missing",
                artifact_type="node_result",
                content={"content": "bad"},
            )
        )

    await store.append_event(
        ExecutionEvent(
            execution_id="exec-1",
            event_type="execution_level_event",
        )
    )

    assert (await store.list_events("exec-1"))[-1].node_id == ""

    await store.close()
