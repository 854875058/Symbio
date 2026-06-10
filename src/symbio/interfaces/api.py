"""Symbio FastAPI 服务端

使用 SQLite 持久化存储，集成 LLM 对话、模型管理、任务监控、
记忆管理、技能管理等完整 API。
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Any, Optional
import asyncio
import hashlib
import json
import os
import re
import inspect
import uuid
import time
import yaml
from enum import Enum
from importlib import resources
from pathlib import Path

from symbio.capabilities import get_capability_report
from symbio.interfaces.database import get_db, close_db
from symbio.memory.manager import MemoryManager
from symbio.core.execution_models import ExecutionStatus
from symbio.core.execution_state_store import ExecutionStateStore
from symbio.core.hitl_gateway import (
    ApprovalGateway,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    WebhookPayload,
    verify_approval_token,
)
from symbio.core.hitl_notifier import HITLNotifier, approval_short_code, parse_im_approval_command
from symbio.utils.logger import get_logger

logger = get_logger("api")

HITL_DB_PATH = str(Path("data") / "hitl.db")
EXECUTION_DB_PATH = str(Path("data") / "executions.db")


def _project_root() -> Path:
    return Path(__file__).parent.parent.parent.parent


def _get_web_dir() -> Path | None:
    source_web_dir = _project_root() / "web"
    if (source_web_dir / "index.html").exists():
        return source_web_dir

    try:
        packaged_web_dir = resources.files("symbio.interfaces.web").joinpath("static")
        if packaged_web_dir.joinpath("index.html").is_file():
            return Path(str(packaged_web_dir))
    except Exception:
        return None
    return None


def _get_default_eval_suite_dir() -> Path | None:
    source_suite_dir = _project_root() / "data" / "eval_suites"
    if (source_suite_dir / "smoke.json").exists():
        return source_suite_dir

    try:
        packaged_suite_dir = resources.files("symbio.data").joinpath("eval_suites")
        if packaged_suite_dir.joinpath("smoke.json").is_file():
            return Path(str(packaged_suite_dir))
    except Exception:
        return None
    return None

app = FastAPI(
    title="Symbio API",
    description="AI Infra 级多 Agent 协同框架",
    version="0.1.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_hitl_gateway() -> ApprovalGateway:
    return ApprovalGateway(persist_path=HITL_DB_PATH)


def _get_execution_store() -> ExecutionStateStore:
    store = getattr(app.state, "execution_store", None)
    if store is not None:
        return store

    orchestrator = getattr(app.state, "orchestrator", None)
    dag_orchestrator = getattr(orchestrator, "dag_orchestrator", None)
    store = getattr(dag_orchestrator, "store", None)
    if store is None:
        store = ExecutionStateStore(EXECUTION_DB_PATH)
    app.state.execution_store = store
    return store


def _get_sandbox_workspace_root() -> Path:
    configured = getattr(app.state, "sandbox_workspace_root", None)
    return Path(configured or Path.cwd()).resolve()


def _get_sandbox_executor():
    executor = getattr(app.state, "sandbox_executor", None)
    if executor is not None:
        return executor

    from symbio.tools.sandbox import PermissionLevel, SandboxExecutor

    workspace_root = _get_sandbox_workspace_root()
    executor = SandboxExecutor(
        default_timeout=60,
        default_permission=PermissionLevel.READ_ONLY,
        default_working_dir=str(workspace_root),
    )
    app.state.sandbox_executor = executor
    return executor


def _get_external_agent_controller():
    controller = getattr(app.state, "external_agent_controller", None)
    if controller is not None:
        return controller

    from symbio.tools.external_agents import ExternalAgentController

    controller = ExternalAgentController(
        state_path=Path("data") / "external_agents.json",
        workspace_root=_project_root(),
    )
    app.state.external_agent_controller = controller
    return controller


def _get_external_transcript_roots() -> dict[str, Path]:
    configured = getattr(app.state, "external_transcript_roots", None)
    if configured:
        return {key: Path(value).resolve() for key, value in configured.items()}

    from symbio.tools.external_transcripts import default_external_transcript_roots

    return {key: value.resolve() for key, value in default_external_transcript_roots().items()}


def _validate_external_transcript_path(provider: str, path: str) -> Path:
    from symbio.tools.external_agents import ExternalAgentSessionCreate

    normalized_provider = ExternalAgentSessionCreate(provider=provider).provider
    roots = _get_external_transcript_roots()
    root = roots.get(normalized_provider)
    if root is None:
        raise ValueError(f"Unsupported external transcript provider: {provider}")
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"Transcript path is outside {normalized_provider} root")
    if not resolved.exists() or resolved.suffix.lower() != ".jsonl":
        raise ValueError("Transcript file does not exist or is not JSONL")
    return resolved


def _build_sandbox_policy(access_mode: str, approval_policy: str):
    from symbio.tools.sandbox import (
        SandboxPolicy,
        normalize_approval_policy,
        normalize_sandbox_access_mode,
    )

    workspace_root = _get_sandbox_workspace_root()
    return SandboxPolicy(
        access_mode=normalize_sandbox_access_mode(access_mode),
        approval_policy=normalize_approval_policy(approval_policy),
        workspace_roots=[str(workspace_root)],
        writable_roots=[str(workspace_root)],
    )


# ============ 生命周期事件 ============

@app.on_event("startup")
async def startup():
    """启动时初始化数据库和记忆管理器"""
    await get_db()
    # 初始化 MemoryManager（语义搜索）
    try:
        memory_manager = MemoryManager()
        await memory_manager.initialize()
        app.state.memory_manager = memory_manager
        logger.info("MemoryManager 已初始化")
    except Exception as e:
        logger.warning(f"MemoryManager 初始化失败（将仅使用 SQLite）: {e}")
        app.state.memory_manager = None
    # 初始化 HITL 审批网关
    app.state.hitl_gateway = _build_hitl_gateway()
    app.state.hitl_notifier = HITLNotifier.from_settings()
    try:
        from symbio.core.orchestrator import Orchestrator

        orchestrator = Orchestrator()
        orchestrator.hitl_gateway = app.state.hitl_gateway
        orchestrator.hitl_notifier = app.state.hitl_notifier
        app.state.orchestrator = orchestrator
        app.state.execution_store = orchestrator.dag_orchestrator.store
        logger.info("HITL Orchestrator 已初始化")
    except Exception as e:
        app.state.orchestrator = None
        app.state.execution_store = ExecutionStateStore(EXECUTION_DB_PATH)
        logger.warning(f"HITL Orchestrator 初始化失败（审批 API 仍可用）: {e}")
    logger.info("HITL ApprovalGateway 已初始化")
    logger.info("Symbio API 已启动，数据库已连接")


@app.on_event("shutdown")
async def shutdown():
    """关闭时释放数据库连接和记忆管理器"""
    if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
        await app.state.memory_manager.close()
    if hasattr(app.state, "hitl_gateway") and app.state.hitl_gateway:
        await app.state.hitl_gateway.close()
    if hasattr(app.state, "execution_store") and app.state.execution_store:
        await app.state.execution_store.close()
    await close_db()
    logger.info("Symbio API 已关闭")


# ============ 数据模型 ============

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    model: Optional[str] = None


class ChatResponse(BaseModel):
    success: bool
    content: str
    session_id: str
    token_usage: Optional[dict] = None


class ModelCreate(BaseModel):
    model_id: str
    provider: str = "anthropic"
    display_name: str = ""
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    enabled: bool = True


class SkillImport(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0.0"
    source: str = "imported"
    enabled: bool = True
    trigger_keywords: list[str] = []


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    version: Optional[str] = None
    enabled: Optional[bool] = None
    trigger_keywords: Optional[list[str]] = None


class ConfigUpdate(BaseModel):
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    model_low: Optional[str] = None
    model_medium: Optional[str] = None
    model_high: Optional[str] = None


class DirImportRequest(BaseModel):
    path: str


class MemoryStoreRequest(BaseModel):
    content: str
    title: str = ""
    tags: list[str] = []
    importance: float = 0.5
    memory_type: str = "long_term"  # short_term, long_term, episodic, semantic, procedural


class ConversationExportRequest(BaseModel):
    format: str = "sharegpt"
    session_id: Optional[str] = None
    output_path: Optional[str] = None
    preview: bool = True
    limit: int = 50


class SandboxExecuteRequest(BaseModel):
    command: str
    permission_level: str = "read_only"
    working_dir: Optional[str] = None
    timeout: Optional[int] = None
    shell: bool = False
    access_mode: str = "workspace-write"
    approval_policy: str = "on-request"
    approved: bool = False


class ExternalAgentSessionCreateRequest(BaseModel):
    provider: str
    label: str = ""
    workspace: str = "."
    external_session_id: str = ""
    model: str = ""
    sandbox_mode: str = "workspace-write"
    approval_policy: str = "on-request"
    permission_mode: str = ""
    metadata: Optional[dict[str, Any]] = None


class ExternalAgentRunApiRequest(BaseModel):
    prompt: str
    dry_run: bool = False
    approved: bool = False
    timeout: int = 300
    model: str = ""
    sandbox_mode: str = ""
    approval_policy: str = ""
    permission_mode: str = ""


class ExternalTranscriptImportRequest(BaseModel):
    provider: str
    path: str
    title: str = ""


def _json_safe(value: Any) -> Any:
    """Convert internal ontology values to JSON-friendly primitives."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _get_ontology_engine():
    orchestrator = getattr(app.state, "orchestrator", None)
    memory_bridge = getattr(orchestrator, "memory_bridge", None)
    ontology = getattr(memory_bridge, "ontology", None)
    if ontology is not None:
        return ontology

    memory_bridge = getattr(app.state, "memory_bridge", None)
    ontology = getattr(memory_bridge, "ontology", None)
    if ontology is not None:
        return ontology

    return getattr(app.state, "ontology", None)


def _ontology_graph_snapshot(ontology) -> dict[str, Any]:
    concepts = getattr(ontology, "_concepts", {}) or {}
    individuals = getattr(ontology, "_individuals", {}) or {}
    relation_defs = getattr(ontology, "_relation_defs", {}) or {}
    relation_instances = getattr(ontology, "_relation_instances", {}) or {}
    properties = getattr(ontology, "_properties", {}) or {}

    def label_for(node_id: str) -> str:
        if node_id in concepts:
            return concepts[node_id].name
        if node_id in individuals:
            return individuals[node_id].name
        return node_id

    nodes: list[dict[str, Any]] = []
    degree_by_id: dict[str, int] = {}

    for concept_id, concept in concepts.items():
        parent_ids = [pid for pid in concept.parent_concepts if pid in concepts]
        property_ids = [pid for pid in concept.properties if pid in properties]
        nodes.append({
            "id": concept_id,
            "label": concept.name,
            "category": "concept",
            "entity_type": "concept",
            "description": concept.description,
            "parent_ids": parent_ids,
            "parent_labels": [concepts[pid].name for pid in parent_ids],
            "property_ids": property_ids,
            "property_labels": [properties[pid].name for pid in property_ids],
            "metadata": _json_safe(concept.metadata),
            "created_at": _json_safe(concept.created_at),
            "degree": 0,
        })

    for individual_id, individual in individuals.items():
        concept_ids = [cid for cid in individual.concept_ids if cid in concepts]
        nodes.append({
            "id": individual_id,
            "label": individual.name,
            "category": "individual",
            "entity_type": "individual",
            "concept_ids": concept_ids,
            "concept_labels": [concepts[cid].name for cid in concept_ids],
            "properties": _json_safe(individual.properties),
            "metadata": _json_safe(individual.metadata),
            "created_at": _json_safe(individual.created_at),
            "last_updated": _json_safe(individual.last_updated),
            "degree": 0,
        })

    edges: list[dict[str, Any]] = []
    for edge_id, relation in relation_instances.items():
        rel_def = relation_defs.get(relation.relation_id)
        relation_type = rel_def.relation_type.value if rel_def else "custom"
        label = rel_def.name if rel_def else relation_type
        source_id = relation.source_id
        target_id = relation.target_id
        degree_by_id[source_id] = degree_by_id.get(source_id, 0) + 1
        degree_by_id[target_id] = degree_by_id.get(target_id, 0) + 1
        edges.append({
            "id": edge_id,
            "source": source_id,
            "target": target_id,
            "source_label": label_for(source_id),
            "target_label": label_for(target_id),
            "label": label,
            "relation_id": relation.relation_id,
            "relation_type": relation_type,
            "weight": relation.weight,
            "metadata": _json_safe(relation.metadata),
            "created_at": _json_safe(relation.created_at),
        })

    for node in nodes:
        node["degree"] = degree_by_id.get(node["id"], 0)

    try:
        stats = ontology.get_statistics()
    except Exception as e:
        stats = {"error": str(e)}

    return {
        "stats": _json_safe(stats),
        "nodes": nodes,
        "edges": edges,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "node_categories": {
            "concept": len(concepts),
            "individual": len(individuals),
        },
    }


def _public_model_payload(model: dict[str, Any]) -> dict[str, Any]:
    payload = dict(model)
    api_key = payload.pop("api_key", "")
    payload["has_api_key"] = bool(api_key)
    return payload


def _format_conversation_export_sample(format_name: str, session_id: str, messages: list[dict]) -> dict:
    normalized = [
        {"role": str(message.get("role", "")), "content": str(message.get("content", ""))}
        for message in messages
        if message.get("role") in {"user", "assistant", "system"}
    ]
    fmt = format_name.lower()
    if fmt == "sharegpt":
        role_map = {"user": "human", "assistant": "gpt", "system": "system"}
        return {
            "id": session_id,
            "conversations": [
                {"role": role_map.get(message["role"], message["role"]), "content": message["content"]}
                for message in normalized
            ],
        }
    if fmt == "alpaca":
        first_user = next((message["content"] for message in normalized if message["role"] == "user"), "")
        last_assistant = next(
            (message["content"] for message in reversed(normalized) if message["role"] == "assistant"),
            "",
        )
        return {"id": session_id, "instruction": first_user, "input": "", "output": last_assistant}
    if fmt == "openai":
        return {"id": session_id, "messages": normalized}
    if fmt == "raw":
        return {"id": session_id, "messages": normalized}
    raise HTTPException(status_code=400, detail="Unsupported export format")


async def _collect_conversation_export_samples(request: ConversationExportRequest) -> list[dict]:
    db = await get_db()
    if request.session_id:
        session = await db.get_session(request.session_id)
        sessions = [session] if session else []
    else:
        sessions = await db.list_sessions()

    samples: list[dict] = []
    for session in sessions[: max(request.limit, 1)]:
        if not session:
            continue
        messages = await db.list_messages_by_session(session["id"])
        messages = [message for message in messages if message.get("role") in {"user", "assistant", "system"}]
        if len(messages) < 2:
            continue
        samples.append(_format_conversation_export_sample(request.format, session["id"], messages))
    return samples


def _marketplace_package_payload(package) -> dict[str, Any]:
    payload = package.model_dump(mode="json")
    payload["status"] = payload.get("status", "published")
    return payload


def _marketplace_install_record_payload(record) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _seed_skill_marketplace(marketplace) -> None:
    if marketplace.search(page_size=1).total > 0:
        return

    from symbio.skills.schema import SkillManifest

    packages = [
        (
            SkillManifest(
                name="code_review_plus",
                display_name="Code Review Plus",
                version="1.0.0",
                description="Review code for correctness, security, tests, and maintainability.",
                skill_type="tool",
                author="Symbio",
                license="MIT",
                tags=["code", "review", "security", "testing"],
                capabilities=["code_review", "security_check", "test_gap_analysis"],
                entry_point="symbio.skills.builtin.code_review:CodeReviewSkill",
            ),
            ["engineering", "quality"],
        ),
        (
            SkillManifest(
                name="dataset_exporter",
                display_name="Dataset Exporter",
                version="1.0.0",
                description="Prepare ShareGPT, Alpaca, OpenAI, and raw JSONL fine-tuning samples.",
                skill_type="workflow",
                author="Symbio",
                license="MIT",
                tags=["dataset", "export", "fine-tuning", "evolution"],
                capabilities=["dataset_export", "pii_masking", "quality_filter"],
                entry_point="symbio.evolution.dataset_exporter:DatasetExporter",
            ),
            ["evolution", "data"],
        ),
        (
            SkillManifest(
                name="hitl_connector",
                display_name="HITL Connector Pack",
                version="1.0.0",
                description="Approval connector helpers for Web, QQ, WeCom, Feishu, and IM callbacks.",
                skill_type="integration",
                author="Symbio",
                license="MIT",
                tags=["hitl", "approval", "qq", "wechat", "feishu"],
                capabilities=["human_approval", "im_callback", "risk_review"],
                entry_point="symbio.core.hitl_notifier:HITLNotifier",
            ),
            ["approval", "integration"],
        ),
    ]

    for manifest, categories in packages:
        marketplace.publish_package(manifest=manifest, categories=categories)


def _get_skill_marketplace():
    marketplace = getattr(app.state, "skill_marketplace", None)
    if marketplace is not None:
        return marketplace

    from symbio.skills.marketplace import SkillMarketplace

    storage_dir = getattr(app.state, "skill_marketplace_dir", None) or str(
        Path("data") / "skill_marketplace"
    )
    marketplace = SkillMarketplace(storage_dir=storage_dir)
    _seed_skill_marketplace(marketplace)
    app.state.skill_marketplace = marketplace
    return marketplace


async def _bootstrap_ontology_from_memories(ontology) -> None:
    """Populate an empty ontology from existing memories for first-render UX."""
    try:
        stats = ontology.get_statistics()
    except Exception:
        return

    if stats.get("tbox", {}).get("concepts", 0) or stats.get("abox", {}).get("individuals", 0):
        return

    db = await get_db()
    memories = await db.list_memories()
    texts: list[str] = []
    seen: set[str] = set()
    for memory in memories[:24]:
        parts = [str(memory.get("title") or "").strip(), str(memory.get("content") or "").strip()]
        text = " ".join(part for part in parts if part).strip()
        if text and text not in seen:
            seen.add(text)
            texts.append(text)

    if not texts:
        return

    try:
        from symbio.memory.auto_populator import AutoPopulator

        populator = AutoPopulator(ontology)
        for text in texts:
            await populator.populate_from_text(text, source="memory_bootstrap")
        logger.info(f"Bootstrapped ontology from {len(texts)} memories")
    except Exception as e:
        logger.warning(f"AutoPopulator bootstrap failed, falling back to entity extraction: {e}")
        orchestrator = getattr(app.state, "orchestrator", None)
        memory_bridge = getattr(orchestrator, "memory_bridge", None)
        if memory_bridge is None:
            return
        for text in texts:
            try:
                await memory_bridge.extract_and_store_entities(text, source="memory_bootstrap")
            except Exception as item_error:
                logger.debug(f"Bootstrap entity extraction skipped: {item_error}")


# ============ 辅助函数 ============

async def _ensure_session(db, session_id: str, title: str = "新对话"):
    """确保会话存在，不存在则创建"""
    existing = await db.get_session(session_id)
    if not existing:
        await db.create_session(session_id, title=title)
    return existing


def _skill_trigger_keywords(
    name: str,
    source: str = "",
    provided: Optional[list] = None,
) -> list[str]:
    """Return explicit keywords, or deterministic fallbacks for detected skills."""
    keywords = []

    def add(value) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text and text not in keywords:
            keywords.append(text)

    for keyword in provided or []:
        add(keyword)

    if keywords:
        return keywords

    add(name)
    for part in re.split(r"[-_\s.]+", name):
        if len(part) >= 2:
            add(part)
    add(source)
    return keywords


def _manifest_keywords(manifest: Optional[dict]) -> list:
    if isinstance(manifest, dict):
        value = manifest.get("trigger_keywords", [])
        return value if isinstance(value, list) else []
    return []


async def _load_llm_settings():
    """加载 LLM 配置（从 symbio.yaml）"""
    from symbio.config.settings import Settings
    config_path = Path("symbio.yaml")
    if config_path.exists():
        return Settings.from_yaml(config_path)
    return Settings()


# ============ 对话常量 ============

MAX_CONTEXT_MESSAGES = 20  # 对话历史最大条数，防止 token 溢出

SYMBIO_SYSTEM_PROMPT = (
    "你是 Symbio AI 助手，一个强大的多智能体协同框架。"
    "你善于分析问题、编写代码、调用工具来完成复杂任务。请用中文回复。"
)


async def _build_history_messages(db, session_id: str, max_messages: int = MAX_CONTEXT_MESSAGES):
    """从数据库获取会话历史，构建 Anthropic API 格式的消息列表"""
    history = await db.list_messages_by_session(session_id)
    # 只保留最近 N 条
    if len(history) > max_messages:
        history = history[-max_messages:]
    messages = []
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    return messages


# ============ API 路由 ============

@app.get("/")
async def root():
    """根路径"""
    return {
        "name": "Symbio",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/health")
@app.get("/api/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


@app.get("/api/capabilities")
async def capabilities():
    """Expose README/whitepaper claim implementation status."""
    return get_capability_report()


@app.get("/api/sandbox/policy")
async def sandbox_policy():
    """Expose the active local sandbox policy."""
    workspace_root = _get_sandbox_workspace_root()
    return {
        "access_mode": "workspace-write",
        "approval_policy": "on-request",
        "workspace_roots": [str(workspace_root)],
        "writable_roots": [str(workspace_root)],
        "allow_network": False,
    }


@app.post("/api/sandbox/execute")
async def sandbox_execute(request: SandboxExecuteRequest):
    """Run a command through the workspace-bounded sandbox policy."""
    from symbio.tools.sandbox import PermissionLevel

    try:
        policy = _build_sandbox_policy(request.access_mode, request.approval_policy)
        permission_level = PermissionLevel(request.permission_level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    executor = _get_sandbox_executor()
    result = await executor.execute_with_policy(
        command=request.command,
        policy=policy,
        permission_level=permission_level,
        timeout=request.timeout,
        working_dir=request.working_dir or str(_get_sandbox_workspace_root()),
        shell=request.shell,
        approved=request.approved,
    )
    return {
        "success": result.exit_code == 0,
        "approval_required": bool(result.metadata.get("approval_required")),
        "result": result.model_dump(mode="json"),
    }


@app.get("/api/sandbox/audit")
async def sandbox_audit(limit: int = 50):
    """Return recent sandbox execution decisions and results."""
    executor = _get_sandbox_executor()
    records = executor.audit_records[-max(limit, 1):]
    return {
        "records": [record.model_dump(mode="json") for record in reversed(records)],
        "total": len(executor.audit_records),
    }


@app.get("/api/external-agents/providers")
async def external_agent_providers():
    """List supported external coding-agent CLIs and discovery status."""
    controller = _get_external_agent_controller()
    return {
        "providers": [
            provider.model_dump(mode="json")
            for provider in controller.list_providers()
        ]
    }


@app.get("/api/external-agents/sessions")
async def external_agent_sessions():
    """List Symbio-managed external coding-agent sessions."""
    controller = _get_external_agent_controller()
    return {
        "sessions": [
            session.model_dump(mode="json")
            for session in controller.list_sessions()
        ],
        "total": len(controller.sessions),
    }


@app.post("/api/external-agents/sessions")
async def create_external_agent_session(request: ExternalAgentSessionCreateRequest):
    """Register an existing Codex/Claude Code session under Symbio control."""
    from symbio.tools.external_agents import ExternalAgentSessionCreate

    controller = _get_external_agent_controller()
    try:
        session = controller.create_session(
            ExternalAgentSessionCreate(
                provider=request.provider,
                label=request.label,
                workspace=request.workspace,
                external_session_id=request.external_session_id,
                model=request.model,
                sandbox_mode=request.sandbox_mode,
                approval_policy=request.approval_policy,
                permission_mode=request.permission_mode,
                metadata=request.metadata or {},
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "session": session.model_dump(mode="json")}


@app.post("/api/external-agents/sessions/{session_id}/run")
async def run_external_agent_session(session_id: str, request: ExternalAgentRunApiRequest):
    """Send a prompt to a registered external coding-agent session."""
    from symbio.tools.external_agents import ExternalAgentRunRequest

    controller = _get_external_agent_controller()
    try:
        result = await controller.run_session(
            session_id,
            ExternalAgentRunRequest(
                prompt=request.prompt,
                dry_run=request.dry_run,
                approved=request.approved,
                timeout=request.timeout,
                model=request.model,
                sandbox_mode=request.sandbox_mode,
                approval_policy=request.approval_policy,
                permission_mode=request.permission_mode,
            ),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="External agent session not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": result.success,
        "result": result.model_dump(mode="json"),
    }


@app.get("/api/external-agents/audit")
async def external_agent_audit(limit: int = 50):
    """Return recent external-agent command audit records."""
    controller = _get_external_agent_controller()
    records = controller.list_audit(limit=limit)
    return {
        "records": [record.model_dump(mode="json") for record in records],
        "total": len(controller.audit),
    }


@app.get("/api/external-agents/transcripts")
async def external_agent_transcripts(limit: int = 50):
    """List importable local Codex and Claude Code transcript summaries."""
    from symbio.tools.external_transcripts import discover_external_transcripts

    roots = _get_external_transcript_roots()
    summaries = discover_external_transcripts(
        codex_root=roots.get("codex"),
        claude_root=roots.get("claude-code"),
        limit=limit,
    )
    return {
        "transcripts": [summary.model_dump(mode="json") for summary in summaries],
        "total": len(summaries),
    }


@app.post("/api/external-agents/transcripts/import")
async def import_external_agent_transcript(request: ExternalTranscriptImportRequest):
    """Import a local Codex/Claude Code transcript into Symbio chat history."""
    from symbio.tools.external_agents import ExternalAgentSessionCreate
    from symbio.tools.external_transcripts import parse_external_transcript

    provider = ExternalAgentSessionCreate(provider=request.provider).provider
    try:
        path = _validate_external_transcript_path(provider, request.path)
        transcript = parse_external_transcript(path, provider=provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Transcript file not found")

    if not transcript.messages:
        raise HTTPException(status_code=400, detail="Transcript has no importable chat messages")

    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    session_id = f"external-{provider}-{digest}"
    title = request.title.strip() or f"{provider}: {transcript.title}"
    db = await get_db()
    if await db.get_session(session_id):
        await db.delete_session(session_id)
    session = await db.create_session(
        session_id,
        title=title,
        created_at=transcript.created_at or None,
        updated_at=transcript.updated_at or None,
        message_count=0,
    )
    for index, message in enumerate(transcript.messages):
        message_digest = hashlib.sha256(f"{path}:{index}:{message.role}".encode("utf-8")).hexdigest()[:16]
        await db.create_message(
            f"external-{message_digest}",
            session_id,
            message.role,
            message.content,
            message.timestamp or None,
            0,
        )
    session = await db.get_session(session_id) or session
    return {
        "success": True,
        "session": session,
        "imported_messages": len(transcript.messages),
        "transcript": transcript.summary().model_dump(mode="json"),
    }


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _observability_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return _json_safe(value)


async def _observability_summary() -> dict[str, Any]:
    tracer = getattr(app.state, "tracer", None)
    if tracer is None:
        try:
            from symbio.core.tracer import get_tracer

            tracer = get_tracer()
        except Exception:
            tracer = None

    if tracer is None:
        return {
            "enabled": False,
            "is_started": False,
            "service_name": "",
            "exporter": "",
            "spans": {"captured": 0},
            "metrics": {"records": 0},
            "tokens": {"total_tokens": 0, "entries": 0},
        }

    config = getattr(tracer, "config", None)
    if callable(config):
        config = config()
    is_started = getattr(tracer, "is_started", None)
    is_started = bool(is_started() if callable(is_started) else is_started)

    spans = []
    get_spans = getattr(tracer, "get_captured_spans", None)
    if callable(get_spans):
        spans = await _maybe_await(get_spans())

    metrics = []
    get_metrics = getattr(tracer, "get_metric_records", None)
    if callable(get_metrics):
        metrics = get_metrics()

    token_heatmap: Any = {}
    get_heatmap = getattr(tracer, "get_token_heatmap", None)
    if callable(get_heatmap):
        token_heatmap = await _maybe_await(get_heatmap())
    token_heatmap = _observability_value(token_heatmap) or {}
    token_entries = token_heatmap.get("entries", []) if isinstance(token_heatmap, dict) else []

    return {
        "enabled": True,
        "is_started": is_started,
        "service_name": getattr(config, "service_name", "") if config is not None else "",
        "exporter": _json_safe(getattr(config, "exporter", "")) if config is not None else "",
        "spans": {"captured": len(spans or [])},
        "metrics": {"records": len(metrics or [])},
        "tokens": {
            "total_tokens": token_heatmap.get("total_tokens", 0) if isinstance(token_heatmap, dict) else 0,
            "entries": len(token_entries or []),
        },
    }


@app.get("/api/observability/summary")
async def observability_summary():
    """Expose a minimal runtime observability summary for the Web UI."""
    return await _observability_summary()


@app.post("/api/export/conversations")
async def export_conversations(request: ConversationExportRequest):
    """Export persisted conversations as fine-tuning ready samples."""
    samples = await _collect_conversation_export_samples(request)
    output_path = request.output_path
    written = False

    if not request.preview:
        path = Path(output_path or f"data/exports/symbio_{request.format}_{int(time.time())}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for sample in samples:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        output_path = str(path)
        written = True

    return {
        "format": request.format.lower(),
        "sample_count": len(samples),
        "written": written,
        "output_path": output_path,
        "samples": samples[: min(len(samples), 10)],
    }


@app.get("/api/evaluation/suites")
async def list_evaluation_suites(path: str = "data/eval_suites"):
    """List local evaluation suite JSON files for the Web UI."""
    suite_dir = Path(path)
    if path == "data/eval_suites" and not suite_dir.exists():
        default_suite_dir = _get_default_eval_suite_dir()
        if default_suite_dir is not None:
            suite_dir = default_suite_dir
    if not suite_dir.exists():
        return {"suites": [], "total": 0, "path": str(suite_dir)}
    if not suite_dir.is_dir():
        raise HTTPException(status_code=400, detail="Evaluation path must be a directory")

    try:
        from symbio.evolution.eval_pipeline import TestSuiteLoader
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Evaluation pipeline unavailable: {e}")

    suites = []
    errors = []
    for file_path in sorted(suite_dir.glob("*.json")):
        try:
            suite = TestSuiteLoader.load_from_file(file_path)
            suites.append({
                "name": suite.name,
                "description": suite.description,
                "version": suite.version,
                "case_count": len(suite.cases),
                "path": str(file_path),
                "suite_id": suite.suite_id,
                "tags": sorted({tag for case in suite.cases for tag in case.tags}),
            })
        except Exception as e:
            errors.append({"path": str(file_path), "error": str(e)})

    return {"suites": suites, "total": len(suites), "path": str(suite_dir), "errors": errors}


# ============ 对话 API ============

@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """对话接口 - 调用真实 LLM，同时持久化消息到数据库"""
    db = await get_db()
    session_id = request.session_id or "default"
    now_str = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 确保会话存在
    await _ensure_session(db, session_id, title=request.message[:30] if request.message else "新对话")

    # 保存用户消息
    user_msg_id = f"msg-{uuid.uuid4().hex[:12]}"
    await db.create_message(user_msg_id, session_id, "user", request.message, now_str, 0)

    # 自动存入 MemoryManager（语义搜索）
    if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
        await app.state.memory_manager.add_conversation_turn("user", request.message, session_id)

    # 更新会话标题（如果是新会话的第一条消息）
    session = await db.get_session(session_id)
    if session and session["title"] == "新对话":
        await db.update_session(session_id, title=request.message[:30])

    try:
        import anthropic

        settings = await _load_llm_settings()
        api_key = settings.model.anthropic_api_key
        base_url = settings.model.anthropic_base_url

        if not api_key:
            error_msg = "错误: 未配置 API Key，请在 Models 页面配置 LLM 或编辑 symbio.yaml"
            await db.create_message(f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant", error_msg, time.strftime("%Y-%m-%dT%H:%M:%S"), 0)
            return ChatResponse(success=False, content=error_msg, session_id=session_id)

        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        model = request.model or settings.model.model_medium

        # 构建含历史对话的消息列表
        messages = await _build_history_messages(db, session_id)
        logger.info(f"HTTP 对话 - 会话: {session_id}, 历史消息数: {len(messages)}")

        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYMBIO_SYSTEM_PROMPT,
            messages=messages,
        )

        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        token_usage = {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
            "total": response.usage.input_tokens + response.usage.output_tokens,
        }

        # 保存 AI 回复
        await db.create_message(
            f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant",
            content, time.strftime("%Y-%m-%dT%H:%M:%S"), token_usage["total"],
        )

        # 自动存入 MemoryManager（语义搜索）
        if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
            await app.state.memory_manager.add_conversation_turn("assistant", content, session_id)

        return ChatResponse(success=True, content=content, session_id=session_id, token_usage=token_usage)

    except Exception as e:
        logger.error(f"对话失败: {e}")
        error_content = f"错误: {str(e)}"
        await db.create_message(
            f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant",
            error_content, time.strftime("%Y-%m-%dT%H:%M:%S"), 0,
        )
        return ChatResponse(success=False, content=error_content, session_id=session_id)


# ============ 会话 API ============

@app.get("/api/sessions")
async def list_sessions():
    """返回会话列表，按更新时间倒序"""
    db = await get_db()
    sessions = await db.list_sessions()
    return {"sessions": sessions, "total": len(sessions)}


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """返回指定会话的消息历史"""
    db = await get_db()
    session = await db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = await db.list_messages_by_session(session_id)
    return {"messages": msgs, "total": len(msgs), "session_id": session_id}


# ============ 任务 API ============

@app.get("/api/tasks")
async def list_tasks(status: Optional[str] = None):
    """任务列表，支持状态过滤"""
    db = await get_db()
    tasks = await db.list_tasks(status=status)
    return {"tasks": tasks, "total": len(tasks)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """获取任务详情"""
    db = await get_db()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task}


@app.get("/api/tasks/{task_id}/dag")
async def get_task_dag(task_id: str):
    """Return a DAG-friendly task graph for UI and integrations."""
    db = await get_db()
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    nodes = [{
        "id": task["id"],
        "label": task["name"],
        "type": "task",
        "status": task["status"],
        "agent": task.get("agent", ""),
        "metadata": {
            "created_at": task.get("created_at"),
            "completed_at": task.get("completed_at"),
        },
    }]
    edges = []

    previous_id = task["id"]
    for index, step in enumerate(task.get("steps", []), start=1):
        node_id = f"{task['id']}:step:{step['id']}"
        nodes.append({
            "id": node_id,
            "label": step["name"],
            "type": "step",
            "status": step["status"],
            "metadata": {
                "step_id": step["id"],
                "duration": step.get("duration"),
                "order": index,
            },
        })
        edges.append({
            "id": f"{previous_id}->{node_id}",
            "source": previous_id,
            "target": node_id,
            "type": "sequence",
        })
        previous_id = node_id

    return {
        "task_id": task_id,
        "nodes": nodes,
        "edges": edges,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
    }


# ============ 模型 API ============

@app.get("/api/executions/{execution_id}")
async def get_execution_detail(execution_id: str):
    """Return persisted execution detail, nodes, and graph history."""
    store = _get_execution_store()
    execution = await store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution record not found")

    nodes = await store.list_nodes(execution_id)
    graph_versions = await store.list_graph_versions(execution_id)
    return {
        "execution": execution.model_dump(mode="json"),
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "graph_versions": [version.model_dump(mode="json") for version in graph_versions],
    }


@app.get("/api/executions/{execution_id}/events")
async def get_execution_events(execution_id: str):
    """Return ordered execution events."""
    store = _get_execution_store()
    execution = await store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution record not found")

    events = await store.list_events(execution_id)
    return {
        "execution_id": execution_id,
        "events": [event.model_dump(mode="json") for event in events],
        "total": len(events),
    }


@app.get("/api/executions/{execution_id}/artifacts")
async def get_execution_artifacts(execution_id: str):
    """Return artifacts emitted by an execution."""
    store = _get_execution_store()
    execution = await store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution record not found")

    artifacts = await store.list_artifacts(execution_id)
    return {
        "execution_id": execution_id,
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        "total": len(artifacts),
    }


@app.post("/api/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str):
    """Cancel an execution."""
    store = _get_execution_store()
    execution = await store.get_execution(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution record not found")

    if execution.status != ExecutionStatus.CANCELLED:
        execution = await store.update_execution_status(
            execution_id, ExecutionStatus.CANCELLED
        )
    return {"execution": execution.model_dump(mode="json")}


@app.get("/api/models")
async def list_models():
    """模型列表"""
    db = await get_db()
    models = await db.list_models()
    return {"models": [_public_model_payload(model) for model in models]}


@app.post("/api/models")
async def create_model(model: ModelCreate):
    """添加模型"""
    db = await get_db()
    new_model = await db.create_model(
        model_id=model.model_id,
        provider=model.provider,
        display_name=model.display_name,
        api_key=model.api_key,
        base_url=model.base_url,
        enabled=model.enabled,
    )
    return {"model": _public_model_payload(new_model)}


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str):
    """删除模型"""
    db = await get_db()
    deleted = await db.delete_model(model_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="模型不存在")
    return {"success": True}


@app.post("/api/models/{model_id}/test")
async def test_model(model_id: str):
    """测试模型连接"""
    db = await get_db()
    target = await db.get_model(model_id)
    if not target:
        raise HTTPException(status_code=404, detail="模型不存在")

    api_key = target.get("api_key") or ""
    base_url = target.get("base_url", "https://api.anthropic.com")
    provider = target.get("provider", "anthropic")
    settings = await _load_llm_settings()
    configured_models = {
        getattr(settings.model, "model_low", ""),
        getattr(settings.model, "model_medium", ""),
        getattr(settings.model, "model_high", ""),
    }
    if target.get("model_id") in configured_models:
        if provider == "anthropic":
            api_key = settings.model.anthropic_api_key or api_key
            base_url = settings.model.anthropic_base_url or base_url
        elif provider == "openai":
            api_key = settings.model.openai_api_key or api_key
            base_url = settings.model.openai_base_url or base_url

    if not api_key:
        return {"success": False, "message": "未配置 API Key，无法测试连接"}

    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
            resp = await client.messages.create(
                model=target["model_id"],
                max_tokens=16,
                messages=[{"role": "user", "content": "Hi"}],
            )
            return {"success": True, "message": f"连接成功 (tokens: {resp.usage.input_tokens}+{resp.usage.output_tokens})"}
        else:
            import httpx
            async with httpx.AsyncClient(timeout=15) as http_client:
                resp = await http_client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": target["model_id"], "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 16},
                )
                if resp.status_code == 200:
                    return {"success": True, "message": "连接成功，模型响应正常"}
                else:
                    return {"success": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)}"}


# ============ 记忆 API ============

@app.get("/api/memory")
async def list_memories():
    """记忆列表（SQLite 持久化 + MemoryManager 统计）"""
    db = await get_db()
    memories = await db.list_memories()

    # 附加 MemoryManager 统计信息
    stats = None
    if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
        mm_stats = app.state.memory_manager.get_stats()
        stats = mm_stats.model_dump()

    return {"memories": memories, "total": len(memories), "stats": stats}


@app.get("/api/memory/search")
async def search_memories(q: str = Query("", description="搜索关键词")):
    """搜索记忆（语义搜索 + 关键词回退）"""
    db = await get_db()
    if not q:
        memories = await db.list_memories()
        return {"memories": memories, "query": q, "search_type": "keyword"}

    # 尝试语义搜索
    semantic_results = []
    if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
        try:
            search_results = await app.state.memory_manager.search(q)
            semantic_results = [
                {
                    "memory_id": r.memory.memory_id,
                    "content": r.memory.content,
                    "memory_type": r.memory.memory_type.value,
                    "importance": r.memory.importance,
                    "tags": r.memory.tags,
                    "score": r.score,
                    "match_type": r.match_type,
                    "created_at": r.memory.created_at.isoformat() if r.memory.created_at else None,
                }
                for r in search_results
            ]
        except Exception as e:
            logger.warning(f"语义搜索失败，回退到关键词搜索: {e}")

    # SQLite 关键词搜索
    keyword_results = await db.search_memories(q)

    # 合并结果，按内容去重
    if semantic_results:
        seen_contents = {r["content"].strip() for r in semantic_results}
        merged = list(semantic_results)
        for kr in keyword_results:
            if kr.get("content", "").strip() not in seen_contents:
                merged.append(kr)
                seen_contents.add(kr.get("content", "").strip())
        search_type = "hybrid" if keyword_results else "semantic"
        return {"memories": merged, "query": q, "search_type": search_type}

    return {"memories": keyword_results, "query": q, "search_type": "keyword"}


@app.post("/api/memory/store")
async def store_memory(req: MemoryStoreRequest):
    """手动存储记忆（同时写入 SQLite 和 MemoryManager）"""
    db = await get_db()
    memory_id = f"mem-{uuid.uuid4().hex[:12]}"
    now_str = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 写入 SQLite（持久化）
    await db.create_memory(
        memory_id=memory_id,
        content=req.content,
        title=req.title or req.content[:30],
        tags=req.tags,
        importance=req.importance,
    )

    # 写入 MemoryManager（语义搜索）
    memory_item = None
    if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
        from symbio.memory.manager import MemoryType
        mt = MemoryType(req.memory_type) if req.memory_type in [e.value for e in MemoryType] else MemoryType.LONG_TERM
        memory_item = await app.state.memory_manager.add_memory(
            content=req.content,
            memory_type=mt,
            tags=req.tags,
            importance=req.importance,
            source="manual",
        )

    return {
        "success": True,
        "memory_id": memory_id,
        "semantic_id": memory_item.memory_id if memory_item else None,
    }


@app.post("/api/memory/consolidate")
async def consolidate_memories():
    """触发记忆巩固（将重要短期记忆转为长期记忆）"""
    if not hasattr(app.state, 'memory_manager') or not app.state.memory_manager:
        raise HTTPException(status_code=503, detail="MemoryManager 未初始化")

    consolidated = await app.state.memory_manager.consolidate()
    return {"success": True, "consolidated": consolidated}


@app.get("/api/memory/stats")
async def memory_stats():
    """记忆系统统计信息"""
    db = await get_db()
    # SQLite 统计
    memories = await db.list_memories()
    sqlite_total = len(memories)

    # MemoryManager 统计
    mm_stats = None
    if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
        mm_stats = app.state.memory_manager.get_stats().model_dump()

    return {
        "sqlite": {"total": sqlite_total},
        "memory_manager": mm_stats,
    }


@app.get("/api/ontology")
async def ontology_graph():
    """Return a frontend-ready ontology graph snapshot."""
    orchestrator = getattr(app.state, "orchestrator", None)
    memory_bridge = getattr(orchestrator, "memory_bridge", None)
    if memory_bridge is not None and not getattr(memory_bridge, "_initialized", False):
        try:
            await memory_bridge.initialize()
        except Exception as e:
            logger.warning(f"Ontology graph initialization failed: {e}")

    ontology = _get_ontology_engine()
    if ontology is None:
        return {
            "stats": {
                "tbox": {"concepts": 0, "properties": 0, "relation_definitions": 0},
                "abox": {"individuals": 0, "relation_instances": 0},
            },
            "nodes": [],
            "edges": [],
            "total_nodes": 0,
            "total_edges": 0,
            "node_categories": {"concept": 0, "individual": 0},
        }

    await _bootstrap_ontology_from_memories(ontology)
    return _ontology_graph_snapshot(ontology)


# ============ Skills API ============

@app.get("/api/skills")
async def list_skills(include_detected: bool = Query(False, description="Include non-imported local skills")):
    """返回 Skills 列表（数据库 + 本地扫描）"""
    db = await get_db()
    skills = await db.list_skills()

    # 合并本地扫描的真实 Skill
    if include_detected:
        db_names = {s["name"] for s in skills}
        for real in _scan_real_skills():
            if real["name"] not in db_names:
                skills.append(real)

    return {"skills": skills, "total": len(skills)}


@app.get("/api/skills/search")
async def search_skills(q: str = Query("", description="搜索关键词")):
    """搜索 Skills"""
    db = await get_db()
    if not q:
        skills = await db.list_skills()
        return {"skills": skills, "query": q}
    results = await db.search_skills(q)
    return {"skills": results, "query": q}


@app.post("/api/skills/import")
async def import_skill(skill: SkillImport):
    """导入一个新的 Skill"""
    db = await get_db()
    # 检查是否已存在同名 skill
    existing = await db.search_skills(skill.name)
    for sk in existing:
        if sk["name"] == skill.name:
            raise HTTPException(status_code=400, detail=f"Skill '{skill.name}' 已存在")

    new_skill = await db.create_skill(
        skill_id=f"sk-{uuid.uuid4().hex[:8]}",
        name=skill.name,
        description=skill.description,
        version=skill.version,
        source=skill.source,
        enabled=skill.enabled,
        trigger_keywords=_skill_trigger_keywords(skill.name, skill.source, skill.trigger_keywords),
    )
    return {"skill": new_skill}


@app.get("/api/skills/marketplace")
async def list_skill_marketplace(
    q: str = Query("", description="Search marketplace packages"),
    category: str = Query("", description="Filter by category"),
):
    marketplace = _get_skill_marketplace()
    categories = [category] if category else None
    result = marketplace.search(query=q, categories=categories, page_size=50)
    stats = marketplace.get_statistics()
    return {
        "packages": [_marketplace_package_payload(package) for package in result.packages],
        "total": result.total,
        "has_more": result.has_more,
        "stats": stats.model_dump(mode="json"),
        "categories": marketplace.get_categories(),
        "popular_tags": marketplace.get_popular_tags(),
        "installed": [
            _marketplace_install_record_payload(record)
            for record in marketplace.list_installed()
        ],
    }


@app.post("/api/skills/marketplace/{package_id}/install")
async def install_marketplace_skill(package_id: str):
    marketplace = _get_skill_marketplace()
    package = marketplace.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Marketplace package not found")
    record = marketplace.install(
        package_id,
        install_dir=Path("data") / "skills" / "marketplace_installed" / package.name,
    )
    return {"success": record.status == "installed", "record": _marketplace_install_record_payload(record)}


@app.put("/api/skills/{skill_id}")
async def update_skill(skill_id: str, update: SkillUpdate):
    """更新 Skill"""
    db = await get_db()
    kwargs = {}
    if update.name is not None:
        kwargs["name"] = update.name
    if update.description is not None:
        kwargs["description"] = update.description
    if update.version is not None:
        kwargs["version"] = update.version
    if update.enabled is not None:
        kwargs["enabled"] = update.enabled
    if update.trigger_keywords is not None:
        kwargs["trigger_keywords"] = update.trigger_keywords

    updated = await db.update_skill(skill_id, **kwargs)
    if not updated:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"skill": updated}


@app.delete("/api/skills/{skill_id}")
async def delete_skill(skill_id: str):
    """删除 Skill"""
    db = await get_db()
    deleted = await db.delete_skill(skill_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"success": True}


@app.post("/api/skills/auto-detect")
async def auto_detect_skills():
    """自动检测已安装的 Skills（Claude Code、Codex 等）"""
    import os
    db = await get_db()

    found = 0
    detected = []

    # 检测 Claude Code skills
    cc_skill_dirs = [
        os.path.expanduser("~/.claude/skills"),
        os.path.expanduser("~/.claude/commands"),
    ]
    for dir_path in cc_skill_dirs:
        if os.path.isdir(dir_path):
            for item in os.listdir(dir_path):
                item_path = os.path.join(dir_path, item)
                if os.path.isdir(item_path) or item.endswith(('.md', '.yaml', '.json')):
                    name = item.replace('.md', '').replace('.yaml', '').replace('.json', '')
                    existing = await db.search_skills(name)
                    if not any(s["name"] == name for s in existing):
                        await db.create_skill(
                            skill_id=f"sk-{uuid.uuid4().hex[:8]}",
                            name=name,
                            description=f"Auto-detected from Claude Code: {dir_path}",
                            source="claude-code",
                            trigger_keywords=_skill_trigger_keywords(name, "claude-code"),
                        )
                        found += 1
                        detected.append(name)

    # 检测 Codex / OpenAI tools
    codex_config = os.path.expanduser("~/.codex/config.json")
    if os.path.exists(codex_config):
        try:
            with open(codex_config) as f:
                config = json.load(f)
            for tool in config.get("tools", []):
                name = tool.get("name", "")
                if name:
                    existing = await db.search_skills(name)
                    if not any(s["name"] == name for s in existing):
                        await db.create_skill(
                            skill_id=f"sk-{uuid.uuid4().hex[:8]}",
                            name=name,
                            description=tool.get("description", "Auto-detected from Codex"),
                            source="codex",
                            trigger_keywords=_skill_trigger_keywords(
                                name,
                                "codex",
                                tool.get("trigger_keywords") if isinstance(tool.get("trigger_keywords"), list) else [],
                            ),
                        )
                        found += 1
                        detected.append(name)
        except Exception:
            pass

    return {"found": found, "detected": detected}


@app.post("/api/skills/import-dir")
async def import_skills_from_dir(req: DirImportRequest):
    """从目录批量导入 Skills"""
    import os
    db = await get_db()

    dir_path = req.path
    if not os.path.isdir(dir_path):
        raise HTTPException(status_code=400, detail=f"目录不存在: {dir_path}")

    imported = 0
    for item in os.listdir(dir_path):
        item_path = os.path.join(dir_path, item)
        if os.path.isdir(item_path):
            manifest = None
            for f in ["skill.yaml", "skill.json", "manifest.json", "manifest.yaml"]:
                fp = os.path.join(item_path, f)
                if os.path.exists(fp):
                    manifest = fp
                    break
            if manifest:
                try:
                    with open(manifest) as f:
                        data = json.load(f) if manifest.endswith('.json') else yaml.safe_load(f)
                    name = data.get("name", item)
                    existing = await db.search_skills(name)
                    if not any(s["name"] == name for s in existing):
                        await db.create_skill(
                            skill_id=f"sk-{uuid.uuid4().hex[:8]}",
                            name=name,
                            description=data.get("description", f"Imported from {dir_path}"),
                            version=data.get("version", "1.0.0"),
                            source="imported",
                            trigger_keywords=_skill_trigger_keywords(
                                name,
                                "imported",
                                data.get("trigger_keywords", []) if isinstance(data.get("trigger_keywords", []), list) else [],
                            ),
                        )
                        imported += 1
                except Exception:
                    pass
        elif item.endswith(('.md', '.yaml', '.json')):
            name = item.replace('.md', '').replace('.yaml', '').replace('.json', '')
            existing = await db.search_skills(name)
            if not any(s["name"] == name for s in existing):
                await db.create_skill(
                    skill_id=f"sk-{uuid.uuid4().hex[:8]}",
                    name=name,
                    description=f"Imported from {dir_path}/{item}",
                    source="imported",
                    trigger_keywords=_skill_trigger_keywords(name, "imported"),
                )
                imported += 1

    return {"imported": imported}


def _find_skill_directory(skill_name: str) -> Optional[Path]:
    """查找 Skill 目录（精确匹配 + 模糊匹配）"""
    search_dirs = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".claude" / "commands",
        Path("skills"),
        Path("src/symbio/skills/builtin"),
    ]
    # 精确匹配
    for base in search_dirs:
        p = base / skill_name
        if p.is_dir():
            return p

    # 模糊匹配（忽略大小写、连字符/下划线）
    normalized = skill_name.lower().replace("-", "").replace("_", "")
    for base in search_dirs:
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if d.is_dir():
                d_normalized = d.name.lower().replace("-", "").replace("_", "")
                if d_normalized == normalized:
                    return d

    return None


def _scan_real_skills() -> list[dict]:
    """扫描 ~/.claude/skills/ 目录下的真实 Skill"""
    skills = []
    skills_dir = Path.home() / ".claude" / "skills"
    if not skills_dir.is_dir():
        return skills

    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue

        # 读取 SKILL.md 获取描述
        description = ""
        readme_content = _read_skill_readme(d)
        if readme_content:
            # 提取第一行作为描述
            lines = [l.strip() for l in readme_content.split("\n") if l.strip() and not l.startswith("#")]
            if lines:
                description = lines[0][:200]

        # 读取 manifest
        manifest = _read_skill_manifest(d)

        skills.append({
            "id": f"fs-{d.name}",
            "name": d.name,
            "description": description or f"本地 Skill: {d.name}",
            "version": manifest.get("version", "1.0.0") if manifest else "1.0.0",
            "source": "local",
            "enabled": True,
            "trigger_keywords": _skill_trigger_keywords(d.name, "local", _manifest_keywords(manifest)),
            "created_at": "",
            "_directory": str(d),
        })

    return skills


def _list_skill_files(skill_dir: Path) -> list[dict]:
    """列出 Skill 目录下的所有文件"""
    files = []
    try:
        for item in sorted(skill_dir.rglob("*")):
            if item.is_file() and not item.name.startswith("."):
                rel = item.relative_to(skill_dir)
                files.append({
                    "name": str(rel),
                    "path": str(item),
                    "size": item.stat().st_size,
                    "type": _get_file_type(item.suffix),
                })
    except Exception:
        pass
    return files


def _get_file_type(suffix: str) -> str:
    """根据后缀判断文件类型"""
    types = {
        ".md": "markdown", ".yaml": "config", ".yml": "config", ".json": "config",
        ".py": "code", ".js": "code", ".ts": "code",
        ".txt": "text", ".sh": "script",
    }
    return types.get(suffix.lower(), "other")


def _read_skill_readme(skill_dir: Path) -> Optional[str]:
    """读取 skill.md 或 README.md（大小写不敏感）"""
    for item in skill_dir.iterdir():
        if item.is_file() and item.stem.lower() in ("skill", "readme") and item.suffix.lower() == ".md":
            try:
                return item.read_text(encoding="utf-8", errors="ignore")[:50000]
            except Exception:
                pass
    return None


def _read_skill_manifest(skill_dir: Path) -> Optional[dict]:
    """读取 manifest 文件 (skill.yaml / skill.json)"""
    for name in ["skill.yaml", "skill.json", "manifest.json", "manifest.yaml"]:
        p = skill_dir / name
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                if name.endswith(".json"):
                    return json.loads(content)
                else:
                    return yaml.safe_load(content)
            except Exception:
                pass
    return None


def _read_skill_prompts(skill_dir: Path) -> list[dict]:
    """读取提示词文件"""
    prompts = []
    prompt_dir = skill_dir / "prompts"
    if not prompt_dir.is_dir():
        prompt_dir = skill_dir
    for p in prompt_dir.glob("*.md"):
        if p.name.lower() not in ("readme.md", "skill.md"):
            try:
                prompts.append({
                    "name": p.stem,
                    "content": p.read_text(encoding="utf-8", errors="ignore")[:20000],
                })
            except Exception:
                pass
    return prompts


def _read_skill_tests(skill_dir: Path) -> list[dict]:
    """读取测试文件"""
    tests = []
    test_dir = skill_dir / "tests"
    if not test_dir.is_dir():
        test_dir = skill_dir
    for p in list(test_dir.glob("test_*.py")) + list(test_dir.glob("*.test.js")):
        try:
            tests.append({
                "name": p.name,
                "content": p.read_text(encoding="utf-8", errors="ignore")[:10000],
            })
        except Exception:
            pass
    return tests


@app.get("/api/skills/{skill_id}/detail")
async def get_skill_detail(skill_id: str):
    """获取 Skill 完整详情（含文件内容和目录结构）"""
    db = await get_db()
    skill = await db.get_skill(skill_id)

    # 处理本地文件系统 Skill（fs-* 前缀）
    if not skill and skill_id.startswith("fs-"):
        skill_name = skill_id[3:]  # 去掉 "fs-" 前缀
        skill_dir = Path.home() / ".claude" / "skills" / skill_name
        if skill_dir.is_dir():
            readme = _read_skill_readme(skill_dir)
            manifest = _read_skill_manifest(skill_dir)
            description = ""
            if readme:
                lines = [l.strip() for l in readme.split("\n") if l.strip() and not l.startswith("#")]
                if lines:
                    description = lines[0][:200]
            skill = {
                "id": skill_id,
                "name": skill_name,
                "description": description or f"本地 Skill: {skill_name}",
                "version": manifest.get("version", "1.0.0") if manifest else "1.0.0",
                "source": "local",
                "enabled": True,
                "trigger_keywords": _skill_trigger_keywords(skill_name, "local", _manifest_keywords(manifest)),
                "created_at": "",
            }

    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")

    # 查找 Skill 目录
    skill_dir = _find_skill_directory(skill["name"])

    result = {
        "skill": skill,
        "directory": None,
        "files": [],
        "readme": None,
        "manifest": None,
        "prompts": [],
        "tests": [],
    }

    if skill_dir and os.path.isdir(skill_dir):
        result["directory"] = str(skill_dir)
        result["files"] = _list_skill_files(skill_dir)
        result["readme"] = _read_skill_readme(skill_dir)
        result["manifest"] = _read_skill_manifest(skill_dir)
        result["prompts"] = _read_skill_prompts(skill_dir)
        result["tests"] = _read_skill_tests(skill_dir)

    return result


async def _resolve_skill_dir(skill_id: str) -> Path:
    """统一解析 Skill 目录（支持数据库 Skill 和本地 fs-* Skill）"""
    db = await get_db()
    skill = await db.get_skill(skill_id)

    if skill:
        skill_dir = _find_skill_directory(skill["name"])
        if skill_dir:
            return skill_dir

    # 处理本地文件系统 Skill
    if skill_id.startswith("fs-"):
        skill_name = skill_id[3:]
        skill_dir = Path.home() / ".claude" / "skills" / skill_name
        if skill_dir.is_dir():
            return skill_dir

    raise HTTPException(status_code=404, detail="Skill 目录不存在")


@app.get("/api/skills/{skill_id}/file")
async def get_skill_file(skill_id: str, path: str = Query(..., description="文件相对路径")):
    """读取 Skill 目录中的指定文件"""
    skill_dir = await _resolve_skill_dir(skill_id)

    file_path = skill_dir / path
    # Security: prevent path traversal
    try:
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(skill_dir.resolve())):
            raise HTTPException(status_code=403, detail="路径不允许")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="无效路径")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")[:100000]
        return {"path": path, "content": content, "size": file_path.stat().st_size}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {str(e)}")


class FileUpdateRequest(BaseModel):
    path: str
    content: str


@app.put("/api/skills/{skill_id}/file")
async def update_skill_file(skill_id: str, req: FileUpdateRequest):
    """保存 Skill 目录中的文件"""
    skill_dir = await _resolve_skill_dir(skill_id)

    file_path = skill_dir / req.path
    # Security: prevent path traversal
    try:
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(skill_dir.resolve())):
            raise HTTPException(status_code=403, detail="路径不允许")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="无效路径")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(req.content, encoding="utf-8")
        return {"success": True, "path": req.path, "size": file_path.stat().st_size}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)}")


# ============ 配置 API ============

@app.get("/api/config")
async def get_config():
    """获取 LLM 配置"""
    settings = await _load_llm_settings()
    return {
        "anthropic_api_key": settings.model.anthropic_api_key,
        "anthropic_base_url": settings.model.anthropic_base_url,
        "openai_api_key": settings.model.openai_api_key,
        "openai_base_url": settings.model.openai_base_url,
        "model_low": settings.model.model_low,
        "model_medium": settings.model.model_medium,
        "model_high": settings.model.model_high,
    }


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    """保存 LLM 配置到 symbio.yaml"""
    from symbio.config.settings import Settings

    config_path = Path("symbio.yaml")
    if config_path.exists():
        settings = Settings.from_yaml(config_path)
    else:
        settings = Settings()

    if update.anthropic_api_key is not None:
        settings.model.anthropic_api_key = update.anthropic_api_key
    if update.anthropic_base_url is not None:
        settings.model.anthropic_base_url = update.anthropic_base_url
    if update.openai_api_key is not None:
        settings.model.openai_api_key = update.openai_api_key
    if update.openai_base_url is not None:
        settings.model.openai_base_url = update.openai_base_url
    if update.model_low is not None:
        settings.model.model_low = update.model_low
    if update.model_medium is not None:
        settings.model.model_medium = update.model_medium
    if update.model_high is not None:
        settings.model.model_high = update.model_high

    settings.to_yaml(config_path)

    # 同时清除缓存的 settings 实例
    from symbio.config.settings import get_settings
    get_settings.cache_clear()

    return {"success": True}


# ============ HITL 审批 API ============

def _get_hitl_gateway() -> ApprovalGateway:
    if not hasattr(app.state, "hitl_gateway"):
        app.state.hitl_gateway = _build_hitl_gateway()
    return app.state.hitl_gateway


def _get_hitl_notifier() -> HITLNotifier:
    if not hasattr(app.state, "hitl_notifier"):
        app.state.hitl_notifier = HITLNotifier.from_settings()
    return app.state.hitl_notifier


def _hitl_request_payload(request: ApprovalRequest) -> dict:
    payload = request.model_dump(mode="json")
    notifications = payload.get("metadata", {}).get("notifications", [])
    latest_notification = notifications[-1] if notifications else None
    payload["id"] = request.request_id
    payload["code"] = approval_short_code(request.request_id)
    payload["title"] = request.action or f"Approval {request.request_id[:8]}"
    payload["description"] = request.reason or request.impact_scope
    payload["risk"] = request.risk_level.value
    payload["status"] = request.status.value
    payload["agent"] = payload.get("agent") or "orchestrator"
    payload["approval_count"] = len(request.approvals)
    payload["pending_approvals"] = max(request.required_approvers - len(request.approvals), 0)
    payload["notification_status"] = payload.get("metadata", {}).get(
        "notification_status",
        "not_configured" if not notifications else latest_notification.get("delivery_status", "prepared"),
    )
    payload["notification_count"] = len(notifications)
    payload["latest_notification"] = latest_notification
    return payload


async def _notify_hitl_request(request: ApprovalRequest) -> list[dict]:
    if request.status != ApprovalStatus.PENDING:
        return []
    results = await _get_hitl_notifier().notify(request)
    result_payloads = [result.model_dump(mode="json") for result in results]
    notifications = [item["payload"] for item in result_payloads if item.get("payload")]
    request.metadata["notifications"] = notifications
    if notifications:
        request.metadata["notification_status"] = notifications[-1].get("delivery_status", "prepared")
    else:
        request.metadata["notification_status"] = "not_configured"
    await _get_hitl_gateway().update_request(request)
    return result_payloads


async def _try_resume_hitl_task(request_id: str) -> Optional[dict]:
    orchestrator = getattr(app.state, "orchestrator", None)
    gateway = _get_hitl_gateway()
    if orchestrator is None or getattr(orchestrator, "hitl_gateway", None) is not gateway:
        return None

    result = await orchestrator.resume_after_approval(request_id)
    if result is None:
        return None
    return result.model_dump(mode="json")


async def _resolve_hitl_request_id(request_ref: str) -> str:
    gateway = _get_hitl_gateway()
    request_ref = request_ref.strip()
    if await gateway.get_request(request_ref):
        return request_ref

    matches = []
    for request in [*(await gateway.get_pending()), *(await gateway.get_history())]:
        if approval_short_code(request.request_id).lower() == request_ref.lower():
            matches.append(request.request_id)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="审批短码冲突，请使用完整 request_id")
    raise HTTPException(status_code=404, detail="审批请求不存在")


class IMApprovalCallback(BaseModel):
    platform: str = ""
    sender_id: str = ""
    text: str = ""
    token: str = ""
    request_id: str = ""
    action: str = ""
    comment: str = ""


@app.get("/api/hitl")
async def get_all_approvals(limit: int = 50):
    gateway = _get_hitl_gateway()
    pending = await gateway.get_pending()
    history = (await gateway.get_history())[-limit:]
    requests = [_hitl_request_payload(r) for r in [*pending, *history]]
    return {"requests": requests, "total": len(requests)}


@app.get("/api/hitl/pending")
async def get_pending_approvals():
    """获取所有待审批请求"""
    gateway = _get_hitl_gateway()
    pending = await gateway.get_pending()
    return {"requests": [_hitl_request_payload(r) for r in pending], "total": len(pending)}


@app.post("/api/hitl/submit")
async def submit_approval_request(request: ApprovalRequest):
    """提交审批请求"""
    gateway = _get_hitl_gateway()
    request_id = await gateway.submit_request(request)
    stored = await gateway.get_request(request_id)
    notification_results = await _notify_hitl_request(stored or request)
    return {
        "request_id": request_id,
        "status": "submitted",
        "request": _hitl_request_payload(stored or request),
        "notifications": notification_results,
    }


@app.post("/api/hitl/{request_id}/approve")
async def approve_request(request_id: str, approver_id: str = "web-user", comment: str = ""):
    """审批通过"""
    gateway = _get_hitl_gateway()
    request_id = await _resolve_hitl_request_id(request_id)
    try:
        result = await gateway.approve(request_id, approver_id, comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    resumed_result = None
    if result.status == ApprovalStatus.APPROVED:
        resumed_result = await _try_resume_hitl_task(request_id)
    return {"request": _hitl_request_payload(result), "resumed_result": resumed_result}


@app.post("/api/hitl/{request_id}/reject")
async def reject_request(request_id: str, approver_id: str = "web-user", comment: str = ""):
    """审批拒绝"""
    gateway = _get_hitl_gateway()
    request_id = await _resolve_hitl_request_id(request_id)
    try:
        result = await gateway.reject(request_id, approver_id, comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    return {"request": _hitl_request_payload(result)}


@app.post("/api/hitl/im-callback")
async def hitl_im_callback(callback: IMApprovalCallback):
    """Handle approval commands forwarded by QQ/WeChat/IM bots."""
    from symbio.config.settings import get_settings

    configured_token = get_settings().hitl.im_webhook_token
    if configured_token and callback.token != configured_token:
        raise HTTPException(status_code=401, detail="无效的 IM 回调 token")

    action = callback.action.lower().strip()
    request_id = callback.request_id.strip()
    comment = callback.comment.strip()
    if not action or not request_id:
        command = parse_im_approval_command(callback.text)
        if command is None:
            raise HTTPException(status_code=400, detail="无法解析审批命令")
        action = command.action
        request_id = command.request_id
        comment = comment or command.comment

    approver_id = callback.sender_id or f"{callback.platform or 'im'}-user"
    gateway = _get_hitl_gateway()
    request_id = await _resolve_hitl_request_id(request_id)
    try:
        if action in {"approve", "approved", "yes", "ok"}:
            result = await gateway.approve(request_id, approver_id=approver_id, comment=comment)
            resumed_result = None
            if result.status == ApprovalStatus.APPROVED:
                resumed_result = await _try_resume_hitl_task(request_id)
            return {"request": _hitl_request_payload(result), "resumed_result": resumed_result}
        if action in {"reject", "rejected", "no"}:
            result = await gateway.reject(request_id, approver_id=approver_id, comment=comment)
            return {"request": _hitl_request_payload(result), "resumed_result": None}
    except KeyError:
        raise HTTPException(status_code=404, detail="审批请求不存在或已处理")

    raise HTTPException(status_code=400, detail="未知审批动作")


@app.post("/api/hitl/webhook")
async def hitl_webhook(payload: WebhookPayload):
    """Handle signed approval webhook callbacks."""
    request_id = verify_approval_token(payload.token)
    if request_id is None or request_id != payload.request_id:
        raise HTTPException(status_code=401, detail="无效的审批 token")

    gateway = _get_hitl_gateway()
    try:
        if payload.status == ApprovalStatus.APPROVED:
            result = await gateway.approve(
                payload.request_id,
                approver_id=payload.approver_id or "webhook",
                comment=payload.comment,
            )
            resumed_result = None
            if result.status == ApprovalStatus.APPROVED:
                resumed_result = await _try_resume_hitl_task(payload.request_id)
            return {"request": _hitl_request_payload(result), "resumed_result": resumed_result}
        if payload.status == ApprovalStatus.REJECTED:
            result = await gateway.reject(
                payload.request_id,
                approver_id=payload.approver_id or "webhook",
                comment=payload.comment,
            )
            return {"request": _hitl_request_payload(result), "resumed_result": None}
    except KeyError:
        raise HTTPException(status_code=404, detail="审批请求不存在或已处理")

    raise HTTPException(status_code=400, detail="Webhook 只支持 approved/rejected")


@app.get("/api/hitl/channels")
async def get_hitl_channels():
    notifier = _get_hitl_notifier()
    return {
        "channels": [
            target.model_dump(exclude={"access_token"})
            for target in notifier.targets
        ],
        "enabled": [
            target.platform
            for target in notifier.enabled_targets()
        ],
    }


@app.get("/api/hitl/history")
async def get_approval_history(limit: int = 50):
    gateway = _get_hitl_gateway()
    history = (await gateway.get_history())[-limit:]
    return {"history": [_hitl_request_payload(r) for r in history], "total": len(history)}


@app.get("/api/hitl/{request_id}")
async def get_approval_request(request_id: str):
    """获取审批请求详情"""
    gateway = _get_hitl_gateway()
    request = await gateway.get_request(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    return {"request": _hitl_request_payload(request)}


# ============ WebSocket ============

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket 对话 - 支持真实 LLM 流式输出"""
    await websocket.accept()
    logger.info("WebSocket 连接建立")

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            content = message.get("content", "")
            session_id = message.get("session_id", "default")
            model_override = message.get("model", None)
            now_str = time.strftime("%Y-%m-%dT%H:%M:%S")

            db = await get_db()

            # 确保会话存在
            await _ensure_session(db, session_id, title=content[:30] if content else "新对话")

            # 保存用户消息
            user_msg_id = f"msg-{uuid.uuid4().hex[:12]}"
            await db.create_message(user_msg_id, session_id, "user", content, now_str, 0)

            # 自动存入 MemoryManager（语义搜索）
            if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
                await app.state.memory_manager.add_conversation_turn("user", content, session_id)

            # 更新会话标题
            session = await db.get_session(session_id)
            if session and session["title"] == "新对话":
                await db.update_session(session_id, title=content[:30])

            full_response = ""
            token_input = 0
            token_output = 0

            try:
                import anthropic

                settings = await _load_llm_settings()
                api_key = settings.model.anthropic_api_key
                base_url = settings.model.anthropic_base_url

                if not api_key:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "content": "未配置 API Key，请在 Models 页面配置 LLM",
                    }))
                    continue

                client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
                model = model_override or settings.model.model_medium

                # 构建含历史对话的消息列表
                messages = await _build_history_messages(db, session_id)
                logger.info(f"WebSocket 对话 - 会话: {session_id}, 历史消息数: {len(messages)}")

                # 流式调用
                async with client.messages.stream(
                    model=model,
                    max_tokens=4096,
                    system=SYMBIO_SYSTEM_PROMPT,
                    messages=messages,
                ) as stream:
                    async for text in stream.text_stream:
                        full_response += text
                        await websocket.send_text(json.dumps({
                            "type": "token",
                            "content": text,
                        }))

                    final = await stream.get_final_message()
                    token_input = final.usage.input_tokens
                    token_output = final.usage.output_tokens

            except ImportError:
                response = f"收到: {content}"
                for char in response:
                    full_response += char
                    await websocket.send_text(json.dumps({"type": "token", "content": char}))
                    await asyncio.sleep(0.02)
                token_input = len(content) // 4
                token_output = len(full_response) // 4
            except Exception as e:
                logger.error(f"WebSocket LLM 调用失败: {e}")
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "content": f"LLM 调用失败: {str(e)}",
                }))
                continue

            # 保存 AI 回复
            await db.create_message(
                f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant",
                full_response, time.strftime("%Y-%m-%dT%H:%M:%S"), token_input + token_output,
            )

            # 自动存入 MemoryManager（语义搜索）
            if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
                await app.state.memory_manager.add_conversation_turn("assistant", full_response, session_id)

            # 发送完成信号
            await websocket.send_text(json.dumps({
                "type": "done",
                "content": full_response,
                "session_id": session_id,
                "token_usage": {
                    "input": token_input,
                    "output": token_output,
                    "total": token_input + token_output,
                },
            }))

    except WebSocketDisconnect:
        logger.info("WebSocket 连接断开")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        try:
            await websocket.close()
        except Exception:
            pass


# ============ 静态文件 ============

web_dir = _get_web_dir()
if web_dir is not None:
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/ui")
    async def serve_ui():
        """提供 Web UI"""
        return FileResponse(str(web_dir / "index.html"))
