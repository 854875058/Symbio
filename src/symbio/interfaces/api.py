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
    """对话接口 - 调用真实 LLM"""
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
                session_id=request.session_id,
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

        return ChatResponse(
            success=True,
            content=content,
            session_id=request.session_id,
            token_usage=token_usage,
        )
    except Exception as e:
        logger.error(f"对话失败: {e}")
        return ChatResponse(
            success=False,
            content=f"错误: {str(e)}",
            session_id=request.session_id,
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

web_dir = Path(__file__).parent.parent.parent.parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/ui")
    async def serve_ui():
        """提供 Web UI"""
        return FileResponse(str(web_dir / "index.html"))
