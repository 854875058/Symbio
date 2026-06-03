"""Compile tasks and decompositions into DAG execution plans."""

from __future__ import annotations

from typing import Any

from symbio.core.decomposer import DecompositionResult, TaskDecomposer
from symbio.core.execution_models import ExecutionNode, ExecutionPlan
from symbio.core.workflow_policy import WorkflowPolicy
from symbio.utils.types import Task


class ExecutionPlanner:
    """Build an ExecutionPlan for a task."""

    def __init__(self, decomposer: TaskDecomposer | None = None) -> None:
        self.decomposer = decomposer or TaskDecomposer()

    async def plan(
        self,
        task: Task,
        force_single_node: bool = False,
    ) -> ExecutionPlan:
        """Compile a task into an execution plan."""
        if force_single_node:
            return self._single_node_plan(task)

        decomposition = await self.decomposer.decompose(task.intent, task.task_id)
        if len(decomposition.subtasks) <= 1:
            return self._single_node_plan(task, decomposition)

        rejection_reason = self._decomposition_rejection_reason(decomposition)
        if rejection_reason:
            return self._single_node_plan(
                task,
                decomposition,
                rejection_reason=rejection_reason,
            )

        return self._decomposition_plan(task, decomposition)

    def _single_node_plan(
        self,
        task: Task,
        decomposition: DecompositionResult | None = None,
        rejection_reason: str = "",
    ) -> ExecutionPlan:
        workflow_policy = self._workflow_policy(task)
        node_id = f"{task.task_id}:root"
        node = ExecutionNode(
            node_id=node_id,
            name=task.intent.raw_text,
            description=task.intent.raw_text,
            action=task.intent.action or "chat",
            executor=task.metadata.get("suggested_agent", "general"),
            workflow_policy=workflow_policy,
            verification_required=self._verification_required(task),
            metadata={"parameters": task.intent.parameters},
        )
        return ExecutionPlan(
            task_id=task.task_id,
            root_node_id=node_id,
            nodes=[node],
            edges=[],
            metadata=self._plan_metadata(task, decomposition, rejection_reason),
        )

    def _decomposition_plan(
        self,
        task: Task,
        decomposition: DecompositionResult,
    ) -> ExecutionPlan:
        workflow_policy = self._workflow_policy(task)
        verification_required = self._verification_required(task)
        nodes = [
            ExecutionNode(
                node_id=subtask.subtask_id,
                name=subtask.name,
                description=subtask.description,
                action=subtask.action,
                executor=subtask.suggested_agent,
                dependencies=list(subtask.dependencies),
                workflow_policy=workflow_policy,
                verification_required=verification_required,
                metadata={
                    "parameters": subtask.parameters,
                    "estimated_complexity": self._json_value(
                        subtask.estimated_complexity
                    ),
                },
            )
            for subtask in decomposition.subtasks
        ]
        root_node_id = next(
            node.node_id for node in nodes if not node.dependencies
        )
        edges = [
            {"source": dependency, "target": node.node_id}
            for node in nodes
            for dependency in node.dependencies
        ]
        return ExecutionPlan(
            task_id=task.task_id,
            root_node_id=root_node_id,
            nodes=nodes,
            edges=edges,
            metadata=self._plan_metadata(task, decomposition),
        )

    def _plan_metadata(
        self,
        task: Task,
        decomposition: DecompositionResult | None = None,
        rejection_reason: str = "",
    ) -> dict[str, Any]:
        metadata = {
            "intent": task.intent.model_dump(mode="json"),
            "decomposition_reasoning": decomposition.reasoning
            if decomposition is not None
            else "",
            "needs_debate": decomposition.needs_debate
            if decomposition is not None
            else False,
        }
        if rejection_reason:
            metadata["decomposition_rejected"] = True
            metadata["decomposition_rejection_reason"] = rejection_reason
        return metadata

    @staticmethod
    def _workflow_policy(task: Task) -> dict[str, Any]:
        workflow_policy = task.metadata.get("workflow_policy")
        if isinstance(workflow_policy, WorkflowPolicy):
            return workflow_policy.model_dump(mode="json")
        if hasattr(workflow_policy, "model_dump"):
            return workflow_policy.model_dump(mode="json")
        if isinstance(workflow_policy, dict):
            return dict(workflow_policy)
        return {}

    @staticmethod
    def _verification_required(task: Task) -> bool:
        workflow_policy = task.metadata.get("workflow_policy")
        if isinstance(workflow_policy, dict):
            return bool(
                workflow_policy.get(
                    "require_verification_before_completion",
                    False,
                )
            )
        return bool(
            getattr(
                workflow_policy,
                "require_verification_before_completion",
                False,
            )
        )

    @staticmethod
    def _json_value(value: Any) -> Any:
        return value.value if hasattr(value, "value") else value

    @staticmethod
    def _decomposition_rejection_reason(
        decomposition: DecompositionResult,
    ) -> str:
        subtask_ids = [subtask.subtask_id for subtask in decomposition.subtasks]
        unique_ids = set(subtask_ids)
        if len(unique_ids) != len(subtask_ids):
            return "duplicate subtask_id"

        dependencies_by_id = {
            subtask.subtask_id: list(subtask.dependencies)
            for subtask in decomposition.subtasks
        }
        for subtask_id, dependencies in dependencies_by_id.items():
            for dependency in dependencies:
                if dependency not in unique_ids:
                    return f"unknown dependency '{dependency}' for subtask '{subtask_id}'"

        if not any(not dependencies for dependencies in dependencies_by_id.values()):
            return "decomposition has no root node"

        visiting: set[str] = set()
        visited: set[str] = set()

        def has_cycle(subtask_id: str) -> bool:
            if subtask_id in visiting:
                return True
            if subtask_id in visited:
                return False

            visiting.add(subtask_id)
            for dependency in dependencies_by_id[subtask_id]:
                if has_cycle(dependency):
                    return True
            visiting.remove(subtask_id)
            visited.add(subtask_id)
            return False

        for subtask_id in subtask_ids:
            if has_cycle(subtask_id):
                return "decomposition contains a dependency cycle"

        return ""


__all__ = ["ExecutionPlanner"]
