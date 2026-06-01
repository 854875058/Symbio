"""Symbio FastAPI 服务端

使用 SQLite 持久化存储，集成 LLM 对话、模型管理、任务监控、
记忆管理、技能管理等完整 API。
"""

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

from symbio.interfaces.database import get_db, close_db
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


# ============ 生命周期事件 ============

@app.on_event("startup")
async def startup():
    """启动时初始化数据库"""
    await get_db()
    logger.info("Symbio API 已启动，数据库已连接")


@app.on_event("shutdown")
async def shutdown():
    """关闭时释放数据库连接"""
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


# ============ 辅助函数 ============

async def _ensure_session(db, session_id: str, title: str = "新对话"):
    """确保会话存在，不存在则创建"""
    existing = await db.get_session(session_id)
    if not existing:
        await db.create_session(session_id, title=title)
    return existing


async def _load_llm_settings():
    """加载 LLM 配置（从 symbio.yaml）"""
    from symbio.config.settings import Settings
    config_path = Path("symbio.yaml")
    if config_path.exists():
        return Settings.from_yaml(config_path)
    return Settings()


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
        await db.create_message(
            f"msg-{uuid.uuid4().hex[:12]}", session_id, "assistant",
            content, time.strftime("%Y-%m-%dT%H:%M:%S"), token_usage["total"],
        )

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


# ============ 模型 API ============

@app.get("/api/models")
async def list_models():
    """模型列表"""
    db = await get_db()
    models = await db.list_models()
    return {"models": models}


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
    return {"model": new_model}


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
    """记忆列表"""
    db = await get_db()
    memories = await db.list_memories()
    return {"memories": memories, "total": len(memories)}


@app.get("/api/memory/search")
async def search_memories(q: str = Query("", description="搜索关键词")):
    """搜索记忆（关键词匹配 + 重要度排序）"""
    db = await get_db()
    if not q:
        memories = await db.list_memories()
        return {"memories": memories, "query": q}
    results = await db.search_memories(q)
    return {"memories": results, "query": q}


# ============ Skills API ============

@app.get("/api/skills")
async def list_skills():
    """返回 Skills 列表"""
    db = await get_db()
    skills = await db.list_skills()
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
        trigger_keywords=skill.trigger_keywords,
    )
    return {"skill": new_skill}


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
                            trigger_keywords=data.get("trigger_keywords", []),
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
                )
                imported += 1

    return {"imported": imported}


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
