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
        "claim": "Human approval through Web, webhook, QQ, WeCom, Feishu, and text commands.",
        "status": "implemented",
        "evidence": [
            "src/symbio/core/hitl_gateway.py",
            "src/symbio/core/hitl_notifier.py",
            "src/symbio/interfaces/api.py",
            "tests/test_hitl_notifier.py",
        ],
        "docs": ["README.md", "README_zh.md", "docs/features.md"],
        "next_step": "Add timeout escalation policies and richer per-channel delivery diagnostics.",
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
        "claim": "Marketplace browsing, search, and local install records for skill packages.",
        "status": "partial",
        "evidence": [
            "src/symbio/skills/marketplace.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_marketplace_api.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Connect remote/private registries, package signing, dependency install, sandboxed execution, and version compatibility checks.",
    },
    {
        "id": "mcp_gateway",
        "module": "tools",
        "claim": "Native MCP stdio JSON-RPC tool bridge and config discovery.",
        "status": "partial",
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
        "claim": "Directly attach to and control local Codex and Claude Code sessions.",
        "status": "implemented",
        "evidence": [
            "src/symbio/tools/external_agents.py",
            "src/symbio/tools/external_transcripts.py",
            "src/symbio/interfaces/api.py",
            "web/index.html",
            "web/app.js",
            "tests/test_external_agents.py",
            "tests/test_external_transcript_import.py",
        ],
        "docs": ["docs/feature-checklist.md", "docs/external-agent-control.md"],
        "next_step": "Add live streaming, cancellation, and richer MCP/environment injection per external session.",
    },
    {
        "id": "sandbox_cluster",
        "module": "tools",
        "claim": "Local sandbox plus Docker/K8s resource isolation path.",
        "status": "partial",
        "evidence": [
            "src/symbio/tools/sandbox.py",
            "src/symbio/tools/k8s_sandbox.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_sandbox_runtime.py",
        ],
        "docs": ["README.md", "docs/module-design-whitepaper.md"],
        "next_step": "Add persistent sandbox audit storage, OS/container-level network enforcement, and a production executor that creates, monitors, and destroys runtime pods.",
    },
    {
        "id": "observability_otel",
        "module": "observability",
        "claim": "OpenTelemetry trace, metrics, token heatmap, and trace visualization.",
        "status": "partial",
        "evidence": [
            "src/symbio/core/tracer.py",
            "web/app.js",
            "docker-compose.observability.yml",
            "config/otel/collector.yaml",
            "config/prometheus/prometheus.yml",
            "config/grafana/provisioning/datasources/datasource.yml",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Add Grafana dashboards JSON and Symbio metrics endpoint; wire OTLP exporter in production settings.",
    },
    {
        "id": "data_flywheel",
        "module": "evolution",
        "claim": "Trajectory capture, SOP distillation, dataset export, and fine-tuning loop.",
        "status": "partial",
        "evidence": [
            "src/symbio/evolution/sop_distiller.py",
            "src/symbio/evolution/dataset_exporter.py",
            "src/symbio/evolution/fine_tuner.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_evolution_api.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Replace training stubs with a real SFT/LoRA backend and add full eval run reports.",
    },
    {
        "id": "ray_actor_runtime",
        "module": "distributed",
        "claim": "Ray-native SubAgent actor dispatch with local asyncio fallback.",
        "status": "partial",
        "evidence": [
            "src/symbio/agents/subagent.py",
            "pyproject.toml",
        ],
        "docs": ["README.md", "docs/module-design-whitepaper.md"],
        "next_step": "Productize Ray actor submission, result collection, cancellation, and cluster diagnostics.",
    },
    {
        "id": "a2a_protocol",
        "module": "external_agents",
        "claim": "Agent-to-Agent protocol compatibility with external agent systems.",
        "status": "partial",
        "evidence": [
            "src/symbio/interfaces/a2a.py",
            "src/symbio/interfaces/api.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Add streaming A2A responses, push notification delivery, OAuth auth scheme, and full conformance test suite.",
    },
    {
        "id": "computer_use_loop",
        "module": "browser",
        "claim": "Computer Use loop: screenshot understanding, coordinate planning, GUI action, replay audit.",
        "status": "partial",
        "evidence": [
            "src/symbio/tools/computer_use.py",
            "src/symbio/tools/registry.py",
            "src/symbio/interfaces/api.py",
            "web/app.js",
            "tests/test_computer_use.py",
        ],
        "docs": ["README.md", "docs/features.md", "docs/feature-checklist.md"],
        "next_step": "Wire an LLM/vision planner into ActionPlanner, add coordinate grounding from screenshots, and harden multi-tab/session lifecycle.",
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
