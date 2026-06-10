# Agent Workflow Policy

Symbio adopts several engineering practices from the Superpowers skill set as runtime policy, not just operator advice.

## Core Rules

1. **Plan before implementation**
   Multi-step feature work should produce a short plan before behavior changes. The plan must identify expected behavior, files or components affected, and verification.

2. **Ask when blocked by ambiguity**
   Agents should not silently invent missing requirements, credentials, production targets, or success criteria. If the answer materially changes behavior or risk, ask a concise question or suspend through HITL.

3. **TDD for behavior changes**
   Feature work, bug fixes, and refactors should start from a focused failing test or an explicitly documented verification gap. The preferred cycle is red, green, refactor.

4. **Root cause before fixes**
   For failures, agents must reproduce or characterize the issue, identify the likely root cause, then make one targeted fix. Multiple speculative fixes in one batch are discouraged.

5. **Evidence before completion**
   Agents must run or cite the verification command before claiming completion. Partial checks must be reported as partial.

6. **Spec and quality review**
   Larger tasks should be checked against the requested scope first, then reviewed for code quality. This prevents both under-building and unrequested behavior.

## Runtime Integration

`symbio.core.workflow_policy` converts an intent into a structured `WorkflowPolicy`.

The orchestrator attaches two metadata fields to every task:

- `workflow_policy`: machine-readable booleans and checklist items.
- `workflow_guidance`: prompt text for execution agents.

Execution agents can use this metadata to decide whether to plan, ask a clarifying question, follow TDD, perform root-cause analysis, or run verification before returning.

The DAG runtime treats `require_verification_before_completion` as a blocking completion gate. Feature and bug-fix intents must produce explicit verification evidence before the execution can be marked complete. Lightweight chat intents keep the policy advisory and do not require a verification artifact.

## Current Scope

Implemented:

- Intent-based workflow policy generation.
- Orchestrator task metadata injection.
- DAG runtime completion gating for feature and bug-fix verification.
- `submit_task` workflow-policy evidence enforcement.
- Deterministic planner/reviewer loop scaffolding for large plans, with structured plan/spec-review/quality-review results.
- Planner/reviewer loop integration in the orchestrator before HITL and DAG execution, with task metadata, state metadata, workflow checkpoints, and structured handoff persistence.
- GeneralAgent prompt injection.
- `StateManager` workflow-policy checkpoint persistence across planning, memory injection, HITL suspension/approval, and execution completion.
- `StateManager` structured agent handoff persistence for cross-agent artifact exchange.
- Web UI evidence panels for workflow policy, verification evidence, approval context, execution nodes, timeline events, and artifacts.
- Web UI planner/reviewer controls for status summary, blocking findings, plan, spec review, and quality review.
- Unit/integration tests for policy generation and prompt propagation.

Still pending:

- Real external planner/reviewer agent collaboration beyond the current deterministic gate.
