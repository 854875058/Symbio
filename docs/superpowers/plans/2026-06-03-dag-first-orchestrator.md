# DAG-First Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Symbio's Orchestrator-owned execution path with a DAG-first execution kernel while keeping user-facing results result-first.

**Architecture:** The thin Orchestrator prepares a `Task`, then delegates to `DAGOrchestrator`. `ExecutionPlanner` compiles tasks into an execution graph, `DAGRuntime` runs graph nodes through existing agents, `ExecutionStateStore` persists execution state, `Replanner` decides retry/patch/replan actions, and `ResultReducer` produces the final `Result`.

**Tech Stack:** Python 3.12, Pydantic, pytest, pytest-asyncio, aiosqlite, existing `DAGEngine`, existing agent registry, FastAPI, plain Web UI.

---

## File Structure

- Create `src/symbio/core/execution_models.py`
  - Owns execution-specific Pydantic models and enums.
- Create `src/symbio/core/execution_state_store.py`
  - Owns SQLite persistence for executions, nodes, events, artifacts, and graph versions.
- Create `src/symbio/core/execution_planner.py`
  - Compiles `Task` and `DecompositionResult` into `ExecutionPlan`.
- Create `src/symbio/core/dag_runtime.py`
  - Runs `ExecutionPlan` nodes using the existing agent registry and records execution state.
- Create `src/symbio/core/replanner.py`
  - Converts node failures and observations into explicit runtime decisions.
- Create `src/symbio/core/result_reducer.py`
  - Converts persisted runtime state into final result-first `Result`.
- Create `src/symbio/core/dag_orchestrator.py`
  - Coordinates planner, store, runtime, and reducer.
- Modify `src/symbio/core/orchestrator.py`
  - Keep ingress logic and delegate execution to `DAGOrchestrator`.
- Modify `src/symbio/core/__init__.py`
  - Export the new execution components.
- Modify `src/symbio/interfaces/api.py`
  - Add execution endpoints and enrich existing task DAG endpoint.
- Modify `web/app.js`
  - Render execution timeline, graph state summary, graph version selector, and artifacts.
- Modify `web/style.css`
  - Style execution panels.
- Add tests:
  - `tests/test_execution_models.py`
  - `tests/test_execution_state_store.py`
  - `tests/test_execution_planner.py`
  - `tests/test_dag_runtime.py`
  - `tests/test_replanner.py`
  - `tests/test_result_reducer.py`
  - `tests/test_dag_orchestrator.py`
  - Extend `tests/test_phase2.py`
  - Extend `tests/test_integration.py`

---

### Task 1: Execution Models

**Files:**
- Create: `src/symbio/core/execution_models.py`
- Test: `tests/test_execution_models.py`

- [ ] **Step 1: Write failing tests for execution model defaults**

Create `tests/test_execution_models.py`:

```python
"""Tests for DAG-first execution models."""

from __future__ import annotations

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionGraphVersion,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionPlan,
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

    assert plan.plan_version == 1
    assert plan.replan_generation == 0
    assert plan.nodes[0].status == ExecutionNodeStatus.PENDING


def test_execution_record_is_created_state():
    record = ExecutionRecord(task_id="task-1", intent_text="hello")

    assert record.status == ExecutionStatus.CREATED
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
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python -m pytest tests/test_execution_models.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'symbio.core.execution_models'`.

- [ ] **Step 3: Implement execution models**

Create `src/symbio/core/execution_models.py`:

```python
"""Execution models for DAG-first task orchestration."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ExecutionStatus(str, Enum):
    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    WAITING_CLARIFICATION = "waiting_clarification"
    REPLANNING = "replanning"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_VERIFICATION = "needs_verification"
    FAILED_POLICY = "failed_policy"


class ExecutionNodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReplanDecisionType(str, Enum):
    NONE = "none"
    RETRY = "retry"
    LOCAL_PATCH = "local_patch"
    GLOBAL_REPLAN = "global_replan"
    WAITING_HITL = "waiting_hitl"
    WAITING_CLARIFICATION = "waiting_clarification"
    FAIL = "fail"


class ExecutionNode(BaseModel):
    node_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    action: str = "chat"
    executor: str = "general"
    dependencies: list[str] = Field(default_factory=list)
    status: ExecutionNodeStatus = ExecutionNodeStatus.PENDING
    retry_count: int = 0
    max_retries: int = 1
    workflow_policy: dict[str, Any] = Field(default_factory=dict)
    verification_required: bool = False
    hitl_policy: dict[str, Any] = Field(default_factory=dict)
    input_refs: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    root_node_id: str
    nodes: list[ExecutionNode]
    edges: list[dict[str, str]] = Field(default_factory=list)
    plan_version: int = 1
    replan_generation: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionRecord(BaseModel):
    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    intent_text: str
    status: ExecutionStatus = ExecutionStatus.CREATED
    plan_version: int = 1
    replan_generation: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    node_id: str = ""
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


class ExecutionArtifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    node_id: str = ""
    artifact_type: str
    content: dict[str, Any] = Field(default_factory=dict)
    path_ref: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class ExecutionGraphVersion(BaseModel):
    execution_id: str
    graph_version: int
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    created_at: datetime = Field(default_factory=datetime.now)


class ReplanDecision(BaseModel):
    decision: ReplanDecisionType = ReplanDecisionType.NONE
    reason: str = ""
    node_id: str = ""
    mutations: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: Run model tests**

Run:

```bash
python -m pytest tests/test_execution_models.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/symbio/core/execution_models.py tests/test_execution_models.py
git commit -m "feat: add DAG execution models"
```

---

### Task 2: Execution State Store

**Files:**
- Create: `src/symbio/core/execution_state_store.py`
- Test: `tests/test_execution_state_store.py`

- [ ] **Step 1: Write failing persistence tests**

Create `tests/test_execution_state_store.py`:

```python
"""Tests for DAG execution state persistence."""

from __future__ import annotations

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionPlan,
    ExecutionStatus,
)
from symbio.core.execution_state_store import ExecutionStateStore


async def test_create_and_restore_execution(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    plan = ExecutionPlan(
        task_id="task-1",
        root_node_id="node-1",
        nodes=[ExecutionNode(node_id="node-1", name="Answer", executor="general")],
        edges=[],
    )

    record = await store.create_execution(plan, intent_text="hello")
    restored = await store.get_execution(record.execution_id)
    nodes = await store.list_nodes(record.execution_id)

    assert restored is not None
    assert restored.status == ExecutionStatus.PLANNED
    assert nodes[0].node_id == "node-1"

    await store.close()


async def test_events_artifacts_and_graph_versions_are_append_only(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    plan = ExecutionPlan(
        task_id="task-1",
        root_node_id="node-1",
        nodes=[ExecutionNode(node_id="node-1", name="Answer")],
        edges=[],
    )
    record = await store.create_execution(plan, intent_text="hello")

    await store.append_event(ExecutionEvent(
        execution_id=record.execution_id,
        node_id="node-1",
        event_type="node_started",
        payload={"status": "running"},
    ))
    await store.append_artifact(ExecutionArtifact(
        execution_id=record.execution_id,
        node_id="node-1",
        artifact_type="observation",
        content={"content": "done"},
    ))
    await store.save_graph_version(record.execution_id, 2, [{"id": "node-1"}], [])

    events = await store.list_events(record.execution_id)
    artifacts = await store.list_artifacts(record.execution_id)
    versions = await store.list_graph_versions(record.execution_id)

    assert events[0].event_type == "node_started"
    assert artifacts[0].content["content"] == "done"
    assert [v.graph_version for v in versions] == [1, 2]

    await store.close()


async def test_update_node_and_execution_status(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    plan = ExecutionPlan(
        task_id="task-1",
        root_node_id="node-1",
        nodes=[ExecutionNode(node_id="node-1", name="Answer")],
        edges=[],
    )
    record = await store.create_execution(plan, intent_text="hello")

    await store.update_node_status(record.execution_id, "node-1", ExecutionNodeStatus.COMPLETED)
    await store.update_execution_status(record.execution_id, ExecutionStatus.COMPLETED)

    nodes = await store.list_nodes(record.execution_id)
    restored = await store.get_execution(record.execution_id)

    assert nodes[0].status == ExecutionNodeStatus.COMPLETED
    assert restored is not None
    assert restored.status == ExecutionStatus.COMPLETED
    assert restored.completed_at is not None

    await store.close()
```

- [ ] **Step 2: Run persistence tests and verify failure**

Run:

```bash
python -m pytest tests/test_execution_state_store.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'symbio.core.execution_state_store'`.

- [ ] **Step 3: Implement `ExecutionStateStore`**

Create `src/symbio/core/execution_state_store.py`:

```python
"""SQLite persistence for DAG-first executions."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionGraphVersion,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionStatus,
)


class ExecutionStateStore:
    def __init__(self, db_path: str = "data/executions.db") -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._db is not None:
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                intent_text TEXT NOT NULL,
                status TEXT NOT NULL,
                plan_version INTEGER NOT NULL,
                replan_generation INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_nodes (
                execution_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                node_json TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (execution_id, node_id)
            );
            CREATE TABLE IF NOT EXISTS execution_events (
                event_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                node_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_artifacts (
                artifact_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                node_id TEXT,
                artifact_type TEXT NOT NULL,
                content_json TEXT NOT NULL,
                path_ref TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_graph_versions (
                execution_id TEXT NOT NULL,
                graph_version INTEGER NOT NULL,
                nodes_json TEXT NOT NULL,
                edges_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (execution_id, graph_version)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_events_execution
            ON execution_events(execution_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_execution_artifacts_execution
            ON execution_artifacts(execution_id);
        """)
        await self._db.commit()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("ExecutionStateStore is not connected")
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def create_execution(self, plan: ExecutionPlan, intent_text: str) -> ExecutionRecord:
        await self.connect()
        record = ExecutionRecord(
            execution_id=plan.execution_id,
            task_id=plan.task_id,
            intent_text=intent_text,
            status=ExecutionStatus.PLANNED,
            plan_version=plan.plan_version,
            replan_generation=plan.replan_generation,
            metadata=plan.metadata,
        )
        await self.db.execute(
            """
            INSERT OR REPLACE INTO executions
            (execution_id, task_id, intent_text, status, plan_version, replan_generation,
             created_at, completed_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.execution_id,
                record.task_id,
                record.intent_text,
                record.status.value,
                record.plan_version,
                record.replan_generation,
                record.created_at.isoformat(),
                record.completed_at.isoformat() if record.completed_at else None,
                json.dumps(record.metadata, ensure_ascii=False),
            ),
        )
        for node in plan.nodes:
            await self.upsert_node(plan.execution_id, node)
        await self.save_graph_version(
            plan.execution_id,
            plan.plan_version,
            [node.model_dump(mode="json") for node in plan.nodes],
            plan.edges,
        )
        await self.append_event(ExecutionEvent(
            execution_id=plan.execution_id,
            event_type="execution_planned",
            payload={"node_count": len(plan.nodes), "edge_count": len(plan.edges)},
        ))
        await self.db.commit()
        return record

    async def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        await self.connect()
        cursor = await self.db.execute(
            """
            SELECT execution_id, task_id, intent_text, status, plan_version,
                   replan_generation, created_at, completed_at, metadata_json
            FROM executions WHERE execution_id = ?
            """,
            (execution_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ExecutionRecord(
            execution_id=row[0],
            task_id=row[1],
            intent_text=row[2],
            status=ExecutionStatus(row[3]),
            plan_version=row[4],
            replan_generation=row[5],
            created_at=datetime.fromisoformat(row[6]),
            completed_at=datetime.fromisoformat(row[7]) if row[7] else None,
            metadata=json.loads(row[8]),
        )

    async def update_execution_status(self, execution_id: str, status: ExecutionStatus) -> None:
        await self.connect()
        completed_at = datetime.now().isoformat() if status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.FAILED_POLICY,
        } else None
        await self.db.execute(
            "UPDATE executions SET status = ?, completed_at = ? WHERE execution_id = ?",
            (status.value, completed_at, execution_id),
        )
        await self.db.commit()

    async def upsert_node(self, execution_id: str, node: ExecutionNode) -> None:
        await self.connect()
        await self.db.execute(
            """
            INSERT OR REPLACE INTO execution_nodes
            (execution_id, node_id, node_json, status) VALUES (?, ?, ?, ?)
            """,
            (
                execution_id,
                node.node_id,
                node.model_dump_json(),
                node.status.value,
            ),
        )

    async def list_nodes(self, execution_id: str) -> list[ExecutionNode]:
        await self.connect()
        cursor = await self.db.execute(
            "SELECT node_json FROM execution_nodes WHERE execution_id = ? ORDER BY node_id",
            (execution_id,),
        )
        return [ExecutionNode.model_validate_json(row[0]) for row in await cursor.fetchall()]

    async def update_node_status(
        self,
        execution_id: str,
        node_id: str,
        status: ExecutionNodeStatus,
    ) -> None:
        await self.connect()
        nodes = await self.list_nodes(execution_id)
        target = next(node for node in nodes if node.node_id == node_id)
        target.status = status
        await self.upsert_node(execution_id, target)
        await self.db.commit()

    async def append_event(self, event: ExecutionEvent) -> None:
        await self.connect()
        await self.db.execute(
            """
            INSERT INTO execution_events
            (event_id, execution_id, node_id, event_type, payload_json, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.execution_id,
                event.node_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False),
                event.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def list_events(self, execution_id: str) -> list[ExecutionEvent]:
        await self.connect()
        cursor = await self.db.execute(
            """
            SELECT event_id, execution_id, node_id, event_type, payload_json, timestamp
            FROM execution_events WHERE execution_id = ? ORDER BY timestamp ASC
            """,
            (execution_id,),
        )
        rows = await cursor.fetchall()
        return [
            ExecutionEvent(
                event_id=row[0],
                execution_id=row[1],
                node_id=row[2] or "",
                event_type=row[3],
                payload=json.loads(row[4]),
                timestamp=datetime.fromisoformat(row[5]),
            )
            for row in rows
        ]

    async def append_artifact(self, artifact: ExecutionArtifact) -> None:
        await self.connect()
        await self.db.execute(
            """
            INSERT INTO execution_artifacts
            (artifact_id, execution_id, node_id, artifact_type, content_json, path_ref, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.artifact_id,
                artifact.execution_id,
                artifact.node_id,
                artifact.artifact_type,
                json.dumps(artifact.content, ensure_ascii=False),
                artifact.path_ref,
                artifact.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def list_artifacts(self, execution_id: str) -> list[ExecutionArtifact]:
        await self.connect()
        cursor = await self.db.execute(
            """
            SELECT artifact_id, execution_id, node_id, artifact_type, content_json, path_ref, created_at
            FROM execution_artifacts WHERE execution_id = ? ORDER BY created_at ASC
            """,
            (execution_id,),
        )
        rows = await cursor.fetchall()
        return [
            ExecutionArtifact(
                artifact_id=row[0],
                execution_id=row[1],
                node_id=row[2] or "",
                artifact_type=row[3],
                content=json.loads(row[4]),
                path_ref=row[5],
                created_at=datetime.fromisoformat(row[6]),
            )
            for row in rows
        ]

    async def save_graph_version(
        self,
        execution_id: str,
        graph_version: int,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> None:
        await self.connect()
        version = ExecutionGraphVersion(
            execution_id=execution_id,
            graph_version=graph_version,
            nodes=nodes,
            edges=edges,
        )
        await self.db.execute(
            """
            INSERT OR REPLACE INTO execution_graph_versions
            (execution_id, graph_version, nodes_json, edges_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                graph_version,
                json.dumps(nodes, ensure_ascii=False),
                json.dumps(edges, ensure_ascii=False),
                version.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def list_graph_versions(self, execution_id: str) -> list[ExecutionGraphVersion]:
        await self.connect()
        cursor = await self.db.execute(
            """
            SELECT execution_id, graph_version, nodes_json, edges_json, created_at
            FROM execution_graph_versions WHERE execution_id = ? ORDER BY graph_version ASC
            """,
            (execution_id,),
        )
        rows = await cursor.fetchall()
        return [
            ExecutionGraphVersion(
                execution_id=row[0],
                graph_version=row[1],
                nodes=json.loads(row[2]),
                edges=json.loads(row[3]),
                created_at=datetime.fromisoformat(row[4]),
            )
            for row in rows
        ]
```

- [ ] **Step 4: Run persistence tests**

Run:

```bash
python -m pytest tests/test_execution_state_store.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/symbio/core/execution_state_store.py tests/test_execution_state_store.py
git commit -m "feat: persist DAG executions"
```

---

### Task 3: Execution Planner

**Files:**
- Create: `src/symbio/core/execution_planner.py`
- Test: `tests/test_execution_planner.py`

- [ ] **Step 1: Write failing planner tests**

Create `tests/test_execution_planner.py`:

```python
"""Tests for compiling tasks into DAG execution plans."""

from __future__ import annotations

from unittest.mock import AsyncMock

from symbio.core.decomposer import DecompositionResult, SubTask
from symbio.core.execution_planner import ExecutionPlanner
from symbio.utils.types import Intent, Task


def _task(text: str = "hello") -> Task:
    return Task(
        task_id="task-1",
        intent=Intent(raw_text=text, action="chat"),
        model="test-model",
        metadata={
            "workflow_policy": {"require_verification_before_completion": True},
            "risk_level": "low",
        },
    )


async def test_simple_task_compiles_to_single_node_plan():
    decomposer = AsyncMock()
    planner = ExecutionPlanner(decomposer=decomposer)

    plan = await planner.plan(_task("hello"), force_single_node=True)

    assert plan.task_id == "task-1"
    assert len(plan.nodes) == 1
    assert plan.nodes[0].name == "hello"
    assert plan.nodes[0].workflow_policy["require_verification_before_completion"] is True
    assert plan.edges == []
    decomposer.decompose.assert_not_called()


async def test_decomposition_compiles_dependencies_to_edges():
    decomposer = AsyncMock()
    decomposer.decompose.return_value = DecompositionResult(
        task_id="task-1",
        original_intent="build report",
        subtasks=[
            SubTask(subtask_id="collect", name="Collect", description="Collect data", action="search"),
            SubTask(
                subtask_id="write",
                name="Write",
                description="Write report",
                action="write_code",
                dependencies=["collect"],
            ),
        ],
    )
    planner = ExecutionPlanner(decomposer=decomposer)

    plan = await planner.plan(_task("build report"))

    assert [node.node_id for node in plan.nodes] == ["collect", "write"]
    assert plan.edges == [{"source": "collect", "target": "write"}]
    assert plan.root_node_id == "collect"
```

- [ ] **Step 2: Run planner tests and verify failure**

Run:

```bash
python -m pytest tests/test_execution_planner.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'symbio.core.execution_planner'`.

- [ ] **Step 3: Implement `ExecutionPlanner`**

Create `src/symbio/core/execution_planner.py`:

```python
"""Compile Symbio tasks into DAG execution plans."""

from __future__ import annotations

from symbio.core.decomposer import DecompositionResult, TaskDecomposer
from symbio.core.execution_models import ExecutionNode, ExecutionPlan
from symbio.utils.types import Task


class ExecutionPlanner:
    def __init__(self, decomposer: TaskDecomposer | None = None) -> None:
        self.decomposer = decomposer or TaskDecomposer()

    async def plan(self, task: Task, force_single_node: bool = False) -> ExecutionPlan:
        if force_single_node:
            return self._single_node_plan(task)

        decomposition = await self.decomposer.decompose(task.intent, task.task_id)
        if len(decomposition.subtasks) <= 1:
            return self._single_node_plan(task)
        return self._from_decomposition(task, decomposition)

    def _single_node_plan(self, task: Task) -> ExecutionPlan:
        node = ExecutionNode(
            node_id=f"{task.task_id}:root",
            name=task.intent.raw_text,
            description=task.intent.raw_text,
            action=task.intent.action or "chat",
            executor=task.metadata.get("suggested_agent", "general"),
            workflow_policy=task.metadata.get("workflow_policy", {}),
            verification_required=bool(
                task.metadata.get("workflow_policy", {}).get("require_verification_before_completion", False)
            ),
            metadata={"task_metadata": task.metadata},
        )
        return ExecutionPlan(
            task_id=task.task_id,
            root_node_id=node.node_id,
            nodes=[node],
            edges=[],
            metadata={"intent": task.intent.model_dump(mode="json")},
        )

    def _from_decomposition(self, task: Task, decomposition: DecompositionResult) -> ExecutionPlan:
        nodes: list[ExecutionNode] = []
        for subtask in decomposition.subtasks:
            nodes.append(ExecutionNode(
                node_id=subtask.subtask_id,
                name=subtask.name,
                description=subtask.description,
                action=subtask.action,
                executor=subtask.suggested_agent or "general",
                dependencies=subtask.dependencies,
                workflow_policy=task.metadata.get("workflow_policy", {}),
                verification_required=bool(
                    task.metadata.get("workflow_policy", {}).get("require_verification_before_completion", False)
                ),
                metadata={
                    "parameters": subtask.parameters,
                    "estimated_complexity": subtask.estimated_complexity.value,
                    "parent_task_id": task.task_id,
                },
            ))

        edges = [
            {"source": dep_id, "target": node.node_id}
            for node in nodes
            for dep_id in node.dependencies
        ]
        root_node_id = next((node.node_id for node in nodes if not node.dependencies), nodes[0].node_id)

        return ExecutionPlan(
            task_id=task.task_id,
            root_node_id=root_node_id,
            nodes=nodes,
            edges=edges,
            metadata={
                "intent": task.intent.model_dump(mode="json"),
                "decomposition_reasoning": decomposition.reasoning,
                "needs_debate": decomposition.needs_debate,
            },
        )
```

- [ ] **Step 4: Run planner tests**

Run:

```bash
python -m pytest tests/test_execution_planner.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/symbio/core/execution_planner.py tests/test_execution_planner.py
git commit -m "feat: compile tasks into DAG execution plans"
```

---

### Task 4: Replanner and Result Reducer

**Files:**
- Create: `src/symbio/core/replanner.py`
- Create: `src/symbio/core/result_reducer.py`
- Test: `tests/test_replanner.py`
- Test: `tests/test_result_reducer.py`

- [ ] **Step 1: Write failing replanner tests**

Create `tests/test_replanner.py`:

```python
"""Tests for DAG runtime replan decisions."""

from __future__ import annotations

from symbio.core.execution_models import ReplanDecisionType
from symbio.core.replanner import Replanner


def test_transient_tool_error_retries_first():
    decision = Replanner(max_retries=2).decide(
        node_id="node-1",
        failure={"kind": "tool_transient_error", "message": "timeout", "retry_count": 0},
    )

    assert decision.decision == ReplanDecisionType.RETRY
    assert decision.node_id == "node-1"


def test_verification_failure_creates_local_patch():
    decision = Replanner(max_retries=1).decide(
        node_id="node-1",
        failure={"kind": "verification_failure", "message": "pytest failed", "retry_count": 1},
    )

    assert decision.decision == ReplanDecisionType.LOCAL_PATCH
    assert decision.mutations[0]["action"] == "add_node"


def test_requirement_ambiguity_waits_for_clarification():
    decision = Replanner().decide(
        node_id="node-1",
        failure={"kind": "requirement_ambiguity", "message": "target env unknown"},
    )

    assert decision.decision == ReplanDecisionType.WAITING_CLARIFICATION
```

- [ ] **Step 2: Write failing result reducer tests**

Create `tests/test_result_reducer.py`:

```python
"""Tests for reducing DAG execution state into final Results."""

from __future__ import annotations

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionRecord,
    ExecutionStatus,
)
from symbio.core.result_reducer import ResultReducer


def test_result_reducer_prefers_final_output_first():
    record = ExecutionRecord(
        execution_id="exec-1",
        task_id="task-1",
        intent_text="answer",
        status=ExecutionStatus.COMPLETED,
    )
    nodes = [ExecutionNode(node_id="node-1", name="Answer", status=ExecutionNodeStatus.COMPLETED)]
    artifacts = [
        ExecutionArtifact(
            execution_id="exec-1",
            node_id="node-1",
            artifact_type="node_result",
            content={"content": "final answer"},
        )
    ]

    result = ResultReducer().reduce(record, nodes, artifacts, [])

    assert result.success is True
    assert result.content == "final answer"
    assert result.data["execution_id"] == "exec-1"


def test_result_reducer_blocks_completion_without_required_verification():
    record = ExecutionRecord(
        execution_id="exec-1",
        task_id="task-1",
        intent_text="change code",
        status=ExecutionStatus.COMPLETED,
    )
    nodes = [
        ExecutionNode(
            node_id="node-1",
            name="Implement",
            status=ExecutionNodeStatus.COMPLETED,
            verification_required=True,
        )
    ]

    result = ResultReducer().reduce(record, nodes, [], [])

    assert result.success is False
    assert result.data["status"] == "needs_verification"
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m pytest tests/test_replanner.py tests/test_result_reducer.py -q
```

Expected: fail with missing modules.

- [ ] **Step 4: Implement `Replanner`**

Create `src/symbio/core/replanner.py`:

```python
"""Replanning policy for DAG-first execution."""

from __future__ import annotations

from symbio.core.execution_models import ReplanDecision, ReplanDecisionType


class Replanner:
    def __init__(self, max_retries: int = 1, max_replan_count: int = 3) -> None:
        self.max_retries = max_retries
        self.max_replan_count = max_replan_count

    def decide(self, node_id: str, failure: dict) -> ReplanDecision:
        kind = failure.get("kind", "unknown")
        retry_count = int(failure.get("retry_count", 0))
        message = str(failure.get("message", ""))

        if kind == "tool_transient_error" and retry_count < self.max_retries:
            return ReplanDecision(
                decision=ReplanDecisionType.RETRY,
                node_id=node_id,
                reason=message or "Transient tool error",
            )

        if kind == "verification_failure":
            return ReplanDecision(
                decision=ReplanDecisionType.LOCAL_PATCH,
                node_id=node_id,
                reason=message or "Verification failed",
                mutations=[{
                    "action": "add_node",
                    "node_id": f"{node_id}:repair",
                    "name": "Repair failed verification",
                    "dependencies": [node_id],
                }],
            )

        if kind == "requirement_ambiguity":
            return ReplanDecision(
                decision=ReplanDecisionType.WAITING_CLARIFICATION,
                node_id=node_id,
                reason=message or "Requirement ambiguity",
            )

        if kind == "permission_required":
            return ReplanDecision(
                decision=ReplanDecisionType.WAITING_HITL,
                node_id=node_id,
                reason=message or "Approval required",
            )

        return ReplanDecision(
            decision=ReplanDecisionType.FAIL,
            node_id=node_id,
            reason=message or "Unhandled runtime failure",
        )
```

- [ ] **Step 5: Implement `ResultReducer`**

Create `src/symbio/core/result_reducer.py`:

```python
"""Reduce DAG execution state into user-facing Results."""

from __future__ import annotations

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionRecord,
)
from symbio.utils.types import Result


class ResultReducer:
    def reduce(
        self,
        record: ExecutionRecord,
        nodes: list[ExecutionNode],
        artifacts: list[ExecutionArtifact],
        events: list[ExecutionEvent],
    ) -> Result:
        verification_node_ids = {node.node_id for node in nodes if node.verification_required}
        verified_node_ids = {
            artifact.node_id
            for artifact in artifacts
            if artifact.artifact_type in {"verification", "verification_result"}
        }
        missing_verification = sorted(verification_node_ids - verified_node_ids)
        if missing_verification:
            return Result(
                task_id=record.task_id,
                success=False,
                content="Task needs verification before completion.",
                data={
                    "execution_id": record.execution_id,
                    "status": "needs_verification",
                    "missing_verification": missing_verification,
                },
            )

        final_content = self._final_content(artifacts)
        success = record.status.value == "completed"
        return Result(
            task_id=record.task_id,
            success=success,
            content=final_content,
            data={
                "execution_id": record.execution_id,
                "status": record.status.value,
                "node_count": len(nodes),
                "event_count": len(events),
                "artifact_count": len(artifacts),
            },
        )

    def _final_content(self, artifacts: list[ExecutionArtifact]) -> str:
        for artifact in reversed(artifacts):
            if artifact.artifact_type == "node_result":
                content = artifact.content.get("content")
                if content:
                    return str(content)
        return "DAG execution completed."
```

- [ ] **Step 6: Run reducer and replanner tests**

Run:

```bash
python -m pytest tests/test_replanner.py tests/test_result_reducer.py -q
```

Expected: `5 passed`.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/symbio/core/replanner.py src/symbio/core/result_reducer.py tests/test_replanner.py tests/test_result_reducer.py
git commit -m "feat: add DAG replanner and result reducer"
```

---

### Task 5: DAG Runtime

**Files:**
- Create: `src/symbio/core/dag_runtime.py`
- Test: `tests/test_dag_runtime.py`

- [ ] **Step 1: Write failing runtime tests**

Create `tests/test_dag_runtime.py`:

```python
"""Tests for DAG runtime execution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from symbio.core.dag_runtime import DAGRuntime
from symbio.core.execution_models import ExecutionNode, ExecutionNodeStatus, ExecutionPlan
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.utils.types import Result


def _agent(content: str = "done"):
    agent = MagicMock()
    agent.execute = AsyncMock(return_value=Result(task_id="node", success=True, content=content))
    return agent


async def test_runtime_executes_single_node_and_records_artifact(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    registry = MagicMock()
    registry.get.return_value = _agent("final")
    plan = ExecutionPlan(
        task_id="task-1",
        root_node_id="node-1",
        nodes=[ExecutionNode(node_id="node-1", name="Answer", executor="general")],
    )
    record = await store.create_execution(plan, "hello")

    runtime = DAGRuntime(store=store, registry=registry)
    await runtime.run(record.execution_id)

    nodes = await store.list_nodes(record.execution_id)
    artifacts = await store.list_artifacts(record.execution_id)

    assert nodes[0].status == ExecutionNodeStatus.COMPLETED
    assert artifacts[0].artifact_type == "node_result"
    assert artifacts[0].content["content"] == "final"
    await store.close()


async def test_runtime_blocks_dependent_node_until_dependency_completes(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    registry = MagicMock()
    registry.get.return_value = _agent("ok")
    plan = ExecutionPlan(
        task_id="task-1",
        root_node_id="a",
        nodes=[
            ExecutionNode(node_id="a", name="A", executor="general"),
            ExecutionNode(node_id="b", name="B", executor="general", dependencies=["a"]),
        ],
        edges=[{"source": "a", "target": "b"}],
    )
    record = await store.create_execution(plan, "hello")

    runtime = DAGRuntime(store=store, registry=registry)
    await runtime.run(record.execution_id)

    events = await store.list_events(record.execution_id)
    started = [event.node_id for event in events if event.event_type == "node_started"]

    assert started == ["a", "b"]
    await store.close()
```

- [ ] **Step 2: Run runtime tests and verify failure**

Run:

```bash
python -m pytest tests/test_dag_runtime.py -q
```

Expected: fail with missing module.

- [ ] **Step 3: Implement `DAGRuntime`**

Create `src/symbio/core/dag_runtime.py`:

```python
"""Runtime executor for persisted DAG execution plans."""

from __future__ import annotations

from symbio.agents.registry import AgentRegistry
from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionStatus,
)
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.utils.types import Intent, Result, Task


class DAGRuntime:
    def __init__(self, store: ExecutionStateStore, registry: AgentRegistry) -> None:
        self.store = store
        self.registry = registry

    async def run(self, execution_id: str) -> None:
        await self.store.update_execution_status(execution_id, ExecutionStatus.RUNNING)
        while True:
            nodes = await self.store.list_nodes(execution_id)
            ready = self._ready_nodes(nodes)
            if not ready:
                break
            for node in ready:
                await self._execute_node(execution_id, node)

        nodes = await self.store.list_nodes(execution_id)
        final_status = (
            ExecutionStatus.FAILED
            if any(node.status == ExecutionNodeStatus.FAILED for node in nodes)
            else ExecutionStatus.COMPLETED
        )
        await self.store.update_execution_status(execution_id, final_status)

    def _ready_nodes(self, nodes: list[ExecutionNode]) -> list[ExecutionNode]:
        completed = {
            node.node_id
            for node in nodes
            if node.status == ExecutionNodeStatus.COMPLETED
        }
        return [
            node
            for node in nodes
            if node.status == ExecutionNodeStatus.PENDING
            and all(dep_id in completed for dep_id in node.dependencies)
        ]

    async def _execute_node(self, execution_id: str, node: ExecutionNode) -> None:
        await self.store.update_node_status(execution_id, node.node_id, ExecutionNodeStatus.RUNNING)
        await self.store.append_event(ExecutionEvent(
            execution_id=execution_id,
            node_id=node.node_id,
            event_type="node_started",
            payload={"name": node.name},
        ))

        agent = self.registry.get(node.executor)
        if agent is None:
            await self._record_failure(execution_id, node, f"Agent '{node.executor}' not found")
            return

        task = Task(
            task_id=node.node_id,
            intent=Intent(
                raw_text=node.description or node.name,
                action=node.action,
                parameters=node.metadata.get("parameters", {}),
            ),
            metadata={
                "workflow_policy": node.workflow_policy,
                "execution_id": execution_id,
                "node_id": node.node_id,
            },
        )
        try:
            result: Result = await agent.execute(task)
        except Exception as exc:
            await self._record_failure(execution_id, node, str(exc))
            return

        if not result.success:
            await self._record_failure(execution_id, node, result.content)
            return

        await self.store.append_artifact(ExecutionArtifact(
            execution_id=execution_id,
            node_id=node.node_id,
            artifact_type="node_result",
            content={
                "content": result.content,
                "data": result.data,
                "token_usage": result.token_usage.model_dump(mode="json"),
            },
        ))
        await self.store.update_node_status(execution_id, node.node_id, ExecutionNodeStatus.COMPLETED)
        await self.store.append_event(ExecutionEvent(
            execution_id=execution_id,
            node_id=node.node_id,
            event_type="node_completed",
            payload={"success": True},
        ))

    async def _record_failure(self, execution_id: str, node: ExecutionNode, error: str) -> None:
        await self.store.update_node_status(execution_id, node.node_id, ExecutionNodeStatus.FAILED)
        await self.store.append_event(ExecutionEvent(
            execution_id=execution_id,
            node_id=node.node_id,
            event_type="node_failed",
            payload={"error": error},
        ))
```

- [ ] **Step 4: Run runtime tests**

Run:

```bash
python -m pytest tests/test_dag_runtime.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/symbio/core/dag_runtime.py tests/test_dag_runtime.py
git commit -m "feat: run persisted DAG executions"
```

---

### Task 6: DAG Orchestrator

**Files:**
- Create: `src/symbio/core/dag_orchestrator.py`
- Modify: `src/symbio/core/__init__.py`
- Test: `tests/test_dag_orchestrator.py`

- [ ] **Step 1: Write failing orchestration tests**

Create `tests/test_dag_orchestrator.py`:

```python
"""Tests for the DAG-first orchestration coordinator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from symbio.core.dag_orchestrator import DAGOrchestrator
from symbio.core.execution_models import ExecutionNode, ExecutionPlan
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.utils.types import Intent, Result, Task


async def test_dag_orchestrator_returns_reduced_result(tmp_path):
    store = ExecutionStateStore(str(tmp_path / "executions.db"))
    planner = MagicMock()
    runtime = MagicMock()
    reducer = MagicMock()
    plan = ExecutionPlan(
        task_id="task-1",
        root_node_id="node-1",
        nodes=[ExecutionNode(node_id="node-1", name="Answer")],
    )
    planner.plan = AsyncMock(return_value=plan)
    runtime.run = AsyncMock()
    reducer.reduce.return_value = Result(
        task_id="task-1",
        success=True,
        content="final",
        data={"execution_id": plan.execution_id},
    )
    task = Task(task_id="task-1", intent=Intent(raw_text="hello", action="chat"))

    result = await DAGOrchestrator(
        planner=planner,
        store=store,
        runtime=runtime,
        reducer=reducer,
    ).execute(task)

    assert result.success is True
    assert result.content == "final"
    assert result.data["execution_id"] == plan.execution_id
    runtime.run.assert_awaited_once_with(plan.execution_id)
    await store.close()
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
python -m pytest tests/test_dag_orchestrator.py -q
```

Expected: fail with missing module.

- [ ] **Step 3: Implement `DAGOrchestrator`**

Create `src/symbio/core/dag_orchestrator.py`:

```python
"""DAG-first orchestration coordinator."""

from __future__ import annotations

from symbio.agents.registry import AgentRegistry, get_registry
from symbio.core.dag_runtime import DAGRuntime
from symbio.core.execution_planner import ExecutionPlanner
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.core.result_reducer import ResultReducer
from symbio.utils.types import Result, Task


class DAGOrchestrator:
    def __init__(
        self,
        planner: ExecutionPlanner | None = None,
        store: ExecutionStateStore | None = None,
        runtime: DAGRuntime | None = None,
        reducer: ResultReducer | None = None,
        registry: AgentRegistry | None = None,
    ) -> None:
        self.planner = planner or ExecutionPlanner()
        self.store = store or ExecutionStateStore()
        self.registry = registry or get_registry()
        self.runtime = runtime or DAGRuntime(self.store, self.registry)
        self.reducer = reducer or ResultReducer()

    async def execute(self, task: Task) -> Result:
        plan = await self.planner.plan(task)
        record = await self.store.create_execution(plan, task.intent.raw_text)
        await self.runtime.run(plan.execution_id)
        record = await self.store.get_execution(plan.execution_id) or record
        nodes = await self.store.list_nodes(plan.execution_id)
        artifacts = await self.store.list_artifacts(plan.execution_id)
        events = await self.store.list_events(plan.execution_id)
        return self.reducer.reduce(record, nodes, artifacts, events)
```

Modify `src/symbio/core/__init__.py` by adding:

```python
from symbio.core.dag_orchestrator import DAGOrchestrator
from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionGraphVersion,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionStatus,
    ReplanDecision,
    ReplanDecisionType,
)
from symbio.core.execution_planner import ExecutionPlanner
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.core.replanner import Replanner
from symbio.core.result_reducer import ResultReducer
```

Add these names to `__all__` if the file already defines an export list.

- [ ] **Step 4: Run orchestration test**

Run:

```bash
python -m pytest tests/test_dag_orchestrator.py -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/symbio/core/dag_orchestrator.py src/symbio/core/__init__.py tests/test_dag_orchestrator.py
git commit -m "feat: add DAG-first orchestration coordinator"
```

---

### Task 7: Wire Thin Orchestrator to DAG-First Path

**Files:**
- Modify: `src/symbio/core/orchestrator.py`
- Test: extend `tests/test_phase2.py`

- [ ] **Step 1: Add failing integration test for default DAG path**

Append to `tests/test_phase2.py` inside `TestOrchestratorWorkflowPolicy` or a new `TestDAGFirstOrchestratorIntegration` class:

```python
class TestDAGFirstOrchestratorIntegration:
    async def test_orchestrator_delegates_execution_to_dag_orchestrator(self):
        orchestrator = Orchestrator()
        orchestrator.initialize_memory = AsyncMock()
        orchestrator.memory_bridge.enhance_context = AsyncMock(return_value="")

        captured_task = None

        class FakeDAGOrchestrator:
            async def execute(self, task):
                nonlocal captured_task
                captured_task = task
                return Result(
                    task_id=task.task_id,
                    success=True,
                    content="dag result",
                    data={"execution_id": "exec-1"},
                )

        orchestrator.dag_orchestrator = FakeDAGOrchestrator()

        message = Message(
            source=MessageSource.CLI,
            user_id="test-user",
            content="Implement a new API endpoint",
            session_id="test-session",
        )

        with patch("symbio.core.orchestrator.get_settings", return_value=_make_mock_settings(api_key="")):
            result = await orchestrator.process(message)

        assert result.success is True
        assert result.content == "dag result"
        assert result.data["execution_id"] == "exec-1"
        assert captured_task is not None
        assert "workflow_policy" in captured_task.metadata
```

- [ ] **Step 2: Run integration test and verify failure**

Run:

```bash
python -m pytest tests/test_phase2.py::TestDAGFirstOrchestratorIntegration::test_orchestrator_delegates_execution_to_dag_orchestrator -q
```

Expected: fail because `orchestrator.dag_orchestrator` is not used.

- [ ] **Step 3: Modify `Orchestrator.__init__`**

In `src/symbio/core/orchestrator.py`, add import:

```python
from symbio.core.dag_orchestrator import DAGOrchestrator
```

Add in `__init__` after `self.tool_loader = ToolLazyLoader()`:

```python
self.dag_orchestrator = DAGOrchestrator(registry=self.registry)
```

- [ ] **Step 4: Replace old direct execution path**

In `_process_inner`, after HITL precheck returns no request, replace the old
decomposition/debate/subagent/agent execution block with:

```python
try:
    result = await self.dag_orchestrator.execute(task)
    task.result = result
    task.state = AgentState.COMPLETED if result.success else AgentState.FAILED
    return result
finally:
    self.guardrail.release_ticket(task.task_id)
```

Keep existing intent parsing, model routing, memory injection, workflow policy
injection, and HITL precheck intact.

- [ ] **Step 5: Run DAG delegation integration test**

Run:

```bash
python -m pytest tests/test_phase2.py::TestDAGFirstOrchestratorIntegration::test_orchestrator_delegates_execution_to_dag_orchestrator -q
```

Expected: `1 passed`.

- [ ] **Step 6: Run existing orchestrator tests**

Run:

```bash
python -m pytest tests/test_phase2.py -k "Orchestrator" -q
```

Expected: all selected tests pass. If older tests assert direct `SubAgentManager` behavior, update them to assert DAG-first behavior through `DAGOrchestrator` while preserving final `Result` semantics.

- [ ] **Step 7: Commit Task 7**

```bash
git add src/symbio/core/orchestrator.py tests/test_phase2.py
git commit -m "feat: route orchestrator through DAG runtime"
```

---

### Task 8: Execution API and UI Evidence

**Files:**
- Modify: `src/symbio/interfaces/api.py`
- Modify: `web/app.js`
- Modify: `web/style.css`
- Test: extend `tests/test_integration.py`

- [ ] **Step 1: Add failing API tests**

Append to `tests/test_integration.py`:

```python
class TestExecutionAPI:
    async def test_execution_detail_events_and_artifacts_endpoints(self, client):
        from symbio.core.execution_models import ExecutionArtifact, ExecutionEvent, ExecutionNode, ExecutionPlan
        from symbio.core.execution_state_store import ExecutionStateStore
        from symbio.interfaces.api import app

        store = ExecutionStateStore(":memory:")
        plan = ExecutionPlan(
            task_id="task-api-exec",
            root_node_id="node-1",
            nodes=[ExecutionNode(node_id="node-1", name="Answer")],
        )
        record = await store.create_execution(plan, "hello")
        await store.append_event(ExecutionEvent(
            execution_id=record.execution_id,
            node_id="node-1",
            event_type="node_started",
        ))
        await store.append_artifact(ExecutionArtifact(
            execution_id=record.execution_id,
            node_id="node-1",
            artifact_type="node_result",
            content={"content": "done"},
        ))
        app.state.execution_store = store

        detail = await client.get(f"/api/executions/{record.execution_id}")
        events = await client.get(f"/api/executions/{record.execution_id}/events")
        artifacts = await client.get(f"/api/executions/{record.execution_id}/artifacts")

        assert detail.status_code == 200
        assert detail.json()["execution"]["execution_id"] == record.execution_id
        assert events.json()["events"][0]["event_type"] == "execution_planned"
        assert artifacts.json()["artifacts"][0]["artifact_type"] == "node_result"

        await store.close()
```

- [ ] **Step 2: Run API test and verify failure**

Run:

```bash
python -m pytest tests/test_integration.py::TestExecutionAPI::test_execution_detail_events_and_artifacts_endpoints -q
```

Expected: fail with `404 Not Found`.

- [ ] **Step 3: Add execution store accessor and endpoints**

In `src/symbio/interfaces/api.py`, add imports:

```python
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.core.execution_models import ExecutionStatus
```

Add helper near HITL helpers:

```python
def _get_execution_store() -> ExecutionStateStore:
    if not hasattr(app.state, "execution_store"):
        app.state.execution_store = ExecutionStateStore()
    return app.state.execution_store
```

Add endpoints:

```python
@app.get("/api/executions/{execution_id}")
async def get_execution(execution_id: str):
    store = _get_execution_store()
    execution = await store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="execution not found")
    nodes = await store.list_nodes(execution_id)
    versions = await store.list_graph_versions(execution_id)
    return {
        "execution": execution.model_dump(mode="json"),
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "graph_versions": [version.model_dump(mode="json") for version in versions],
    }


@app.get("/api/executions/{execution_id}/events")
async def get_execution_events(execution_id: str):
    store = _get_execution_store()
    if await store.get_execution(execution_id) is None:
        raise HTTPException(status_code=404, detail="execution not found")
    events = await store.list_events(execution_id)
    return {"events": [event.model_dump(mode="json") for event in events], "total": len(events)}


@app.get("/api/executions/{execution_id}/artifacts")
async def get_execution_artifacts(execution_id: str):
    store = _get_execution_store()
    if await store.get_execution(execution_id) is None:
        raise HTTPException(status_code=404, detail="execution not found")
    artifacts = await store.list_artifacts(execution_id)
    return {"artifacts": [artifact.model_dump(mode="json") for artifact in artifacts], "total": len(artifacts)}


@app.post("/api/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str):
    store = _get_execution_store()
    if await store.get_execution(execution_id) is None:
        raise HTTPException(status_code=404, detail="execution not found")
    await store.update_execution_status(execution_id, ExecutionStatus.CANCELLED)
    return {"success": True}
```

- [ ] **Step 4: Run API test**

Run:

```bash
python -m pytest tests/test_integration.py::TestExecutionAPI::test_execution_detail_events_and_artifacts_endpoints -q
```

Expected: `1 passed`.

- [ ] **Step 5: Add UI execution panels**

In `web/app.js`, add these helper functions near existing evidence render helpers:

```javascript
function renderExecutionTimeline(execution) {
  const events = execution?.events || [];
  if (!events.length) return '<div class="evidence-empty">No execution events recorded.</div>';
  return `<div class="execution-timeline">${events.map(ev => `
    <div class="execution-event">
      <span class="execution-event-type">${esc(ev.event_type)}</span>
      <span class="execution-event-node">${esc(ev.node_id || 'execution')}</span>
    </div>
  `).join('')}</div>`;
}

function renderExecutionGraphSummary(execution) {
  const nodes = execution?.nodes || [];
  if (!nodes.length) return '<div class="evidence-empty">No execution graph recorded.</div>';
  return `<div class="execution-node-list">${nodes.map(node => `
    <div class="execution-node-row">
      <span>${esc(node.name || node.node_id)}</span>
      <span class="execution-node-status">${esc(node.status)}</span>
    </div>
  `).join('')}</div>`;
}
```

Use these helpers in task detail rendering after existing approval/evidence
panels:

```javascript
${renderExecutionGraphSummary(task.execution)}
${renderExecutionTimeline(task.execution)}
```

- [ ] **Step 6: Add UI CSS**

In `web/style.css`, add:

```css
.execution-timeline {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.execution-event,
.execution-node-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  min-width: 0;
}
.execution-event-type,
.execution-node-status {
  font-family: var(--font-mono);
  font-size: 0.68rem;
  color: var(--accent);
}
.execution-event-node {
  font-family: var(--font-mono);
  font-size: 0.68rem;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.execution-node-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
```

- [ ] **Step 7: Validate frontend syntax**

Run:

```bash
node --check web/app.js
git diff --check -- web/app.js web/style.css
```

Expected: both commands exit `0`.

- [ ] **Step 8: Commit Task 8**

```bash
git add src/symbio/interfaces/api.py tests/test_integration.py web/app.js web/style.css
git commit -m "feat: expose DAG execution state"
```

---

### Task 9: Full Regression and Documentation Update

**Files:**
- Modify: `docs/feature-checklist.md`
- Modify: `docs/agent-workflow-policy.md` only when the final implementation changes workflow-policy enforcement behavior

- [ ] **Step 1: Run targeted DAG-first tests**

Run:

```bash
python -m pytest tests/test_execution_models.py tests/test_execution_state_store.py tests/test_execution_planner.py tests/test_replanner.py tests/test_result_reducer.py tests/test_dag_runtime.py tests/test_dag_orchestrator.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run orchestrator and API regression tests**

Run:

```bash
python -m pytest tests/test_phase2.py -k "Orchestrator or DAG" -q
python -m pytest tests/test_integration.py -k "task or execution or hitl" -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run full backend test suite**

Run:

```bash
python -m pytest -q
```

Expected: full suite passes. Existing FastAPI `on_event` deprecation warnings may remain until the lifecycle migration task is handled.

- [ ] **Step 4: Update checklist documentation**

In `docs/feature-checklist.md`, update the dynamic DAG section to reflect:

```markdown
- [x] Orchestrator default path now delegates execution to DAG-first runtime
- [x] Decomposition compiles into persisted execution graph nodes and edges
- [x] Execution events, artifacts, and graph versions are exposed through API
- [~] Observation-driven replan supports explicit retry/local-patch/global-replan decisions; domain-specific graph mutations are represented by explicit mutation records and can be expanded by later task-specific planners
```

If the file's current encoding makes direct Chinese editing unreliable, add an
English "DAG-first execution status" subsection at the end of the file instead
of rewriting existing mojibake lines.

- [ ] **Step 5: Run docs diff check**

Run:

```bash
git diff --check -- docs/feature-checklist.md
```

Expected: exit `0`.

- [ ] **Step 6: Commit Task 9**

```bash
git add docs/feature-checklist.md
git commit -m "docs: update DAG-first execution status"
```

---

## Final Verification

Run:

```bash
python -m pytest -q
node --check web/app.js
git diff --check
```

Expected:

- Full pytest suite passes.
- `web/app.js` has no syntax errors.
- `git diff --check` reports no whitespace errors.

If Playwright or browser-level UI checks are added later, run them after this
plan's backend and static frontend checks pass.
