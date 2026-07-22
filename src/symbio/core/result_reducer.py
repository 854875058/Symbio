"""Reduce DAG execution records into the public task result type."""

from __future__ import annotations

from symbio.core.execution_models import (
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionNode,
    ExecutionRecord,
    ExecutionStatus,
)
from symbio.utils.types import Result


class ResultReducer:
    """Build a final Result from persisted execution state."""

    def reduce(
        self,
        record: ExecutionRecord,
        nodes: list[ExecutionNode],
        artifacts: list[ExecutionArtifact],
        events: list[ExecutionEvent],
    ) -> Result:
        data = {
            "execution_id": record.execution_id,
            "status": record.status.value,
            "node_count": len(nodes),
            "event_count": len(events),
            "artifact_count": len(artifacts),
        }

        content = self._final_content(record, artifacts, nodes)
        missing_verification_nodes = self._missing_verification_nodes(nodes, artifacts)
        if missing_verification_nodes:
            data["status"] = ExecutionStatus.NEEDS_VERIFICATION.value
            data["missing_verification"] = missing_verification_nodes
            return Result(
                task_id=record.task_id,
                success=False,
                content=(
                    "Verification required before returning the final result: "
                    + ", ".join(missing_verification_nodes)
                ),
                data=data,
            )

        return Result(
            task_id=record.task_id,
            success=record.status == ExecutionStatus.COMPLETED,
            content=content,
            data=data,
        )

    def _final_content(
        self,
        record: ExecutionRecord,
        artifacts: list[ExecutionArtifact],
        nodes: list[ExecutionNode] | None = None,
    ) -> str:
        # Aggregate results from sink nodes (nodes with no successors)
        if nodes:
            node_ids = {n.node_id for n in nodes}
            has_successor = set()
            for n in nodes:
                for dep in n.dependencies:
                    has_successor.add(dep)
            sink_ids = node_ids - has_successor

            sink_results = []
            for artifact in reversed(artifacts):
                if artifact.artifact_type == "node_result" and artifact.node_id in sink_ids:
                    content = artifact.content.get("content")
                    if content:
                        sink_results.append(str(content))

            if sink_results:
                if len(sink_results) == 1:
                    return sink_results[0]
                return "\n\n---\n\n".join(reversed(sink_results))

        # Fallback: last node_result
        for artifact in reversed(artifacts):
            if artifact.artifact_type == "node_result":
                content = artifact.content.get("content")
                if content:
                    return str(content)
        if record.status == ExecutionStatus.FAILED:
            return "DAG execution failed."
        if record.status == ExecutionStatus.CANCELLED:
            return "DAG execution cancelled."
        return "DAG execution completed."

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
