"""Symbio FastAPI 服务端

使用 SQLite 持久化存储，集成 LLM 对话、模型管理、任务监控、
记忆管理、技能管理等完整 API。
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
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
from symbio.core.chat_pipeline import get_chat_pipeline
from symbio.security.chat_guard import get_chat_guard
from symbio.interfaces.wechat_bridge import WeChatInbound, get_wechat_bridge
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
        # 工作台需要能把 agent 派到整机任意真实项目目录，故放开工作区边界。
        allow_any_workspace=True,
    )
    app.state.external_agent_controller = controller
    return controller


def _get_external_live_manager():
    manager = getattr(app.state, "external_live_manager", None)
    if manager is not None:
        return manager

    from symbio.tools.external_live_session import ExternalLiveSessionManager

    manager = ExternalLiveSessionManager(
        controller=_get_external_agent_controller(),
        state_path=Path("data") / "external_live_sessions.json",
        workspace_root=_project_root(),
    )
    app.state.external_live_manager = manager
    return manager


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
    # 尝试恢复微信登录态（重启免重扫码）；token 过期则 recv loop 自动回落
    try:
        bridge = get_wechat_bridge()
        bridge.set_message_handler(_wechat_dispatch_reply)
        if await bridge.try_restore_session():
            logger.info("微信登录态已从本地恢复")
    except Exception as e:
        logger.warning(f"微信登录态恢复跳过: {e}")
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
    # 附件文件路径（图片/PDF）。会自动摄取：图片走 Claude 视觉模型生成描述，
    # 描述既入库可检索，也作为历史消息让模型在本轮可感知。
    attachments: list[str] = []


class ChatResponse(BaseModel):
    success: bool
    content: str
    session_id: str
    token_usage: Optional[dict] = None
    cached: bool = False
    prune_info: Optional[dict] = None
    # 本轮自动摄取的附件摘要（路径/模态/是否有视觉描述/记忆ID）
    attachments_ingested: Optional[list] = None


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
    hitl: Optional["HITLConfigUpdate"] = None


class HITLNotifyTargetUpdate(BaseModel):
    platform: str = ""
    endpoint: str = ""
    chat_id: str = ""
    chat_type: str = "group"
    access_token: str = ""
    secret: str = ""
    enabled: bool = True


class HITLConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    high_risk_auto_suspend: Optional[bool] = None
    approval_timeout: Optional[int] = None
    callback_base_url: Optional[str] = None
    im_webhook_token: Optional[str] = None
    notify_timeout: Optional[float] = None
    notify_targets: Optional[list[HITLNotifyTargetUpdate]] = None


class DirImportRequest(BaseModel):
    path: str


class MemoryStoreRequest(BaseModel):
    content: str
    title: str = ""
    tags: list[str] = []
    importance: float = 0.5
    memory_type: str = "long_term"  # short_term, long_term, episodic, semantic, procedural
    # 多模态：text（默认）/ image / pdf / code。
    # image/pdf 时 content 为文件路径，图片会调 Claude 视觉模型生成描述后入库。
    modality: str = "text"
    language: str = "python"        # 仅 modality=code 时使用


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


class SandboxContainerExecuteRequest(BaseModel):
    command: str
    image: str = "python:3.11-slim"
    timeout: Optional[int] = 120
    working_dir: str = "/workspace"
    env: dict[str, str] = Field(default_factory=dict)
    memory_limit: str = "512m"
    cpus: str = "1"
    network: str = "none"
    mount_workspace: bool = False


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


class ExternalLiveAttachRequest(BaseModel):
    provider: str
    transcript_path: str = ""
    external_session_id: str = ""
    workspace: str = "."
    label: str = ""
    from_start: bool = True


class ExternalLiveSendRequest(BaseModel):
    prompt: str
    dry_run: bool = False
    model: str = ""
    timeout: int = 300


class ExternalRelayApiRequest(BaseModel):
    seed_prompt: str
    provider_a: str = "codex"
    provider_b: str = "claude-code"
    rounds: int = 2
    workspace: str = "."
    model_a: str = ""
    model_b: str = ""
    role_a: str = ""
    role_b: str = ""
    timeout: int = 300
    dry_run: bool = False


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


def _remote_skill_source(repo: str, ref: str = "main"):
    """构造 GitHub 远程 Skills 源；测试可注入 app.state.remote_skill_source_factory。"""
    factory = getattr(app.state, "remote_skill_source_factory", None)
    if factory is not None:
        return factory(repo or "anthropics/skills", ref or "main")
    from symbio.skills.remote_source import GitHubSkillSource

    return GitHubSkillSource(repo=repo or "anthropics/skills", ref=ref or "main")


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
    from symbio.config.settings import HITLConfig, Settings
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
    from symbio import __version__ as _ver

    return {
        "name": "Symbio",
        "version": _ver,
        "status": "running",
    }


@app.get("/health")
@app.get("/api/health")
async def health():
    """健康检查"""
    from symbio import __version__ as _ver

    return {"status": "ok", "version": _ver}


@app.get("/api/fs/dirs")
async def browse_directories(path: str = Query("", description="要浏览的绝对目录；空则返回盘符/根")):
    """服务端目录浏览：列出某目录下的子文件夹，供工作台选择工作区。

    只列文件夹名，不读文件内容。path 为空时：Windows 返回可用盘符，其它平台返回根 /。
    """
    entries: list[dict[str, Any]] = []

    # path 为空：给出起点（Windows 列盘符，POSIX 从根开始）
    if not path:
        if os.name == "nt":
            import string

            drives = [
                f"{letter}:\\"
                for letter in string.ascii_uppercase
                if Path(f"{letter}:\\").exists()
            ]
            for drive in drives:
                entries.append({"name": drive, "path": drive})
            return {"path": "", "parent": None, "separator": os.sep, "entries": entries}
        path = "/"

    try:
        current = Path(path).resolve()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"无效路径：{exc}")
    if not current.exists() or not current.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在：{current}")

    try:
        for child in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except (PermissionError, OSError):
                continue  # 无权限的项跳过，不整体失败
    except (PermissionError, OSError) as exc:
        raise HTTPException(status_code=403, detail=f"无法读取目录：{exc}")

    parent = str(current.parent) if current.parent != current else None
    return {"path": str(current), "parent": parent, "separator": os.sep, "entries": entries}


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


@app.get("/api/sandbox/docker/status")
async def sandbox_docker_status():
    """Report whether the Docker engine is reachable for container isolation."""
    from symbio.tools.sandbox import check_docker_available

    available, detail = await check_docker_available()
    return {"available": available, "detail": detail}


@app.post("/api/sandbox/docker/execute")
async def sandbox_docker_execute(request: SandboxContainerExecuteRequest):
    """Run a command inside an isolated Docker container.

    默认断网 + 只读根文件系统 + 内存/CPU 限制；mount_workspace=True 时
    以只读方式把工作区挂进 /workspace。引擎不可用返回 503。
    """
    executor = _get_sandbox_executor()
    volumes: dict[str, str] = {}
    if request.mount_workspace:
        volumes[str(_get_sandbox_workspace_root())] = "/workspace"

    result = await executor.execute_in_container(
        command=request.command,
        image=request.image,
        volumes=volumes,
        timeout=request.timeout,
        working_dir=request.working_dir,
        env=request.env or None,
        memory_limit=request.memory_limit,
        cpus=request.cpus,
        network=request.network,
    )
    if result.error_message.startswith("DOCKER_UNAVAILABLE"):
        raise HTTPException(status_code=503, detail=result.error_message)
    return {
        "success": result.exit_code == 0,
        "result": result.model_dump(mode="json"),
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


@app.get("/api/external-agents/live")
async def external_live_sessions():
    """List live two-way-synced external-agent conversations."""
    manager = _get_external_live_manager()
    return {
        "sessions": [session.model_dump(mode="json") for session in manager.list_sessions()],
    }


@app.post("/api/external-agents/live/attach")
async def external_live_attach(request: ExternalLiveAttachRequest):
    """Attach to a Codex/Claude Code conversation for live two-way sync."""
    from symbio.tools.external_agents import ExternalAgentSessionCreate

    provider = ExternalAgentSessionCreate(provider=request.provider).provider
    manager = _get_external_live_manager()
    transcript_path = request.transcript_path
    try:
        if transcript_path:
            transcript_path = str(_validate_external_transcript_path(provider, transcript_path))
        session = manager.attach(
            provider=provider,
            transcript_path=transcript_path or None,
            external_session_id=request.external_session_id,
            workspace=request.workspace,
            label=request.label,
            from_start=request.from_start,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"success": True, "session": session.model_dump(mode="json")}


@app.post("/api/external-agents/live/{session_id}/poll")
async def external_live_poll(session_id: str):
    """Tail new turns appended to the conversation since the last poll (inbound sync)."""
    manager = _get_external_live_manager()
    try:
        messages = manager.poll(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Live session not found")
    session = manager.get_session(session_id)
    return {
        "messages": [message.model_dump(mode="json") for message in messages],
        "byte_offset": session.byte_offset if session else 0,
    }


@app.post("/api/external-agents/live/{session_id}/send")
async def external_live_send(session_id: str, request: ExternalLiveSendRequest):
    """Inject a prompt into the conversation via --resume (outbound sync)."""
    manager = _get_external_live_manager()
    try:
        result = await manager.send(
            session_id,
            request.prompt,
            dry_run=request.dry_run,
            model=request.model,
            timeout=request.timeout,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Live session not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"success": result.success, "result": result.model_dump(mode="json")}


@app.delete("/api/external-agents/live/{session_id}")
async def external_live_detach(session_id: str):
    """Stop tracking a live conversation."""
    manager = _get_external_live_manager()
    if not manager.detach(session_id):
        raise HTTPException(status_code=404, detail="Live session not found")
    return {"success": True}


@app.post("/api/external-agents/relay")
async def external_agent_relay(request: ExternalRelayApiRequest):
    """Run a turn-by-turn relay between two external coding agents (互相调用)."""
    from symbio.tools.external_relay import ExternalRelayOrchestrator, RelayConfig

    orchestrator = ExternalRelayOrchestrator(
        controller=_get_external_agent_controller(),
        workspace_root=str(_project_root()),
    )
    try:
        result = await orchestrator.run(RelayConfig(**request.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": result.success, "result": result.model_dump(mode="json")}


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


async def _collect_prometheus_metrics() -> str:
    """收集 Symbio 运行指标，输出 Prometheus 文本暴露格式。

    每个子系统独立 try/except，单点失败不影响整体；缺失项以 0 兜底。
    """
    lines: list[str] = []

    def emit(name: str, value, help_text: str, mtype: str = "gauge", labels: str = ""):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        label_str = f"{{{labels}}}" if labels else ""
        try:
            lines.append(f"{name}{label_str} {float(value)}")
        except (TypeError, ValueError):
            lines.append(f"{name}{label_str} 0")

    # build info
    try:
        from symbio import __version__ as _ver
    except Exception:
        _ver = "unknown"
    emit("symbio_build_info", 1, "Symbio build info", "gauge", f'version="{_ver}"')

    # 会话与消息
    try:
        db = await get_db()
        sessions = await db.list_sessions()
        emit("symbio_sessions_total", len(sessions), "Total chat sessions")
        emit("symbio_messages_total", sum((s.get("message_count") or 0) for s in sessions),
             "Total chat messages")
    except Exception as e:
        logger.debug(f"metrics sessions skipped: {e}")

    # 成本 / 缓存
    try:
        pipeline = get_chat_pipeline()
        summary = await pipeline.cost_summary(24)
        if summary.get("available"):
            emit("symbio_tokens_total_24h", summary.get("total_tokens", 0), "Tokens used in last 24h", "counter")
            emit("symbio_llm_requests_total_24h", summary.get("total_requests", 0), "LLM requests in last 24h", "counter")
        cache = await pipeline.cache_stats()
        emit("symbio_cache_hits_total", cache.get("cache_hits", 0), "Semantic cache hits", "counter")
        emit("symbio_cache_misses_total", cache.get("cache_misses", 0), "Semantic cache misses", "counter")
        emit("symbio_cache_hit_rate", cache.get("hit_rate", 0.0), "Semantic cache hit rate")
        emit("symbio_cache_tokens_saved", cache.get("estimated_token_saved", 0), "Estimated tokens saved by cache", "counter")
    except Exception as e:
        logger.debug(f"metrics cost/cache skipped: {e}")

    # 安全防火墙
    try:
        sec = get_chat_guard().stats()
        emit("symbio_security_analyzed_total", sec.get("total_analyzed", 0), "Inputs analyzed by firewall", "counter")
        emit("symbio_security_block_rate", sec.get("block_rate", 0.0), "Firewall block rate")
    except Exception as e:
        logger.debug(f"metrics security skipped: {e}")

    # HITL 待审批
    try:
        pending = await _get_hitl_gateway().get_pending()
        emit("symbio_hitl_pending", len(pending), "Pending HITL approvals")
    except Exception as e:
        logger.debug(f"metrics hitl skipped: {e}")

    # 可观测性 tracer
    try:
        obs = await _observability_summary()
        emit("symbio_spans_captured", obs.get("spans", {}).get("captured", 0), "Captured trace spans")
        emit("symbio_metric_records", obs.get("metrics", {}).get("records", 0), "Tracer metric records")
        emit("symbio_tracer_started", 1 if obs.get("is_started") else 0, "Whether tracer is started")
    except Exception as e:
        logger.debug(f"metrics tracer skipped: {e}")

    # 数据飞轮
    try:
        from symbio.evolution.flywheel import get_flywheel
        ov = await get_flywheel().overview()
        an = ov.get("stages", {}).get("analysis", {})
        fb = ov.get("stages", {}).get("feedback", {})
        emit("symbio_flywheel_failures_total", an.get("total_failures", 0), "Recorded failure analyses", "counter")
        emit("symbio_flywheel_feedback_total", fb.get("total_explicit", 0), "Collected explicit feedback", "counter")
    except Exception as e:
        logger.debug(f"metrics flywheel skipped: {e}")

    return "\n".join(lines) + "\n"


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 文本格式运行指标（被 config/prometheus/prometheus.yml 抓取）。"""
    from fastapi.responses import PlainTextResponse
    text = await _collect_prometheus_metrics()
    return PlainTextResponse(content=text, media_type="text/plain; version=0.0.4; charset=utf-8")


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


# ============ 数据飞轮 API ============

class FailureRecordRequest(BaseModel):
    task_id: str = ""
    trajectory_id: str = ""
    prompt_id: str = ""
    category: str = "unknown"
    severity: str = "medium"
    description: str = ""
    error_message: str = ""
    steps_to_failure: int = 0


class FeedbackRequest(BaseModel):
    session_id: str = ""
    task_id: str = ""
    prompt_id: str = ""
    user_id: str = ""
    rating: float = 0.0
    comment: str = ""
    tags: list[str] = []


class DistillRequest(BaseModel):
    trajectory_id: str = ""
    task_type: str = "general"
    steps: list[dict] = []
    success: bool = True
    token_count: int = 0
    duration_ms: int = 0


@app.get("/api/flywheel/overview")
async def flywheel_overview():
    """数据飞轮四阶段总览：捕获 / 失效分析 / SOP 蒸馏 / 反哺优化。"""
    from symbio.evolution.flywheel import get_flywheel
    fw = get_flywheel()
    data = await fw.overview()
    capture = getattr(app.state, "trajectory_capture", None)
    data["stages"]["capture"].update(fw.trajectory_stats(capture))
    return data


@app.get("/api/flywheel/failures")
async def flywheel_failures(limit: int = 50):
    """失效分析与根因列表（阶段二）。"""
    from symbio.evolution.flywheel import get_flywheel
    fw = get_flywheel()
    failures = await fw.list_failures(limit=limit)
    root_causes = await fw.list_root_causes(limit=limit)
    return {"failures": failures, "root_causes": root_causes,
            "total_failures": len(failures), "total_root_causes": len(root_causes)}


@app.post("/api/flywheel/failures")
async def flywheel_record_failure(payload: FailureRecordRequest):
    """记录一次失败分析（阶段二，驱动闭环）。"""
    from symbio.evolution.flywheel import get_flywheel
    return await get_flywheel().record_failure(payload.model_dump())


@app.get("/api/flywheel/sops")
async def flywheel_sops():
    """SOP 列表：内置种子 + 已蒸馏（阶段三）。"""
    from symbio.evolution.flywheel import get_flywheel
    return get_flywheel().list_sops()


@app.post("/api/flywheel/sops/distill")
async def flywheel_distill(payload: DistillRequest):
    """从一条成功轨迹蒸馏 SOP（阶段三）。"""
    from symbio.evolution.flywheel import get_flywheel
    return get_flywheel().distill_from_trajectory(payload.model_dump())


@app.get("/api/flywheel/feedback")
async def flywheel_feedback_stats():
    """反馈统计（阶段四）。"""
    from symbio.evolution.flywheel import get_flywheel
    return await get_flywheel().feedback_stats()


@app.post("/api/flywheel/feedback")
async def flywheel_collect_feedback(payload: FeedbackRequest):
    """收集一条显式反馈（阶段四）。"""
    from symbio.evolution.flywheel import get_flywheel
    return await get_flywheel().collect_feedback(payload.model_dump())


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


# ============ 成本监控 API ============

class BudgetUpdate(BaseModel):
    project_id: str = "default"
    monthly_limit_tokens: int


@app.get("/api/costs/summary")
async def get_cost_summary(period_hours: int = 24):
    """Token 用量摘要：总量、按 Agent、按模型分组（来自成本追踪数据库）。"""
    pipeline = get_chat_pipeline()
    return await pipeline.cost_summary(period_hours)


@app.get("/api/costs/cache")
async def get_semantic_cache_stats():
    """语义缓存命中率与节省 Token 估算。"""
    pipeline = get_chat_pipeline()
    return await pipeline.cache_stats()


@app.get("/api/costs/budget")
async def get_budget_status(project_id: str = "default"):
    """项目月度预算状态：用量、余量、是否需要降级模型。"""
    pipeline = get_chat_pipeline()
    return await pipeline.budget_status(project_id)


@app.post("/api/costs/budget")
async def set_budget(update: BudgetUpdate):
    """设置项目月度 Token 预算。"""
    if update.monthly_limit_tokens < 0:
        raise HTTPException(status_code=400, detail="预算不能为负数")
    pipeline = get_chat_pipeline()
    return await pipeline.set_budget(update.project_id, update.monthly_limit_tokens)


@app.get("/api/costs/dashboard")
async def get_cost_dashboard(period_hours: int = 24, project_id: str = "default"):
    """成本仪表盘聚合接口：摘要 + 缓存统计 + 预算状态一次返回。"""
    pipeline = get_chat_pipeline()
    summary, cache, budget = await asyncio.gather(
        pipeline.cost_summary(period_hours),
        pipeline.cache_stats(),
        pipeline.budget_status(project_id),
    )
    return {"summary": summary, "cache": cache, "budget": budget}


# ============ 安全防火墙 API ============

class SecurityScanRequest(BaseModel):
    text: str


@app.get("/api/security/stats")
async def security_stats():
    """威胁分布、攻击类型分布、拦截率（来自防火墙审计日志）。"""
    return get_chat_guard().stats()


@app.get("/api/security/audit")
async def security_audit(limit: int = 50, threat_level: Optional[str] = None):
    """最近的安全审计记录。"""
    records = get_chat_guard().audit(limit=limit, threat_level=threat_level)
    return {"records": records, "total": len(records)}


@app.post("/api/security/scan")
async def security_scan(payload: SecurityScanRequest):
    """对任意文本做一次三层安全扫描，返回威胁等级与净化结果。"""
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    return get_chat_guard().scan(payload.text)


@app.post("/api/security/selftest")
async def security_selftest(category: Optional[str] = None):
    """用内置攻击样本库自检防火墙，返回拦截率/准确率与未命中样本。"""
    return get_chat_guard().selftest(category=category)


# ============ 对话 API ============

# 聊天附件按扩展名判模态
_IMAGE_ATTACH_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


async def _ingest_chat_attachments(
    memory_manager,
    db,
    session_id: str,
    attachments: list[str],
) -> list[dict]:
    """把聊天附件（图片/PDF）自动摄取进记忆，并把描述落为一条历史消息。

    - 图片：调 Claude 视觉模型生成中文描述（无 key 时降级占位）
    - PDF：抽取文本内容
    - 描述写入语义索引（可后续检索），并作为一条 user 消息落库，
      使模型在本轮 _build_history_messages 时可感知附件内容
    - 任何单个附件失败都跳过，不影响其余附件与正常对话

    Returns:
        每个成功摄取附件的摘要列表（path/modality/memory_id/has_vision_description）
    """
    notes: list[dict] = []
    if not memory_manager or not attachments:
        return notes

    from symbio.memory.manager import MemoryType

    for raw_path in attachments:
        try:
            path = Path(str(raw_path))
        except Exception:
            continue
        ext = path.suffix.lower()
        if ext in _IMAGE_ATTACH_EXTS:
            modality = "image"
        elif ext == ".pdf":
            modality = "pdf"
        else:
            logger.info(f"附件类型未知，跳过摄取: {raw_path}")
            continue

        try:
            item = await memory_manager.add_multimodal_memory(
                content=str(path),
                modality=modality,
                memory_type=MemoryType.LONG_TERM,
                session_id=session_id,
                source="chat_attachment",
                tags=["chat_attachment"],
            )
        except Exception as exc:
            logger.warning(f"附件摄取失败: {raw_path}: {exc}")
            continue

        if item is None:
            logger.warning(f"附件处理未产出内容，跳过: {raw_path}")
            continue

        note_text = f"[附件] {path.name}\n{item.content}"
        # 落一条 user 历史消息，让模型本轮可感知附件
        if db is not None:
            try:
                await db.create_message(
                    f"msg-{uuid.uuid4().hex[:12]}",
                    session_id,
                    "user",
                    note_text,
                    time.strftime("%Y-%m-%dT%H:%M:%S"),
                    0,
                )
            except Exception as exc:
                logger.warning(f"附件历史消息落库失败: {raw_path}: {exc}")

        notes.append({
            "path": str(path),
            "modality": modality,
            "memory_id": item.memory_id,
            "has_vision_description": bool(item.metadata.get("has_vision_description")),
            "description": item.metadata.get("vision_description") or item.content,
        })

    return notes


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

    # 聊天附件自动摄取（图片/PDF -> 视觉/文本描述 -> 入库 + 落历史，模型本轮可感知）
    attachments_ingested = await _ingest_chat_attachments(
        getattr(app.state, "memory_manager", None), db, session_id, request.attachments
    )

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
            return ChatResponse(
                success=False, content=error_msg, session_id=session_id,
                attachments_ingested=attachments_ingested or None,
            )

        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        model = request.model or settings.model.model_medium

        # 安全防火墙：Prompt Injection 三层检测（高危输入直接拦截，不调用 LLM）
        guard = get_chat_guard()
        verdict = guard.inspect(request.message, session_id=session_id)
        if not verdict["allowed"]:
            block_msg = f"⛔ 该消息被安全防火墙拦截：{verdict['reason']}"
            await db.create_message(
                f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant",
                block_msg, time.strftime("%Y-%m-%dT%H:%M:%S"), 0,
            )
            return ChatResponse(
                success=False, content=block_msg, session_id=session_id,
                token_usage={"input": 0, "output": 0, "total": 0},
            )

        # 构建含历史对话的消息列表
        messages = await _build_history_messages(db, session_id)
        logger.info(f"HTTP 对话 - 会话: {session_id}, 历史消息数: {len(messages)}")

        # 成本优化管线：语义缓存查询（命中则零 Token 返回）
        pipeline = get_chat_pipeline()
        ctx_hash = pipeline.context_hash(messages[:-1] if messages else [])
        cached = await pipeline.lookup_cache(request.message, model=model, context_hash=ctx_hash)
        if cached:
            content = cached["content"]
            await db.create_message(
                f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant",
                content, time.strftime("%Y-%m-%dT%H:%M:%S"), 0,
            )
            if hasattr(app.state, 'memory_manager') and app.state.memory_manager:
                await app.state.memory_manager.add_conversation_turn("assistant", content, session_id)
            logger.info(f"语义缓存命中，零 Token 返回 - 会话: {session_id}")
            return ChatResponse(
                success=True, content=content, session_id=session_id,
                token_usage={"input": 0, "output": 0, "total": 0},
                cached=True,
                attachments_ingested=attachments_ingested or None,
            )

        # 成本优化管线：上下文剪枝（超出预算时裁剪历史）
        messages, prune_info = pipeline.prune_history(messages)

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

        # 成本优化管线：记录用量 + 回写语义缓存
        await pipeline.record_usage(
            session_id=session_id, model=model,
            input_tokens=token_usage["input"], output_tokens=token_usage["output"],
        )
        await pipeline.store_cache(request.message, content, model=model, context_hash=ctx_hash)

        return ChatResponse(
            success=True, content=content, session_id=session_id,
            token_usage=token_usage, prune_info=prune_info,
            attachments_ingested=attachments_ingested or None,
        )

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
    """手动存储记忆（同时写入 SQLite 和 MemoryManager）。

    modality 为 image/pdf/code 时走多模态摄取：图片用 Claude 视觉模型生成描述，
    PDF/代码抽取结构化文本，再把"可检索文本表示"写入 SQLite 与语义索引。
    """
    db = await get_db()
    memory_id = f"mem-{uuid.uuid4().hex[:12]}"

    modality = (req.modality or "text").lower().strip()
    is_multimodal = modality in ("image", "pdf", "code")

    memory_item = None
    if is_multimodal:
        if not (hasattr(app.state, 'memory_manager') and app.state.memory_manager):
            raise HTTPException(status_code=503, detail="MemoryManager 未初始化，无法处理多模态内容")
        from symbio.memory.manager import MemoryType
        mt = MemoryType(req.memory_type) if req.memory_type in [e.value for e in MemoryType] else MemoryType.LONG_TERM
        memory_item = await app.state.memory_manager.add_multimodal_memory(
            content=req.content,
            modality=modality,
            memory_type=mt,
            language=req.language,
            tags=req.tags,
            importance=req.importance,
            source="manual",
        )
        if memory_item is None:
            raise HTTPException(
                status_code=422,
                detail=f"多模态内容处理失败（modality={modality}，请确认文件路径有效）",
            )
        # 用处理后的文本表示落 SQLite，保证两侧内容一致
        stored_content = memory_item.content
        await db.create_memory(
            memory_id=memory_id,
            content=stored_content,
            title=req.title or stored_content[:30],
            tags=req.tags,
            importance=req.importance,
        )
        return {
            "success": True,
            "memory_id": memory_id,
            "semantic_id": memory_item.memory_id,
            "modality": modality,
            "text_representation": stored_content,
            "has_vision_description": bool(memory_item.metadata.get("has_vision_description")),
        }

    # 纯文本：原有路径
    await db.create_memory(
        memory_id=memory_id,
        content=req.content,
        title=req.title or req.content[:30],
        tags=req.tags,
        importance=req.importance,
    )

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


class RemoteSkillInstallRequest(BaseModel):
    repo: str = "anthropics/skills"
    path: str
    name: str = ""
    ref: str = "main"
    html_url: str = ""


@app.get("/api/skills/marketplace/remote")
async def search_remote_skills(
    q: str = Query("", description="Keyword filter on skill name"),
    repo: str = Query("anthropics/skills", description="GitHub owner/repo to browse"),
):
    """从 GitHub 仓库列出可接入的 Agent Skills（默认官方 anthropics/skills）。"""
    try:
        source = _remote_skill_source(repo)
        skills = source.list_skills(query=q, limit=60)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"拉取远程 Skills 失败：{exc}")
    return {
        "repo": repo or "anthropics/skills",
        "skills": [skill.model_dump(mode="json") for skill in skills],
        "total": len(skills),
    }


@app.post("/api/skills/marketplace/remote/install")
async def install_remote_skill(request: RemoteSkillInstallRequest):
    """从 GitHub 拉取一个 Agent Skill 并安装到本地市场。"""
    from symbio.skills.remote_source import RemoteSkill

    marketplace = _get_skill_marketplace()
    source = _remote_skill_source(request.repo, request.ref)
    remote = RemoteSkill(
        name=request.name or request.path.rstrip("/").split("/")[-1],
        repo=request.repo or "anthropics/skills",
        path=request.path,
        ref=request.ref or "main",
        html_url=request.html_url,
    )
    try:
        record = marketplace.install_from_remote(source, remote)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"安装远程 Skill 失败：{exc}")
    return {
        "success": record.status == "installed",
        "record": _marketplace_install_record_payload(record),
    }


@app.post("/api/skills/marketplace/{package_id}/install")
async def install_marketplace_skill(package_id: str):
    marketplace = _get_skill_marketplace()
    package = marketplace.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Marketplace package not found")
    # 默认装到市场存储目录下的 installed/<name>（data/skill_marketplace/，已 gitignore）
    record = marketplace.install(package_id)
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
    from symbio.config.settings import HITLConfig
    hitl = getattr(settings, "hitl", None) or HITLConfig()
    return {
        "anthropic_api_key": settings.model.anthropic_api_key,
        "anthropic_base_url": settings.model.anthropic_base_url,
        "openai_api_key": settings.model.openai_api_key,
        "openai_base_url": settings.model.openai_base_url,
        "model_low": settings.model.model_low,
        "model_medium": settings.model.model_medium,
        "model_high": settings.model.model_high,
        "hitl": {
            "enabled": hitl.enabled,
            "high_risk_auto_suspend": hitl.high_risk_auto_suspend,
            "approval_timeout": hitl.approval_timeout,
            "callback_base_url": hitl.callback_base_url,
            "im_webhook_token": hitl.im_webhook_token,
            "notify_timeout": hitl.notify_timeout,
            "notify_targets": hitl.notify_targets,
        },
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
    if update.hitl is not None:
        if getattr(settings, "hitl", None) is None:
            settings.hitl = HITLConfig()
        hitl_update = update.hitl
        if hitl_update.enabled is not None:
            settings.hitl.enabled = hitl_update.enabled
        if hitl_update.high_risk_auto_suspend is not None:
            settings.hitl.high_risk_auto_suspend = hitl_update.high_risk_auto_suspend
        if hitl_update.approval_timeout is not None:
            settings.hitl.approval_timeout = hitl_update.approval_timeout
        if hitl_update.callback_base_url is not None:
            settings.hitl.callback_base_url = hitl_update.callback_base_url.strip().rstrip("/")
        if hitl_update.im_webhook_token is not None:
            settings.hitl.im_webhook_token = hitl_update.im_webhook_token
        if hitl_update.notify_timeout is not None:
            settings.hitl.notify_timeout = hitl_update.notify_timeout
        if hitl_update.notify_targets is not None:
            settings.hitl.notify_targets = [
                target.model_dump()
                for target in hitl_update.notify_targets
                if target.platform.strip()
            ]

    settings.to_yaml(config_path)

    # 同时清除缓存的 settings 实例
    from symbio.config.settings import get_settings
    get_settings.cache_clear()
    app.state.hitl_notifier = HITLNotifier.from_settings(settings)

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


async def _push_hitl_to_wechat(request: ApprovalRequest) -> Optional[dict]:
    """把审批卡推送到已登录个人微信（iLink bridge）的配置审批人。

    出向闭环：webhook 通知之外，直接把审批卡（含短码）发到用户的个人微信，
    用户回复"同意 <短码>"即可审批。best-effort——任何异常都不影响 submit。
    """
    from symbio.config.settings import get_settings

    wcfg = getattr(get_settings(), "wechat", None)
    approver = (getattr(wcfg, "hitl_approver", "") or "").strip() if wcfg else ""
    if not approver:
        return None

    code = approval_short_code(request.request_id)
    bridge = get_wechat_bridge()
    has_endpoint = bool(getattr(wcfg, "send_endpoint", "")) if wcfg else False
    note = {
        "platform": "wechat-ilink",
        "recipient": approver,
        "short_code": code,
        "approve_command": f"同意 {code}",
        "reject_command": f"拒绝 {code} 原因",
    }
    if not bridge.is_logged_in and not has_endpoint:
        note["delivery_status"] = "prepared"
        note["reason"] = "微信未登录且未配置 send_endpoint，无法推送审批卡"
        return note

    message = _get_hitl_notifier().render_message(request)
    try:
        delivery = await bridge.send(approver, message)
    except Exception as exc:  # pragma: no cover - 网络异常
        logger.warning(f"推送 HITL 审批卡到微信失败: {exc}")
        note["delivery_status"] = "failed"
        note["error"] = str(exc)
        return note
    note["delivery_status"] = delivery.get("delivery_status", "sent")
    note["via"] = delivery.get("via", "")
    note["message"] = message
    return note


async def _notify_hitl_request(request: ApprovalRequest) -> list[dict]:
    if request.status != ApprovalStatus.PENDING:
        return []
    results = await _get_hitl_notifier().notify(request)
    result_payloads = [result.model_dump(mode="json") for result in results]
    notifications = [item["payload"] for item in result_payloads if item.get("payload")]

    # 同时推送到已登录的个人微信（iLink bridge）
    wechat_note = await _push_hitl_to_wechat(request)
    if wechat_note:
        notifications.append(wechat_note)
        result_payloads.append({
            "platform": wechat_note.get("platform", "wechat-ilink"),
            "success": wechat_note.get("delivery_status") == "sent",
            "delivery_status": wechat_note.get("delivery_status", "prepared"),
            "payload": wechat_note,
        })

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

    ref = request_ref.lower()

    def _match(requests) -> list[str]:
        return [r.request_id for r in requests if approval_short_code(r.request_id).lower() == ref]

    # 短码优先在待审批里解析：历史里的同码（短码空间小、随时间增多）不应挡住当前请求
    pending_matches = _match(await gateway.get_pending())
    if len(pending_matches) == 1:
        return pending_matches[0]
    if len(pending_matches) > 1:
        raise HTTPException(status_code=409, detail="审批短码冲突，请使用完整 request_id")

    all_matches = pending_matches + _match(await gateway.get_history())
    if len(all_matches) == 1:
        return all_matches[0]
    if len(all_matches) > 1:
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


@app.post("/api/hitl/{request_id}/repush-wechat")
async def repush_hitl_wechat(request_id: str):
    """把某条审批的审批卡重新推送到已登录微信（投递失败/未送达时手动重推）。"""
    gateway = _get_hitl_gateway()
    rid = await _resolve_hitl_request_id(request_id)
    request = await gateway.get_request(rid)
    if request is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    note = await _push_hitl_to_wechat(request)
    if note is None:
        raise HTTPException(status_code=400, detail="未配置微信审批人（wechat.hitl_approver），无法推送")
    notifications = request.metadata.get("notifications", [])
    notifications.append(note)
    request.metadata["notifications"] = notifications
    request.metadata["notification_status"] = note.get("delivery_status", "prepared")
    await gateway.update_request(request)
    return {"success": note.get("delivery_status") == "sent", "note": note,
            "request": _hitl_request_payload(request)}


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


# ============ 个人微信双向 Bridge ============

class WeChatSendRequest(BaseModel):
    to_user: str
    content: str
    is_group: bool = False


async def _wechat_route_approval(command, approver_id: str) -> dict:
    """把微信里的审批命令路由到 HITL 网关，返回可读结果。

    带短码 → 解析该请求；裸"同意/拒绝"（无短码）→ 仅当只有一条待审批时生效，
    0 条或多条时返回 ok=False + 友好提示（由 _wechat_dispatch 直接回给用户）。
    """
    gateway = _get_hitl_gateway()
    if command.request_id:
        request_id = await _resolve_hitl_request_id(command.request_id)
    else:
        pending = await gateway.get_pending()
        if not pending:
            return {"kind": "approval", "ok": False, "reply": "当前没有待审批的请求。"}
        if len(pending) > 1:
            codes = "、".join(approval_short_code(p.request_id) for p in pending[:5])
            first = approval_short_code(pending[0].request_id)
            return {"kind": "approval", "ok": False,
                    "reply": f"当前有 {len(pending)} 条待审批，请指定短码，例如「同意 {first}」。待审批短码：{codes}"}
        request_id = pending[0].request_id

    try:
        if command.action == "approve":
            result = await gateway.approve(request_id, approver_id=approver_id, comment=command.comment)
            resumed = None
            if result.status == ApprovalStatus.APPROVED:
                resumed = await _try_resume_hitl_task(request_id)
            return {"kind": "approval", "ok": True, "action": "approve",
                    "request_id": request_id, "short_code": approval_short_code(request_id),
                    "status": result.status.value, "resumed_result": resumed}
        result = await gateway.reject(request_id, approver_id=approver_id, comment=command.comment)
        return {"kind": "approval", "ok": True, "action": "reject",
                "request_id": request_id, "short_code": approval_short_code(request_id),
                "status": result.status.value}
    except KeyError:
        raise HTTPException(status_code=404, detail="审批请求不存在或已处理")


async def _wechat_dispatch(from_user: str, content: str, group_id: str = "", is_group: bool = False) -> tuple[str, dict]:
    """处理一条入站微信消息：审批命令路由到 HITL，否则走对话管线。

    返回 (reply 文本, result dict)。供 inbound 端点与内置 iLink 收消息循环共用。
    """
    bridge = get_wechat_bridge()
    kind, parsed = bridge.classify(content)

    if kind == "approval":
        approver_id = from_user or "wechat-user"
        routed = await _wechat_route_approval(parsed, approver_id)
        if not routed.get("ok", True):
            return routed.get("reply", "该审批指令无法处理。"), routed
        code = routed.get("short_code") or parsed.request_id
        reply = (f"✅ 已{'通过' if routed['action'] == 'approve' else '拒绝'}审批 "
                 f"{code}（{routed['status']}）")
        return reply, routed

    # 走完整对话管线（防火墙 + 语义缓存 + LLM + 持久化），会话按微信用户隔离
    session_id = f"wechat-{group_id or from_user}"
    resp = await chat(ChatRequest(message=content, session_id=session_id))
    result = {"kind": "chat", "cached": getattr(resp, "cached", False),
              "session_id": session_id, "success": resp.success}
    return resp.content, result


@app.post("/api/wechat/inbound", tags=["wechat"])
async def wechat_inbound(inbound: WeChatInbound):
    """接收外部微信 bridge 转发的消息：审批命令路由到 HITL，否则走对话管线。

    入参为归一化的 WeChatInbound（from_user/content/...）。返回 reply 文本，
    并在配置了 send_endpoint 时主动回推给 bridge。
    """
    settings = await _load_llm_settings()
    wcfg = getattr(settings, "wechat", None)
    if wcfg is not None and not getattr(wcfg, "enabled", False):
        raise HTTPException(status_code=403, detail="微信 bridge 未启用（symbio.yaml: wechat.enabled）")
    expected = getattr(wcfg, "inbound_token", "") if wcfg else ""
    if expected and inbound.token != expected:
        raise HTTPException(status_code=401, detail="无效的微信 inbound token")

    bridge = get_wechat_bridge()
    bridge.record_message("in", inbound.from_user, inbound.content)
    reply, result = await _wechat_dispatch(
        inbound.from_user, inbound.content, group_id=inbound.group_id, is_group=inbound.is_group,
    )
    bridge.record_message("out", inbound.from_user, reply, kind=result.get("kind", ""))

    # 出站回推（异步 bridge）；同步 bridge 用响应里的 reply
    delivery = await bridge.send(inbound.from_user, reply, is_group=inbound.is_group)

    out = {"ok": True, "result": result, "delivery": delivery.get("delivery_status")}
    if wcfg is None or getattr(wcfg, "reply_in_response", True):
        out["reply"] = reply
    return out


@app.post("/api/wechat/send", tags=["wechat"])
async def wechat_send(req: WeChatSendRequest):
    """主动通过微信 bridge 发送一条消息（测试 / Agent 主动通知用）。"""
    delivery = await get_wechat_bridge().send(req.to_user, req.content, is_group=req.is_group)
    return delivery


class WeChatLoginEvent(BaseModel):
    status: str                  # logged_out / waiting_scan / scanned / logged_in / failed
    qr: str = ""                 # 二维码内容（URL/字符串）
    qr_image: str = ""           # 二维码图片 data URL
    user: str = ""               # 绑定的微信账号
    token: str = ""


@app.post("/api/wechat/login/event", tags=["wechat"])
async def wechat_login_event(event: WeChatLoginEvent):
    """外部 bridge 推送扫码登录事件（二维码 / 已扫码 / 登录成功）。"""
    settings = await _load_llm_settings()
    wcfg = getattr(settings, "wechat", None)
    expected = getattr(wcfg, "inbound_token", "") if wcfg else ""
    if expected and event.token != expected:
        raise HTTPException(status_code=401, detail="无效的微信 inbound token")
    state = get_wechat_bridge().update_login(
        event.status, qr=event.qr, qr_image=event.qr_image, user=event.user,
    )
    return {"ok": True, "login": state}


@app.get("/api/wechat/login/status", tags=["wechat"])
async def wechat_login_status():
    """返回当前微信扫码绑定状态（供 Web UI 显示二维码与登录态）。"""
    settings = await _load_llm_settings()
    wcfg = getattr(settings, "wechat", None)
    state = get_wechat_bridge().login_state()
    state["enabled"] = bool(getattr(wcfg, "enabled", False)) if wcfg else False
    state["send_endpoint_configured"] = bool(getattr(wcfg, "send_endpoint", "")) if wcfg else False
    return state


@app.post("/api/wechat/login/start", tags=["wechat"])
async def wechat_login_start():
    """发起内置 iLink 扫码登录（clawbot）：拉取微信二维码并后台轮询登录态。

    返回登录态，前端拿到 qr 后渲染二维码，再轮询 /login/status 等待 logged_in。
    """
    bridge = get_wechat_bridge()
    # 注入消息处理器：登录后收到的微信消息走统一分流
    bridge.set_message_handler(_wechat_dispatch_reply)
    try:
        state = await bridge.start_ilink_login()
    except Exception as e:
        logger.error(f"发起微信扫码登录失败: {e}")
        raise HTTPException(status_code=502, detail=f"无法连接微信 iLink 服务: {e}")
    return {"ok": True, "login": state}


async def _wechat_dispatch_reply(from_user: str, content: str, is_group: bool) -> str:
    """供内置 iLink 收消息循环调用的消息处理器，返回回复文本。"""
    reply, _ = await _wechat_dispatch(from_user, content, is_group=is_group)
    return reply


@app.post("/api/wechat/logout", tags=["wechat"])
async def wechat_logout():
    """登出微信并停止后台收发任务。"""
    state = await get_wechat_bridge().logout()
    return {"ok": True, "login": state}


@app.get("/api/wechat/messages", tags=["wechat"])
async def wechat_messages(limit: int = 40):
    """返回最近的收/发消息流（供 Web UI 实时展示收发是否通畅）。"""
    msgs = get_wechat_bridge().recent_messages(limit)
    return {"messages": msgs, "total": len(msgs)}


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


@app.get("/api/hitl/action")
async def hitl_action(
    request_id: str,
    action: str,
    token: str,
    approver_id: str = "im-card",
    comment: str = "",
):
    """Approve or reject a request from an external approval card button."""
    token_request_id = verify_approval_token(token)
    if token_request_id is None:
        raise HTTPException(status_code=401, detail="Invalid approval token")

    resolved_request_id = await _resolve_hitl_request_id(request_id)
    if token_request_id != resolved_request_id:
        raise HTTPException(status_code=401, detail="Approval token does not match request")

    gateway = _get_hitl_gateway()
    normalized_action = action.lower().strip()
    try:
        if normalized_action in {"approve", "approved", "yes", "ok"}:
            result = await gateway.approve(resolved_request_id, approver_id=approver_id, comment=comment)
            resumed_result = None
            if result.status == ApprovalStatus.APPROVED:
                resumed_result = await _try_resume_hitl_task(resolved_request_id)
            return {"request": _hitl_request_payload(result), "resumed_result": resumed_result}
        if normalized_action in {"reject", "rejected", "no"}:
            result = await gateway.reject(resolved_request_id, approver_id=approver_id, comment=comment)
            return {"request": _hitl_request_payload(result), "resumed_result": None}
    except KeyError:
        raise HTTPException(status_code=404, detail="Approval request is missing or already handled")

    raise HTTPException(status_code=400, detail="Unknown approval action")


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
            cache_hit = False

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

                # 安全防火墙：Prompt Injection 三层检测
                guard = get_chat_guard()
                verdict = guard.inspect(content, session_id=session_id)
                if not verdict["allowed"]:
                    block_msg = f"⛔ 该消息被安全防火墙拦截：{verdict['reason']}"
                    await db.create_message(
                        f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant",
                        block_msg, time.strftime("%Y-%m-%dT%H:%M:%S"), 0,
                    )
                    await websocket.send_text(json.dumps({
                        "type": "blocked",
                        "content": block_msg,
                        "threat_level": verdict["threat_level"],
                        "attack_type": verdict["attack_type"],
                    }))
                    await websocket.send_text(json.dumps({
                        "type": "done", "content": block_msg, "session_id": session_id,
                        "blocked": True,
                        "token_usage": {"input": 0, "output": 0, "total": 0},
                    }))
                    continue

                # 构建含历史对话的消息列表
                messages = await _build_history_messages(db, session_id)
                logger.info(f"WebSocket 对话 - 会话: {session_id}, 历史消息数: {len(messages)}")

                # 成本优化管线：语义缓存查询（命中则分块流式回放缓存回答）
                pipeline = get_chat_pipeline()
                ctx_hash = pipeline.context_hash(messages[:-1] if messages else [])
                cached = await pipeline.lookup_cache(content, model=model, context_hash=ctx_hash)
                if cached:
                    cached_text = cached["content"]
                    chunk_size = 48
                    for i in range(0, len(cached_text), chunk_size):
                        full_response += cached_text[i:i + chunk_size]
                        await websocket.send_text(json.dumps({
                            "type": "token",
                            "content": cached_text[i:i + chunk_size],
                        }))
                        await asyncio.sleep(0.01)
                    token_input = 0
                    token_output = 0
                    cache_hit = True
                    logger.info(f"语义缓存命中，零 Token 流式回放 - 会话: {session_id}")
                else:
                    # 成本优化管线：上下文剪枝
                    messages, _prune_info = pipeline.prune_history(messages)

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

                    # 成本优化管线：记录用量 + 回写语义缓存
                    await pipeline.record_usage(
                        session_id=session_id, model=model,
                        input_tokens=token_input, output_tokens=token_output,
                    )
                    await pipeline.store_cache(content, full_response, model=model, context_hash=ctx_hash)

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
                "cached": cache_hit,
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


# ============ 交互式终端 WebSocket ============

def _terminal_client_allowed(websocket: WebSocket) -> bool:
    """终端 WS 鉴权：默认只允许本机（环回）连接。

    终端能在本机跑任意命令，blast radius 远大于聊天。默认仅 127.0.0.1/::1；
    如需对外开放，设 SYMBIO_TERMINAL_ALLOW_REMOTE=1（需自担风险）。
    """
    if os.environ.get("SYMBIO_TERMINAL_ALLOW_REMOTE") == "1":
        return True
    client = websocket.client
    host = client.host if client else ""
    return host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    """在网页里起一个真 PTY 终端跑 claude-code / codex / shell。

    协议（JSON 文本帧）：
      客户端→服务端: {"type":"start","kind":"claude-code|codex|shell","cwd":".","cols":100,"rows":30}
                     {"type":"input","data":"..."}         键盘输入
                     {"type":"resize","cols":120,"rows":40} 尺寸变化
      服务端→客户端: {"type":"output","data":"..."}         PTY 输出（含 ANSI）
                     {"type":"exit"}                        子进程结束
                     {"type":"error","message":"..."}       出错
    """
    from symbio.tools.terminal_session import TerminalSession, resolve_terminal_command

    await websocket.accept()
    if not _terminal_client_allowed(websocket):
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": "终端仅允许本机访问（设 SYMBIO_TERMINAL_ALLOW_REMOTE=1 可放开，风险自担）",
        }))
        await websocket.close()
        return

    session: "TerminalSession | None" = None
    loop = asyncio.get_event_loop()
    out_queue: "asyncio.Queue[str | None]" = asyncio.Queue()

    async def pump_output() -> None:
        """把 PTY 输出队列灌回 WebSocket，直到 EOF(None)。"""
        while True:
            chunk = await out_queue.get()
            if chunk is None:
                await websocket.send_text(json.dumps({"type": "exit"}))
                return
            await websocket.send_text(json.dumps({"type": "output", "data": chunk}))

    pump_task: "asyncio.Task | None" = None
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")

            if mtype == "start":
                if session is not None:
                    continue  # 已启动，忽略重复 start
                kind = msg.get("kind", "shell")
                cwd = msg.get("cwd") or None
                resume_id = msg.get("resume_id", "")
                cols = int(msg.get("cols", 100))
                rows = int(msg.get("rows", 30))
                try:
                    command = resolve_terminal_command(kind, resume_id=resume_id)
                except (ValueError, FileNotFoundError) as exc:
                    await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
                    continue
                session = TerminalSession(command, cwd=cwd, cols=cols, rows=rows)
                try:
                    session.start()
                except Exception as exc:
                    await websocket.send_text(json.dumps({"type": "error", "message": f"终端启动失败：{exc}"}))
                    session = None
                    continue
                session.start_reader(loop, out_queue)
                pump_task = asyncio.create_task(pump_output())

            elif mtype == "input" and session is not None:
                session.write(msg.get("data", ""))

            elif mtype == "resize" and session is not None:
                session.resize(int(msg.get("cols", 100)), int(msg.get("rows", 30)))

    except WebSocketDisconnect:
        logger.info("终端 WebSocket 断开")
    except Exception as e:
        logger.error(f"终端 WebSocket 错误: {e}")
    finally:
        if session is not None:
            session.terminate()
        if pump_task is not None:
            pump_task.cancel()
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


# ============ A2A 协议 ============

from symbio.interfaces.a2a import (
    A2AAgentCard,
    A2AMessage,
    A2AMessageRole,
    A2ASession,
    A2ASessionManager,
    A2ATask,
    A2ATaskResult,
    A2ATaskState,
    A2ATextPart,
    build_agent_card,
    fetch_remote_agent_card,
    fetch_remote_task,
    send_task_to_agent,
)

_A2A_PERSIST_PATH = Path("data") / "a2a_state.json"


def _get_a2a_manager() -> A2ASessionManager:
    mgr = getattr(app.state, "a2a_manager", None)
    if mgr is None:
        mgr = A2ASessionManager(persist_path=_A2A_PERSIST_PATH)
        app.state.a2a_manager = mgr
    return mgr


def _server_base_url(request) -> str:
    """Best-effort base URL of this server (for AgentCard)."""
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return "http://localhost:9090"


def _build_self_agent_card(request):
    """构建本实例的动态 AgentCard：真实包版本 + 能力账本快照。

    让 /.well-known/agent.json 反映真实状态，而非硬编码死数据。
    """
    try:
        from symbio import __version__ as version
    except Exception:
        version = None

    metadata: dict = {}
    try:
        from symbio.capabilities import get_capability_report
        report = get_capability_report()
        metadata = {
            "capability_summary": report["summary"],
            "implemented_capabilities": [
                item["id"] for item in report["items"]
                if item["status"] == "implemented"
            ],
        }
    except Exception:
        metadata = {}

    return build_agent_card(
        base_url=_server_base_url(request),
        version=version,
        metadata=metadata or None,
        authentication=(
            {"schemes": ["bearer"]} if _a2a_expected_token() else {"schemes": ["none"]}
        ),
    )


# -- AgentCard (self-description) ------------------------------------------

@app.get("/.well-known/agent.json", tags=["a2a"])
async def agent_card(request: Request):
    """A2A AgentCard — describes this agent's capabilities to external agents."""
    return _build_self_agent_card(request).model_dump(mode="json")


@app.get("/api/a2a/card", tags=["a2a"])
async def get_own_agent_card(request: Request):
    """Return this agent's own A2A card (same as /.well-known/agent.json)."""
    return _build_self_agent_card(request).model_dump(mode="json")


# -- Inbound tasks (we receive from external agents) -----------------------

class A2AInboundTaskRequest(BaseModel):
    id: Optional[str] = None
    sessionId: Optional[str] = None
    message: dict  # raw dict; validated inside handler
    # A2A pushNotificationConfig：{"url": "https://..."}，状态变更时回调
    pushNotification: Optional[dict] = None


def _a2a_expected_token() -> str:
    """A2A 鉴权 token：app.state 优先，其次环境变量；空串表示开放访问。"""
    configured = getattr(app.state, "a2a_auth_token", None)
    if configured is not None:
        return str(configured)
    return os.environ.get("SYMBIO_A2A_TOKEN", "")


def _check_a2a_auth(request: Request) -> None:
    """校验 Bearer token；未配置 token 时开放（AgentCard 会如实声明 schemes）。"""
    expected = _a2a_expected_token()
    if not expected:
        return
    auth_header = request.headers.get("Authorization", "")
    if auth_header != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid or missing A2A bearer token")


@app.post("/api/a2a/tasks", tags=["a2a"])
async def receive_a2a_task(payload: A2AInboundTaskRequest, request: Request):
    """Receive a task from an external A2A-compatible agent."""
    _check_a2a_auth(request)
    mgr = _get_a2a_manager()

    # Normalise message
    raw_msg = payload.message
    role_str = raw_msg.get("role", "user")
    parts = raw_msg.get("parts", [])
    if not parts:
        parts = [{"type": "text", "text": raw_msg.get("text", "")}]

    msg = A2AMessage(
        role=A2AMessageRole(role_str),
        parts=[A2ATextPart(**p) for p in parts],
        messageId=raw_msg.get("messageId", f"msg-{uuid.uuid4().hex[:12]}"),
    )

    task = A2ATask(
        id=payload.id or f"a2a-task-{uuid.uuid4().hex[:16]}",
        sessionId=payload.sessionId,
        message=msg,
        origin="inbound",
    )

    task = await mgr.receive_task(task)

    # 推送通知注册（A2A pushNotificationConfig：状态变更时 POST 到 webhook）
    push_url = (payload.pushNotification or {}).get("url", "")
    if push_url:
        mgr.set_push_config(task.id, push_url)

    # Fire-and-forget: process the task via Symbio's chat pipeline
    asyncio.create_task(_process_inbound_a2a_task(task.id, msg.text_content))

    return {
        "id": task.id,
        "sessionId": task.sessionId,
        "state": task.state,
        "created_at": task.created_at,
    }


async def _a2a_default_executor(prompt: str) -> str:
    """默认入站任务执行器：走 Symbio 的 LLM 后端。"""
    settings = await _load_llm_settings()
    import anthropic

    if not settings.model.anthropic_api_key:
        raise ValueError("No API key configured")

    client = anthropic.AsyncAnthropic(
        api_key=settings.model.anthropic_api_key,
        base_url=settings.model.anthropic_base_url,
    )
    resp = await client.messages.create(
        model=settings.model.model_medium,
        max_tokens=2048,
        system=SYMBIO_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text if resp.content else ""


async def _a2a_orchestrator_executor(prompt: str) -> str:
    """编排器执行器：入站 A2A 任务走 Orchestrator 完整调度管线。

    意图解析 → 复杂度评估 → 模型路由 → Planner/Reviewer → Agent 执行，
    与 Web/CLI 消息同一条链路，而不是旁路裸调 LLM。
    """
    from symbio.utils.types import Message, MessageSource

    orchestrator = getattr(app.state, "orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("Orchestrator is not initialized")

    message = Message(
        source=MessageSource.A2A,
        user_id="a2a-remote-agent",
        content=prompt,
        session_id=f"a2a-{uuid.uuid4().hex[:12]}",
        metadata={"channel": "a2a"},
    )
    result = await orchestrator.process(message)
    if not result.success and not result.content:
        raise RuntimeError(result.error or "Orchestrator returned empty failure")
    return result.content


def _resolve_a2a_executor() -> tuple[Any, str]:
    """选择入站任务执行器：注入 > 编排器 > 裸 LLM。返回 (executor, mode)。"""
    injected = getattr(app.state, "a2a_task_executor", None)
    if injected is not None:
        return injected, "injected"
    if getattr(app.state, "orchestrator", None) is not None:
        return _a2a_orchestrator_executor, "orchestrator"
    return _a2a_default_executor, "llm"


async def _process_inbound_a2a_task(task_id: str, prompt: str) -> None:
    """Process an inbound A2A task and update the task state.

    执行器可注入：设置 app.state.a2a_task_executor（async 或 sync 可调用，
    prompt -> str）即可改走自定义后端；未注入且编排器在场时走 Orchestrator
    完整调度管线，否则回退裸 LLM。
    无论成功失败都把任务推进到 COMPLETED，绝不让对端无限等待。
    """
    mgr = _get_a2a_manager()
    await mgr.update_task_state(task_id, A2ATaskState.WORKING)

    executor, executor_mode = _resolve_a2a_executor()
    response_text = ""
    try:
        result_value = executor(prompt)
        if inspect.isawaitable(result_value):
            result_value = await result_value
        response_text = str(result_value)
    except Exception as exc:
        response_text = f"[Symbio error: {exc}]"

    result = A2ATaskResult(
        state=A2ATaskState.COMPLETED,
        message=A2AMessage.text(A2AMessageRole.AGENT, response_text),
        metadata={"executor": executor_mode},
    )
    await mgr.update_task_state(task_id, A2ATaskState.COMPLETED, result=result)


@app.get("/api/a2a/tasks/{task_id}", tags=["a2a"])
async def get_a2a_task(task_id: str):
    mgr = _get_a2a_manager()
    task = await mgr.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="A2A task not found")
    return task.model_dump(mode="json")


@app.get("/api/a2a/tasks/{task_id}/stream", tags=["a2a"])
async def stream_a2a_task(task_id: str, timeout: int = 300):
    """SSE 流式订阅任务状态（A2A tasks/sendSubscribe 语义）。

    先发当前快照，之后每次状态变更推一个 event；任务到达终态或超时后关闭。
    事件格式：`event: task-update\\ndata: {...task json...}\\n\\n`
    """
    mgr = _get_a2a_manager()
    task = await mgr.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="A2A task not found")

    terminal_states = {
        A2ATaskState.COMPLETED.value,
        A2ATaskState.FAILED.value,
        A2ATaskState.CANCELLED.value,
    }

    async def event_stream():
        queue = mgr.subscribe(task_id)
        try:
            snapshot = (await mgr.get_task(task_id)).model_dump(mode="json")
            yield f"event: task-update\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            if snapshot["state"] in terminal_states:
                return
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    yield "event: timeout\ndata: {}\n\n"
                    return
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    yield "event: timeout\ndata: {}\n\n"
                    return
                yield f"event: task-update\ndata: {json.dumps(update, ensure_ascii=False)}\n\n"
                if update.get("state") in terminal_states:
                    return
        finally:
            mgr.unsubscribe(task_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/a2a/tasks", tags=["a2a"])
async def list_a2a_tasks(origin: Optional[str] = None, limit: int = 50):
    mgr = _get_a2a_manager()
    tasks = await mgr.list_tasks(origin=origin, limit=limit)
    return {"tasks": [t.model_dump(mode="json") for t in tasks], "total": len(tasks)}


# -- Outbound sessions (we initiate to remote agents) ---------------------

class A2ACreateSessionRequest(BaseModel):
    remote_url: str
    remote_name: str = "remote-agent"
    initial_message: Optional[str] = None
    metadata: dict = {}


class A2ASendMessageRequest(BaseModel):
    message: str


@app.post("/api/a2a/sessions", tags=["a2a"])
async def create_a2a_session(req: A2ACreateSessionRequest):
    """Create an outbound A2A session to a remote agent (optionally sends first message)."""
    mgr = _get_a2a_manager()

    # Try to discover the remote agent's card first
    card = await fetch_remote_agent_card(req.remote_url, timeout=5)
    remote_name = card.get("name", req.remote_name) if card else req.remote_name

    session = await mgr.create_session(
        remote_url=req.remote_url,
        remote_name=remote_name,
        metadata=req.metadata,
    )

    result_payload: dict = {
        "session": session.model_dump(mode="json"),
        "remote_card": card,
    }

    # Optionally send the first message
    if req.initial_message:
        try:
            send_result = await send_task_to_agent(
                req.remote_url, req.initial_message, session_id=session.id
            )
            msg = A2AMessage.text(A2AMessageRole.USER, req.initial_message)
            await mgr.append_session_message(session.id, msg, task_id=send_result.get("task_id"))
            await mgr.update_session_state(session.id, A2ATaskState.WORKING)
            result_payload["send_result"] = send_result
        except Exception as exc:
            result_payload["send_error"] = str(exc)
            await mgr.update_session_state(session.id, A2ATaskState.FAILED)

    return result_payload


@app.get("/api/a2a/sessions", tags=["a2a"])
async def list_a2a_sessions(limit: int = 50):
    mgr = _get_a2a_manager()
    sessions = await mgr.list_sessions(limit=limit)
    return {"sessions": [s.model_dump(mode="json") for s in sessions], "total": len(sessions)}


@app.get("/api/a2a/sessions/{session_id}", tags=["a2a"])
async def get_a2a_session(session_id: str):
    mgr = _get_a2a_manager()
    session = await mgr.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="A2A session not found")
    return session.model_dump(mode="json")


@app.post("/api/a2a/sessions/{session_id}/send", tags=["a2a"])
async def send_a2a_session_message(session_id: str, req: A2ASendMessageRequest):
    """Send a follow-up message in an existing outbound A2A session."""
    mgr = _get_a2a_manager()
    session = await mgr.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="A2A session not found")

    try:
        send_result = await send_task_to_agent(
            session.remote_url, req.message, session_id=session_id
        )
        msg = A2AMessage.text(A2AMessageRole.USER, req.message)
        await mgr.append_session_message(session_id, msg, task_id=send_result.get("task_id"))
        await mgr.update_session_state(session_id, A2ATaskState.WORKING)
        return {"session_id": session_id, "send_result": send_result}
    except Exception as exc:
        await mgr.update_session_state(session_id, A2ATaskState.FAILED)
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/a2a/sessions/{session_id}/poll", tags=["a2a"])
async def poll_a2a_session(session_id: str):
    """Pull remote results for this session's outbound tasks（闭合出站往返）。

    对会话里每个已发出的任务查询远端状态；远端已 COMPLETED 且回复尚未入会话的，
    把 agent 回复追加进会话消息。全部完成时会话状态推进到 COMPLETED。
    """
    mgr = _get_a2a_manager()
    session = await mgr.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="A2A session not found")

    seen_message_ids = {m.messageId for m in session.messages}
    updates: list[dict] = []
    all_completed = bool(session.task_ids)

    for task_id in session.task_ids:
        remote = await fetch_remote_task(session.remote_url, task_id)
        if remote is None:
            all_completed = False
            updates.append({"task_id": task_id, "state": "unreachable"})
            continue

        state = remote.get("state", "unknown")
        if state != A2ATaskState.COMPLETED.value:
            all_completed = False
            updates.append({"task_id": task_id, "state": state})
            continue

        reply_text = ""
        result = remote.get("result") or {}
        reply_msg = result.get("message") or {}
        reply_id = reply_msg.get("messageId", "")
        for part in reply_msg.get("parts", []):
            if part.get("type") == "text":
                reply_text += part.get("text", "")

        appended = False
        if reply_text and reply_id and reply_id not in seen_message_ids:
            msg = A2AMessage(
                role=A2AMessageRole.AGENT,
                parts=[A2ATextPart(text=reply_text)],
                messageId=reply_id,
            )
            await mgr.append_session_message(session_id, msg, task_id=task_id)
            seen_message_ids.add(reply_id)
            appended = True

        updates.append({
            "task_id": task_id,
            "state": state,
            "reply": reply_text or None,
            "appended": appended,
        })

    if all_completed:
        await mgr.update_session_state(session_id, A2ATaskState.COMPLETED)

    session = await mgr.get_session(session_id)
    return {
        "session": session.model_dump(mode="json"),
        "updates": updates,
        "all_completed": all_completed,
    }


# -- Remote agent card probe -----------------------------------------------

@app.get("/api/a2a/probe", tags=["a2a"])
async def probe_remote_agent(url: str):
    """Fetch and return the AgentCard from a remote agent URL."""
    card = await fetch_remote_agent_card(url, timeout=8)
    if card is None:
        raise HTTPException(status_code=502, detail=f"Could not reach agent card at {url}")
    return card


# ============ HITL 渠道管理 API ============

class HITLChannelCreate(BaseModel):
    platform: str
    endpoint: str = ""
    chat_id: str = ""
    chat_type: str = "group"
    access_token: str = ""
    secret: str = ""
    enabled: bool = True


_hitl_channels_persist = Path("data") / "hitl_channels.json"


def _load_custom_channels() -> list[dict]:
    if _hitl_channels_persist.exists():
        try:
            return json.loads(_hitl_channels_persist.read_text())
        except Exception:
            pass
    return []


def _save_custom_channels(channels: list[dict]) -> None:
    _hitl_channels_persist.parent.mkdir(parents=True, exist_ok=True)
    _hitl_channels_persist.write_text(json.dumps(channels, indent=2))


@app.post("/api/hitl/channels", tags=["hitl"])
async def add_hitl_channel(req: HITLChannelCreate):
    """添加一个 HITL 通知渠道（持久化保存）。"""
    from symbio.core.hitl_notifier import HITLNotificationTarget, PLATFORM_LABELS
    channels = _load_custom_channels()
    new_ch = req.model_dump()
    new_ch["id"] = f"ch-{uuid.uuid4().hex[:12]}"
    new_ch["display_name"] = PLATFORM_LABELS.get(req.platform.lower(), req.platform)
    channels.append(new_ch)
    _save_custom_channels(channels)
    # Reload notifier
    notifier = _get_hitl_notifier()
    notifier.targets.append(HITLNotificationTarget(**{k: v for k, v in new_ch.items() if k not in {"id", "display_name"}}))
    safe = {k: v for k, v in new_ch.items() if k != "access_token"}
    safe["has_access_token"] = bool(new_ch.get("access_token"))
    return {"channel": safe}


@app.delete("/api/hitl/channels/{channel_id}", tags=["hitl"])
async def delete_hitl_channel(channel_id: str):
    """删除已保存的通知渠道。"""
    channels = _load_custom_channels()
    new_list = [c for c in channels if c.get("id") != channel_id]
    if len(new_list) == len(channels):
        raise HTTPException(status_code=404, detail="Channel not found")
    _save_custom_channels(new_list)
    # Reload notifier targets
    from symbio.core.hitl_notifier import HITLNotifier as _HN
    app.state.hitl_notifier = _HN.from_settings()
    return {"deleted": channel_id}


@app.get("/api/hitl/channels/list", tags=["hitl"])
async def list_hitl_channels():
    """列出所有已配置的通知渠道（包括 yaml 和手动添加的）。"""
    from symbio.core.hitl_notifier import PLATFORM_LABELS
    notifier = _get_hitl_notifier()
    all_targets = notifier.targets
    saved = _load_custom_channels()
    saved_by_platform_endpoint = {
        (c.get("platform", ""), c.get("endpoint", "")): c.get("id")
        for c in saved
    }
    result = []
    for t in all_targets:
        ch_id = saved_by_platform_endpoint.get((t.platform, t.endpoint), None)
        result.append({
            "id": ch_id or f"built-in-{t.platform}",
            "platform": t.platform,
            "display_name": PLATFORM_LABELS.get(t.platform.lower(), t.platform),
            "endpoint": t.endpoint,
            "chat_id": t.chat_id,
            "chat_type": t.chat_type,
            "enabled": t.enabled,
            "has_access_token": bool(t.access_token),
            "has_secret": bool(t.secret),
            "deletable": ch_id is not None,
        })
    return {"channels": result, "total": len(result)}


@app.post("/api/hitl/channels/test", tags=["hitl"])
async def test_hitl_channel(req: HITLChannelCreate):
    """向指定渠道发送一条测试通知。"""
    from symbio.core.hitl_notifier import HITLNotifier, HITLNotificationTarget
    from symbio.core.hitl_gateway import ApprovalRequest, RiskLevel

    test_request = ApprovalRequest(
        request_id=f"test-{uuid.uuid4().hex[:8]}",
        task_id="test-task",
        risk_level=RiskLevel.LOW,
        action="test notification",
        reason="This is a test message from Symbio.",
        impact_scope="none",
    )
    target = HITLNotificationTarget(**req.model_dump())
    notifier = HITLNotifier(targets=[target], callback_base_url="")
    results = await notifier.notify(test_request)
    r = results[0] if results else None
    return {
        "success": r.success if r else False,
        "delivery_status": r.delivery_status if r else "not_sent",
        "error": r.error if r else "No target",
        "status_code": r.status_code if r else 0,
    }


# ============ HITL 审批超时策略 ============

class HITLTimeoutPolicy(BaseModel):
    request_id: str
    action: str = "auto_reject"  # auto_reject | auto_approve | escalate
    comment: str = "Auto-handled: approval timeout"


def _normalize_timeout_action(action: str) -> str:
    """把外部传入的动作名归一化为网关识别的 reject/approve/escalate。"""
    a = (action or "").lower().strip()
    if a in ("auto_approve", "approve"):
        return "approve"
    if a in ("escalate", "transfer"):
        return "escalate"
    return "reject"


async def _apply_timeout_action(gateway, request_id: str, action: str, comment: str):
    """对单个请求应用超时动作，返回 (updated_request, resume_result)。"""
    norm = _normalize_timeout_action(action)
    resume_result = None
    if norm == "approve":
        updated = await gateway.approve(request_id, approver_id="timeout-policy", comment=comment)
        resume_result = await _try_resume_hitl_task(request_id)
    elif norm == "escalate":
        settings = await _load_llm_settings()
        target = getattr(settings.hitl, "escalation_target", "") if hasattr(settings, "hitl") else ""
        updated = await gateway.escalate(request_id, escalation_target=target, comment=comment)
        # 升级后若仍 pending，尝试重新发送审批通知到（可能更高优先级的）渠道
        from symbio.core.hitl_gateway import ApprovalStatus
        if updated.status == ApprovalStatus.PENDING:
            try:
                updated.metadata["escalated"] = True
                await _notify_hitl_request(updated)
            except Exception as e:
                logger.warning(f"升级通知发送失败: {e}")
    else:
        updated = await gateway.reject(request_id, approver_id="timeout-policy", comment=comment)
    return updated, resume_result


@app.post("/api/hitl/{request_id}/timeout-action", tags=["hitl"])
async def hitl_timeout_action(request_id: str, policy: HITLTimeoutPolicy):
    """手动触发超时处理（通常由后台任务调用）。支持 auto_reject / auto_approve / escalate。"""
    gateway = _get_hitl_gateway()
    request = await gateway.get_request(request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    from symbio.core.hitl_gateway import ApprovalStatus
    if request.status != ApprovalStatus.PENDING:
        return {"skipped": True, "reason": f"Request is {request.status.value}, not pending"}

    updated, resume_result = await _apply_timeout_action(
        gateway, request_id, policy.action, policy.comment,
    )

    return {
        "request_id": request_id,
        "action": _normalize_timeout_action(policy.action),
        "status": updated.status.value if updated else "unknown",
        "escalation_level": getattr(updated, "escalation_level", 0),
        "resume_result": resume_result,
    }


@app.get("/api/hitl/timeout/check", tags=["hitl"])
async def check_hitl_timeouts(max_age_seconds: int = 300, action: str = ""):
    """检查超时的审批请求并批量处理（可由外部 cron 或前端轮询调用）。

    action 留空时使用 symbio.yaml 中配置的默认超时策略 hitl.timeout_action。
    """
    gateway = _get_hitl_gateway()
    import time as _time

    if not action:
        settings = await _load_llm_settings()
        action = getattr(settings.hitl, "timeout_action", "reject") if hasattr(settings, "hitl") else "reject"

    pending = await gateway.list_requests(status_filter="pending")
    now = _time.time()
    handled = []

    for req in pending:
        created_ts = None
        try:
            from datetime import datetime
            created_ts = datetime.fromisoformat(req.created_at.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        age = now - created_ts
        if age >= max_age_seconds:
            comment = f"Auto-handled: timed out after {int(age)}s"
            updated, _ = await _apply_timeout_action(gateway, req.request_id, action, comment)
            handled.append({
                "request_id": req.request_id,
                "age_seconds": int(age),
                "action": _normalize_timeout_action(action),
                "status": updated.status.value if updated else "unknown",
            })

    return {"checked": len(pending), "handled": len(handled), "action": _normalize_timeout_action(action), "items": handled}


@app.get("/api/hitl/timeout/policy", tags=["hitl"])
async def get_hitl_timeout_policy():
    """读取默认超时策略配置。"""
    settings = await _load_llm_settings()
    hitl = settings.hitl
    return {
        "timeout_action": getattr(hitl, "timeout_action", "reject"),
        "escalation_target": getattr(hitl, "escalation_target", ""),
        "max_escalations": getattr(hitl, "max_escalations", 1),
        "approval_timeout": getattr(hitl, "approval_timeout", 300),
    }


class HITLTimeoutPolicyConfig(BaseModel):
    timeout_action: Optional[str] = None
    escalation_target: Optional[str] = None
    max_escalations: Optional[int] = None
    approval_timeout: Optional[int] = None


@app.post("/api/hitl/timeout/policy", tags=["hitl"])
async def set_hitl_timeout_policy(update: HITLTimeoutPolicyConfig):
    """更新默认超时策略并写入 symbio.yaml。"""
    from symbio.config.settings import HITLConfig, Settings

    config_path = Path("symbio.yaml")
    settings = Settings.from_yaml(config_path) if config_path.exists() else Settings()
    if getattr(settings, "hitl", None) is None:
        settings.hitl = HITLConfig()

    if update.timeout_action is not None:
        if update.timeout_action not in ("reject", "approve", "escalate"):
            raise HTTPException(status_code=400, detail="timeout_action 必须是 reject/approve/escalate")
        settings.hitl.timeout_action = update.timeout_action
    if update.escalation_target is not None:
        settings.hitl.escalation_target = update.escalation_target
    if update.max_escalations is not None:
        if update.max_escalations < 0:
            raise HTTPException(status_code=400, detail="max_escalations 不能为负")
        settings.hitl.max_escalations = update.max_escalations
    if update.approval_timeout is not None:
        settings.hitl.approval_timeout = update.approval_timeout

    settings.to_yaml(config_path)
    from symbio.config.settings import get_settings
    get_settings.cache_clear()
    return await get_hitl_timeout_policy()


# ============ Computer Use API ============

class ComputerUseSessionCreate(BaseModel):
    start_url: str = ""
    headless: bool = True


class ComputerUseAction(BaseModel):
    action: str
    params: dict = {}


class ComputerUsePlanRequest(BaseModel):
    goal: str
    auto_execute: bool = True
    use_llm: bool = False


@app.post("/api/computer-use/sessions", tags=["computer-use"])
async def create_computer_use_session(payload: ComputerUseSessionCreate):
    """创建一个 Computer Use 浏览器会话。"""
    from symbio.tools.computer_use import get_computer_use_manager
    session = get_computer_use_manager().create_session(
        start_url=payload.start_url, headless=payload.headless,
    )
    return session.to_dict(include_steps=False)


@app.get("/api/computer-use/sessions", tags=["computer-use"])
async def list_computer_use_sessions():
    """列出所有 Computer Use 会话。"""
    from symbio.tools.computer_use import get_computer_use_manager
    sessions = get_computer_use_manager().list_sessions()
    return {"sessions": sessions, "total": len(sessions),
            "playwright_available": not (sessions[0]["dry_run"] if sessions else _computer_use_dry_run())}


def _computer_use_dry_run() -> bool:
    from symbio.tools.computer_use import _playwright_available
    return not _playwright_available()


@app.get("/api/computer-use/sessions/{session_id}", tags=["computer-use"])
async def get_computer_use_session(session_id: str):
    """获取会话详情与完整审计轨迹。"""
    from symbio.tools.computer_use import get_computer_use_manager
    session = get_computer_use_manager().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session.to_dict(include_steps=True)


@app.post("/api/computer-use/sessions/{session_id}/act", tags=["computer-use"])
async def computer_use_act(session_id: str, payload: ComputerUseAction):
    """在会话中执行一个动作（navigate/screenshot/click/type/scroll/extract_text/wait）。"""
    from symbio.tools.computer_use import get_computer_use_manager
    session = get_computer_use_manager().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    step = await session.act(payload.action, payload.params)
    return {"step": step, "step_count": len(session.steps)}


@app.post("/api/computer-use/sessions/{session_id}/plan", tags=["computer-use"])
async def computer_use_plan(session_id: str, payload: ComputerUsePlanRequest):
    """规划朝目标的下一步动作；use_llm 为真时用 LLM 视觉/文本规划，否则启发式。
    auto_execute 为真时直接执行。"""
    from symbio.tools.computer_use import get_computer_use_manager, ActionPlanner, LLMActionPlanner
    session = get_computer_use_manager().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if payload.use_llm:
        plan = await LLMActionPlanner().plan(payload.goal, session)
    else:
        plan = ActionPlanner.plan(payload.goal, session)
        plan.setdefault("planner", "heuristic")
    executed = None
    if payload.auto_execute:
        executed = await session.act(plan["action"], plan["params"])
    return {"plan": plan, "executed": executed, "step_count": len(session.steps)}


@app.post("/api/computer-use/sessions/{session_id}/replay", tags=["computer-use"])
async def computer_use_replay(session_id: str):
    """回放会话已记录的动作轨迹。"""
    from symbio.tools.computer_use import get_computer_use_manager
    session = get_computer_use_manager().get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return await session.replay()


@app.delete("/api/computer-use/sessions/{session_id}", tags=["computer-use"])
async def close_computer_use_session(session_id: str):
    """关闭会话并持久化审计。"""
    from symbio.tools.computer_use import get_computer_use_manager
    ok = await get_computer_use_manager().close_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"closed": True, "session_id": session_id}


# ============ MCP 工具网关 API ============

class MCPServerAdd(BaseModel):
    name: str
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    description: str = ""


_mcp_servers_persist = Path("data") / "mcp_servers.json"


def _load_mcp_servers() -> list[dict]:
    if _mcp_servers_persist.exists():
        try:
            return json.loads(_mcp_servers_persist.read_text())
        except Exception:
            pass
    return []


def _save_mcp_servers(servers: list[dict]) -> None:
    _mcp_servers_persist.parent.mkdir(parents=True, exist_ok=True)
    _mcp_servers_persist.write_text(json.dumps(servers, indent=2))


@app.get("/api/mcp/servers", tags=["mcp"])
async def list_mcp_servers():
    """列出所有已配置的 MCP 服务器。"""
    servers = _load_mcp_servers()
    # Also try to read from symbio.yaml mcp_servers section
    try:
        settings = await _load_llm_settings()
        yaml_mcp = getattr(settings, "mcp_servers", None) or {}
        for name, cfg in yaml_mcp.items():
            if isinstance(cfg, dict) and not any(s.get("name") == name for s in servers):
                servers.append({"name": name, "command": cfg.get("command", ""), "args": cfg.get("args", []),
                                 "env": cfg.get("env", {}), "source": "yaml"})
    except Exception:
        pass
    return {"servers": servers, "total": len(servers)}


@app.post("/api/mcp/servers", tags=["mcp"])
async def add_mcp_server(req: MCPServerAdd):
    """添加一个 MCP 服务器配置。"""
    servers = _load_mcp_servers()
    new_srv = req.model_dump()
    new_srv["id"] = f"mcp-{uuid.uuid4().hex[:12]}"
    new_srv["source"] = "manual"
    servers.append(new_srv)
    _save_mcp_servers(servers)
    return {"server": new_srv}


@app.delete("/api/mcp/servers/{server_id}", tags=["mcp"])
async def delete_mcp_server(server_id: str):
    servers = _load_mcp_servers()
    new_list = [s for s in servers if s.get("id") != server_id]
    if len(new_list) == len(servers):
        raise HTTPException(status_code=404, detail="MCP server not found")
    _save_mcp_servers(new_list)
    return {"deleted": server_id}


def _find_mcp_server(server_id: str) -> dict:
    srv = next((s for s in _load_mcp_servers() if s.get("id") == server_id), None)
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return srv


@app.post("/api/mcp/servers/{server_id}/tools", tags=["mcp"])
async def probe_mcp_server_tools(server_id: str):
    """连接指定 MCP 服务器（连接池复用），返回可用工具列表与声明的能力。"""
    srv = _find_mcp_server(server_id)
    try:
        from symbio.tools.mcp import get_mcp_pool
        cmd = [srv["command"]] + srv.get("args", [])
        client = await get_mcp_pool().get_client(srv["name"], cmd, env=srv.get("env") or None)
        tools = await client.list_tools()
        return {
            "server_id": server_id,
            "tools": [{"name": t.name, "description": t.description} for t in tools],
            "total": len(tools),
            "capabilities": list((client.server_capabilities or {}).keys()),
            "server_info": client.server_info,
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCP probe failed: {exc}")


@app.post("/api/mcp/servers/{server_id}/resources", tags=["mcp"])
async def probe_mcp_server_resources(server_id: str):
    """列出 MCP 服务器暴露的资源（resources/list）。"""
    srv = _find_mcp_server(server_id)
    try:
        from symbio.tools.mcp import get_mcp_pool
        cmd = [srv["command"]] + srv.get("args", [])
        client = await get_mcp_pool().get_client(srv["name"], cmd, env=srv.get("env") or None)
        resources = await client.list_resources()
        return {"server_id": server_id, "resources": resources, "total": len(resources),
                "supported": client.supports("resources")}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCP resources probe failed: {exc}")


@app.post("/api/mcp/servers/{server_id}/prompts", tags=["mcp"])
async def probe_mcp_server_prompts(server_id: str):
    """列出 MCP 服务器暴露的 prompt 模板（prompts/list）。"""
    srv = _find_mcp_server(server_id)
    try:
        from symbio.tools.mcp import get_mcp_pool
        cmd = [srv["command"]] + srv.get("args", [])
        client = await get_mcp_pool().get_client(srv["name"], cmd, env=srv.get("env") or None)
        prompts = await client.list_prompts()
        return {"server_id": server_id, "prompts": prompts, "total": len(prompts),
                "supported": client.supports("prompts")}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCP prompts probe failed: {exc}")


@app.post("/api/mcp/servers/{server_id}/mount", tags=["mcp"])
async def mount_mcp_server_tools(server_id: str):
    """把 MCP 服务器的工具挂载进全局工具注册中心，供 Agent 执行时调用。"""
    srv = _find_mcp_server(server_id)
    try:
        from symbio.tools.mcp import get_mcp_pool, MCPTool
        from symbio.tools.registry import get_tool_registry
        cmd = [srv["command"]] + srv.get("args", [])
        client = await get_mcp_pool().get_client(srv["name"], cmd, env=srv.get("env") or None)
        specs = await client.list_tools()
        registry = get_tool_registry()
        mounted = []
        for spec in specs:
            tool = MCPTool(client, spec, name_prefix=srv["name"])
            registry.register(tool)
            mounted.append(tool.name)
        return {"server_id": server_id, "mounted": mounted, "total": len(mounted)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCP mount failed: {exc}")
