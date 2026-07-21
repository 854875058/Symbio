"""High-level DAG orchestration for task execution."""

from __future__ import annotations

from symbio.agents.registry import AgentRegistry, get_registry
from symbio.core.dag_runtime import DAGRuntime
from symbio.core.execution_models import ExecutionPlan, ExecutionStatus
from symbio.core.execution_planner import ExecutionPlanner
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.core.result_reducer import ResultReducer
from symbio.utils.types import Result, Task


class DAGOrchestrator:
    """Compose planning, persistence, runtime execution, and result reduction."""

    def __init__(
        self,
        planner: ExecutionPlanner | None = None,
        store: ExecutionStateStore | None = None,
        runtime: DAGRuntime | None = None,
        reducer: ResultReducer | None = None,
        registry: AgentRegistry | None = None,
        guardrail: object | None = None,
    ) -> None:
        resolved_store = store or ExecutionStateStore()
        resolved_registry = registry or get_registry()
        self._ensure_builtin_agents(resolved_registry)

        self.planner = planner or ExecutionPlanner()
        self.store = resolved_store
        self.runtime = runtime or DAGRuntime(resolved_store, resolved_registry, guardrail=guardrail)
        self.reducer = reducer or ResultReducer()
        self.registry = resolved_registry

    async def execute(self, task: Task) -> Result:
        """Plan, persist, execute, and reduce a task via the DAG pipeline."""
        plan: ExecutionPlan | None = None
        try:
            plan = await self.planner.plan(task)
            await self.store.create_execution(plan, task.intent.raw_text)
            await self.runtime.run(plan.execution_id)

            record = await self.store.get_execution(plan.execution_id)
            if record is None:
                raise KeyError(f"Unknown execution_id: {plan.execution_id}")

            nodes = await self.store.list_nodes(plan.execution_id)
            artifacts = await self.store.list_artifacts(plan.execution_id)
            events = await self.store.list_events(plan.execution_id)
            return self.reducer.reduce(record, nodes, artifacts, events)
        except Exception as exc:
            if plan is not None and hasattr(self.store, "update_execution_status"):
                try:
                    await self.store.update_execution_status(
                        plan.execution_id,
                        ExecutionStatus.FAILED,
                    )
                except Exception:
                    pass
            return Result(
                task_id=task.task_id,
                success=False,
                content=f"DAG execution failed: {exc}",
                data={
                    "status": "failed",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    **({"execution_id": plan.execution_id} if plan else {}),
                },
            )

    @staticmethod
    def _ensure_builtin_agents(registry: AgentRegistry) -> None:
        if not hasattr(registry, "get") or not hasattr(registry, "register_instance"):
            return
        if registry.get("general") is not None:
            return
        from symbio.agents.builtin.general_agent import GeneralAgent

        registry.register_instance(GeneralAgent())


__all__ = ["DAGOrchestrator"]
