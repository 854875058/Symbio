"""Async SQLite-backed persistent execution state storage."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionGraphVersion,
    ExecutionNode,
    ExecutionPlan,
    ExecutionRecord,
    ExecutionStatus,
)


TERMINAL_EXECUTION_STATUSES = {
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
    ExecutionStatus.FAILED_POLICY,
}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str) -> Any:
    return json.loads(value) if value else None


class ExecutionStateStore:
    """Persist execution state in a local SQLite database."""

    def __init__(self, db_path: str = "data/executions.db"):
        self._db_path = Path(db_path)
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        if self._connection is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(str(self._db_path))
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            self._connection = connection
            await self._initialize_schema()
        return self._connection

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def create_execution(
        self, plan: ExecutionPlan, intent_text: str
    ) -> ExecutionRecord:
        record = ExecutionRecord(
            execution_id=plan.execution_id,
            task_id=plan.task_id,
            intent_text=intent_text,
            status=ExecutionStatus.PLANNED,
            plan_version=plan.plan_version,
            replan_generation=plan.replan_generation,
            metadata=plan.metadata,
        )

        async with self._write_lock:
            connection = await self.connect()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """
                    INSERT INTO executions (
                        execution_id,
                        task_id,
                        intent_text,
                        status,
                        plan_version,
                        replan_generation,
                        created_at,
                        completed_at,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.execution_id,
                        record.task_id,
                        record.intent_text,
                        record.status.value,
                        record.plan_version,
                        record.replan_generation,
                        record.created_at.isoformat(),
                        None,
                        _json_dumps(record.metadata),
                    ),
                )
                for index, node in enumerate(plan.nodes):
                    await self._upsert_node(connection, record.execution_id, node, index)
                await self._save_graph_version(
                    connection,
                    ExecutionGraphVersion(
                        execution_id=plan.execution_id,
                        graph_version=1,
                        nodes=[node.model_dump(mode="json") for node in plan.nodes],
                        edges=plan.edges,
                    ),
                )
                await self._append_event(
                    connection,
                    ExecutionEvent(
                        execution_id=record.execution_id,
                        event_type="execution_planned",
                        payload={
                            "intent_text": intent_text,
                            "plan_version": plan.plan_version,
                            "replan_generation": plan.replan_generation,
                        },
                    ),
                )
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()
        return record

    async def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        connection = await self.connect()
        async with connection.execute(
            "SELECT * FROM executions WHERE execution_id = ?",
            (execution_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

    async def update_execution_status(
        self, execution_id: str, status: ExecutionStatus
    ) -> ExecutionRecord:
        completed_at = (
            datetime.now(timezone.utc).isoformat()
            if status in TERMINAL_EXECUTION_STATUSES
            else None
        )
        async with self._write_lock:
            connection = await self.connect()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE executions
                    SET status = ?, completed_at = ?
                    WHERE execution_id = ?
                    """,
                    (status.value, completed_at, execution_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Unknown execution_id: {execution_id}")
                await self._append_event(
                    connection,
                    ExecutionEvent(
                        execution_id=execution_id,
                        event_type="execution_status_updated",
                        payload={"status": status.value},
                    ),
                )
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()

        record = await self.get_execution(execution_id)
        if record is None:
            raise KeyError(f"Unknown execution_id: {execution_id}")
        return record

    async def upsert_node(self, execution_id: str, node: ExecutionNode) -> ExecutionNode:
        async with self._write_lock:
            connection = await self.connect()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                async with connection.execute(
                    """
                    SELECT sort_order
                    FROM execution_nodes
                    WHERE execution_id = ? AND node_id = ?
                    """,
                    (execution_id, node.node_id),
                ) as cursor:
                    existing_row = await cursor.fetchone()

                sort_order = (
                    existing_row["sort_order"]
                    if existing_row is not None
                    else await self._next_node_sort_order(connection, execution_id)
                )
                await self._upsert_node(connection, execution_id, node, sort_order)
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()
        return node

    async def list_nodes(self, execution_id: str) -> list[ExecutionNode]:
        connection = await self.connect()
        async with connection.execute(
            """
            SELECT * FROM execution_nodes
            WHERE execution_id = ?
            ORDER BY sort_order ASC, rowid ASC
            """,
            (execution_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._node_from_row(row) for row in rows]

    async def update_node_status(
        self, execution_id: str, node_id: str, status: Any
    ) -> ExecutionNode:
        status_value = getattr(status, "value", status)
        async with self._write_lock:
            connection = await self.connect()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    """
                    UPDATE execution_nodes
                    SET status = ?
                    WHERE execution_id = ? AND node_id = ?
                    """,
                    (status_value, execution_id, node_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Unknown node_id: {node_id}")
                await self._append_event(
                    connection,
                    ExecutionEvent(
                        execution_id=execution_id,
                        node_id=node_id,
                        event_type="node_status_updated",
                        payload={"status": status_value},
                    ),
                )
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()

        for node in await self.list_nodes(execution_id):
            if node.node_id == node_id:
                return node
        raise KeyError(f"Unknown node_id: {node_id}")

    async def append_event(self, event: ExecutionEvent) -> ExecutionEvent:
        async with self._write_lock:
            connection = await self.connect()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_node_ref(connection, event.execution_id, event.node_id)
                await self._append_event(connection, event)
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()
        return event

    async def list_events(self, execution_id: str) -> list[ExecutionEvent]:
        connection = await self.connect()
        async with connection.execute(
            """
            SELECT * FROM execution_events
            WHERE execution_id = ?
            ORDER BY sequence_id ASC
            """,
            (execution_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._event_from_row(row) for row in rows]

    async def append_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        async with self._write_lock:
            connection = await self.connect()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await self._ensure_node_ref(
                    connection, artifact.execution_id, artifact.node_id
                )
                await connection.execute(
                    """
                    INSERT INTO execution_artifacts (
                        execution_id,
                        artifact_id,
                        node_id,
                        artifact_type,
                        content_json,
                        path_ref,
                        metadata_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.execution_id,
                        artifact.artifact_id,
                        artifact.node_id,
                        artifact.artifact_type,
                        _json_dumps(artifact.content),
                        artifact.path_ref,
                        _json_dumps(artifact.metadata),
                        artifact.created_at.isoformat(),
                    ),
                )
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()
        return artifact

    async def list_artifacts(self, execution_id: str) -> list[ExecutionArtifact]:
        connection = await self.connect()
        async with connection.execute(
            """
            SELECT * FROM execution_artifacts
            WHERE execution_id = ?
            ORDER BY sequence_id ASC
            """,
            (execution_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._artifact_from_row(row) for row in rows]

    async def save_graph_version(
        self,
        execution_id: str,
        graph_version: int,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> None:
        async with self._write_lock:
            connection = await self.connect()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await self._save_graph_version(
                    connection,
                    ExecutionGraphVersion(
                        execution_id=execution_id,
                        graph_version=graph_version,
                        nodes=nodes,
                        edges=edges,
                    ),
                )
            except sqlite3.IntegrityError:
                await connection.rollback()
                raise
            except Exception:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def list_graph_versions(self, execution_id: str) -> list[ExecutionGraphVersion]:
        connection = await self.connect()
        async with connection.execute(
            """
            SELECT * FROM execution_graph_versions
            WHERE execution_id = ?
            ORDER BY graph_version ASC, sequence_id ASC
            """,
            (execution_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._graph_version_from_row(row) for row in rows]

    async def _initialize_schema(self) -> None:
        connection = await self.connect()
        await connection.executescript(
            """
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
                sort_order INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                action TEXT NOT NULL,
                executor TEXT NOT NULL,
                dependencies_json TEXT NOT NULL,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL,
                max_retries INTEGER NOT NULL,
                workflow_policy_json TEXT NOT NULL,
                verification_required INTEGER NOT NULL,
                hitl_policy_json TEXT NOT NULL,
                input_refs_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY (execution_id, node_id),
                FOREIGN KEY (execution_id) REFERENCES executions (execution_id)
            );

            CREATE TABLE IF NOT EXISTS execution_events (
                sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                node_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES executions (execution_id)
            );

            CREATE TABLE IF NOT EXISTS execution_artifacts (
                sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL UNIQUE,
                node_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                content_json TEXT NOT NULL,
                path_ref TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (execution_id) REFERENCES executions (execution_id)
            );

            CREATE TABLE IF NOT EXISTS execution_graph_versions (
                sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL,
                graph_version INTEGER NOT NULL,
                nodes_json TEXT NOT NULL,
                edges_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (execution_id, graph_version),
                FOREIGN KEY (execution_id) REFERENCES executions (execution_id)
            );
            """
        )
        await connection.commit()

    async def _next_node_sort_order(
        self, connection: aiosqlite.Connection, execution_id: str
    ) -> int:
        async with connection.execute(
            """
            SELECT COALESCE(MAX(sort_order), -1) AS max_sort_order
            FROM execution_nodes
            WHERE execution_id = ?
            """,
            (execution_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["max_sort_order"]) + 1

    async def _ensure_node_ref(
        self,
        connection: aiosqlite.Connection,
        execution_id: str,
        node_id: str,
    ) -> None:
        if not node_id:
            return
        async with connection.execute(
            """
            SELECT 1
            FROM execution_nodes
            WHERE execution_id = ? AND node_id = ?
            """,
            (execution_id, node_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ValueError(
                f"node_id '{node_id}' does not exist for execution '{execution_id}'"
            )

    async def _upsert_node(
        self,
        connection: aiosqlite.Connection,
        execution_id: str,
        node: ExecutionNode,
        sort_order: int,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO execution_nodes (
                execution_id,
                node_id,
                sort_order,
                name,
                description,
                action,
                executor,
                dependencies_json,
                status,
                retry_count,
                max_retries,
                workflow_policy_json,
                verification_required,
                hitl_policy_json,
                input_refs_json,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id, node_id) DO UPDATE SET
                sort_order = excluded.sort_order,
                name = excluded.name,
                description = excluded.description,
                action = excluded.action,
                executor = excluded.executor,
                dependencies_json = excluded.dependencies_json,
                status = excluded.status,
                retry_count = excluded.retry_count,
                max_retries = excluded.max_retries,
                workflow_policy_json = excluded.workflow_policy_json,
                verification_required = excluded.verification_required,
                hitl_policy_json = excluded.hitl_policy_json,
                input_refs_json = excluded.input_refs_json,
                metadata_json = excluded.metadata_json
            """,
            (
                execution_id,
                node.node_id,
                sort_order,
                node.name,
                node.description,
                node.action,
                node.executor,
                _json_dumps(node.dependencies),
                node.status.value,
                node.retry_count,
                node.max_retries,
                _json_dumps(node.workflow_policy),
                int(node.verification_required),
                _json_dumps(node.hitl_policy),
                _json_dumps(node.input_refs),
                _json_dumps(node.metadata),
            ),
        )

    async def _append_event(
        self, connection: aiosqlite.Connection, event: ExecutionEvent
    ) -> None:
        await connection.execute(
            """
            INSERT INTO execution_events (
                execution_id,
                event_id,
                event_type,
                node_id,
                payload_json,
                timestamp
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.execution_id,
                event.event_id,
                event.event_type,
                event.node_id,
                _json_dumps(event.payload),
                event.timestamp.isoformat(),
            ),
        )

    async def _save_graph_version(
        self,
        connection: aiosqlite.Connection,
        graph_version: ExecutionGraphVersion,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO execution_graph_versions (
                execution_id,
                graph_version,
                nodes_json,
                edges_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                graph_version.execution_id,
                graph_version.graph_version,
                _json_dumps(graph_version.nodes),
                _json_dumps(graph_version.edges),
                graph_version.created_at.isoformat(),
            ),
        )

    def _record_from_row(self, row: aiosqlite.Row) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=row["execution_id"],
            task_id=row["task_id"],
            intent_text=row["intent_text"],
            status=row["status"],
            plan_version=row["plan_version"],
            replan_generation=row["replan_generation"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            metadata=_json_loads(row["metadata_json"]) or {},
        )

    def _node_from_row(self, row: aiosqlite.Row) -> ExecutionNode:
        return ExecutionNode(
            node_id=row["node_id"],
            name=row["name"],
            description=row["description"],
            action=row["action"],
            executor=row["executor"],
            dependencies=_json_loads(row["dependencies_json"]) or [],
            status=row["status"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            workflow_policy=_json_loads(row["workflow_policy_json"]) or {},
            verification_required=bool(row["verification_required"]),
            hitl_policy=_json_loads(row["hitl_policy_json"]) or {},
            input_refs=_json_loads(row["input_refs_json"]) or {},
            metadata=_json_loads(row["metadata_json"]) or {},
        )

    def _event_from_row(self, row: aiosqlite.Row) -> ExecutionEvent:
        return ExecutionEvent(
            event_id=row["event_id"],
            execution_id=row["execution_id"],
            event_type=row["event_type"],
            node_id=row["node_id"],
            payload=_json_loads(row["payload_json"]) or {},
            timestamp=row["timestamp"],
        )

    def _artifact_from_row(self, row: aiosqlite.Row) -> ExecutionArtifact:
        return ExecutionArtifact(
            artifact_id=row["artifact_id"],
            execution_id=row["execution_id"],
            node_id=row["node_id"],
            artifact_type=row["artifact_type"],
            content=_json_loads(row["content_json"]) or {},
            path_ref=row["path_ref"],
            metadata=_json_loads(row["metadata_json"]) or {},
            created_at=row["created_at"],
        )

    def _graph_version_from_row(self, row: aiosqlite.Row) -> ExecutionGraphVersion:
        return ExecutionGraphVersion(
            execution_id=row["execution_id"],
            graph_version=row["graph_version"],
            nodes=_json_loads(row["nodes_json"]) or [],
            edges=_json_loads(row["edges_json"]) or [],
            created_at=row["created_at"],
        )
