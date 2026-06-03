"""DAG execution runtime."""

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
from symbio.utils.types import Intent, Task


class DAGRuntime:
    """Execute persisted DAG nodes with registered agents."""

    def __init__(self, store: ExecutionStateStore, registry: AgentRegistry) -> None:
        self.store = store
        self.registry = registry

    async def run(self, execution_id: str) -> None:
        await self.store.update_execution_status(execution_id, ExecutionStatus.RUNNING)

        failed = False
        while True:
            nodes = await self.store.list_nodes(execution_id)
            ready_nodes = self._ready_nodes(nodes)
            if not ready_nodes:
                break

            for node in ready_nodes:
                if not await self._execute_node(execution_id, node):
                    failed = True
                    break
            if failed:
                break

        nodes = await self.store.list_nodes(execution_id)
        if failed or any(node.status == ExecutionNodeStatus.FAILED for node in nodes):
            await self.store.update_execution_status(execution_id, ExecutionStatus.FAILED)
            return

        if all(node.status == ExecutionNodeStatus.COMPLETED for node in nodes):
            artifacts = await self.store.list_artifacts(execution_id)
            missing_verification = self._missing_verification_nodes(nodes, artifacts)
            if missing_verification:
                await self.store.update_execution_status(
                    execution_id,
                    ExecutionStatus.NEEDS_VERIFICATION,
                )
                await self.store.append_event(
                    ExecutionEvent(
                        execution_id=execution_id,
                        event_type="execution_needs_verification",
                        payload={"missing_verification": missing_verification},
                    )
                )
                return
            await self.store.update_execution_status(
                execution_id,
                ExecutionStatus.COMPLETED,
            )
            return

        await self.store.update_execution_status(execution_id, ExecutionStatus.FAILED)

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
            and all(dependency in completed for dependency in node.dependencies)
        ]

    async def _execute_node(self, execution_id: str, node: ExecutionNode) -> bool:
        await self.store.update_node_status(
            execution_id,
            node.node_id,
            ExecutionNodeStatus.RUNNING,
        )
        await self.store.append_event(
            ExecutionEvent(
                execution_id=execution_id,
                node_id=node.node_id,
                event_type="node_started",
                payload={"executor": node.executor},
            )
        )

        agent = self.registry.get(node.executor)
        if agent is None:
            await self._fail_node(
                execution_id,
                node,
                {"reason": "agent_not_found", "executor": node.executor},
            )
            return False

        task = self._node_to_task(execution_id, node)
        try:
            result = await agent.execute(task)
        except Exception as exc:
            await self._fail_node(
                execution_id,
                node,
                {
                    "reason": "agent_exception",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return False

        if not result.success:
            await self._fail_node(
                execution_id,
                node,
                {
                    "reason": "agent_result_failed",
                    "content": result.content,
                    "data": result.data,
                },
            )
            return False

        await self.store.append_artifact(
            ExecutionArtifact(
                execution_id=execution_id,
                node_id=node.node_id,
                artifact_type="node_result",
                content=result.model_dump(mode="json"),
            )
        )
        await self.store.update_node_status(
            execution_id,
            node.node_id,
            ExecutionNodeStatus.COMPLETED,
        )
        await self.store.append_event(
            ExecutionEvent(
                execution_id=execution_id,
                node_id=node.node_id,
                event_type="node_completed",
                payload={"result_id": result.result_id},
            )
        )
        return True

    def _node_to_task(self, execution_id: str, node: ExecutionNode) -> Task:
        parameters = dict(node.metadata.get("parameters", {}))
        parameters.update(node.input_refs)
        task_metadata = dict(node.metadata.get("task_metadata", {}))
        runtime_metadata = {
            "execution_id": execution_id,
            "node_id": node.node_id,
            "workflow_policy": node.workflow_policy,
            "node_metadata": node.metadata,
        }
        if any(key in task_metadata for key in runtime_metadata):
            task_metadata["dag_runtime"] = runtime_metadata
        else:
            task_metadata.update(runtime_metadata)
        return Task(
            task_id=node.node_id,
            intent=Intent(
                raw_text=node.description or node.name,
                action=node.action,
                parameters=parameters,
            ),
            metadata=task_metadata,
        )

    @staticmethod
    def _missing_verification_nodes(
        nodes: list[ExecutionNode],
        artifacts: list[ExecutionArtifact],
    ) -> list[str]:
        verified_node_ids = {
            artifact.node_id
            for artifact in artifacts
            if artifact.artifact_type in {"verification", "verification_result"}
            and artifact.content.get("passed") is True
        }
        return [
            node.node_id
            for node in nodes
            if node.verification_required and node.node_id not in verified_node_ids
        ]

    async def _fail_node(
        self,
        execution_id: str,
        node: ExecutionNode,
        payload: dict,
    ) -> None:
        await self.store.update_node_status(
            execution_id,
            node.node_id,
            ExecutionNodeStatus.FAILED,
        )
        await self.store.append_event(
            ExecutionEvent(
                execution_id=execution_id,
                node_id=node.node_id,
                event_type="node_failed",
                payload=payload,
            )
        )
