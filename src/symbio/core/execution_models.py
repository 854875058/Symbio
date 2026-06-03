"""DAG-first execution model definitions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_json_ready(value: Any, field_name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    return value


class ExecutionStatus(str, Enum):
    """Lifecycle status for an execution record."""

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
    """Lifecycle status for a node in an execution graph."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReplanDecisionType(str, Enum):
    """Decision outcomes for graph replanning."""

    NONE = "none"
    RETRY = "retry"
    LOCAL_PATCH = "local_patch"
    GLOBAL_REPLAN = "global_replan"
    WAITING_HITL = "waiting_hitl"
    WAITING_CLARIFICATION = "waiting_clarification"
    FAIL = "fail"


class ExecutionNode(BaseModel):
    """A single executable node in an execution plan."""

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

    @field_validator("workflow_policy", "hitl_policy", "input_refs", "metadata")
    @classmethod
    def validate_json_fields(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        return _ensure_json_ready(value, info.field_name)


class ExecutionPlan(BaseModel):
    """DAG execution plan for a task."""

    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    root_node_id: str
    nodes: list[ExecutionNode] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    plan_version: int = 1
    replan_generation: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("edges")
    @classmethod
    def validate_edges_json_ready(
        cls, value: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return _ensure_json_ready(value, "edges")

    @field_validator("metadata")
    @classmethod
    def validate_metadata_json_ready(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _ensure_json_ready(value, "metadata")

    @model_validator(mode="after")
    def validate_dag_contract(self) -> "ExecutionPlan":
        node_ids = [node.node_id for node in self.nodes]
        unique_node_ids = set(node_ids)

        if len(unique_node_ids) != len(node_ids):
            raise ValueError("node_id values must be unique")

        if self.root_node_id not in unique_node_ids:
            raise ValueError("root_node_id must reference an existing node")

        dependencies_by_node = {
            node.node_id: set(node.dependencies)
            for node in self.nodes
        }
        if dependencies_by_node[self.root_node_id]:
            raise ValueError("root_node_id must reference a node without dependencies")

        edge_pairs: set[tuple[str, str]] = set()
        for edge in self.edges:
            if "source" not in edge or "target" not in edge:
                raise ValueError("each edge must include source and target")
            source = edge["source"]
            target = edge["target"]
            if source not in unique_node_ids or target not in unique_node_ids:
                raise ValueError("edge endpoints must reference existing nodes")
            edge_pairs.add((source, target))

        dependency_pairs = {
            (dep, node.node_id)
            for node in self.nodes
            for dep in node.dependencies
        }
        for node in self.nodes:
            for dep in node.dependencies:
                if dep not in unique_node_ids:
                    raise ValueError("dependencies must reference existing nodes")

        if edge_pairs != dependency_pairs:
            raise ValueError("edges and dependencies must describe the same graph")

        return self


class ExecutionRecord(BaseModel):
    """Top-level execution state for a task."""

    execution_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    intent_text: str
    status: ExecutionStatus = ExecutionStatus.CREATED
    plan_version: int = 1
    replan_generation: int = 0
    created_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata_json_ready(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _ensure_json_ready(value, "metadata")


class ExecutionEvent(BaseModel):
    """Append-only event emitted during execution."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    event_type: str
    node_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utc_now)

    @field_validator("payload")
    @classmethod
    def validate_payload_json_ready(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _ensure_json_ready(value, "payload")


class ExecutionArtifact(BaseModel):
    """Artifact produced during execution."""

    artifact_id: str = Field(default_factory=lambda: str(uuid4()))
    execution_id: str
    node_id: str = ""
    artifact_type: str
    content: dict[str, Any] = Field(default_factory=dict)
    path_ref: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("content", "metadata")
    @classmethod
    def validate_json_fields(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        return _ensure_json_ready(value, info.field_name)


class ExecutionGraphVersion(BaseModel):
    """Persisted version of an execution graph."""

    execution_id: str
    graph_version: int
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("nodes", "edges")
    @classmethod
    def validate_json_fields(
        cls, value: list[dict[str, Any]], info: Any
    ) -> list[dict[str, Any]]:
        return _ensure_json_ready(value, info.field_name)


class ReplanDecision(BaseModel):
    """Decision record produced by replanning policy."""

    decision: ReplanDecisionType = ReplanDecisionType.NONE
    reason: str = ""
    node_id: str = ""
    mutations: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("mutations", "metadata")
    @classmethod
    def validate_json_fields(cls, value: Any, info: Any) -> Any:
        return _ensure_json_ready(value, info.field_name)
