# DAG-First Orchestrator Design

Date: 2026-06-03

## Summary

Symbio will move from an Orchestrator-owned execution flow to a DAG-first
execution architecture. The existing Orchestrator becomes a thin ingress and
control layer. The primary execution kernel becomes a DAG runtime that owns
planning, scheduling, node state, retry, HITL suspension, recovery,
observation-driven graph mutation, and final result reduction.

The product remains result-first for users: UI and API consumers should see the
final task result first. The underlying system is process-driven so that every
result has traceable execution evidence, graph history, approvals, verification
records, and recovery state.

## Goals

- Make DAG execution the default path for all tasks, including single-step
  tasks.
- Replace the current Decomposer plus Agent execution path with a DAG-first
  runtime.
- Preserve task history, node history, graph versions, verification evidence,
  HITL decisions, and replan decisions.
- Support retry, local graph patching, global replanning, suspension, resume,
  cancellation, and final reduction through one execution model.
- Keep user-facing APIs and UI result-first while exposing process evidence as
  inspectable detail.

## Non-Goals

- This design does not implement MCP, Browser, or Computer Use product loops.
- This design does not redesign the memory subsystem.
- This design does not replace all Agent implementations.
- This design does not remove HITL, StateManager, TaskDecomposer, or DAGEngine;
  it repositions them behind clearer execution boundaries.

## Chosen Approach

Use a direct DAG-first rewrite instead of a gradual dual-track migration.

The Orchestrator is reduced to:

- receive `Message`
- parse `Intent`
- evaluate complexity
- select model
- inject workflow policy and memory context
- perform pre-execution HITL checks
- delegate execution to the DAG-first orchestration layer

The main execution path moves to a new DAG orchestration stack:

- `DAGOrchestrator`
- `ExecutionPlanner`
- `DAGRuntime`
- `ExecutionStateStore`
- `Replanner`
- `ResultReducer`

## Core Components

### Thin Orchestrator

File: `src/symbio/core/orchestrator.py`

Responsibilities:

- Ingress from CLI, Web, IM, and API callers.
- Intent parsing and model selection.
- Workflow policy injection.
- Memory context injection.
- Pre-runtime HITL gate.
- Delegation to `DAGOrchestrator`.

The Orchestrator should not directly execute subtasks, own graph mutation, or
aggregate node results.

### DAGOrchestrator

File: `src/symbio/core/dag_orchestrator.py`

Responsibilities:

- Accept a prepared `Task`.
- Ask `ExecutionPlanner` to produce an execution plan.
- Persist the initial plan with `ExecutionStateStore`.
- Run the graph through `DAGRuntime`.
- Invoke `ResultReducer` after terminal graph state.
- Return a final `Result`.

### ExecutionPlanner

File: `src/symbio/core/execution_planner.py`

Responsibilities:

- Convert a simple task into a single-node DAG.
- Convert complex tasks through `TaskDecomposer`.
- Compile decomposition output into DAG nodes and edges.
- Attach node metadata:
  - executor
  - dependencies
  - workflow policy
  - verification requirement
  - retry policy
  - HITL policy
  - input and output references

### DAGRuntime

File: `src/symbio/core/dag_runtime.py`

Responsibilities:

- Load execution graph state.
- Schedule ready nodes.
- Execute dependency-free nodes concurrently.
- Persist node lifecycle events.
- Persist node outputs, observations, verification evidence, and token usage.
- Suspend nodes for HITL or clarification.
- Resume suspended executions.
- Call `Replanner` after node completion, node failure, and verification
  failure.

### ExecutionStateStore

File: `src/symbio/core/execution_state_store.py`

Responsibilities:

- Store task execution records.
- Store node definitions and node states.
- Store append-only execution events.
- Store artifacts and verification evidence.
- Store graph versions.
- Restore executions after process restart.

`StateManager` can remain as an underlying adapter or supporting store, but the
DAG runtime should consume a dedicated execution-state interface rather than
reaching directly into Orchestrator internals.

### Replanner

File: `src/symbio/core/replanner.py`

Responsibilities:

- Inspect node failure, verification failure, missing context, and runtime
  observations.
- Decide whether to retry, locally patch the graph, globally replan, suspend,
  or fail.
- Return explicit graph mutations rather than mutating state directly.
- Persist every decision through `ExecutionStateStore`.

### ResultReducer

File: `src/symbio/core/result_reducer.py`

Responsibilities:

- Summarize terminal DAG state into a user-facing `Result`.
- Prefer final output first.
- Attach process evidence in `Result.data`.
- Enforce workflow-policy completion gates before returning `completed`.
- Return `needs_verification`, `waiting_hitl`, `waiting_clarification`, or
  `failed_policy` when the graph cannot honestly be marked complete.

## Execution Flow

1. `Message` enters the thin Orchestrator.
2. Orchestrator produces a prepared `Task` with intent, model, workflow policy,
   memory context, and request metadata.
3. `DAGOrchestrator.execute(task)` receives the task.
4. `ExecutionPlanner` creates an execution plan.
5. `ExecutionStateStore` persists the execution, graph version, nodes, edges,
   and initial event.
6. `DAGRuntime` executes ready nodes and persists every state transition.
7. `Replanner` evaluates observations and failures after each relevant event.
8. Runtime applies approved graph mutations and continues.
9. `ResultReducer` produces the final result-first response with evidence.

## State Model

Task-level states:

```text
created
planned
running
waiting_hitl
waiting_clarification
replanning
verifying
completed
failed
cancelled
```

Node-level states:

```text
pending
ready
running
waiting_hitl
retrying
completed
failed
skipped
```

All tasks go through DAG state. A simple task is represented as a single-node
DAG, not as a separate execution path.

## Replanning Rules

Only these triggers can invoke replanning:

- node failure
- verification failure
- missing dependency or missing information
- runtime observation that invalidates the current plan

Replanning has three levels:

1. Retry
   - Re-run the same node with adjusted parameters, timeout, model, or executor.
   - Does not change graph structure.

2. Local patch
   - Adds diagnostic, preprocessing, verification, or substitute execution
     nodes near the failed area.
   - Preserves unaffected nodes and consumed stable outputs by default.

3. Global replan
   - Creates a new graph generation when the original decomposition is no
     longer valid.
   - Does not overwrite old graph history.
   - Links versions through `replan_generation` and graph-version records.

Hard constraints:

- No infinite replanning. Enforce `max_replan_count`.
- No overwriting historical events.
- No rerunning stable consumed nodes unless explicitly invalidated.
- Every replan decision must be auditable.

## Persistence Model

The execution store should include these logical records:

### executions

- `execution_id`
- `task_id`
- `intent_json`
- `status`
- `plan_version`
- `replan_generation`
- `created_at`
- `completed_at`

### execution_nodes

- `execution_id`
- `node_id`
- `node_type`
- `executor`
- `dependencies_json`
- `policy_json`
- `input_refs_json`
- `status`

### execution_events

- `event_id`
- `execution_id`
- `node_id`
- `event_type`
- `payload_json`
- `timestamp`

Events are append-only.

### execution_artifacts

- `artifact_id`
- `execution_id`
- `node_id`
- `artifact_type`
- `content_json`
- `path_ref`

Artifacts hold node outputs, observations, approval evidence, verification
commands, verification results, and file summaries.

### execution_graph_versions

- `execution_id`
- `graph_version`
- `nodes_json`
- `edges_json`
- `created_at`

Graph versions allow the UI and audit layer to compare the original plan,
local patches, and global replans.

## API Impact

Existing endpoints remain but become execution-aware:

- `GET /api/tasks/{id}` returns final task summary plus execution summary.
- `GET /api/tasks/{id}/dag` returns the current graph version, node states, and
  recent events.

New endpoints:

- `GET /api/executions/{execution_id}`
- `GET /api/executions/{execution_id}/events`
- `GET /api/executions/{execution_id}/artifacts`
- `POST /api/executions/{execution_id}/resume`
- `POST /api/executions/{execution_id}/cancel`

## UI Impact

The UI remains result-first:

- task cards show final status and final result first
- process evidence is shown in expandable detail panels

Required panels:

- execution timeline
- DAG graph state panel
- graph-version comparison panel
- evidence panel for workflow policy, verification, approvals,
  observations, and replan reasons

## Error Handling

- Transient tool errors first become retries.
- Test or assertion failures become diagnostic or repair subgraphs.
- Permission or high-risk operations move to `waiting_hitl`.
- Requirement ambiguity moves to `waiting_clarification`.
- Upstream contract mismatch triggers local patch before global replan.
- Planner-level invalidity triggers global replan.

## Testing Strategy

### Planner tests

- Simple task becomes a single-node DAG.
- Decomposition becomes valid nodes and edges.
- Workflow policy and verification requirements are attached to nodes.

### Runtime tests

- Ready-node scheduling.
- Dependency blocking.
- Concurrent independent node execution.
- Retry behavior.
- HITL suspension and resume.
- Local patch mutation.
- Global replan generation.

### Persistence tests

- Execution graph can be restored after restart.
- Node state survives restart.
- Graph versions are append-only.
- Events and artifacts are queryable.

### Integration tests

- `Orchestrator.process()` uses the DAG-first path.
- Simple single-node task returns a final result.
- Multi-node task respects dependencies.
- Verification failure creates a repair path.
- Risky task suspends through HITL and resumes.
- Runtime observation triggers replan.

## Rollout Decision

The selected rollout is direct main-path replacement, not dual-track gray
release. The implementation should still preserve focused regression tests
around the old behavior so that externally visible APIs do not regress.

## Acceptance Criteria

- All tasks, including simple tasks, execute through DAG state.
- `Orchestrator` no longer owns direct subtask execution.
- DAG execution persists task, node, event, artifact, and graph-version state.
- HITL suspension and resume work through the DAG runtime.
- Replanning decisions are persisted and visible through API/UI.
- Final user responses remain result-first.
- Workflow-policy evidence is enforced before a task is marked completed.
- Full test suite passes after migration.
