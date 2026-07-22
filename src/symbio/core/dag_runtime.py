"""DAG execution runtime."""

from __future__ import annotations

import asyncio
from enum import Enum

from symbio.agents.registry import AgentRegistry
from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionNodeStatus,
    ExecutionStatus,
    ReplanDecision,
    ReplanDecisionType,
)
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.core.replanner import Replanner
from symbio.utils.types import Intent, Task


class _NodeOutcome(Enum):
    """Internal outcome of processing a single node."""

    SUCCESS = "success"
    FAILED = "failed"
    SUSPENDED = "suspended"  # WAITING_HITL or WAITING_CLARIFICATION
    RETRY = "retry"  # Node failed but will be retried


class DAGRuntime:
    """Execute persisted DAG nodes with registered agents."""

    def __init__(
        self,
        store: ExecutionStateStore,
        registry: AgentRegistry,
        replanner: Replanner | None = None,
        guardrail: object | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.replanner = replanner or Replanner()
        self.guardrail = guardrail

    async def run(self, execution_id: str) -> None:
        await self.store.update_execution_status(execution_id, ExecutionStatus.RUNNING)

        while True:
            # Check if execution was cancelled
            record = await self.store.get_execution(execution_id)
            if record and record.status == ExecutionStatus.CANCELLED:
                return

            nodes = await self.store.list_nodes(execution_id)
            ready_nodes = self._ready_nodes(nodes)
            if not ready_nodes:
                break

            # Execute ready nodes concurrently
            outcomes = await asyncio.gather(
                *[self._process_node(execution_id, node) for node in ready_nodes],
                return_exceptions=True,
            )

            should_stop = False
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    should_stop = True
                    break
                if outcome == _NodeOutcome.FAILED:
                    should_stop = True
                    break
                if outcome == _NodeOutcome.SUSPENDED:
                    return
                # RETRY and SUCCESS continue to next iteration
            if should_stop:
                break

        nodes = await self.store.list_nodes(execution_id)

        # Re-check cancellation after processing
        record = await self.store.get_execution(execution_id)
        if record and record.status == ExecutionStatus.CANCELLED:
            return

        if self._has_unrepaired_failure(nodes):
            await self.store.update_execution_status(execution_id, ExecutionStatus.FAILED)
            return

        if all(
            node.status in {ExecutionNodeStatus.COMPLETED, ExecutionNodeStatus.FAILED}
            for node in nodes
        ):
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

        # If we have any WAITING nodes, preserve that status
        waiting_statuses = {ExecutionNodeStatus.WAITING_HITL}
        if any(n.status in waiting_statuses for n in nodes):
            return

        await self.store.update_execution_status(execution_id, ExecutionStatus.FAILED)

    async def _process_node(self, execution_id: str, node: ExecutionNode) -> _NodeOutcome:
        """Process a single node and return its outcome."""
        if not await self._execute_node(execution_id, node):
            return await self._handle_failed_node(execution_id, node)
        return _NodeOutcome.SUCCESS

    def _ready_nodes(self, nodes: list[ExecutionNode]) -> list[ExecutionNode]:
        completed = {node.node_id for node in nodes if node.status == ExecutionNodeStatus.COMPLETED}
        failed = {node.node_id for node in nodes if node.status == ExecutionNodeStatus.FAILED}
        return [
            node
            for node in nodes
            if node.status == ExecutionNodeStatus.PENDING
            and all(
                dependency in completed
                or dependency in failed
                and dependency in node.metadata.get("replan", {}).get("patches", [])
                for dependency in node.dependencies
            )
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

        # Fetch existing artifacts for data dependency injection
        all_artifacts = await self.store.list_artifacts(execution_id)
        task = await self._node_to_task(execution_id, node, all_artifacts)
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
            failure_payload = {
                "reason": "agent_result_failed",
                "content": result.content,
                "data": result.data,
            }
            failure_type = (
                result.data.get("failure_type")
                or result.data.get("kind")
                or result.data.get("type")
            )
            if failure_type:
                failure_payload["failure_type"] = failure_type
            await self._fail_node(
                execution_id,
                node,
                failure_payload,
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

        # Budget check: deduct tokens after successful execution
        if self.guardrail is not None and hasattr(result, "token_usage") and result.token_usage:
            try:
                tu = result.token_usage
                allowed = self.guardrail.check_and_deduct(
                    task_id=execution_id,
                    tokens=getattr(tu, "total_tokens", 0),
                    cost_usd=getattr(tu, "estimated_cost", 0.0),
                )
                if not allowed:
                    await self._fail_node(
                        execution_id,
                        node,
                        {"reason": "budget_exceeded", "tokens": getattr(tu, "total_tokens", 0)},
                    )
                    return False
            except Exception as exc:
                from symbio.utils.logger import get_logger

                get_logger("dag_runtime").warning(f"预算扣减检查失败（不阻断）: {exc}")

        # Mark verification as pending (no longer auto-pass).
        # VerificationStage in the pipeline will handle real verification.
        if node.verification_required:
            await self.store.append_artifact(
                ExecutionArtifact(
                    execution_id=execution_id,
                    node_id=node.node_id,
                    artifact_type="verification",
                    content={
                        "passed": None,
                        "verification_status": "pending",
                        "method": "awaiting_verification",
                        "result_content_preview": (result.content or "")[:500],
                    },
                ),
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

    async def _node_to_task(
        self, execution_id: str, node: ExecutionNode, all_artifacts: list | None = None
    ) -> Task:
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

        # Restore model selection from planner metadata
        selected_model = task_metadata.get("selected_model", "")

        # Inject predecessor results as data dependencies
        if node.dependencies and all_artifacts is not None:
            predecessor_results = {}
            for dep_id in node.dependencies:
                dep_artifacts = [
                    a
                    for a in all_artifacts
                    if a.node_id == dep_id and a.artifact_type == "node_result"
                ]
                if dep_artifacts:
                    latest = dep_artifacts[-1]
                    predecessor_results[dep_id] = latest.content.get("content", "")
            if predecessor_results:
                task_metadata["predecessor_results"] = predecessor_results

        return Task(
            task_id=node.node_id,
            intent=Intent(
                raw_text=node.description or node.name,
                action=node.action,
                parameters=parameters,
            ),
            model=selected_model,
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
        latest_node = await self._get_node(execution_id, node.node_id)
        failure_kind = payload.get("failure_type") or payload.get("kind") or payload.get("reason")
        latest_node.metadata = {
            **latest_node.metadata,
            "last_failure": {
                **payload,
                "kind": failure_kind,
                "message": payload.get("content")
                or payload.get("error")
                or payload.get("reason", ""),
            },
        }
        latest_node.status = ExecutionNodeStatus.FAILED
        await self.store.upsert_node(execution_id, latest_node)
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

    async def _handle_failed_node(self, execution_id: str, node: ExecutionNode) -> _NodeOutcome:
        latest_node = await self._get_node(execution_id, node.node_id)
        failure = self._failure_payload(node, latest_node)
        decision = self.replanner.decide(node.node_id, failure)
        await self._record_replan_event(execution_id, decision, latest_node)

        if decision.decision == ReplanDecisionType.RETRY:
            await self._apply_retry(execution_id, latest_node, decision)
            return _NodeOutcome.RETRY

        if decision.decision in {
            ReplanDecisionType.LOCAL_PATCH,
            ReplanDecisionType.GLOBAL_REPLAN,
        }:
            applied = await self._apply_graph_mutation(execution_id, latest_node, decision)
            return _NodeOutcome.SUCCESS if applied else _NodeOutcome.FAILED

        if decision.decision == ReplanDecisionType.WAITING_HITL:
            await self.store.update_node_status(
                execution_id,
                node.node_id,
                ExecutionNodeStatus.WAITING_HITL,
            )
            await self.store.update_execution_status(
                execution_id,
                ExecutionStatus.WAITING_HITL,
            )
            return _NodeOutcome.SUSPENDED

        if decision.decision == ReplanDecisionType.WAITING_CLARIFICATION:
            await self.store.update_execution_status(
                execution_id,
                ExecutionStatus.WAITING_CLARIFICATION,
            )
            return _NodeOutcome.SUSPENDED

        return _NodeOutcome.FAILED

    def _failure_payload(
        self,
        original_node: ExecutionNode,
        latest_node: ExecutionNode,
    ) -> dict:
        failure = {
            "kind": "unknown",
            "retry_count": latest_node.retry_count,
            "max_retries": latest_node.max_retries,
            "replan_count": int(latest_node.metadata.get("replan_count", 0)),
        }
        failure.update(latest_node.metadata.get("last_failure", {}))
        failure.update(original_node.metadata.get("last_failure", {}))
        if "kind" not in failure and "failure_type" in failure:
            failure["kind"] = failure["failure_type"]
        return failure

    async def _record_replan_event(
        self,
        execution_id: str,
        decision: ReplanDecision,
        node: ExecutionNode,
    ) -> None:
        await self.store.append_event(
            ExecutionEvent(
                execution_id=execution_id,
                node_id=node.node_id,
                event_type="node_replanned",
                payload={
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "retry_count": node.retry_count
                    + (1 if decision.decision == ReplanDecisionType.RETRY else 0),
                    "mutation_count": len(decision.mutations),
                    "metadata": decision.metadata,
                },
            )
        )

    async def _apply_retry(
        self,
        execution_id: str,
        node: ExecutionNode,
        decision: ReplanDecision,
    ) -> None:
        node.retry_count += 1
        node.status = ExecutionNodeStatus.PENDING
        node.metadata = {
            **node.metadata,
            "replan_count": int(node.metadata.get("replan_count", 0)) + 1,
            "last_replan": {
                "decision": decision.decision.value,
                "reason": decision.reason,
            },
        }
        await self.store.upsert_node(execution_id, node)
        await self.store.update_node_status(
            execution_id,
            node.node_id,
            ExecutionNodeStatus.PENDING,
        )
        await self._save_mutated_graph_version(
            execution_id,
            [{"action": "retry_node", "node_id": node.node_id, "retry_count": node.retry_count}],
        )

    async def _apply_graph_mutation(
        self,
        execution_id: str,
        node: ExecutionNode,
        decision: ReplanDecision,
    ) -> bool:
        if not decision.mutations:
            return False

        for mutation in decision.mutations:
            if mutation.get("action") != "add_node":
                continue
            patch_node = ExecutionNode(
                node_id=mutation["node_id"],
                name=mutation.get("name", mutation["node_id"]),
                description=mutation.get("description", ""),
                action=mutation.get("node_action", mutation.get("action", "chat")),
                executor=mutation.get("executor", "general"),
                dependencies=list(mutation.get("dependencies", [])),
                verification_required=bool(mutation.get("verification_required", False)),
                metadata={
                    **mutation.get("metadata", {}),
                    "replan": {
                        "decision": decision.decision.value,
                        "patches": [node.node_id],
                    },
                },
            )
            await self.store.upsert_node(execution_id, patch_node)

        await self._save_mutated_graph_version(execution_id, decision.mutations)
        return True

    async def _save_mutated_graph_version(
        self,
        execution_id: str,
        mutations: list[dict],
    ) -> None:
        graph_versions = await self.store.list_graph_versions(execution_id)
        next_version = (graph_versions[-1].graph_version if graph_versions else 0) + 1
        nodes = [node.model_dump(mode="json") for node in await self.store.list_nodes(execution_id)]
        edges = [
            {"source": dependency, "target": node["node_id"]}
            for node in nodes
            for dependency in node.get("dependencies", [])
        ]
        await self.store.save_graph_version(execution_id, next_version, nodes, edges)
        await self.store.append_event(
            ExecutionEvent(
                execution_id=execution_id,
                event_type="graph_mutated",
                payload={"graph_version": next_version, "mutations": mutations},
            )
        )

    async def _get_node(self, execution_id: str, node_id: str) -> ExecutionNode:
        for node in await self.store.list_nodes(execution_id):
            if node.node_id == node_id:
                return node
        raise KeyError(f"Unknown node_id: {node_id}")

    @staticmethod
    def _has_unrepaired_failure(nodes: list[ExecutionNode]) -> bool:
        repaired = {
            patched
            for node in nodes
            if node.status == ExecutionNodeStatus.COMPLETED
            for patched in node.metadata.get("replan", {}).get("patches", [])
        }
        return any(
            node.status == ExecutionNodeStatus.FAILED and node.node_id not in repaired
            for node in nodes
        )
