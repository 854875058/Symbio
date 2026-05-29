"""Symbio FastAPI 服务端"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import asyncio
import json
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


class ModelConfig(BaseModel):
    model_id: str
    provider: str = "anthropic"
    display_name: str = ""
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
        from symbio.config.settings import get_settings

        settings = get_settings()

        client = anthropic.AsyncAnthropic(
            api_key=settings.model.anthropic_api_key,
            base_url=settings.model.anthropic_base_url,
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


@app.get("/api/tasks")
async def list_tasks():
    """任务列表"""
    return {
        "tasks": [],
        "total": 0,
    }


@app.get("/api/models")
async def list_models():
    """模型列表"""
    return {
        "models": [
            {
                "model_id": "claude-3-5-haiku-20241022",
                "provider": "anthropic",
                "display_name": "Claude 3.5 Haiku",
                "enabled": True,
            },
            {
                "model_id": "claude-sonnet-4-20250514",
                "provider": "anthropic",
                "display_name": "Claude Sonnet 4",
                "enabled": True,
            },
            {
                "model_id": "claude-opus-4-20250514",
                "provider": "anthropic",
                "display_name": "Claude Opus 4",
                "enabled": True,
            },
        ]
    }


@app.get("/api/memory")
async def list_memories():
    """记忆列表"""
    return {
        "memories": [],
        "total": 0,
    }


@app.get("/api/memory/search")
async def search_memories(query: str = ""):
    """搜索记忆"""
    return {
        "memories": [],
        "query": query,
    }


# ============ WebSocket ============

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket 对话"""
    await websocket.accept()
    logger.info("WebSocket 连接建立")

    try:
        while True:
            # 接收消息
            data = await websocket.receive_text()
            message = json.loads(data)

            # 模拟流式响应
            response = f"收到: {message.get('content', '')}"
            for char in response:
                await websocket.send_text(json.dumps({
                    "type": "token",
                    "content": char,
                }))
                await asyncio.sleep(0.02)

            # 发送完成信号
            await websocket.send_text(json.dumps({
                "type": "done",
                "content": "",
            }))

    except WebSocketDisconnect:
        logger.info("WebSocket 连接断开")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")


# ============ 静态文件 ============

# 挂载 Web UI 静态文件
web_dir = Path(__file__).parent.parent.parent.parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/ui")
    async def serve_ui():
        """提供 Web UI"""
        return FileResponse(str(web_dir / "index.html"))
