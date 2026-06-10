"""Rule-based replanning decisions for DAG execution failures."""

from __future__ import annotations

from typing import Any

from symbio.core.execution_models import ReplanDecision, ReplanDecisionType


class Replanner:
    """Decide how execution should proceed after a node failure."""

    def __init__(self, max_retries: int = 1, max_replan_count: int = 3) -> None:
        self.max_retries = max_retries
        self.max_replan_count = max_replan_count

    def decide(self, node_id: str, failure: dict[str, Any]) -> ReplanDecision:
        failure_type = failure.get("kind") or failure.get("failure_type") or failure.get("type", "")
        retry_count = int(failure.get("retry_count", 0))
        max_retries = int(failure.get("max_retries", self.max_retries))
        replan_count = int(failure.get("replan_count", 0))
        message = str(failure.get("message") or failure.get("details") or "")

        if failure_type == "tool_transient_error" and retry_count < max_retries:
            return ReplanDecision(
                decision=ReplanDecisionType.RETRY,
                reason=message or "transient tool error within retry limit",
                node_id=node_id,
                metadata={"retry_count": retry_count, "max_retries": max_retries},
            )

        if failure_type == "verification_failure" and replan_count >= self.max_replan_count:
            return ReplanDecision(
                decision=ReplanDecisionType.FAIL,
                reason=message or "maximum replan count reached",
                node_id=node_id,
                metadata={"replan_count": replan_count, "max_replan_count": self.max_replan_count},
            )

        if failure_type == "verification_failure":
            return ReplanDecision(
                decision=ReplanDecisionType.LOCAL_PATCH,
                reason=message or "verification failed; add a repair node",
                node_id=node_id,
                mutations=[
                    {
                        "action": "add_node",
                        "node_id": f"{node_id}:repair",
                        "name": "Repair failed verification",
                        "description": "Repair the failed verification evidence and rerun checks.",
                        "node_action": "repair",
                        "executor": "general",
                        "dependencies": [node_id],
                        "metadata": {"failure": failure},
                    }
                ],
            )

        if failure_type == "requirement_ambiguity":
            return ReplanDecision(
                decision=ReplanDecisionType.WAITING_CLARIFICATION,
                reason="requirement ambiguity needs clarification",
                node_id=node_id,
            )

        if failure_type == "permission_required":
            return ReplanDecision(
                decision=ReplanDecisionType.WAITING_HITL,
                reason="permission required before continuing",
                node_id=node_id,
            )

        return ReplanDecision(
            decision=ReplanDecisionType.FAIL,
            reason=f"unhandled failure type: {failure_type or 'unknown'}",
            node_id=node_id,
        )
