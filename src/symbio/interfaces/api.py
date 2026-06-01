"""Symbio FastAPI 服务端"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import asyncio
import json
import uuid
import time
import yaml
from pathlib import Path

from symbio.utils.logger import get_logger

logger = get_logger("api")

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


# ============ 内存数据存储 ============

# 会话列表
sessions_store: list[dict] = [
    {
        "id": "default",
        "title": "新对话",
        "created_at": "2026-05-28T10:00:00",
        "updated_at": "2026-05-28T10:00:00",
        "message_count": 0,
    },
]

# 消息列表
messages_store: list[dict] = []

# Skills 列表
skills_store: list[dict] = [
    {
        "id": "sk-001",
        "name": "code-review",
        "description": "Review code for correctness, security, and performance issues with detailed findings",
        "version": "1.2.0",
        "source": "builtin",
        "enabled": True,
        "trigger_keywords": ["代码审查", "code review", "review"],
        "created_at": "2026-05-20T10:00:00",
    },
    {
        "id": "sk-002",
        "name": "doc-writer",
        "description": "Generate technical documentation from code, APIs, or specifications",
        "version": "1.0.3",
        "source": "builtin",
        "enabled": True,
        "trigger_keywords": ["文档", "documentation", "docs"],
        "created_at": "2026-05-20T10:00:00",
    },
    {
        "id": "sk-003",
        "name": "data-analyst",
        "description": "Analyze datasets, generate statistics, and produce visualizations",
        "version": "0.9.1",
        "source": "custom",
        "enabled": True,
        "trigger_keywords": ["数据分析", "data analysis", "统计"],
        "created_at": "2026-05-21T14:00:00",
    },
    {
        "id": "sk-004",
        "name": "test-generator",
        "description": "Automatically generate unit tests and integration tests for given code",
        "version": "1.1.0",
        "source": "builtin",
        "enabled": True,
        "trigger_keywords": ["测试", "test", "单元测试"],
        "created_at": "2026-05-22T09:00:00",
    },
    {
        "id": "sk-005",
        "name": "security-scanner",
        "description": "Scan code and dependencies for known security vulnerabilities and CVEs",
        "version": "2.0.1",
        "source": "external",
        "enabled": False,
        "trigger_keywords": ["安全", "security", "CVE", "漏洞"],
        "created_at": "2026-05-23T16:00:00",
    },
    {
        "id": "sk-006",
        "name": "translator",
        "description": "Translate text between multiple languages with context-aware accuracy",
        "version": "1.3.2",
        "source": "builtin",
        "enabled": True,
        "trigger_keywords": ["翻译", "translate", "i18n"],
        "created_at": "2026-05-24T11:00:00",
    },
]

# 模型列表
models_store: list[dict] = [
    {
        "id": "m-001",
        "model_id": "claude-3-5-haiku-20241022",
        "provider": "anthropic",
        "display_name": "Claude 3.5 Haiku",
        "api_key": "",
        "base_url": "https://api.anthropic.com",
        "enabled": True,
        "created_at": "2026-05-20T10:00:00",
    },
    {
        "id": "m-002",
        "model_id": "claude-sonnet-4-20250514",
        "provider": "anthropic",
        "display_name": "Claude Sonnet 4",
        "api_key": "",
        "base_url": "https://api.anthropic.com",
        "enabled": True,
        "created_at": "2026-05-20T10:00:00",
    },
    {
        "id": "m-003",
        "model_id": "claude-opus-4-20250514",
        "provider": "anthropic",
        "display_name": "Claude Opus 4",
        "api_key": "",
        "base_url": "https://api.anthropic.com",
        "enabled": True,
        "created_at": "2026-05-20T10:00:00",
    },
]

# 任务列表
tasks_store: list[dict] = [
    {
        "id": "t-001",
        "name": "代码审查: api.py",
        "status": "completed",
        "agent": "general_agent",
        "created_at": "2026-05-28T09:30:00",
        "completed_at": "2026-05-28T09:45:00",
        "description": "对 api.py 进行代码审查，检查安全性与性能问题",
        "result": "发现 3 个潜在问题，已生成修复建议",
        "steps": [
            {"name": "加载代码", "status": "completed", "duration": "2s"},
            {"name": "静态分析", "status": "completed", "duration": "8s"},
            {"name": "生成报告", "status": "completed", "duration": "5s"},
        ],
    },
    {
        "id": "t-002",
        "name": "数据清洗: 用户数据集",
        "status": "running",
        "agent": "data_agent",
        "created_at": "2026-05-28T10:00:00",
        "completed_at": None,
        "description": "清洗用户行为数据，移除异常值和重复记录",
        "result": None,
        "steps": [
            {"name": "读取数据源", "status": "completed", "duration": "3s"},
            {"name": "去重处理", "status": "completed", "duration": "12s"},
            {"name": "异常检测", "status": "running", "duration": None},
            {"name": "格式标准化", "status": "pending", "duration": None},
        ],
    },
    {
        "id": "t-003",
        "name": "API 文档生成",
        "status": "failed",
        "agent": "doc_agent",
        "created_at": "2026-05-28T08:00:00",
        "completed_at": "2026-05-28T08:10:00",
        "description": "自动生成 OpenAPI 文档并导出为 Markdown",
        "result": "错误: 模板引擎渲染失败，缺少依赖模块 jinja2",
        "steps": [
            {"name": "解析路由", "status": "completed", "duration": "4s"},
            {"name": "渲染文档", "status": "failed", "duration": "6s"},
        ],
    },
    {
        "id": "t-004",
        "name": "单元测试: core/orchestrator.py",
        "status": "completed",
        "agent": "test_agent",
        "created_at": "2026-05-27T16:00:00",
        "completed_at": "2026-05-27T16:20:00",
        "description": "运行 orchestrator 模块的单元测试套件",
        "result": "42 个测试通过，0 个失败，覆盖率 87%",
        "steps": [
            {"name": "收集测试", "status": "completed", "duration": "1s"},
            {"name": "执行测试", "status": "completed", "duration": "18s"},
            {"name": "覆盖率报告", "status": "completed", "duration": "2s"},
        ],
    },
    {
        "id": "t-005",
        "name": "依赖安全扫描",
        "status": "running",
        "agent": "security_agent",
        "created_at": "2026-05-28T11:00:00",
        "completed_at": None,
        "description": "扫描项目依赖，检查已知 CVE 漏洞",
        "result": None,
        "steps": [
            {"name": "解析 requirements", "status": "completed", "duration": "2s"},
            {"name": "查询漏洞库", "status": "running", "duration": None},
            {"name": "生成报告", "status": "pending", "duration": None},
        ],
    },
]

# 记忆列表
memory_store: list[dict] = [
    {
        "id": "mem-001",
        "title": "Python 快速排序算法",
        "content": "快速排序使用分治策略，选择基准元素，将数组分成两部分，递归排序。时间复杂度 O(n log n)，最坏 O(n^2)。实现要点：选择中间元素作为 pivot，使用双指针分区。",
        "tags": ["python", "算法", "排序"],
        "source": "chat",
        "importance": 0.85,
        "created_at": "2026-05-27T14:30:00",
        "access_count": 5,
    },
    {
        "id": "mem-002",
        "title": "DAG 引擎设计模式",
        "content": "动态 DAG 引擎用于编排多 Agent 工作流。核心概念：节点表示 Agent 任务，边表示依赖关系。支持条件分支、并行执行、错误重试。拓扑排序决定执行顺序。",
        "tags": ["架构", "DAG", "Agent"],
        "source": "chat",
        "importance": 0.92,
        "created_at": "2026-05-27T15:00:00",
        "access_count": 8,
    },
    {
        "id": "mem-003",
        "title": "数据库 Schema 设计规范",
        "content": "设计数据库 schema 的关键原则：第三范式避免冗余，适度反范式提升查询性能。命名使用 snake_case，主键用 UUID 或自增 ID，时间字段用 TIMESTAMP WITH TIME ZONE。",
        "tags": ["数据库", "设计", "PostgreSQL"],
        "source": "chat",
        "importance": 0.78,
        "created_at": "2026-05-26T11:00:00",
        "access_count": 3,
    },
    {
        "id": "mem-004",
        "title": "Anthropic API 认证方式",
        "content": "Anthropic API 使用 x-api-key 头部认证。Base URL 默认 https://api.anthropic.com，支持自定义。推荐使用 AsyncAnthropic 客户端进行异步调用，配合 retry 和 timeout 配置。",
        "tags": ["API", "认证", "Anthropic"],
        "source": "system",
        "importance": 0.95,
        "created_at": "2026-05-25T09:00:00",
        "access_count": 12,
    },
    {
        "id": "mem-005",
        "title": "WebSocket 心跳机制",
        "content": "WebSocket 长连接需要心跳保活。客户端每 30 秒发送 ping 帧，服务端返回 pong。超过 3 次未收到 pong 则判定断线，触发重连逻辑。指数退避策略：1s, 2s, 4s, 8s, 最大 30s。",
        "tags": ["WebSocket", "网络", "保活"],
        "source": "chat",
        "importance": 0.72,
        "created_at": "2026-05-26T16:00:00",
        "access_count": 2,
    },
]


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
async def health():
    """健康检查"""
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """对话接口 - 调用真实 LLM，同时持久化消息"""
    session_id = request.session_id or "default"
    now_str = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 确保会话存在
    session_exists = any(s["id"] == session_id for s in sessions_store)
    if not session_exists:
        sessions_store.append({
            "id": session_id,
            "title": request.message[:30] if request.message else "新对话",
            "created_at": now_str,
            "updated_at": now_str,
            "message_count": 0,
        })

    # 保存用户消息
    user_msg = {
        "id": f"msg-{uuid.uuid4().hex[:12]}",
        "session_id": session_id,
        "role": "user",
        "content": request.message,
        "timestamp": now_str,
        "tokens": 0,
    }
    messages_store.append(user_msg)

    # 更新会话信息
    for s in sessions_store:
        if s["id"] == session_id:
            s["updated_at"] = now_str
            s["message_count"] = len([m for m in messages_store if m["session_id"] == session_id])
            if s["title"] == "新对话":
                s["title"] = request.message[:30]
            break

    try:
        import anthropic
        from symbio.config.settings import Settings

        # 从 YAML 加载配置
        config_path = Path("symbio.yaml")
        if config_path.exists():
            settings = Settings.from_yaml(config_path)
        else:
            settings = Settings()

        api_key = settings.model.anthropic_api_key
        base_url = settings.model.anthropic_base_url

        if not api_key:
            return ChatResponse(
                success=False,
                content="错误: 未配置 API Key，请编辑 symbio.yaml 中的 anthropic_api_key",
                session_id=session_id,
            )

        client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
        )

        model = request.model or settings.model.model_medium

        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": request.message}],
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
        ai_msg = {
            "id": f"msg-{uuid.uuid4().hex[:12]}",
            "session_id": session_id,
            "role": "assistant",
            "content": content,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tokens": token_usage["total"],
        }
        messages_store.append(ai_msg)

        return ChatResponse(
            success=True,
            content=content,
            session_id=session_id,
            token_usage=token_usage,
        )
    except Exception as e:
        logger.error(f"对话失败: {e}")
        # 保存错误回复
        error_msg = {
            "id": f"msg-{uuid.uuid4().hex[:12]}",
            "session_id": session_id,
            "role": "assistant",
            "content": f"错误: {str(e)}",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tokens": 0,
        }
        messages_store.append(error_msg)
        return ChatResponse(
            success=False,
            content=f"错误: {str(e)}",
            session_id=session_id,
        )


# ============ 任务 API ============

@app.get("/api/tasks")
async def list_tasks(status: Optional[str] = None):
    """任务列表，支持状态过滤"""
    filtered = tasks_store
    if status and status != "all":
        filtered = [t for t in tasks_store if t["status"] == status]
    return {
        "tasks": filtered,
        "total": len(filtered),
    }


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """获取任务详情"""
    for t in tasks_store:
        if t["id"] == task_id:
            return {"task": t}
    raise HTTPException(status_code=404, detail="任务不存在")


# ============ 模型 API ============

@app.get("/api/models")
async def list_models():
    """模型列表"""
    return {
        "models": models_store,
    }


@app.post("/api/models")
async def create_model(model: ModelCreate):
    """添加模型"""
    new_model = {
        "id": f"m-{uuid.uuid4().hex[:8]}",
        "model_id": model.model_id,
        "provider": model.provider,
        "display_name": model.display_name or model.model_id,
        "api_key": model.api_key,
        "base_url": model.base_url,
        "enabled": model.enabled,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    models_store.append(new_model)
    return {"model": new_model}


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str):
    """删除模型"""
    global models_store
    before = len(models_store)
    models_store = [m for m in models_store if m["id"] != model_id]
    if len(models_store) == before:
        raise HTTPException(status_code=404, detail="模型不存在")
    return {"success": True}


@app.post("/api/models/{model_id}/test")
async def test_model(model_id: str):
    """测试模型连接"""
    target = None
    for m in models_store:
        if m["id"] == model_id:
            target = m
            break
    if not target:
        raise HTTPException(status_code=404, detail="模型不存在")

    # 尝试用实际 API key 发一个简单请求
    api_key = target.get("api_key") or ""
    base_url = target.get("base_url", "https://api.anthropic.com")
    provider = target.get("provider", "anthropic")

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
            return {"success": True, "message": f"连接成功，模型响应正常 (tokens: {resp.usage.input_tokens}+{resp.usage.output_tokens})"}
        else:
            # OpenAI 兼容
            import httpx
            async with httpx.AsyncClient(timeout=15) as http_client:
                resp = await http_client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": target["model_id"],
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 16,
                    },
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
    """记忆列表"""
    return {
        "memories": memory_store,
        "total": len(memory_store),
    }


@app.get("/api/memory/search")
async def search_memories(q: str = Query("", description="搜索关键词")):
    """搜索记忆（关键词匹配 + 重要度排序）"""
    if not q:
        return {"memories": memory_store, "query": q}

    q_lower = q.lower()
    results = []
    for mem in memory_store:
        score = 0.0
        if q_lower in mem["title"].lower():
            score += 0.5
        if q_lower in mem["content"].lower():
            score += 0.3
        for tag in mem.get("tags", []):
            if q_lower in tag.lower():
                score += 0.2
        if score > 0:
            entry = {**mem, "relevance": round(score * mem.get("importance", 0.5), 3)}
            results.append(entry)

    results.sort(key=lambda x: x["relevance"], reverse=True)
    return {"memories": results, "query": q}


# ============ 会话 API ============

@app.get("/api/sessions")
async def list_sessions():
    """返回会话列表，按更新时间倒序"""
    sorted_sessions = sorted(sessions_store, key=lambda x: x.get("updated_at", ""), reverse=True)
    return {
        "sessions": sorted_sessions,
        "total": len(sorted_sessions),
    }


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """返回指定会话的消息历史"""
    session_exists = any(s["id"] == session_id for s in sessions_store)
    if not session_exists:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = [m for m in messages_store if m["session_id"] == session_id]
    msgs.sort(key=lambda x: x.get("timestamp", ""))
    return {
        "messages": msgs,
        "total": len(msgs),
        "session_id": session_id,
    }


# ============ Skills API ============

@app.get("/api/skills")
async def list_skills():
    """返回 Skills 列表"""
    return {
        "skills": skills_store,
        "total": len(skills_store),
    }


@app.get("/api/skills/search")
async def search_skills(q: str = Query("", description="搜索关键词")):
    """搜索 Skills（名称、描述、关键词匹配）"""
    if not q:
        return {"skills": skills_store, "query": q}

    q_lower = q.lower()
    results = []
    for sk in skills_store:
        score = 0.0
        if q_lower in sk["name"].lower():
            score += 0.5
        if q_lower in sk.get("description", "").lower():
            score += 0.3
        for kw in sk.get("trigger_keywords", []):
            if q_lower in kw.lower():
                score += 0.2
        if score > 0:
            entry = {**sk, "relevance": round(score, 3)}
            results.append(entry)

    results.sort(key=lambda x: x["relevance"], reverse=True)
    return {"skills": results, "query": q}


class SkillImport(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0.0"
    source: str = "imported"
    enabled: bool = True
    trigger_keywords: list[str] = []


@app.post("/api/skills/import")
async def import_skill(skill: SkillImport):
    """导入一个新的 Skill"""
    # 检查是否已存在同名 skill
    for sk in skills_store:
        if sk["name"] == skill.name:
            raise HTTPException(status_code=400, detail=f"Skill '{skill.name}' 已存在")

    new_skill = {
        "id": f"sk-{uuid.uuid4().hex[:8]}",
        "name": skill.name,
        "description": skill.description,
        "version": skill.version,
        "source": skill.source,
        "enabled": skill.enabled,
        "trigger_keywords": skill.trigger_keywords,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    skills_store.append(new_skill)
    return {"skill": new_skill}


class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    version: Optional[str] = None
    enabled: Optional[bool] = None
    trigger_keywords: Optional[list[str]] = None


@app.put("/api/skills/{skill_id}")
async def update_skill(skill_id: str, update: SkillUpdate):
    """更新 Skill"""
    for sk in skills_store:
        if sk["id"] == skill_id:
            if update.name is not None:
                sk["name"] = update.name
            if update.description is not None:
                sk["description"] = update.description
            if update.version is not None:
                sk["version"] = update.version
            if update.enabled is not None:
                sk["enabled"] = update.enabled
            if update.trigger_keywords is not None:
                sk["trigger_keywords"] = update.trigger_keywords
            return {"skill": sk}
    raise HTTPException(status_code=404, detail="Skill 不存在")


@app.delete("/api/skills/{skill_id}")
async def delete_skill(skill_id: str):
    """删除 Skill"""
    for i, sk in enumerate(skills_store):
        if sk["id"] == skill_id:
            skills_store.pop(i)
            return {"success": True}
    raise HTTPException(status_code=404, detail="Skill 不存在")


@app.post("/api/skills/auto-detect")
async def auto_detect_skills():
    """自动检测已安装的 Skills（Claude Code、Codex 等）"""
    import os
    import glob as glob_mod

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
                    if not any(s["name"] == name for s in skills_store):
                        skills_store.append({
                            "id": f"sk-{uuid.uuid4().hex[:8]}",
                            "name": name,
                            "description": f"Auto-detected from Claude Code: {dir_path}",
                            "version": "1.0.0",
                            "source": "claude-code",
                            "enabled": True,
                            "trigger_keywords": [],
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        })
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
                if name and not any(s["name"] == name for s in skills_store):
                    skills_store.append({
                        "id": f"sk-{uuid.uuid4().hex[:8]}",
                        "name": name,
                        "description": tool.get("description", f"Auto-detected from Codex"),
                        "version": "1.0.0",
                        "source": "codex",
                        "enabled": True,
                        "trigger_keywords": [],
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                    found += 1
                    detected.append(name)
        except Exception:
            pass

    return {"found": found, "detected": detected}


class DirImportRequest(BaseModel):
    path: str


@app.post("/api/skills/import-dir")
async def import_skills_from_dir(req: DirImportRequest):
    """从目录批量导入 Skills"""
    import os

    dir_path = req.path
    if not os.path.isdir(dir_path):
        raise HTTPException(status_code=400, detail=f"目录不存在: {dir_path}")

    imported = 0
    for item in os.listdir(dir_path):
        item_path = os.path.join(dir_path, item)
        if os.path.isdir(item_path):
            # 检查目录下是否有 skill 定义文件
            manifest = None
            for f in ["skill.yaml", "skill.json", "manifest.json", "manifest.yaml"]:
                fp = os.path.join(item_path, f)
                if os.path.exists(fp):
                    manifest = fp
                    break
            if manifest:
                try:
                    with open(manifest) as f:
                        if manifest.endswith('.json'):
                            data = json.load(f)
                        else:
                            data = yaml.safe_load(f)
                    name = data.get("name", item)
                    if not any(s["name"] == name for s in skills_store):
                        skills_store.append({
                            "id": f"sk-{uuid.uuid4().hex[:8]}",
                            "name": name,
                            "description": data.get("description", f"Imported from {dir_path}"),
                            "version": data.get("version", "1.0.0"),
                            "source": "imported",
                            "enabled": True,
                            "trigger_keywords": data.get("trigger_keywords", []),
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        })
                        imported += 1
                except Exception:
                    pass
        elif item.endswith(('.md', '.yaml', '.json')):
            # 单文件 Skill
            name = item.replace('.md', '').replace('.yaml', '').replace('.json', '')
            if not any(s["name"] == name for s in skills_store):
                skills_store.append({
                    "id": f"sk-{uuid.uuid4().hex[:8]}",
                    "name": name,
                    "description": f"Imported from {dir_path}/{item}",
                    "version": "1.0.0",
                    "source": "imported",
                    "enabled": True,
                    "trigger_keywords": [],
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                imported += 1

    return {"imported": imported}


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

            # 确保会话存在
            session_exists = any(s["id"] == session_id for s in sessions_store)
            if not session_exists:
                sessions_store.append({
                    "id": session_id,
                    "title": content[:30] if content else "新对话",
                    "created_at": now_str,
                    "updated_at": now_str,
                    "message_count": 0,
                })

            # 保存用户消息
            user_msg = {
                "id": f"msg-{uuid.uuid4().hex[:12]}",
                "session_id": session_id,
                "role": "user",
                "content": content,
                "timestamp": now_str,
                "tokens": 0,
            }
            messages_store.append(user_msg)

            # 更新会话
            for s in sessions_store:
                if s["id"] == session_id:
                    s["updated_at"] = now_str
                    s["message_count"] = len([m for m in messages_store if m["session_id"] == session_id])
                    if s["title"] == "新对话":
                        s["title"] = content[:30]
                    break

            full_response = ""
            token_input = 0
            token_output = 0

            try:
                import anthropic
                from symbio.config.settings import Settings

                config_path = Path("symbio.yaml")
                if config_path.exists():
                    settings = Settings.from_yaml(config_path)
                else:
                    settings = Settings()

                api_key = settings.model.anthropic_api_key
                base_url = settings.model.anthropic_base_url

                if not api_key:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "content": "未配置 API Key，请编辑 symbio.yaml",
                    }))
                    continue

                client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
                model = model_override or settings.model.model_medium

                # 流式调用
                async with client.messages.stream(
                    model=model,
                    max_tokens=4096,
                    messages=[{"role": "user", "content": content}],
                ) as stream:
                    async for text in stream.text_stream:
                        full_response += text
                        await websocket.send_text(json.dumps({
                            "type": "token",
                            "content": text,
                        }))

                    # 获取 usage
                    final = await stream.get_final_message()
                    token_input = final.usage.input_tokens
                    token_output = final.usage.output_tokens

            except ImportError:
                # anthropic 不可用，使用模拟流式
                response = f"收到: {content}"
                for char in response:
                    full_response += char
                    await websocket.send_text(json.dumps({
                        "type": "token",
                        "content": char,
                    }))
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
            ai_msg = {
                "id": f"msg-{uuid.uuid4().hex[:12]}",
                "session_id": session_id,
                "role": "assistant",
                "content": full_response,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tokens": token_input + token_output,
            }
            messages_store.append(ai_msg)

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


# ============ 配置 API ============

@app.get("/api/config")
async def get_config():
    """获取 LLM 配置"""
    from symbio.config.settings import Settings

    config_path = Path("symbio.yaml")
    if config_path.exists():
        settings = Settings.from_yaml(config_path)
    else:
        settings = Settings()

    return {
        "anthropic_api_key": settings.model.anthropic_api_key,
        "anthropic_base_url": settings.model.anthropic_base_url,
        "openai_api_key": settings.model.openai_api_key,
        "openai_base_url": settings.model.openai_base_url,
        "model_low": settings.model.model_low,
        "model_medium": settings.model.model_medium,
        "model_high": settings.model.model_high,
    }


class ConfigUpdate(BaseModel):
    anthropic_api_key: Optional[str] = None
    anthropic_base_url: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    model_low: Optional[str] = None
    model_medium: Optional[str] = None
    model_high: Optional[str] = None


@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    """保存 LLM 配置"""
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
    return {"success": True}


# ============ 静态文件 ============

web_dir = Path(__file__).parent.parent.parent.parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/ui")
    async def serve_ui():
        """提供 Web UI"""
        return FileResponse(str(web_dir / "index.html"))
