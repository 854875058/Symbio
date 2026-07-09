"""Runtime-visible ledger for README and whitepaper capability claims."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Literal, TypedDict


CapabilityStatus = Literal["implemented", "partial", "missing"]


class CapabilityItem(TypedDict):
    id: str
    module: str
    claim: str
    status: CapabilityStatus
    evidence: list[str]
    docs: list[str]
    next_step: str


CAPABILITY_ITEMS: tuple[CapabilityItem, ...] = (
    {
        "id": "dynamic_dag",
        "module": "orchestration",
        "claim": "Dynamic DAG runtime with persisted graph state and re-planning.",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/dag_runtime.py",
            "src/symbio/core/dag_orchestrator.py",
            "src/symbio/core/replanner.py",
            "tests/test_dag_runtime.py",
        ],
        "docs": ["README.md", "docs/feature-checklist.md"],
        "next_step": "Keep expanding real-world graph mutation cases and UI trace evidence.",
    },
    {
        "id": "planner_reviewer_policy",
        "module": "workflow",
        "claim": "Plan first, review before risky execution, and verify before completion.",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/planner_reviewer.py",
            "src/symbio/core/workflow_policy.py",
            "docs/agent-workflow-policy.md",
        ],
        "docs": ["docs/agent-workflow-policy.md", "docs/feature-checklist.md"],
        "next_step": "Broaden policy evidence display to all long-running execution views.",
    },
    {
        "id": "hitl_im_approval",
        "module": "hitl",
        "claim": "Human approval via Web, webhook, QQ, WeCom, Feishu, text commands, and auto-push of approval cards to the logged-in personal WeChat (with re-push).",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/hitl_gateway.py",
            "src/symbio/core/hitl_notifier.py",
            "src/symbio/interfaces/api.py",
            "tests/test_hitl_notifier.py",
            "tests/test_hitl_wechat_approval.py",
            "tests/test_hitl_timeout_policy.py",
        ],
        "docs": ["README.md", "README_zh.md", "docs/features.md"],
        "next_step": "Add richer per-channel delivery diagnostics and multi-approver UX.",
    },
    {
        "id": "ontology_memory_graph",
        "module": "memory",
        "claim": "Ontology-backed memory graph with zero-token symbolic reasoning surface.",
        "status": "implemented",
        "evidence": [
            "src/symbio/memory/ontology.py",
            "src/symbio/memory/auto_populator.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
        ],
        "docs": ["README.md", "docs/module-design-whitepaper.md"],
        "next_step": "Add editing operations and relation provenance into the ontology UI.",
    },
    {
        "id": "model_routing_config",
        "module": "models",
        "claim": "Configurable model pool and task-to-model routing.",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/router.py",
            "src/symbio/config/settings.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
        ],
        "docs": ["README.md", "docs/module-design-whitepaper.md"],
        "next_step": "Add routing decision explanations to execution artifacts.",
    },
    {
        "id": "token_cost_optimization",
        "module": "cost",
        "claim": "Layered token cost control: semantic cache, context pruning, cost tracking, and budgets wired into the chat runtime.",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/chat_pipeline.py",
            "src/symbio/core/semantic_cache.py",
            "src/symbio/core/context_pruner.py",
            "src/symbio/core/cost_monitor.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_chat_pipeline.py",
        ],
        "docs": ["README.md", "docs/feature-checklist.md"],
        "next_step": "Add prompt-cache TTL keep-alive metrics and per-session routing-decision artifacts to the dashboard.",
    },
    {
        "id": "prompt_injection_defense",
        "module": "security",
        "claim": "Three-layer Prompt Injection firewall (sanitize / semantic detect / intent audit) enforced on chat input.",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/injection_guard.py",
            "src/symbio/security/chat_guard.py",
            "src/symbio/security/attack_samples.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_chat_guard.py",
        ],
        "docs": ["README.md", "docs/feature-checklist.md"],
        "next_step": "Add a semantic ML classifier layer and multi-turn context-aware detection beyond single-message signatures.",
    },
    {
        "id": "skills_marketplace",
        "module": "skills",
        "claim": "Browse/search skills, install with real on-disk materialization, and import real Agent Skills from GitHub repositories.",
        "status": "implemented",
        "evidence": [
            "src/symbio/skills/marketplace.py",
            "src/symbio/skills/remote_source.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_marketplace_api.py",
            "tests/test_remote_skill_source.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Add package signing, sandboxed execution, version compatibility checks, and private/authenticated registries.",
    },
    {
        "id": "mcp_gateway",
        "module": "tools",
        "claim": "Native MCP stdio JSON-RPC tool bridge and config discovery.",
        "status": "implemented",
        "evidence": [
            "src/symbio/tools/mcp.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_mcp_config.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Finish persistent connection pooling, resource/prompt protocols, and auth schemes.",
    },
    {
        "id": "external_agent_control",
        "module": "external_agents",
        "claim": "Attach to, live-sync (tail + resume), orchestrate (relay / tiled workbench), and drive a full interactive PTY terminal (claude-code / codex / shell, real TUI via winpty) for local Codex and Claude Code sessions.",
        "status": "implemented",
        "evidence": [
            "src/symbio/tools/external_agents.py",
            "src/symbio/tools/external_transcripts.py",
            "src/symbio/tools/external_live_session.py",
            "src/symbio/tools/external_relay.py",
            "src/symbio/tools/terminal_session.py",
            "src/symbio/interfaces/api.py",
            "web/index.html",
            "web/app.js",
            "tests/test_external_agents.py",
            "tests/test_external_live_session.py",
            "tests/test_external_relay.py",
            "tests/test_terminal_session.py",
        ],
        "docs": ["docs/feature-checklist.md", "docs/external-agent-control.md"],
        "next_step": "Terminal WS is localhost-only by default; add token auth for the remote-allow path, plus per-turn streaming/cancellation for the non-terminal (-p) call path.",
    },
    {
        "id": "agent_external_backend",
        "module": "agents",
        "claim": "A single Symbio agent can run on a Claude Code / Codex CLI backend.",
        "status": "implemented",
        "evidence": [
            "src/symbio/agents/builtin/external_backed_agent.py",
            "src/symbio/tools/external_agents.py",
            "tests/test_external_backed_agent.py",
        ],
        "docs": ["docs/feature-checklist.md", "docs/external-agent-control.md"],
        "next_step": "Wire external-backed agents into DAG node dispatch and stream their output.",
    },
    {
        "id": "wechat_bridge",
        "module": "interfaces",
        "claim": "Personal WeChat QR-login bot (built-in iLink) with session persistence, two-way chat, and HITL approval push/routing.",
        "status": "implemented",
        "evidence": [
            "src/symbio/interfaces/ilink_client.py",
            "src/symbio/interfaces/wechat_bridge.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_wechat_bridge.py",
            "tests/test_hitl_wechat_approval.py",
        ],
        "docs": ["README.md", "docs/feature-checklist.md"],
        "next_step": "Add media (image/voice/file) handling and group-chat policies on the iLink path.",
    },
    {
        "id": "sandbox_cluster",
        "module": "tools",
        "claim": "Workspace-bounded local sandbox plus real Docker container isolation (network-off + read-only root + mem/CPU limits, engine precheck, orphan-container cleanup, no host-env leak); K8s pod path is still a stub.",
        "status": "partial",
        "evidence": [
            "src/symbio/tools/sandbox.py",
            "src/symbio/tools/k8s_sandbox.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_sandbox_runtime.py",
            "tests/test_docker_sandbox.py",
        ],
        "docs": ["README.md", "docs/module-design-whitepaper.md"],
        "next_step": "Replace the k8s_sandbox.py stub with a real pod executor (create/monitor/destroy), persist sandbox audit records across restarts, and surface Docker container execution in the web UI.",
    },
    {
        "id": "observability_otel",
        "module": "observability",
        "claim": "OpenTelemetry trace, metrics, token heatmap, and trace visualization.",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/tracer.py",
            "web/app.js",
            "docker-compose.observability.yml",
            "config/otel/collector.yaml",
            "config/prometheus/prometheus.yml",
            "config/grafana/provisioning/datasources/datasource.yml",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Wire the OTLP exporter into production settings and add per-node latency SLO panels to the Grafana dashboard.",
    },
    {
        "id": "data_flywheel",
        "module": "evolution",
        "claim": "Trajectory capture, SOP distillation, dataset export, and a real LoRA SFT training backend (transformers+peft, real adapter weights and loss; stub fallback when deps/GPU absent), wired to a background-job API + web submit/monitor UI.",
        "status": "implemented",
        "evidence": [
            "src/symbio/evolution/sop_distiller.py",
            "src/symbio/evolution/dataset_exporter.py",
            "src/symbio/evolution/fine_tuner.py",
            "src/symbio/evolution/lora_trainer.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_evolution_api.py",
            "tests/test_lora_trainer.py",
            "tests/test_finetune_api.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Add full eval run reports and support larger base models / quantized (QLoRA) training.",
    },
    {
        "id": "ray_actor_runtime",
        "module": "distributed",
        "claim": "Real Ray Actor pool for cross-process SubAgent execution: submit / gather / cancel / shutdown, agents rebuilt in workers by name (no client serialization), wired into SubAgentManager behind a config flag; asyncio fallback when Ray is off/unavailable. Verified on a real local Ray cluster (tasks proven to run in distinct worker PIDs); multi-machine cluster deployment not yet validated.",
        "status": "implemented",
        "evidence": [
            "src/symbio/distributed/ray_runtime.py",
            "src/symbio/agents/subagent.py",
            "src/symbio/core/orchestrator.py",
            "src/symbio/config/settings.py",
            "tests/test_ray_runtime.py",
        ],
        "docs": ["README.md", "docs/module-design-whitepaper.md"],
        "next_step": "Validate a real multi-machine Ray cluster deployment and add per-actor load-aware scheduling beyond round-robin.",
    },
    {
        "id": "a2a_protocol",
        "module": "external_agents",
        "claim": "Agent-to-Agent protocol: dynamic AgentCard, inbound tasks routed through the full Orchestrator pipeline, outbound sessions with poll-based reply pull-back, a proven two-process cross-instance round-trip over real HTTP, SSE streaming task updates, webhook push notifications, and optional Bearer-token auth.",
        "status": "implemented",
        "evidence": [
            "src/symbio/interfaces/a2a.py",
            "src/symbio/interfaces/api.py",
            "tests/test_a2a_protocol.py",
            "tests/test_a2a_orchestrator_roundtrip.py",
            "tests/test_a2a_streaming_push_auth.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Upgrade Bearer auth to a full OAuth flow, add artifact (non-text) parts, and validate a cross-machine (not just cross-process) deployment.",
    },
    {
        "id": "computer_use_loop",
        "module": "browser",
        "claim": "Computer Use loop with VLM vision planning: the current screenshot's pixels are fed to Claude vision, which returns pixel-coordinate GUI actions (click x/y, type); three-tier fallback (vision -> text LLM -> heuristic) keeps the loop alive; full screenshot / action / replay audit. Real GUI-task success rate depends on the model.",
        "status": "implemented",
        "evidence": [
            "src/symbio/tools/computer_use.py",
            "src/symbio/tools/registry.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_computer_use.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Add screenshot-relative coordinate grounding aids (element boxes), harden multi-tab/session lifecycle, and add a self-verify step after each action.",
    },
    {
        "id": "multimodal_vision",
        "module": "memory",
        "claim": "Multi-modal memory: real image understanding via Claude vision wired into the ingestion pipeline (described images become searchable memories), auto-ingest of chat-attached images/PDFs, plus PDF/code structure extraction.",
        "status": "implemented",
        "evidence": [
            "src/symbio/memory/multimodal.py",
            "src/symbio/memory/manager.py",
            "src/symbio/interfaces/api.py",
            "tests/test_multimodal_vision.py",
            "tests/test_memory_multimodal_ingest.py",
        ],
        "docs": ["README.md", "docs/feature-checklist.md"],
        "next_step": "Persist cached image descriptions across restarts and add OCR for text-heavy images.",
    },
    {
        "id": "federated_privacy",
        "module": "platform",
        "claim": "Privacy computing, federated learning, and differential privacy for enterprise data.",
        "status": "missing",
        "evidence": [],
        "docs": ["docs/feature-checklist.md", "docs/roadmap.md"],
        "next_step": "Currently roadmap/whitepaper only; implement a federated aggregation path and DP noise injection before claiming support.",
    },
)


def get_capability_report() -> dict:
    """Return a public, UI-friendly summary of claimed vs implemented capabilities."""
    counts = Counter(item["status"] for item in CAPABILITY_ITEMS)
    items = [dict(item) for item in CAPABILITY_ITEMS]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(items),
            "implemented": counts["implemented"],
            "partial": counts["partial"],
            "missing": counts["missing"],
        },
        "items": items,
    }
